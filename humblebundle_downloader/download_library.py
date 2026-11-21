import os
import re
import contextlib
import sys
import json
import time
import email
import queue
import parsel
import shutil
import hashlib
import tempfile
import signal
import logging
import datetime
import requests
import threading
import http.cookiejar
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .progress_styles import PROGRESS_STYLES, DEFAULT_PROGRESS_STYLE
from . import sl_train
from . import auto_jobs
from .auto_jobs import JobGate, JobTuner
from .humble_site import HumbleSite, NotSignedIn, SignedUrl, SiteError, UrlSigner

logger = logging.getLogger(__name__)

# Retried at the transport layer. 429 is deliberately absent: it is
# handled explicitly so the whole pool backs off together
RETRY_STATUSES = (500, 502, 503, 504)
THROTTLE_STATUSES = (429,)
DEFAULT_COOLDOWN = 30

# Conventional exit status for "killed by SIGINT"
INTERRUPT_EXIT_CODE = 130
# Exit status for a run that finished but gave up on some files
FAILED_EXIT_CODE = 1
# How many of those the end of the run names, before "and N more"
FAILURES_LISTED = 20
# Longest the cache goes unwritten while files are being recorded
CACHE_FLUSH_SECONDS = 5.0
# How long a first Ctrl-C waits for transfers to wind themselves up.
# Ordinary transfers stop at their next chunk, well inside a second, so
# this is only ever spent on one stuck somewhere it cannot notice
SHUTDOWN_GRACE = 300


class Interrupted(Exception):
    """Raised inside a transfer when the run is being shut down.

    Distinct from a failure: there is nothing to retry or resume, and it
    should not be reported as a download that went wrong.
    """


class ChecksumMismatch(Exception):
    """What arrived is not what the api said it would be.

    Resuming would keep the bad bytes, so this always restarts the file
    from nothing rather than picking it back up.
    """


class LibraryPathError(Exception):
    """The library path cannot be written to.

    Nothing is worth attempting until it can, so this is raised before
    the run starts rather than discovered one file at a time.
    """


def _struct_hashes(entry):
    """Checksums the api offers for a file, strongest first.

    Everything is optional: entries without one are downloaded and kept
    exactly as before.
    """
    found = {}
    for name in ("sha1", "md5"):
        value = entry.get(name)
        if isinstance(value, str) and value.strip():
            found[name] = value.strip().lower()
    return found


def _validator_for(response):
    """What If-Range should carry, so the server can tell us the file
    changed rather than handing back a slice of a different one
    """
    try:
        headers = response.headers
    except Exception:
        return None
    for name in ("ETag", "Last-Modified"):
        value = headers.get(name)
        if value:
            return value
    return None


def _digest_of(algorithm, hasher):
    """What was computed, ready to record in the cache"""
    if hasher is None or algorithm is None:
        return {}
    return {algorithm: hasher.hexdigest()}


def _resume_note(part_file):
    """Beside a part file: what the server called the file it came from"""
    return part_file + ".json"


def _write_resume_note(part_file, validator):
    """Record the validator a later run needs to carry on safely. With
    no validator there is no safe way to, so there is no note, and the
    part file is not kept"""
    note = _resume_note(part_file)
    if not validator:
        _remove_quietly(note)
        return
    try:
        with open(note, "w") as handle:
            json.dump({"validator": validator}, handle)
    except OSError:
        pass


def _resume_point(part_file):
    """(bytes held, validator) for a part file an earlier run left, or
    None when there is nothing safe to carry on from"""
    size = _file_size(part_file)
    if not size:
        return None
    try:
        with open(_resume_note(part_file)) as handle:
            validator = json.load(handle).get("validator")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(validator, str) or not validator:
        return None
    return size, validator


def _feed_hash_from_file(hasher, path):
    """Re-read what is already on disk so a resumed transfer's hash
    covers the whole file, not just the part that arrived this time
    """
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 256), b""):
            hasher.update(block)


SITE_HOST = "www.humblebundle.com"


def _jar_from_header(jar, header):
    """Put the cookies from a Cookie header into the session's jar.

    Not into a header: requests drops a hand set Cookie header when it
    follows a redirect and rebuilds it from the jar alone, and the site
    now redirects every page fetch once (Next.js adds ?_rsc=). With the
    header the redirected request went out signed out, and came back as
    the signed out page with nothing in it.

    Scoped to the site's own host, so none of them go to the download
    cdn either
    """
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            jar.set(name.strip(), value.strip(), domain=SITE_HOST, path="/")


def _write_fd(fd, text):
    """Write to a file descriptor directly, past Python's buffered
    streams and their locks. For a signal handler, which may have
    interrupted a thread that was in the middle of writing"""
    try:
        os.write(fd, text.encode("utf-8", "replace"))
    except OSError:
        pass


def _looks_like_cookie_header(value):
    """name=value, as opposed to a bare session value.

    A bare value can be base64 and end in = padding, but padding only
    ever comes at the end, so an = with something after it means a name
    """
    return re.match(r"^\s*[^=;\s\"]+=[^=]", value or "") is not None


def _unique_keys(keys):
    """Keep the first of each key, in the order they were given"""
    seen = set()
    unique = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


def _mount_hint(path):
    """Why a path might be missing, for the error message"""
    absolute = os.path.abspath(path)
    for mount_root in ("/Volumes/", "/media/", "/mnt/", "/run/media/"):
        if absolute.startswith(mount_root):
            return "is the drive plugged in and mounted?"
    parent = os.path.dirname(absolute)
    if parent and not os.path.exists(parent):
        return "{parent} does not exist either".format(parent=parent)
    return None


def _package_version():
    try:
        from importlib.metadata import version

        return version("humblebundle-downloader")
    except Exception:
        return "dev"


USER_AGENT = (
    "humblebundle-downloader/{version} "
    "(+https://github.com/xtream1101/humblebundle-downloader)"
).format(version=_package_version())


def _format_clock(seconds):
    """mm:ss, or --:-- when there is nothing to go on"""
    if seconds is None or seconds < 0 or seconds != seconds:
        return "--:--"
    seconds = int(seconds)
    if seconds >= 3600:
        return "{hours:02d}:{minutes:02d}h".format(
            hours=seconds // 3600, minutes=(seconds % 3600) // 60
        )
    return "{minutes:02d}:{seconds:02d}".format(
        minutes=seconds // 60, seconds=seconds % 60
    )


def _bar(fraction, width):
    if width < 3:
        return ""
    if fraction is None:
        return "[" + "?" * width + "]"
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


# How far along a style that eats what is at the end gets to it
EAT_FROM = 0.9


def _muncher_bar(fraction, width, style, frame):
    """A muncher eating its way along the track.

    With no known size it paces back and forth instead of creeping
    right, so the line still shows something is happening.
    """
    frames = style["muncher"]
    muncher = frames[frame % len(frames)]
    end = style.get("end", "")

    # A style that eats what is at the end arrives at it a little early
    # and spends the rest of the way eating it, a stage at a time
    eaten = style.get("eaten")
    if eaten and fraction is not None:
        arrive = EAT_FROM
        done = max(0.0, min(1.0, fraction))
        if done > arrive:
            bites = (done - arrive) / (1.0 - arrive)
            end = eaten[min(len(eaten) - 1, int(bites * (len(eaten) - 1) + 1e-9))]
        fraction = min(1.0, done / arrive)

    if len(end) + len(muncher) > width:
        end = ""
    width -= len(end)
    span = max(0, width - len(muncher))
    if span == 0:
        return "[" + muncher[:width] + end + "]"

    if fraction is None:
        # Bounce: 0..span..0
        cycle = span * 2
        step = frame % cycle if cycle else 0
        position = step if step <= span else cycle - step
        ahead = style["pellet"] * (span - position)
        behind = style["pellet"] * position
        return "[" + behind + muncher + ahead + end + "]"

    # Where the muncher's first column goes. A style that enters starts
    # wholly off the left of the track, and one that exits finishes
    # wholly off the right; the rest start and stop inside it. Whatever
    # falls outside the track is not drawn
    fraction = max(0.0, min(1.0, fraction))
    first = -len(muncher) if style.get("enters") else 0
    last = width if style.get("exits") else span
    position = first + int(round(fraction * (last - first)))
    if style.get("enters") and fraction > 0:
        # Under way means in sight, however little of the way
        position = max(position, first + 1)

    shown = muncher[max(0, -position):]
    track = style["wake"] * max(0, position) + shown + style["pellet"] * width
    return "[" + track[:width] + end + "]"


def _fit(text, width):
    """Pad or truncate to exactly `width`, keeping the tail of a name
    since that is where a file's distinguishing part usually is
    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text.ljust(width)
    if width <= 3:
        return text[:width]
    return "…" + text[-(width - 1):]


class NullReporter:
    """Stands in when nothing is being displayed.

    Every call site used to ask whether there was a reporter before
    saying anything to it. There is always one now; this is the one
    that listens and does nothing.
    """

    slots = 0
    jobs_note = None

    def enabled(self):
        return False

    def start(self):
        pass

    def stop(self):
        pass

    def clear(self, wait=None):
        pass

    def paint(self):
        pass

    def printing(self):
        return contextlib.nullcontext()

    def attach_logging(self):
        pass

    def detach_logging(self):
        pass

    def forget_painted(self):
        pass

    def render(self, width=80, height=24):
        return []

    def file_queued(self):
        pass

    def file_skipped(self):
        pass

    def traversal_finished(self):
        pass

    def file_started(self, name, total):
        pass

    def file_progress(self, name, downloaded):
        pass

    def file_finished(self, name, ok=True):
        pass


class ProgressReporter:
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

    def __init__(self, stream=None, slots=1, interval=0.2, window=3.0,
                 style=DEFAULT_PROGRESS_STYLE, big_trains=False):
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


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _content_range_total(response):
    """Total size out of a `Content-Range: bytes 100-999/1000` header"""
    try:
        raw = response.headers.get("Content-Range")
    except Exception:
        return None
    if not raw or "/" not in raw:
        return None
    return _coerce_size(raw.rsplit("/", 1)[-1].strip())


def _retry_after_seconds(response, default=DEFAULT_COOLDOWN):
    """Retry-After is either a number of seconds or an http date"""
    raw = None
    try:
        raw = response.headers.get("Retry-After")
    except Exception:
        return default

    if not raw:
        return default

    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        pass

    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return default
    if when is None:
        return default

    if when.tzinfo is None:
        delta = when - datetime.datetime.now()
    else:
        delta = when - datetime.datetime.now(when.tzinfo)
    return max(0, int(delta.total_seconds()))


def _clean_name(dirty_str):
    allowed_chars = (" ", "_", ".", "-", "[", "]")
    clean = []
    for c in dirty_str.replace("+", "_").replace(":", " -"):
        if c.isalpha() or c.isdigit() or c in allowed_chars:
            clean.append(c)

    return "".join(clean).strip().rstrip(".")


def _human_size(num_bytes):
    """Format a byte count the way a human would read it"""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            if unit == "B":
                return "{size:.0f} {unit}".format(size=size, unit=unit)
            return "{size:.2f} {unit}".format(size=size, unit=unit)
        size /= 1024


def _file_ext(filename):
    """Lowercase extension of a filename, or '' when it has none"""
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def _struct_filename(file_type):
    """Filename of a download_struct entry, or None if it has no url"""
    if "url" not in file_type or "web" not in file_type["url"]:
        return None
    return file_type["url"]["web"].split("?")[0].split("/")[-1]


def _order_error(order):
    """Best explanation available for an order with no product data"""
    if not isinstance(order, dict):
        return "the api returned {kind}, not an order".format(
            kind=type(order).__name__
        )

    for key in ("_errors", "error_code", "error", "message"):
        if order.get(key):
            return "the api said {detail}".format(detail=order[key])

    known = ", ".join(sorted(order.keys())) or "nothing"
    return "no product data in the response (keys: {known})".format(known=known)


def _coerce_size(raw_size):
    """The api is not consistent about the type used for file sizes"""
    if raw_size is None:
        return None
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


class DownloadLibrary:
    def __init__(
        self,
        library_path,
        cookie_path=None,
        cookie_auth=None,
        progress_bar=False,
        ext_include=None,
        ext_exclude=None,
        platform_include=None,
        prefer_format=None,
        purchase_keys=None,
        trove=False,
        vault=False,
        update=False,
        dry_run=False,
        print_urls=False,
        print_checksums=False,
        jobs=1,
        retries=5,
        verify=True,
        progress_style=DEFAULT_PROGRESS_STYLE,
        train_per_download=False,
        shutdown_grace=None,
        log_skipped=True,
    ):
        self.library_path = library_path
        # "auto" runs as many workers as it might ever want, and a gate
        # lets the number it has settled on download at once
        self.auto_jobs = isinstance(jobs, str) and jobs.lower() == "auto"
        self.jobs = auto_jobs.MAXIMUM if self.auto_jobs else max(1, int(jobs))
        self._gate = JobGate(auto_jobs.START if self.auto_jobs else self.jobs)
        self._tuner = JobTuner() if self.auto_jobs else None
        self._tuner_thread = None
        self._tuner_stop = threading.Event()
        # Every byte any worker writes, for the tuner to measure with
        self._bytes_moved = 0
        self._bytes_lock = threading.Lock()
        self.retries = max(0, int(retries))
        # Checked against what the api says, when it says anything
        self.verify = verify
        # Whether every file a filter passes over is announced
        self.log_skipped = log_skipped
        # How long a first Ctrl-C waits for transfers in flight. Read
        # from the module default when not given, so it stays one place
        # to change for anyone embedding this
        self.shutdown_grace = max(
            0.0,
            float(SHUTDOWN_GRACE if shutdown_grace is None else shutdown_grace),
        )
        self.progress_bar = progress_bar
        # Several \r progress bars writing at once is unreadable, so the
        # per-file bar is for sequential runs. In parallel one reporter
        # thread owns a single status line for the whole pool
        self._show_bar = progress_bar and self.jobs == 1
        self._show_status = progress_bar and self.jobs > 1
        self.progress_style = progress_style
        self._progress = (
            ProgressReporter(
                slots=self._gate.limit,
                style=progress_style,
                big_trains=train_per_download,
            )
            if self._show_status
            else NullReporter()
        )

        self._queue = None
        self._workers = []
        self._stop = threading.Event()
        self._cache_lock = threading.Lock()
        # The cache is written at most every CACHE_FLUSH_SECONDS, and
        # once more when the run ends, rather than after every file
        self._cache_dirty = False
        self._cache_written_at = 0.0
        self._thread_local = threading.local()

        # A 429 seen by any one worker pauses all of them: retrying per
        # request just means the other workers keep hammering while one
        # of them politely backs off
        self._cooldown_until = 0.0
        self._cooldown_lock = threading.Lock()
        self._throttled_count = 0

        self._notice_lock = threading.Lock()
        self._range_unsupported = False
        # Set by a worker when something has gone wrong that every
        # remaining file would hit too, so the run stops rather than
        # repeating the same error once per file
        self._fatal = None

        # Destinations already handed out this run. A repeated purchase
        # key would otherwise queue the same file twice, and in parallel
        # that means two workers writing one .part at the same time
        self._claimed = set()
        self._claim_lock = threading.Lock()

        self.ext_include = (
            [] if ext_include is None else list(map(str.lower, ext_include))
        )
        self.ext_exclude = (
            [] if ext_exclude is None else list(map(str.lower, ext_exclude))
        )

        if platform_include is None or "all" in platform_include:
            # if 'all', then do not need to use this check
            platform_include = []
        self.platform_include = list(map(str.lower, platform_include))

        # Ordered: the first format with a match wins, so the last entry
        # acts as the default
        self.prefer_format = (
            []
            if prefer_format is None
            else [ext.lower().lstrip(".") for ext in prefer_format]
        )

        self.purchase_keys = purchase_keys
        self.trove = trove
        self.vault = vault
        self.update = update
        self.dry_run = dry_run
        self.print_urls = print_urls
        self.print_checksums = print_checksums
        self._checksum_header_done = False

        # Tally of everything a real run would fetch, filled in by
        # _record_pending_download while self.dry_run is True
        self.pending_downloads = []
        self.unexpanded_asmjs = 0
        self.skipped_orders = []
        self._current_bundle = ""
        # Files already on disk at the expected size that the cache did
        # not know about, which is every file after the site rebuild:
        # bundles are keyed by new ids, so no old cache entry matches
        self.adopted_count = 0
        # Files Humble has replaced without updating its listing
        self.stale_listings = 0
        # (path, why) for every file this run gave up on, so the run can
        # say which, and exit non-zero rather than look like a success
        self.failed_files = []

        # The library as the rebuilt website presents it. Built in
        # start(), once the session has its cookies
        self.site = None
        self._signer = None

        self.session = self._configure_session(requests.Session())
        if cookie_path:
            try:
                cookie_jar = http.cookiejar.MozillaCookieJar(cookie_path)
                # Session cookies, which is what a sign in usually is,
                # are exported with no expiry or an expiry of 0. By
                # default the first are discarded and the second count
                # as expired, so the cookie that matters never loads.
                # A cookie that really is stale is just refused upstream
                cookie_jar.load(ignore_discard=True, ignore_expires=True)
                self.session.cookies = cookie_jar
            except http.cookiejar.LoadError:
                # Still support the original cookie method
                with open(cookie_path, "r") as f:
                    _jar_from_header(self.session.cookies, f.read().strip())
        elif cookie_auth:
            if _looks_like_cookie_header(cookie_auth):
                # A whole cookie header, name=value pairs and all, which
                # is how a cookie other than _simpleauth_sess gets in
                _jar_from_header(self.session.cookies, cookie_auth.strip())
            else:
                self.session.cookies.set(
                    "_simpleauth_sess", cookie_auth.strip(), domain=SITE_HOST
                )

    def _can_write(self, path):
        """Actually write something, rather than ask about permissions.

        os.access answers from the posix bits, which network shares,
        read only mounts and macOS access controls can all disagree with
        """
        probe = os.path.join(path, ".hbd-write-check")
        try:
            with open(probe, "w"):
                pass
        except OSError:
            return False
        finally:
            _remove_quietly(probe)
        return True

    def _check_library_path(self):
        """Confirm the library is usable before going near the network.

        Without this the first sign of trouble is an error from a worker
        thread, a few hundred files into a run, repeated once per file.
        """
        path = self.library_path
        shown = os.path.abspath(path)

        if os.path.exists(path) and not os.path.isdir(path):
            raise LibraryPathError(
                "Library path is not a folder: {shown}".format(shown=shown)
            )

        if not os.path.isdir(path):
            if self.dry_run is True:
                # A dry run writes nothing, so this is worth saying but
                # not worth stopping for
                logger.warning(
                    "Library path does not exist yet: {shown}".format(shown=shown)
                )
                return
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as error:
                raise LibraryPathError(
                    self._path_problem(shown, error.strerror or error)
                )
            logger.info("Created library folder {shown}".format(shown=shown))
            return

        if self.dry_run is not True and not self._can_write(path):
            raise LibraryPathError(
                self._path_problem(shown, "cannot write to it")
            )

    @staticmethod
    def _path_problem(shown, reason):
        message = "Cannot use library path {shown}: {reason}".format(
            shown=shown, reason=reason
        )
        hint = _mount_hint(shown)
        if hint:
            message += " ({hint})".format(hint=hint)
        return message

    def start(self):
        self._check_library_path()

        self.cache_file = os.path.join(self.library_path, ".cache.json")
        self.cache_data = self._load_cache_data(self.cache_file)
        if self.purchase_keys:
            self.purchase_keys = self._dedupe_keys(self.purchase_keys)

        if self.site is None:
            self.site = HumbleSite(self.session)
        if self._signer is None:
            self._signer = UrlSigner(self.site)

        previous_handler = self._install_interrupt_handler()
        try:
            # One handler for the whole run, not just the walk: a Ctrl-C
            # is at least as likely to land while waiting on the workers
            # as during traversal, and it has to be caught either way
            try:
                self._start_workers()

                for order_id, order in self._all_orders():
                    if self._stop.is_set():
                        # Interrupted, or something a worker found that
                        # the rest of the run would hit too
                        break
                    self._process_order(order_id, order)

                # Everything is queued, so the totals have a real
                # denominator now and can show a bar
                self._progress.traversal_finished()

                self._stop_workers()

                if isinstance(self._fatal, Exception):
                    raise self._fatal
                if self._fatal:
                    raise LibraryPathError(self._fatal)

                if self._stop.is_set():
                    # A sequential transfer was interrupted
                    sys.exit(INTERRUPT_EXIT_CODE)
            except SiteError:
                # Reading the library failed partway, usually an expired
                # cookie. Downloads in flight get to finish their chunk
                self._stop.set()
                self._stop_workers(timeout=self.shutdown_grace)
                raise
            except KeyboardInterrupt:
                self._stop.set()
                # Bounded: transfers check the stop flag between chunks,
                # so this is quick, and anything that will not stop gets
                # left behind rather than holding the run open
                self._stop_workers(timeout=self.shutdown_grace)
                logger.warning("Interrupted, nothing further was downloaded")
                sys.exit(INTERRUPT_EXIT_CODE)
        finally:
            # However the run ends, what it learned is kept
            try:
                self._flush_cache()
            except Exception as error:
                logger.error("Could not save the cache: {0}".format(error))
            self._restore_interrupt_handler(previous_handler)

        if self.dry_run is True:
            self._log_dry_run_summary()

        if self._tuner is not None and self._tuner.best:
            rate, limit = self._tuner.best
            logger.info(
                "Finished at {now} downloads at once. The fastest seen was "
                "{rate}/s, with {limit}: -j {limit} is a good fixed "
                "setting for this connection".format(
                    now=self._gate.limit, rate=_human_size(rate), limit=limit
                )
            )

        if self.failed_files:
            logger.error(
                "{count} file(s) could not be downloaded, and will be tried "
                "again next run:".format(count=len(self.failed_files))
            )
            for local_file, reason in self.failed_files[:FAILURES_LISTED]:
                logger.error(
                    "  {path} ({reason})".format(
                        path=os.path.relpath(local_file, self.library_path),
                        reason=reason,
                    )
                )
            if len(self.failed_files) > FAILURES_LISTED:
                logger.error(
                    "  ...and {more} more".format(
                        more=len(self.failed_files) - FAILURES_LISTED
                    )
                )

        if self.stale_listings:
            logger.warning(
                "{count} file(s) were a different version from the one "
                "Humble lists, so did not match its checksums. They were "
                "kept, and their own checksums recorded in the cache".format(
                    count=self.stale_listings
                )
            )

        if self.adopted_count:
            logger.info(
                "{count} file(s) were already on disk at the right size and "
                "were recorded in the cache rather than downloaded "
                "again".format(count=self.adopted_count)
            )

        if self._throttled_count:
            logger.warning(
                "Throttled {count} time(s) during this run. Lower --jobs "
                "if it keeps happening".format(count=self._throttled_count)
            )

        if self.skipped_orders:
            logger.warning(
                "{count} order(s) were skipped and nothing from them was "
                "checked: {keys}".format(
                    count=len(self.skipped_orders),
                    keys=" ".join(self.skipped_orders),
                )
            )

    def _hard_exit(self):
        """Leave now, without waiting on anything that might be stuck.

        os._exit skips interpreter shutdown, which is the point: nothing
        it would wait for can keep us here. Part files are left where
        they are and the next run truncates them.
        """
        try:
            self._flush_cache(wait=2)
        except Exception:
            pass
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(INTERRUPT_EXIT_CODE)

    def _install_interrupt_handler(self):
        """First Ctrl-C asks for a tidy stop, second one insists.

        Signal handlers only run on the main thread, so this is where
        both presses are seen no matter which thread is transferring.
        """
        if threading.current_thread() is not threading.main_thread():
            return None

        def handler(signum, frame):
            self._stop.set()
            # The second press is the operating system's, not ours. A
            # Python handler only runs when the main thread gets to it,
            # and quitting from one meant taking the terminal lock and
            # writing through sys.stderr, either of which another thread
            # can be holding at the time. Then the press that was meant
            # to quit whatever state things are in would wait on them
            # instead. With the default action, the kernel ends the
            # process the moment it arrives
            try:
                signal.signal(signal.SIGINT, signal.SIG_DFL)
            except (ValueError, OSError):
                pass
            # Straight to the descriptor: no locks, no buffer another
            # thread might be in the middle of. The block is drawn afresh
            # below this rather than over it
            self._progress.forget_painted()
            _write_fd(
                2,
                "\nStopping. Waiting for downloads in flight, press "
                "Ctrl-C again to quit immediately.\n",
            )
            # Same as the default handler, so anything the main thread
            # is blocked on unwinds exactly as it used to
            raise KeyboardInterrupt

        try:
            return signal.signal(signal.SIGINT, handler)
        except ValueError:
            # Not the main thread of the main interpreter
            return None

    def _restore_interrupt_handler(self, previous):
        if previous is None:
            return
        try:
            signal.signal(signal.SIGINT, previous)
        except (ValueError, TypeError):
            pass

    def _configure_session(self, session):
        """Transport level retries, plus a user agent that says who we
        are. The default python-requests one gets treated more harshly
        by a lot of edge configurations
        """
        session.headers.update({"User-Agent": USER_AGENT})

        if self.retries == 0:
            return session

        retry_args = dict(
            total=self.retries,
            connect=self.retries,
            read=self.retries,
            status=self.retries,
            backoff_factor=1,
            status_forcelist=RETRY_STATUSES,
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        try:
            retry = Retry(allowed_methods=frozenset(["GET", "HEAD"]), **retry_args)
        except TypeError:
            # urllib3 < 1.26 spelled it differently
            retry = Retry(method_whitelist=frozenset(["GET", "HEAD"]), **retry_args)

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_maxsize=max(10, self.jobs),
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _wait_out_cooldown(self):
        """Block until any pool wide throttle pause has expired"""
        while not self._stop.is_set():
            with self._cooldown_lock:
                remaining = self._cooldown_until - time.time()
            if remaining <= 0:
                return
            # Woken early if the run is interrupted
            self._stop.wait(min(remaining, 1.0))

    def _enter_cooldown(self, seconds, remote_file):
        """Pause every worker, not just the one that got the 429"""
        with self._cooldown_lock:
            self._throttled_count += 1
            now = time.time()
            until = now + seconds
            if until <= self._cooldown_until:
                # Another worker already called for a longer pause
                return
            # Workers refused at the same moment would otherwise each
            # announce the same pause
            announce = self._cooldown_until <= now or (
                until - self._cooldown_until > 1
            )
            self._cooldown_until = until

        if not announce:
            return

        logger.warning(
            "Throttled by the server (429) on {name}, pausing all "
            "downloads for {seconds}s".format(
                name=os.path.basename(remote_file.split("?")[0]),
                seconds=seconds,
            )
        )

    def _start_workers(self):
        if self.jobs == 1 or self.dry_run is True:
            # Nothing to parallelise: a dry run makes no downloads
            return

        if self.auto_jobs:
            logger.info(
                "Downloading {start} at a time to begin with, finding the "
                "best number as it goes (up to {most})".format(
                    start=self._gate.limit, most=self.jobs
                )
            )
            self._progress.jobs_note = "{0} at once".format(self._gate.limit)
        else:
            logger.info(
                "Downloading with {jobs} parallel jobs".format(jobs=self.jobs)
            )
        self._progress.start()

        # Bounded, so traversal cannot run far ahead of the downloads
        self._queue = queue.Queue(maxsize=self.jobs * 4)
        for _ in range(self.jobs):
            worker = threading.Thread(target=self._worker, daemon=True)
            worker.start()
            self._workers.append(worker)

        if self.auto_jobs:
            self._tuner_stop.clear()
            self._tuner_thread = threading.Thread(target=self._tune, daemon=True)
            self._tuner_thread.start()

    def _count_bytes(self, count):
        with self._bytes_lock:
            self._bytes_moved += count

    def _tune(self, interval=None, tick=1.0):
        """Measure total speed every so often and let the tuner move the
        number of downloads at once. A measurement only counts when every
        slot had something to download most of the time: a slow library
        read is no reason to think more at once would not help
        """
        interval = auto_jobs.INTERVAL if interval is None else interval
        started = time.time()
        with self._bytes_lock:
            moved = self._bytes_moved
        throttled = self._throttled_count
        samples = busy = 0

        while not self._tuner_stop.wait(tick):
            samples += 1
            if self._gate.active >= self._gate.limit:
                busy += 1
            now = time.time()
            if now - started < interval:
                continue

            with self._bytes_lock:
                total = self._bytes_moved
            rate = (total - moved) / (now - started)
            old = self._gate.limit
            new, reason = self._tuner.observe(
                rate,
                busy=busy >= 0.8 * samples,
                throttled=self._throttled_count > throttled,
            )
            if new != old:
                self._gate.set_limit(new)
                logger.info(
                    "Now downloading {new} at once, was {old}: {reason} "
                    "({rate}/s in total)".format(
                        new=new, old=old, reason=reason,
                        rate=_human_size(rate),
                    )
                )
            self._progress.jobs_note = "{0} at once".format(new)

            started, moved = now, total
            throttled = self._throttled_count
            samples = busy = 0

    def _stop_workers(self, timeout=None):
        self._tuner_stop.set()
        if self._tuner_thread is not None:
            self._tuner_thread.join(timeout=2)
            self._tuner_thread = None
        if self._queue is None:
            return

        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                # Workers time out on get() and notice _stop anyway
                pass

        deadline = None if timeout is None else time.time() + timeout
        for worker in self._workers:
            if deadline is None:
                worker.join()
            else:
                worker.join(timeout=max(0, deadline - time.time()))

        stubborn = [worker for worker in self._workers if worker.is_alive()]

        self._workers = []
        self._queue = None

        self._progress.stop()

        if stubborn:
            # A transfer is wedged somewhere uninterruptible. The user
            # asked to stop, so stop
            logger.warning(
                "{count} download(s) did not stop within {grace}s, "
                "quitting anyway".format(
                    count=len(stubborn), grace=timeout
                )
            )
            self._hard_exit()

    def _worker(self):
        while True:
            try:
                # Time limited so a shutdown is noticed even when the
                # sentinel cannot be delivered past a full queue
                job = self._queue.get(timeout=0.25)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue

            try:
                if job is None:
                    return
                if self._stop.is_set():
                    continue
                if not self._gate.acquire(self._stop):
                    continue
                try:
                    self._run_download_job(job)
                finally:
                    self._gate.release()
            except Exception as error:
                logger.exception(
                    "Failed to download {remote_file}".format(
                        remote_file=job.get("remote_file")
                    )
                )
                self._record_failure(
                    os.path.join(job.get("local_folder", ""), job.get("local_filename", "")),
                    str(error) or type(error).__name__,
                )
            finally:
                self._queue.task_done()

    def _session(self):
        """requests.Session is not documented as thread safe, so every
        worker thread gets its own, built from the authenticated one
        """
        if self._queue is None:
            return self.session

        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._new_session()
            self._thread_local.session = session
        return session

    def _new_session(self):
        session = self._configure_session(requests.Session())
        session.headers.update(self.session.headers)
        session.cookies.update(self.session.cookies)
        return session

    def _all_orders(self):
        """The library's bundles, then the vault when asked for. --trove
        meant only the Trove, so it now means only the vault, which is
        what the Trove became
        """
        if self.trove is not True:
            for order_id, order in self.site.orders(keys=self.purchase_keys or None):
                yield order_id, order
        if self.trove is True or self.vault is True:
            yield self.site.vault()

    def _process_order(self, order_id, order):
        """Queue everything in one bundle, given in the old api's shape"""
        if not isinstance(order, dict) or "product" not in order:
            self._skip_order(order_id, _order_error(order))
            return

        bundle_title = _clean_name(order["product"]["human_name"])
        self._current_bundle = bundle_title
        logger.info("Checking bundle: " + str(bundle_title))
        for product in order.get("subproducts", []):
            self._process_product(order_id, bundle_title, product)

    def _skip_order(self, order_id, reason):
        """One unusable order must not take the whole run down with it"""
        self.skipped_orders.append(order_id)
        logger.error(
            "Skipping order {order_id}: {reason}".format(
                order_id=order_id, reason=reason
            )
        )

    def _rename_old_file(self, local_filename, append_str):
        # Check if older file exists, if so rename
        if os.path.isfile(local_filename) is True:
            filename_parts = local_filename.rsplit(".", 1)
            new_name = "{name}_{append_str}.{ext}".format(
                name=filename_parts[0], append_str=append_str, ext=filename_parts[1]
            )
            os.rename(local_filename, new_name)
            logger.info("Renamed older file to {new_name}".format(new_name=new_name))

    def _select_by_format(self, file_types, product_title, platform):
        """Narrow a platform's files down to the preferred format.

        Tries each --prefer-format extension in order and keeps every
        file of the first one that matches, so multi part items (a comic
        split across volumes) survive intact. When none of the preferred
        formats are present, falls back to the single largest file.
        Entries without a downloadable url (asm.js games, external
        links) are never dropped, since they have no format to compare.
        """
        if not self.prefer_format:
            return file_types

        passthrough = []
        candidates = []
        for file_type in file_types:
            filename = _struct_filename(file_type)
            if filename is None:
                passthrough.append(file_type)
            elif self._should_download_file_by_ext_and_log(filename) is True:
                candidates.append((file_type, filename, _file_ext(filename)))

        if len(candidates) < 2:
            # Nothing to choose between
            return passthrough + [entry[0] for entry in candidates]

        for wanted in self.prefer_format:
            chosen = [entry for entry in candidates if entry[2] == wanted]
            if chosen:
                self._log_format_choice(
                    product_title, platform, wanted, candidates, chosen
                )
                return passthrough + [entry[0] for entry in chosen]

        # No preferred format available, so take the biggest file there is
        largest = max(
            candidates,
            key=lambda entry: _coerce_size(entry[0].get("file_size")) or 0,
        )
        self._log_format_choice(
            product_title, platform, None, candidates, [largest]
        )
        return passthrough + [largest[0]]

    def _log_format_choice(
        self, product_title, platform, wanted, candidates, chosen
    ):
        dropped = [
            entry[1] for entry in candidates if entry not in chosen
        ]
        if not dropped:
            return
        if wanted is None:
            reason = "no preferred format available, taking the largest file"
        else:
            reason = "preferring {wanted}".format(wanted=wanted)
        self._log_skip(
            "{product_title} [{platform}]: {reason}, skipping {dropped}".format(
                product_title=product_title,
                platform=platform,
                reason=reason,
                dropped=", ".join(dropped),
            )
        )

    def _process_product(self, order_id, bundle_title, product):
        product_title = _clean_name(product["human_name"])
        # Get all types of download for a product
        for download_type in product["downloads"]:
            if self._should_download_platform(download_type["platform"]) is False:
                self._log_skip(
                    "Skipping {platform} for {product_title}".format(
                        platform=download_type["platform"], product_title=product_title
                    )
                )
                continue

            product_folder = os.path.join(
                self.library_path, bundle_title, product_title
            )
            # Create directory to save the files to
            if self.dry_run is False:
                try:
                    os.makedirs(product_folder)
                except OSError:
                    pass

            # Download each file type of a product
            for file_type in self._select_by_format(
                download_type["download_struct"],
                product_title,
                download_type["platform"],
            ):
                try:
                    if "url" in file_type and "web" in file_type["url"]:
                        # downloadable URL
                        url = file_type["url"]["web"]

                        url_filename = url.split("?")[0].split("/")[-1]

                        signing = file_type.get("hbd_sign")
                        if signing:
                            # The site gives a filename, not a url. One is
                            # minted in the worker, right before it is used
                            url = SignedUrl(
                                self._signer,
                                signing["entitlement"],
                                signing["download_key"],
                                url_filename,
                                vault=bool(signing.get("vault")),
                            )

                        if (
                            self._should_download_file_by_ext_and_log(url_filename)
                            is False
                        ):
                            continue

                        cache_file_key = order_id + ":" + url_filename
                        try:
                            self._check_cache_and_download(
                                cache_file_key,
                                url,
                                product_folder,
                                url_filename,
                                file_size=_coerce_size(file_type.get("file_size")),
                                hashes=_struct_hashes(file_type),
                            )
                        except FileExistsError:
                            continue
                        except Exception:
                            logger.exception("Failed to download {url}".format(url=url))
                    elif "asm_config" in file_type:
                        # asm.js game playable directly in the browser
                        game_name = file_type["asm_config"]["display_item"]
                        local_folder = os.path.join(product_folder, game_name)
                        # Create directory to save the files to
                        if self.dry_run is False:
                            try:
                                os.makedirs(local_folder, exist_ok=True)  # noqa: E701
                            except OSError:
                                pass  # noqa: E701

                        # get the HTML file that presents the game, used in the Humble web interface iframe
                        asmjs_html_filename = game_name + ".html"
                        asmjs_local_html_filename = game_name + ".local.html"
                        cache_file_key = order_id + ":" + asmjs_html_filename
                        # game_name might be "game" or "game_asm" but the path to the file here always uses the "game_asm" version
                        game_asm_name = file_type["asm_manifest"]["asmFile"].split("/")[
                            2
                        ]
                        asmjs_url = (
                            "https://www.humblebundle.com/play/asmjs/"
                            + game_asm_name
                            + "/"
                            + order_id
                        )

                        if (
                            self._should_download_file_by_ext_and_log(
                                asmjs_html_filename
                            )
                            is False
                        ):
                            continue

                        if self.dry_run is True:
                            # The data files this game needs are listed
                            # inside the html page, which a dry run does
                            # not fetch, so only the page itself is sized
                            try:
                                self._check_cache_and_download(
                                    cache_file_key,
                                    asmjs_url,
                                    local_folder,
                                    asmjs_html_filename,
                                )
                            except FileExistsError:
                                pass
                            except Exception:
                                logger.exception(
                                    "Failed to check {asmjs_url}".format(
                                        asmjs_url=asmjs_url
                                    )
                                )
                            self.unexpanded_asmjs += 1
                            continue

                        downloaded = False
                        try:
                            # Not queued: the manifest is read out of this
                            # page, so it has to be on disk before the
                            # data files below can even be named
                            downloaded = self._check_cache_and_download(
                                cache_file_key,
                                asmjs_url,
                                local_folder,
                                asmjs_html_filename,
                                synchronous=True,
                            )
                        except FileExistsError:
                            pass  # we should download the asm/data files even if the html file was previously downloaded
                        except Exception:
                            logger.exception(
                                "Failed to download {asmjs_url}".format(
                                    asmjs_url=asmjs_url
                                )
                            )
                            continue

                        # read from the html file a version of file_type['asm_manifest'] with HMAC/etc auth params on the URLs
                        with open(
                            os.path.join(local_folder, asmjs_html_filename), "r"
                        ) as asmjs_html:
                            asmjs_page = parsel.Selector(text=asmjs_html.read())
                            asm_player_data_text = asmjs_page.css(
                                "#webpack-asm-player-data::text"
                            ).get()  # noqa: E501
                            asm_player_data = json.loads(asm_player_data_text)

                        if downloaded:
                            # create the local playable version of the html file
                            # by replacing remote manifest URLs with the local filename
                            try:
                                with open(
                                    os.path.join(local_folder, asmjs_html_filename), "r"
                                ) as asmjs_html:
                                    with open(
                                        os.path.join(
                                            local_folder, asmjs_local_html_filename
                                        ),
                                        "w",
                                    ) as asmjs_local_html:
                                        for line in asmjs_html:
                                            for (
                                                local_filename,
                                                remote_file,
                                            ) in asm_player_data["asmOptions"][
                                                "manifest"
                                            ].items():
                                                line = line.replace(
                                                    f'"{local_filename}": "{remote_file}"',
                                                    f'"{local_filename}": "{local_filename}"',
                                                )
                                            asmjs_local_html.write(line)
                            except Exception:
                                logger.exception(
                                    "Failed to create local version of {asmjs_html_filename}".format(
                                        asmjs_html_filename=asmjs_html_filename
                                    )
                                )

                        # TODO deduplicate these files? Osmos example has 3 unique files and 2 dupes with different names
                        for local_filename, remote_file in asm_player_data[
                            "asmOptions"
                        ]["manifest"].items():
                            cache_file_key = (
                                order_id + ":" + game_name + ":" + local_filename
                            )
                            try:
                                self._check_cache_and_download(
                                    cache_file_key,
                                    remote_file,
                                    local_folder,
                                    local_filename,
                                )
                            except FileExistsError:
                                continue
                            except Exception:
                                logger.exception(
                                    "Failed to download {url}".format(url=url)
                                )
                                continue

                    elif "external_link" in file_type:
                        logger.info(
                            "External url found: {bundle_title}/{product_title} : {url}".format(
                                bundle_title=bundle_title,
                                product_title=product_title,
                                url=file_type["external_link"],
                            )
                        )

                    else:
                        logger.info(
                            "No downloadable url(s) found: {bundle_title}/{product_title}".format(
                                bundle_title=bundle_title, product_title=product_title
                            )
                        )
                        logger.info(file_type)
                        continue
                except Exception:
                    logger.exception(
                        "Failed to download this 'file':\n{file_type}".format(
                            file_type=file_type
                        )
                    )
                    continue

    def _log_url(self, url):
        if self.print_urls is True and url:
            if isinstance(url, SignedUrl):
                # Asking for the list is asking for these to be signed
                url = self._resolve_signed(url)
                if url is None:
                    return
            # Straight to stdout, one per line, so the list stays usable
            # when piped into another tool. All logging goes to stderr
            with self._progress.printing():
                print(url)

    def _get_remote_headers(self, remote_file):
        """Headers only, so nothing is transferred to size up a file"""
        head_r = self._request_with_backoff(
            "head", remote_file, quiet=True, allow_redirects=True
        )
        if head_r is None:
            return None

        if head_r.status_code != 200:
            logger.debug(
                "File unavailable {remote_file} status code {status_code}".format(
                    remote_file=remote_file, status_code=head_r.status_code
                )
            )
            return None

        return head_r.headers

    def _check_pending_download(
        self, remote_file, local_file, file_size, cache_file_info
    ):
        """Work out if a real run would download this file, and how big
        it is, without fetching any of its content
        """
        cached_modified = cache_file_info.get("url_last_modified")
        headers = None
        if file_size is None or cached_modified is not None:
            headers = self._get_remote_headers(remote_file)

        if headers is not None:
            if (
                cached_modified is not None
                and headers.get("Last-Modified") == cached_modified
            ):
                # Unchanged since the last run, a real run would skip it
                return

            if file_size is None:
                file_size = _coerce_size(headers.get("Content-Length"))

        self._record_pending_download(remote_file, local_file, file_size)

    def _record_pending_download(self, remote_file, local_file, file_size):
        self.pending_downloads.append(
            {
                "bundle": self._current_bundle,
                "local_file": local_file,
                "url": str(remote_file),
                "file_size": file_size,
            }
        )
        if file_size is None:
            logger.info(
                "Would download: {local_file} (size unknown)".format(
                    local_file=local_file
                )
            )
        else:
            logger.info(
                "Would download: {local_file} ({size})".format(
                    local_file=local_file, size=_human_size(file_size)
                )
            )
        self._log_url(remote_file)

    def _log_dry_run_summary(self):
        bundles = {}
        total_size = 0
        unknown_count = 0
        for pending in self.pending_downloads:
            bundle_size, bundle_files = bundles.get(pending["bundle"], (0, 0))
            file_size = pending["file_size"]
            if file_size is None:
                unknown_count += 1
                file_size = 0
            total_size += file_size
            bundles[pending["bundle"]] = (
                bundle_size + file_size,
                bundle_files + 1,
            )

        logger.info("\n" + ("=" * 60))
        logger.info("Dry run: nothing was downloaded")
        logger.info("=" * 60)

        for bundle, (bundle_size, bundle_files) in sorted(
            bundles.items(), key=lambda item: item[1][0], reverse=True
        ):
            logger.info(
                "{size:>10}  {files:>4} file(s)  {bundle}".format(
                    size=_human_size(bundle_size),
                    files=bundle_files,
                    bundle=bundle or "Unknown bundle",
                )
            )

        logger.info("-" * 60)
        logger.info(
            "{files} file(s) to download, {size} ({raw:,} bytes)".format(
                files=len(self.pending_downloads),
                size=_human_size(total_size),
                raw=total_size,
            )
        )
        if unknown_count > 0:
            logger.info(
                "{count} of those file(s) did not report a size, so the "
                "real total will be larger".format(count=unknown_count)
            )
        if self.unexpanded_asmjs > 0:
            logger.info(
                "{count} asm.js game(s) were not sized: their data files "
                "are only listed inside the game page, which a dry run "
                "does not download".format(count=self.unexpanded_asmjs)
            )

    def _update_cache_data(self, cache_file_key, file_info):
        """Record a file in the cache, writing it out now and then.

        Rewriting the whole file after every download meant thousands of
        full rewrites of a growing file, each flushed to disk, with every
        other worker waiting on it: gigabytes of writes over a large
        library, and a first run adopting files back to back did nothing
        else. Now it is written at most every CACHE_FLUSH_SECONDS, and
        once more when the run ends however it ends. A run killed outright
        loses those last seconds of entries, for files that are complete
        on disk, which the next run recognises and records without
        downloading them again
        """
        with self._cache_lock:
            self.cache_data[cache_file_key] = file_info
            self._cache_dirty = True
            if time.time() - self._cache_written_at >= CACHE_FLUSH_SECONDS:
                self._flush_cache_locked()

    def _flush_cache(self, wait=None):
        """Write the cache out if anything has changed since it last was.
        wait bounds how long to wait for the lock, for a run leaving in a
        hurry; past it the write is skipped rather than hanging
        """
        if not self._cache_lock.acquire(timeout=-1 if wait is None else wait):
            return
        try:
            if self._cache_dirty:
                self._flush_cache_locked()
        finally:
            self._cache_lock.release()

    def _flush_cache_locked(self):
        self._write_cache_data()
        self._cache_dirty = False
        self._cache_written_at = time.time()

    def _write_cache_data(self):
        """Write somewhere else, then rename over the top.

        The whole file is rewritten each time, so writing in place
        leaves a window in which the cache is half written. A run killed in that window, which is exactly what a
        second Ctrl-C does, used to leave a truncated or empty
        .cache.json that the next run could not read at all. os.replace
        is atomic, so what is on disk is always one whole version or the
        other.
        """
        folder = os.path.dirname(self.cache_file) or "."
        handle, temp_path = tempfile.mkstemp(
            prefix=".cache.json.", suffix=".tmp", dir=folder
        )
        try:
            with os.fdopen(handle, "w") as outfile:
                json.dump(
                    self.cache_data,
                    outfile,
                    sort_keys=True,
                    indent=4,
                )
                outfile.flush()
                os.fsync(outfile.fileno())
            os.replace(temp_path, self.cache_file)
        except Exception:
            _remove_quietly(temp_path)
            raise

    def _check_cache_and_download(
        self,
        cache_file_key,
        remote_file,
        local_folder,
        local_filename,
        file_size=None,
        synchronous=False,
        hashes=None,
    ):
        cache_file_info = self.cache_data.get(cache_file_key, {})

        if cache_file_info != {} and self.update is not True:
            # Do not care about checking for updates at this time
            raise FileExistsError

        local_file = os.path.join(local_folder, local_filename)

        if cache_file_info == {} and self._adopt(
            cache_file_key, local_file, file_size, remote_file
        ):
            raise FileExistsError

        if not self._claim(local_file):
            logger.debug(
                "Already handled {local_file} this run".format(
                    local_file=local_file
                )
            )
            raise FileExistsError

        self._print_checksum(local_file, hashes)

        if self.dry_run is True:
            self._check_pending_download(
                remote_file,
                local_file,
                file_size,
                cache_file_info,
            )
            return False

        job = {
            "cache_file_key": cache_file_key,
            "remote_file": remote_file,
            "local_folder": local_folder,
            "local_filename": local_filename,
            "cache_file_info": cache_file_info,
            "hashes": hashes or {},
            "listed_size": file_size,
        }

        self._progress.file_queued()

        if self._queue is not None and synchronous is False:
            # Hand it to a worker. Blocks once the queue is full, which
            # keeps traversal from running away from the downloads
            self._queue.put(job)
            return False

        return self._run_download_job(job)

    def _adopt(self, cache_file_key, local_file, file_size, remote_file=None):
        """Take a file already on disk as downloaded, when the cache has
        never heard of it but it is exactly the size it should be.

        The rebuilt site identifies bundles by new ids, so after it no
        old cache entry matches anything and a whole library would be
        downloaded again. Size is the check rather than a checksum: the
        old cache trusted its entries without one, and hashing a large
        library on a slow drive costs hours.

        The entry records no Last-Modified, since none was seen. The
        first --update to reach it fills that in rather than treating
        the mismatch as a change, so later updates compare like for like
        """
        if not file_size:
            return False
        try:
            on_disk = os.path.getsize(local_file)
        except OSError:
            return False
        if on_disk != file_size:
            # Perhaps a newer version than Humble lists, which is what
            # the server sends now. One HEAD request costs far less than
            # downloading it again to find out
            if remote_file is None or not on_disk:
                return False
            headers = self._get_remote_headers(remote_file)
            served = _coerce_size((headers or {}).get("Content-Length"))
            if served != on_disk:
                return False

        self.adopted_count += 1
        logger.debug(
            "Already have {local_file}, recording it".format(local_file=local_file)
        )
        if self.dry_run is not True:
            # Whatever an earlier run left half done is not needed now
            _remove_quietly(local_file + ".part")
            _remove_quietly(_resume_note(local_file + ".part"))
        if self.dry_run is not True:
            self._update_cache_data(cache_file_key, {"adopted": True})
        return True

    def _claim(self, local_file):
        """First caller to ask for a destination gets it.

        Two jobs writing the same file would interleave into one .part
        and produce something corrupt that looks finished
        """
        with self._claim_lock:
            if local_file in self._claimed:
                return False
            self._claimed.add(local_file)
            return True

    def _dedupe_keys(self, keys):
        unique = _unique_keys(keys)
        repeated = len(keys) - len(unique)
        if repeated:
            logger.info(
                "Ignoring {repeated} repeated purchase key(s)".format(
                    repeated=repeated
                )
            )
        return unique

    def _note_skip(self):
        """Queued, then turned out not to need downloading"""
        self._progress.file_skipped()

    def _library_vanished(self):
        """The library root is no longer there.

        Every remaining file would hit this, so stop the run rather than
        say the same thing several hundred times
        """
        if self._fatal:
            return
        self._fatal = self._path_problem(
            os.path.abspath(self.library_path),
            "it went away while the run was going",
        )
        self._stop.set()

    def _folder_unusable(self, local_file, error):
        """Could not make somewhere to put a file"""
        self._note_skip()
        logger.error(
            "Cannot create a folder for {name}: {reason}".format(
                name=os.path.basename(local_file),
                reason=error.strerror or error,
            )
        )

        if not os.path.isdir(self.library_path):
            self._library_vanished()

    def _get_with_backoff(self, remote_file, stream=True, range_start=None,
                          if_range=None):
        """GET that honours the pool wide cooldown and handles a 429.

        Connection errors and 5xx are retried inside the session's
        transport adapter. A 429 is handled here instead, so that every
        worker pauses rather than only the one that was refused.

        range_start asks the server to resume from that byte. Whether it
        agrees is its business: the caller checks the status code.

        if_range carries the validator from the first response, so that
        a file which changed underneath us comes back whole, as a 200,
        instead of a slice that would be spliced onto the wrong bytes.
        """
        headers = None
        if range_start:
            headers = {"Range": "bytes={start}-".format(start=range_start)}
            if if_range:
                headers["If-Range"] = if_range

        return self._request_with_backoff(
            "get", remote_file, stream=stream, headers=headers
        )

    def _request_with_backoff(self, method, url, quiet=False, **kwargs):
        """Send a request, waiting out any pool wide cooldown first and
        treating a 429 as everyone's problem rather than this caller's.

        url may be a SignedUrl, signed here, at the last moment. Humble
        keeps handing out the same signed url until it lapses, so one
        that is refused is signed again, once, and tried again
        """
        if not isinstance(url, SignedUrl):
            return self._send_with_backoff(method, url, quiet=quiet, **kwargs)

        resolved = self._resolve_signed(url)
        if resolved is None:
            return None
        response = self._send_with_backoff(method, resolved, quiet=quiet, **kwargs)
        if response is None or response.status_code not in (401, 403):
            return response

        try:
            response.close()
        except Exception:
            pass
        logger.debug(
            "The download url for {name} was refused, signing a fresh "
            "one".format(name=url.filename)
        )
        resolved = self._resolve_signed(url, refresh=True)
        if resolved is None:
            return None
        return self._send_with_backoff(method, resolved, quiet=quiet, **kwargs)

    def _resolve_signed(self, url, refresh=False):
        """A real url for a SignedUrl, or None when there is not one"""
        try:
            return url.resolve(session=self._session(), refresh=refresh)
        except NotSignedIn as error:
            # Every remaining file would get the same answer
            if not self._fatal:
                self._fatal = error
            self._stop.set()
        except Exception as error:
            logger.error(
                "Could not get a download url for {name}: {error}".format(
                    name=url.filename, error=error
                )
            )
        return None

    def _send_with_backoff(self, method, url, quiet=False, **kwargs):
        """The GET that streams a file and the HEAD that sizes one had
        the same loop twice over, differing only in the verb and in how
        loudly they complain when it does not work.
        """
        for _ in range(self.retries + 1):
            self._wait_out_cooldown()
            if self._stop.is_set():
                return None

            try:
                response = getattr(self._session(), method)(url, **kwargs)
            except Exception:
                trouble = "Failed to {method} {url}".format(
                    method=method.upper(), url=url
                )
                if quiet:
                    logger.debug(trouble)
                else:
                    logger.exception(trouble)
                return None

            if response.status_code not in THROTTLE_STATUSES:
                return response

            self._enter_cooldown(_retry_after_seconds(response), url)
            try:
                response.close()
            except Exception:
                pass

        exhausted = (
            "Giving up on {url}: still throttled after {count} "
            "attempts".format(url=url, count=self.retries + 1)
        )
        if quiet:
            logger.debug(exhausted)
        else:
            logger.error(exhausted)
        return None

    def _run_download_job(self, job):
        cache_file_key = job["cache_file_key"]
        remote_file = job["remote_file"]
        local_folder = job["local_folder"]
        local_filename = job["local_filename"]
        cache_file_info = job["cache_file_info"]

        part_file = os.path.join(local_folder, local_filename) + ".part"
        resume = None
        held = _resume_point(part_file)
        if held is not None:
            already, validator = held
            opened = self._reopen_for_resume(
                remote_file, part_file, already, validator
            )
            if opened is not None:
                remote_file_r, append, total = opened
                resume = (append, already if append else 0, total)
                if append:
                    logger.info(
                        "Picking {name} up from {size}, where an earlier run "
                        "stopped".format(
                            name=local_filename, size=_human_size(already)
                        )
                    )
            else:
                remote_file_r = None
        else:
            remote_file_r = self._get_with_backoff(remote_file)
        if remote_file_r is None:
            self._note_skip()
            self._record_failure(
                os.path.join(local_folder, local_filename), "no answer from the server"
            )
            return False

        # Check to see if the file still exists
        if remote_file_r.status_code not in (200, 206):
            logger.debug(
                "File unavailable {remote_file} status code {status_code}".format(
                    remote_file=remote_file, status_code=remote_file_r.status_code
                )
            )
            self._note_skip()
            self._record_failure(
                os.path.join(local_folder, local_filename),
                "the server answered http {0}".format(remote_file_r.status_code),
            )
            return False

        logger.debug(
            "Item request: {remote_file_r}, Url: {remote_file}".format(
                remote_file_r=remote_file_r, remote_file=remote_file
            )
        )
        file_info = {}
        if "Last-Modified" in remote_file_r.headers:
            file_info["url_last_modified"] = remote_file_r.headers["Last-Modified"]
            if file_info["url_last_modified"] == cache_file_info.get(
                "url_last_modified"
            ):
                self._note_skip()
                return False
            if cache_file_info.get("adopted") and not cache_file_info.get(
                "url_last_modified"
            ):
                # Found on disk with no date to compare against. Take
                # this one as the baseline instead of calling it a change
                try:
                    remote_file_r.close()
                except Exception:
                    pass
                self._update_cache_data(
                    cache_file_key, dict(cache_file_info, **file_info)
                )
                self._note_skip()
                return False
        if "url_last_modified" in cache_file_info:
            last_modified = self._cache_rename_date(cache_file_info)
        else:
            last_modified = None

        local_file = os.path.join(local_folder, local_filename)
        # The library root going missing mid run usually means a drive
        # was unmounted. makedirs below would cheerfully rebuild the
        # tree inside the empty mountpoint and fill the boot disk
        # instead, so check before writing rather than after
        if not os.path.isdir(self.library_path):
            self._note_skip()
            self._library_vanished()
            return False

        # Create directory to save the file to, which might not exist if there's a subdirectory included
        try:
            os.makedirs(os.path.dirname(local_file), exist_ok=True)  # noqa: E701
        except OSError as error:
            self._folder_unusable(local_file, error)
            self._record_failure(local_file, "its folder could not be made")
            return False

        self._log_url(remote_file)

        return self._process_download(
            remote_file_r,
            cache_file_key,
            file_info,
            local_file,
            rename_str=last_modified,
            remote_file=remote_file,
            hashes=job.get("hashes"),
            listed_size=job.get("listed_size"),
            resume=resume,
        )

    def _process_download(
        self,
        open_r,
        cache_file_key,
        file_info,
        local_filename,
        rename_str=None,
        remote_file=None,
        hashes=None,
        listed_size=None,
        resume=None,
    ):
        started = time.time()
        # Content lands in <file>.part and is only moved into place once
        # it is whole, so an interrupted transfer can be resumed and can
        # never be mistaken for a finished file
        part_file = local_filename + ".part"
        label = os.path.basename(local_filename)
        written = 0
        try:
            if rename_str:
                self._rename_old_file(local_filename, rename_str)

            written, digests = self._download_to_part(
                open_r, part_file, remote_file, local_filename, hashes,
                listed_size=listed_size,
                resume=resume,
            )
            # Recorded so a later run can tell a file that changed on
            # disk from one that changed upstream
            file_info.update(digests)
            os.replace(part_file, local_filename)
            _remove_quietly(_resume_note(part_file))

        except (Exception, KeyboardInterrupt) as e:
            if self._show_bar:
                # Do not overwrite the progress bar on next print
                print()

            stopped = isinstance(e, (Interrupted, KeyboardInterrupt))
            if isinstance(e, Interrupted):
                # Asked to stop, so this is not a failure worth shouting,
                # but how far it got and how fast is worth a line
                self._log_finished(
                    local_filename,
                    _file_size(part_file),
                    time.time() - started,
                    total=_content_range_total(open_r) or _coerce_size(
                        (getattr(open_r, "headers", None) or {}).get("Content-Length")
                    ),
                    stopped=True,
                    resumable=os.path.exists(_resume_note(part_file)),
                )
            else:
                logger.error(
                    "Failed to download file {local_filename}".format(
                        local_filename=local_filename
                    )
                )
                self._record_failure(local_filename, str(e) or type(e).__name__)

            self._progress.file_finished(label, ok=False)
            self._progress.clear()

            # A stopped transfer keeps what it has, for the next run to
            # carry on from. A failed one is thrown away: what it holds
            # is not to be trusted. Any previously downloaded copy of
            # the file is left untouched either way
            if not stopped or not os.path.exists(_resume_note(part_file)):
                _remove_quietly(part_file)
                _remove_quietly(_resume_note(part_file))

            if type(e).__name__ == "KeyboardInterrupt":
                # A worker cannot exit the process, so flag it and let
                # start() shut the run down once the workers are joined
                self._stop.set()
                if threading.current_thread() is threading.main_thread():
                    sys.exit(INTERRUPT_EXIT_CODE)

            result = False

        else:
            if self._show_bar:
                # Do not overwrite the progress bar on next print
                print()
            if "url_last_modified" not in file_info:
                # no Last-Modified header so we set the time of the current download
                # this will result in the file not being re-downloaded by default later
                file_info["url_last_modified"] = datetime.datetime.now().strftime(
                    "%a, %d %b %Y %H:%M:%S %Z"
                )
            self._progress.file_finished(label, ok=True)

            self._update_cache_data(cache_file_key, file_info)
            self._log_finished(local_filename, written, time.time() - started)
            result = True

        finally:
            # Since its a stream connection, make sure to close it
            try:
                open_r.connection.close()
            except Exception:
                pass

        return result

    def _log_finished(self, local_filename, done, elapsed, total=None,
                      stopped=False, resumable=False):
        """One line per file once it is over: how much, how long, and how
        fast on average, whether it finished or was stopped part way.
        The progress block, when there is one, keeps it above itself
        """
        name = os.path.basename(local_filename)
        done = done or 0
        rate = done / elapsed if elapsed > 0 else None
        speed = ", avg {0}/s".format(_human_size(rate)) if rate else ""
        if not stopped:
            line = "Downloaded {name}: {size} in {elapsed:.1f}s{speed}".format(
                name=name, size=_human_size(done), elapsed=elapsed, speed=speed
            )
        elif total:
            line = (
                "Stopped {name} at {pct}% ({done} of {total}) after "
                "{elapsed:.1f}s{speed}".format(
                    name=name,
                    pct=int(100 * min(1.0, done / total)),
                    done=_human_size(done),
                    total=_human_size(total),
                    elapsed=elapsed,
                    speed=speed,
                )
            )
        else:
            line = "Stopped {name} after {done} in {elapsed:.1f}s{speed}".format(
                name=name, done=_human_size(done), elapsed=elapsed, speed=speed
            )
        if stopped and resumable and done:
            line += "; the next run carries on from there"
        logger.info(line)

    def _hash_algorithm(self, hashes):
        """Strongest checksum the api gave us that we can actually use"""
        if not self.verify or not hashes:
            return None
        for name in ("sha1", "md5"):
            if hashes.get(name):
                try:
                    hashlib.new(name)
                except ValueError:
                    continue
                return name
        return None

    def _download_to_part(self, response, part_file, remote_file,
                          local_filename, hashes=None, listed_size=None,
                          resume=None):
        """Stream into the part file, picking the transfer back up where
        it broke off when the server allows it.

        Returns (size of the finished file, checksums computed for it).
        """
        # The block already shows what is in flight, and a line per start
        # on top of a line per finish just scrolls the history away
        (logger.debug if self._show_status else logger.info)(
            "Downloading: {local_filename}".format(local_filename=local_filename)
        )

        if resume is not None:
            # Carrying on from an earlier run's part file
            append, already, total = resume
        else:
            append = False
            already = 0
            total = _coerce_size(response.headers.get("Content-Length"))
        attempt = 0
        label = os.path.basename(local_filename)

        # Taken from the first response, so a resume can ask for the
        # rest of *this* file rather than a slice of whatever is there
        # now. Hashing happens as the bytes go past, costing a pass over
        # data we are already walking
        validator = _validator_for(response)
        algorithm = self._hash_algorithm(hashes)
        hasher = hashlib.new(algorithm) if algorithm else None
        if hasher is not None and append and already:
            # The hash has to cover the bytes an earlier run fetched too
            _feed_hash_from_file(hasher, part_file)

        # Written as the transfer starts, not when it stops: a second
        # Ctrl+C ends the process on the spot, and the part file it
        # leaves is only resumable if this is already there
        _write_resume_note(part_file, validator)

        self._progress.file_started(label, total)
        if already:
            self._progress.file_progress(label, already)

        while True:
            try:
                self._stream_to_file(
                    response, part_file, append, already, total, label, hasher
                )
                try:
                    self._check_digest(algorithm, hasher, hashes, label)
                except ChecksumMismatch:
                    if not self._listing_is_stale(label, part_file, listed_size):
                        raise
                return _file_size(part_file), _digest_of(algorithm, hasher)
            except (KeyboardInterrupt, Interrupted):
                # Shutting down: nothing to resume, nothing to report
                raise
            except Exception as error:
                attempt += 1
                spoiled = isinstance(error, ChecksumMismatch)
                if spoiled:
                    # The bytes on disk are wrong, so there is nothing
                    # worth resuming onto
                    _remove_quietly(part_file)
                    already = 0
                else:
                    already = _file_size(part_file)

                if (
                    remote_file is None
                    or attempt > self.retries
                    or self._stop.is_set()
                ):
                    raise

                logger.warning(
                    "{name} {what} ({error}), {action}".format(
                        name=label,
                        what="is not what was promised" if spoiled
                        else "stopped after " + _human_size(already),
                        error=error,
                        action="downloading it again" if spoiled
                        else "picking it back up",
                    )
                )

                resumed = self._reopen_for_resume(
                    remote_file, part_file, already, validator
                )
                if resumed is None:
                    raise
                response, append, total = resumed
                if append is False:
                    already = 0

                # Whatever the server decided, the hash has to cover
                # exactly what ends up in the file
                if hasher is not None:
                    hasher = hashlib.new(algorithm)
                    if append and already:
                        _feed_hash_from_file(hasher, part_file)
                validator = _validator_for(response) or validator

                # A resume changes what the whole file weighs
                self._progress.file_started(label, total)
                self._progress.file_progress(label, already)

    def _print_checksum(self, local_file, hashes):
        """The checksum Humble lists for a file about to be downloaded, as
        shasum -c reads it, to stdout like --print-urls. Paths are
        relative to the library, so the list checks from inside it
        """
        if not self.print_checksums:
            return
        path = os.path.relpath(local_file, self.library_path)
        sha1 = (hashes or {}).get("sha1")
        with self._notice_lock:
            header = not self._checksum_header_done
            self._checksum_header_done = True
        with self._progress.printing():
            if header:
                print(
                    "# sha1 as Humble lists them. To check, from inside the "
                    "library: shasum -c <this list>. A file Humble has "
                    "replaced since will not match its listing"
                )
            if sha1:
                print("{sha1}  {path}".format(sha1=sha1, path=path))
            else:
                print("# no sha1 listed: {path}".format(path=path))

    def _record_failure(self, local_file, reason):
        """A file this run gave up on. A run being stopped is not a
        failure of the files it did not get to"""
        if self._stop.is_set():
            return
        with self._notice_lock:
            self.failed_files.append((local_file, reason))

    def _listing_is_stale(self, label, part_file, listed_size):
        """Whether a checksum that does not match is Humble's listing
        being out of date rather than the download being damaged.

        Humble replaces files (a new edition, a fixed scan) without always
        updating the size and checksums listed for them. The file arrives
        whole, at its new size, and is checked against the old one's
        checksum. A file at the listed size that does not match is
        damaged; one at a different size is a different file
        """
        arrived = _file_size(part_file)
        if not listed_size or arrived == listed_size:
            return False
        with self._notice_lock:
            self.stale_listings += 1
        logger.warning(
            "{name} is not the version Humble lists ({listed}, the server "
            "sent {arrived}): keeping what the server sent, with its own "
            "checksum".format(
                name=label,
                listed=_human_size(listed_size),
                arrived=_human_size(arrived),
            )
        )
        return True

    @staticmethod
    def _check_digest(algorithm, hasher, hashes, label):
        if hasher is None:
            return
        expected = (hashes or {}).get(algorithm, "").lower()
        actual = hasher.hexdigest().lower()
        if expected and actual != expected:
            raise ChecksumMismatch(
                "{algorithm} of {label} is {actual}, expected "
                "{expected}".format(
                    algorithm=algorithm, label=label,
                    actual=actual, expected=expected,
                )
            )

    def _reopen_for_resume(self, remote_file, part_file, already,
                           validator=None):
        """Ask for the rest of the file.

        The server decides whether that is possible, and we believe the
        status code rather than assuming support: 206 means the Range was
        honoured and we append, 200 means it was ignored, or the file
        changed since If-Range was taken, and it starts again from
        nothing.

        Returns (response, append, total), or None if it cannot go on.
        """
        response = self._get_with_backoff(
            remote_file, range_start=already or None, if_range=validator
        )
        if response is None:
            return None

        status = getattr(response, "status_code", 200)

        if status == 206:
            total = _content_range_total(response)
            if total is None:
                length = _coerce_size(response.headers.get("Content-Length"))
                total = None if length is None else already + length
            return response, True, total

        if status == 416:
            # We hold at least as much as is on offer, so the part file
            # is stale. Throw it away and take the file from the top
            _remove_quietly(part_file)
            response = self._get_with_backoff(remote_file)
            if response is None:
                return None
            if getattr(response, "status_code", 200) != 200:
                return None
            return (
                response,
                False,
                _coerce_size(response.headers.get("Content-Length")),
            )

        if status == 200:
            if not already:
                # We asked for the whole file on purpose, so there is
                # nothing surprising to report
                pass
            elif validator:
                # We asked for the rest only if the file was unchanged,
                # and got the whole thing, so it changed underneath us
                logger.info(
                    "{name} changed on the server, taking it from the "
                    "top".format(
                        name=os.path.basename(part_file).replace(".part", "")
                    )
                )
            else:
                self._note_range_unsupported()
            return (
                response,
                False,
                _coerce_size(response.headers.get("Content-Length")),
            )

        return None

    def _note_range_unsupported(self):
        with self._notice_lock:
            if self._range_unsupported:
                return
            self._range_unsupported = True

        logger.info(
            "The server ignored a Range request, so interrupted files "
            "start over instead of resuming"
        )

    def _stream_to_file(
        self, response, part_file, append, already, total, label=None,
        hasher=None,
    ):
        """One pass of the transfer. `already` is what the part file
        holds when appending, so the progress bar counts the whole file
        """
        written = already
        with open(part_file, "ab" if append else "wb") as outfile:
            for data in response.iter_content(chunk_size=4096):
                if self._stop.is_set():
                    # Checked between chunks so a Ctrl-C stops a large
                    # transfer promptly instead of at the end of it
                    raise Interrupted()
                written += len(data)
                outfile.write(data)
                self._count_bytes(len(data))
                if hasher is not None:
                    hasher.update(data)
                if self._show_bar:
                    self._draw_bar(written, total)
                elif label is not None:
                    self._progress.file_progress(label, written)

        if total is not None and written < total:
            raise ValueError(
                "Download did not complete, {written} of {total} bytes".format(
                    written=written, total=total
                )
            )
        if total is not None and written > total:
            if self._show_bar:
                print()
            logger.warning("Downloaded more content than expected")

        return written

    def _draw_bar(self, written, total):
        if total is None:  # no content length header
            print("\t{written}".format(written=written), end="\r")
            return

        pb_width = 50
        done = int(pb_width * written / total)
        print(
            "\t{percent}% [{filler}{space}]".format(
                percent=int(done * (100 / pb_width)),
                filler="=" * min(max(done, 0), pb_width),
                space=" " * min(max((pb_width - done), 0), pb_width),
            ),
            end="\r",
        )

    def _load_cache_data(self, cache_file):
        self._clear_stale_cache_temps(cache_file)

        try:
            with open(cache_file, "r") as f:
                cache_data = json.load(f)
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as error:
            # A cache we cannot read is not worth dying over: the run
            # can rebuild it. Keep the old one in case it is wanted
            self._set_aside_bad_cache(cache_file, error)
            return {}

        if not isinstance(cache_data, dict):
            self._set_aside_bad_cache(
                cache_file, "it does not contain an object"
            )
            return {}

        return self._usable_cache_entries(cache_data)

    @staticmethod
    def _usable_cache_entries(cache_data):
        """Keep the entries that can be used, drop the ones that cannot.

        Everything downstream treats an entry as a mapping: a stray
        string or list would raise AttributeError on the traversal
        thread and take the run down, and because any non empty value
        counts as "already downloaded" it would also quietly skip that
        file forever.

        Dropping the whole cache over one bad entry means downloading
        the library again, so only the bad entries go.
        """
        usable = {}
        dropped = 0
        for key, value in cache_data.items():
            if isinstance(key, str) and isinstance(value, dict):
                usable[key] = value
            else:
                dropped += 1

        if dropped:
            logger.warning(
                "Ignoring {dropped} unreadable entr{plural} in the cache; "
                "those files will be downloaded again".format(
                    dropped=dropped, plural="y" if dropped == 1 else "ies"
                )
            )
        return usable

    @staticmethod
    def _cache_rename_date(cache_file_info):
        """The date to tag an older copy of a file with.

        A cache written by another version, or edited by hand, can hold
        anything at all here. Not knowing the date is only worth losing
        the rename over, not the download
        """
        stamp = cache_file_info.get("url_last_modified")
        if not isinstance(stamp, str):
            return None
        try:
            return datetime.datetime.strptime(
                stamp, "%a, %d %b %Y %H:%M:%S %Z"
            ).strftime("%Y-%m-%d")
        except ValueError:
            logger.debug(
                "Cache holds an unparseable date: {stamp!r}".format(stamp=stamp)
            )
            return None

    def _clear_stale_cache_temps(self, cache_file):
        """Tidy up half written caches left by a run that was killed.

        os.replace means they never became the cache, but a hard kill
        cannot unlink them either, so they would otherwise pile up
        """
        folder = os.path.dirname(cache_file) or "."
        prefix = os.path.basename(cache_file) + "."
        try:
            leftovers = os.listdir(folder)
        except OSError:
            return

        for name in leftovers:
            if name.startswith(prefix) and name.endswith(".tmp"):
                _remove_quietly(os.path.join(folder, name))

    def _set_aside_bad_cache(self, cache_file, reason):
        spoiled = "{cache_file}.unreadable-{stamp}".format(
            cache_file=cache_file,
            stamp=datetime.datetime.now().strftime("%Y%m%d-%H%M%S"),
        )
        try:
            os.replace(cache_file, spoiled)
            kept = " Kept as {spoiled}.".format(
                spoiled=os.path.basename(spoiled)
            )
        except OSError:
            kept = ""

        logger.warning(
            "Could not read {name}: {reason}.{kept} Starting a new one, so "
            "files already downloaded will be fetched again".format(
                name=os.path.basename(cache_file), reason=reason, kept=kept
            )
        )

    def _should_download_platform(self, platform):
        platform = platform.lower()
        if self.platform_include and platform not in self.platform_include:
            return False
        return True

    def _log_skip(self, message):
        """Say what a filter passed over.

        Useful when working out why something did not arrive, and a
        wall of noise on a run that filters most of a library out, so
        it drops to debug rather than disappearing when turned off
        """
        if self.log_skipped:
            logger.info(message)
        else:
            logger.debug(message)

    def _should_download_file_by_ext_and_log(self, filename):
        if self._should_download_file_by_ext(filename) is False:
            self._log_skip(
                "Skipping the file {filename}".format(filename=filename)
            )
            return False
        return True

    def _should_download_file_by_ext(self, filename):
        return self._should_download_ext(_file_ext(filename))

    def _should_download_ext(self, ext):
        ext = ext.lower()
        if self.ext_include != []:
            return ext in self.ext_include
        elif self.ext_exclude != []:
            return ext not in self.ext_exclude
        return True
