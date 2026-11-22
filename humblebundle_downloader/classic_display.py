"""The progress display drawn by hand with escape codes: a block of
lines repainted in place, one per transfer, plus a totals line. And for
a single download at a time, one bar redrawn with a carriage return.

Both are listeners (see events.py): the engine tells them what happens
and they draw it.
"""
import contextlib
import logging
import shutil
import sys
import threading
import time

from . import sl_train
from .events import Listener
from .formatting import _fit, _format_clock, _human_size
from .progress_styles import DEFAULT_PROGRESS_STYLE, PROGRESS_STYLES, _muncher_bar


def _bar(fraction, width):
    if width < 3:
        return ""
    if fraction is None:
        return "[" + "?" * width + "]"
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


class ProgressReporter(Listener):
    """A block of lines repainted in place: one per transfer in flight,
    plus a totals line, in the manner of a package manager.

    Workers only ever touch counters. One reporter thread owns the
    terminal block and is the only thing that writes to it, which is
    what stops concurrent transfers from shredding each other's output.
    """

    # Column widths for the numbers. These are minimums: a value wider
    # than its column pushes the name column in rather than the line out
    SIZE_WIDTH = 10
    RATE_WIDTH = 12
    ETA_WIDTH = 5
    PCT_WIDTH = 4
    # Everything right of the name, bar excluded: four gaps plus the
    # four columns above
    TAIL_WIDTH = 4 + SIZE_WIDTH + RATE_WIDTH + ETA_WIDTH + PCT_WIDTH
    MIN_NAME = 10
    MAX_BAR = 20
    MIN_BAR = 6
    # Longest the display may hold up a run that is leaving
    STOP_WAIT = 2.0

    shows_transfers = True

    def __init__(self, stream=None, slots=1, interval=0.2, window=3.0,
                 style=DEFAULT_PROGRESS_STYLE, big_trains=False):
        super().__init__()
        self.stream = sys.stderr if stream is None else stream
        # With a style that has a scene: give every download one, not
        # only the totals, when the terminal is tall enough
        self.big_trains = big_trains
        self.slots = max(1, int(slots))
        self.interval = interval
        self.window = window
        self.style = PROGRESS_STYLES.get(
            style, PROGRESS_STYLES[DEFAULT_PROGRESS_STYLE]
        )
        # Bumped every repaint, so the munchers chomp on their own even
        # when a transfer is barely moving
        self._frame = 0

        self._lock = threading.Lock()
        self._files = {}                      # name -> dict of counters
        self._slot_names = [None] * self.slots
        self._done = 0
        self._failed = 0
        self._skipped = 0
        self._queued = 0
        self._bytes = 0                       # bytes from finished files
        self._walk_done = False
        self._started_at = time.time()
        self._overall = []                    # (when, bytes seen so far)

        self._thread = None
        self._stop = threading.Event()
        self._painted = 0                     # lines currently on screen
        # Held for every write to the terminal, block or log line alike.
        # Reentrant, because a log line clears and repaints the block
        self._term = threading.RLock()
        self._hooked = []                     # log handlers routed via us
        # Shown on the totals line, for -j auto to say where it is
        self.jobs_note = None

    # -- lifecycle ---------------------------------------------------

    def enabled(self):
        try:
            return bool(self.stream.isatty())
        except Exception:
            return False

    def start(self):
        if not self.enabled() or self._thread is not None:
            return
        self._started_at = time.time()
        self._stop.clear()
        self.attach_logging()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        # Bounded: a painter stuck on a wedged terminal must not keep
        # the run from leaving, and it is a daemon thread anyway
        self._thread.join(timeout=self.STOP_WAIT)
        self._thread = None
        self.detach_logging()
        self.clear(wait=self.STOP_WAIT)

    def forget_painted(self):
        """Draw the block afresh from here on, rather than over the rows
        it last used. For when something has written to the terminal
        without going through the lock, which only the Ctrl-C notice does
        """
        self._painted = 0

    def attach_logging(self):
        """Route log lines bound for the terminal through the block.

        A log line written while the block is on screen lands inside it
        and pushes it down a row, so the next repaint starts a row too
        low and leaves the old top row behind, frozen. That is where
        duplicated rows and bars stuck at 100% came from. Instead, each
        line clears the block, is written, and the block is redrawn
        under it, all under the one terminal lock
        """
        for handler in logging.getLogger().handlers:
            stream = getattr(handler, "stream", None)
            if handler in [h for h, _ in self._hooked]:
                continue
            try:
                to_terminal = bool(stream.isatty())
            except Exception:
                to_terminal = False
            if not to_terminal:
                continue
            original = handler.emit

            def emit(record, original=original):
                with self._term:
                    self.clear()
                    original(record)
                    self.paint()

            handler.emit = emit
            self._hooked.append((handler, original))

    def detach_logging(self):
        for handler, _ in self._hooked:
            try:
                del handler.emit
            except AttributeError:
                pass
        self._hooked = []

    @contextlib.contextmanager
    def printing(self):
        """For output that is not logging, like --print-urls"""
        with self._term:
            self.clear()
            try:
                yield
            finally:
                if self._thread is not None or self._hooked:
                    self.paint()

    # -- events from the run -----------------------------------------

    def downloads_started(self, slots):
        self.start()

    def jobs_changed(self, limit):
        self.jobs_note = "{0} at once".format(limit)

    def downloads_finished(self):
        self.stop()

    def stopping(self):
        # The Ctrl-C notice goes straight to the descriptor, past the
        # lock, so the block is drawn afresh under it
        self.forget_painted()

    def output(self, line):
        with self.printing():
            super().output(line)

    # -- what the workers report -------------------------------------

    def file_queued(self):
        with self._lock:
            self._queued += 1

    def file_skipped(self):
        """Queued, then found not to need downloading after all"""
        with self._lock:
            self._skipped += 1

    def traversal_finished(self):
        """No more files will be queued, so the totals can show a bar"""
        with self._lock:
            self._walk_done = True

    def file_started(self, name, total):
        with self._lock:
            self._files[name] = {
                "done": 0, "total": total, "samples": [],
            }
            if name not in self._slot_names:
                if None in self._slot_names:
                    self._slot_names[self._slot_names.index(None)] = name
                else:
                    self._slot_names.append(name)

    def file_progress(self, name, downloaded):
        # Hot path: one dict write per chunk. Sampling for rates happens
        # in the reporter thread, not here
        with self._lock:
            entry = self._files.get(name)
            if entry is not None:
                entry["done"] = downloaded

    def file_finished(self, name, ok=True):
        with self._lock:
            entry = self._files.pop(name, None)
            if name in self._slot_names:
                self._slot_names[self._slot_names.index(name)] = None
            if ok:
                self._done += 1
                if entry is not None:
                    self._bytes += entry["done"]
            else:
                self._failed += 1

    # -- rates -------------------------------------------------------

    def _sample(self, now):
        """Called from the paint loop, so sampling costs the transfers
        nothing
        """
        seen = self._bytes
        for entry in self._files.values():
            seen += entry["done"]
            samples = entry["samples"]
            samples.append((now, entry["done"]))
            while len(samples) > 2 and now - samples[0][0] > self.window:
                samples.pop(0)

        self._overall.append((now, seen))
        while (len(self._overall) > 2
               and now - self._overall[0][0] > self.window):
            self._overall.pop(0)
        return seen

    @staticmethod
    def _rate(samples):
        if len(samples) < 2:
            return None
        elapsed = samples[-1][0] - samples[0][0]
        if elapsed <= 0:
            return None
        moved = samples[-1][1] - samples[0][1]
        return moved / elapsed if moved > 0 else 0.0

    # -- rendering ---------------------------------------------------

    def _layout(self, width):
        """Widest bar that still leaves room for a name.

        A row is name + space + TAIL_WIDTH + bar + 2 brackets, and one
        column is kept spare so the terminal does not wrap it, so the
        bar can have whatever is left over after MIN_NAME.
        """
        bar = max(self.MIN_BAR, min(
            self.MAX_BAR,
            width - self.MIN_NAME - self.TAIL_WIDTH - 4,
        ))
        name = width - self.TAIL_WIDTH - bar - 4
        return name, bar

    def _fits(self, width):
        return width >= self.MIN_NAME + self.TAIL_WIDTH + self.MIN_BAR + 4

    def _compose(self, label, size, rate, eta, bar, pct, width):
        """Lay out one row.

        The tail is built first and measured, and the name gets whatever
        is left. A number wider than its column then costs the name a
        character instead of pushing the whole line past the terminal.
        """
        tail = "{size:>{sw}} {rate:>{rw}} {eta:>{ew}} {bar} {pct:>{pw}}".format(
            size=size, sw=self.SIZE_WIDTH,
            rate=rate, rw=self.RATE_WIDTH,
            eta=eta, ew=self.ETA_WIDTH,
            bar=bar,
            pct=pct, pw=self.PCT_WIDTH,
        )
        name_width = max(self.MIN_NAME, width - len(tail) - 2)
        return (_fit(label, name_width) + " " + tail).rstrip()

    def _slot_line(self, name, entry, width):
        _, bar_width = self._layout(width)
        total = entry["total"]
        done = entry["done"]
        rate = self._rate(entry["samples"])

        fraction = (done / total) if total else None
        if total and rate:
            eta = _format_clock((total - done) / rate) if rate > 0 else "--:--"
        else:
            eta = "--:--"

        return self._compose(
            label=name,
            size=_human_size(total if total else done),
            rate=(_human_size(rate) + "/s") if rate else "--",
            eta=eta,
            bar=_muncher_bar(fraction, bar_width, self.style, self._frame),
            pct="{0}%".format(int(fraction * 100)) if fraction is not None
            else "--",
            width=width,
        )

    def _idle_line(self, width):
        _, bar_width = self._layout(width)
        return self._compose(
            label="", size="", rate="", eta="",
            bar=" " * (bar_width + 2), pct="", width=width,
        )

    def _total_line(self, seen, width):
        _, bar_width = self._layout(width)
        expected = max(0, self._queued - self._skipped)
        rate = self._rate(self._overall)

        label = "Total {done}/{expected}".format(
            done=self._done, expected=expected if expected else "?"
        )
        if self._failed:
            label += " ({failed} failed)".format(failed=self._failed)
        if not self._walk_done:
            label += " so far"
        if self.jobs_note:
            label += " · " + self.jobs_note

        if self._walk_done and expected:
            fraction = min(1.0, self._done / expected)
            bar = _bar(fraction, bar_width)
            right = "{0}%".format(int(fraction * 100))
        else:
            bar = " " * (bar_width + 2)
            right = "--"

        return self._compose(
            label=label,
            size=_human_size(seen),
            rate=(_human_size(rate) + "/s") if rate else "--",
            eta=_format_clock(time.time() - self._started_at),
            bar=bar,
            pct=right,
            width=width,
        )

    def render(self, width=80, height=24):
        """The whole block, as a list of lines"""
        now = time.time()
        with self._lock:
            seen = self._sample(now)
            names = list(self._slot_names)
            files = {n: dict(e) for n, e in self._files.items()}

            if not self._fits(width):
                return [self._compact(seen, width)]

            each, total = self._scenes(len(names), width, height)
            if each is None:
                return [self._compact(seen, width)]

            # One column short of the edge, like every other row, so the
            # terminal never wraps one and throws the repaint out
            scene_width = width - 1
            lines = []
            for name in names:
                entry = files.get(name)
                if entry is None:
                    lines.append(self._idle_line(width))
                    if each:
                        lines.extend(sl_train.scene(
                            None, scene_width, self._frame, empty=True))
                else:
                    lines.append(self._slot_line(name, entry, width))
                    if each:
                        lines.extend(sl_train.scene(
                            self._fraction(entry), scene_width, self._frame))
            lines.append(self._total_line(seen, width))
            if total:
                lines.extend(sl_train.scene(
                    self._total_fraction(), scene_width, self._frame))
            return lines

    def _scenes(self, slots, width, height):
        """(a scene per download, a scene for the totals), whichever of
        those fit, best first; (None, None) when not even plain rows do.
        A row is always left for the shell
        """
        tall = sl_train.HEIGHT + 1
        scene = (
            self.style.get("scene") == "sl"
            and sl_train.cars_for(width - 1) >= 0
        )
        options = []
        if scene and self.big_trains:
            options.append((True, True))
        if scene:
            options.append((False, True))
        options.append((False, False))
        for each, total in options:
            lines = slots * (1 + (tall if each else 0)) + 1 + (tall if total else 0)
            if height >= lines + 2:
                return each, total
        return None, None

    @staticmethod
    def _fraction(entry):
        total = entry["total"]
        return (entry["done"] / total) if total else None

    def _total_fraction(self):
        expected = max(0, self._queued - self._skipped)
        if self._walk_done and expected:
            return min(1.0, self._done / expected)
        return None

    def _compact(self, seen, width):
        """One line, for a terminal too small for the block"""
        active = len(self._files)
        rate = self._rate(self._overall)
        parts = ["{0} done".format(self._done)]
        if self._failed:
            parts.append("{0} failed".format(self._failed))
        parts.append("{0} active".format(active))
        parts.append(_human_size(seen))
        if rate:
            parts.append(_human_size(rate) + "/s")
        line = " · ".join(parts)
        return line[:max(0, width - 1)]

    # -- terminal ----------------------------------------------------

    def _write(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            pass

    def clear(self, wait=None):
        """Wipe the block so ordinary log output lands on clean rows.

        wait bounds how long to wait for the terminal; past it the block
        is left as it is rather than holding anything up
        """
        if not self._term.acquire(timeout=-1 if wait is None else wait):
            return
        try:
            if not self._painted or not self.enabled():
                self._painted = 0
                return
            out = ["\033[{0}A".format(self._painted)]
            out.extend(["\r\033[K\n"] * self._painted)
            out.append("\033[{0}A".format(self._painted))
            self._painted = 0
            self._write("".join(out))
        finally:
            self._term.release()

    def paint(self):
        if not self.enabled():
            return
        with self._term:
            self._paint()

    def _paint(self):
        try:
            size = shutil.get_terminal_size((80, 24))
            width, height = size.columns, size.lines
        except Exception:
            width, height = 80, 24

        self._frame += 1
        lines = self.render(width, height)

        out = []
        if self._painted:
            out.append("\033[{0}A".format(self._painted))
        for line in lines:
            out.append("\r\033[K" + line[:width - 1] + "\n")

        extra = self._painted - len(lines)
        if extra > 0:
            out.extend(["\r\033[K\n"] * extra)
            out.append("\033[{0}A".format(extra))

        self._painted = len(lines)
        self._write("".join(out))

    def _loop(self):
        while not self._stop.is_set():
            self.paint()
            self._stop.wait(self.interval)


class SingleBar(Listener):
    """One download at a time: a bar on one line, redrawn in place with a
    carriage return, then left behind when the file is done"""

    WIDTH = 50

    def __init__(self, stream=None):
        super().__init__()
        self.stream = sys.stdout if stream is None else stream
        self._totals = {}

    def file_started(self, name, total):
        self._totals[name] = total

    def file_progress(self, name, downloaded):
        total = self._totals.get(name)
        if total is None:
            # No Content-Length to measure against
            self._write("\t{0}\r".format(downloaded))
            return
        done = int(self.WIDTH * downloaded / total) if total else self.WIDTH
        self._write("\t{percent}% [{filler}{space}]\r".format(
            percent=int(done * (100 / self.WIDTH)),
            filler="=" * min(max(done, 0), self.WIDTH),
            space=" " * min(max(self.WIDTH - done, 0), self.WIDTH),
        ))

    def file_finished(self, name, ok=True):
        if self._totals.pop(name, False) is not False:
            # Do not draw over the finished bar
            self._write("\n")

    def _write(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            pass
