"""hbd's interface: the run, live, in the terminal.

The engine (DownloadLibrary) runs on a thread of its own and reports to
a TuiListener, which only keeps counters. The app reads them a few
times a second and redraws: a status line, one row per download in the
chosen progress style, the totals, and the log underneath.

Ctrl-C (or q) asks the engine to stop the way a first Ctrl-C always
has; a second press quits without waiting. When the run is over the
app closes, and what is worth keeping in the scrollback (warnings,
errors and the summary) is printed again in the ordinary terminal,
along with anything meant for stdout like --print-urls.
"""
import collections
import logging
import sys
import threading

from rich.console import Group
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Footer, OptionList, RichLog, Static

from .formatting import _fit, _format_clock, _human_size
from .progress_styles import DEFAULT_PROGRESS_STYLE
from .rich_bars import MuncherBar, PlainBar
from .logs import RedactingFilter
from .run_state import RunState

# Redraws a second. Enough for the animations to move, little enough
# to cost nothing next to the downloads
FPS = 5

LEVEL_STYLES = {
    logging.WARNING: "yellow",
    logging.ERROR: "bold red",
    logging.CRITICAL: "bold red",
}


def can_run(stdin=None, stdout=None, stderr=None, environ=None):
    """Whether there is a terminal to draw on: a full screen interface
    is no use to cron, a pipe or a log file, which get plain lines"""
    import os

    environ = os.environ if environ is None else environ
    if environ.get("TERM", "") in ("", "dumb"):
        return False
    for stream in (stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr):
        try:
            if not stream.isatty():
                return False
        except Exception:
            return False
    return True


class TuiListener(RunState):
    """The run's state, plus what has to wait for the screen to be
    given back: lines for stdout, and the log lines worth repeating"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._held = []
        self._held_lock = threading.Lock()
        self.summary_started = False

    def output(self, line):
        with self._held_lock:
            self._held.append(line)

    def held_output(self):
        with self._held_lock:
            return list(self._held)

    def run_ending(self):
        super().run_ending()
        self.summary_started = True


class LogCollector(logging.Handler):
    """Takes the log while the interface owns the terminal.

    New lines wait in `pending` for the app to show. Warnings, errors
    and everything in the summary are also kept, to print again once
    the terminal is back
    """

    def __init__(self, listener, level=logging.NOTSET):
        super().__init__(level)
        self.listener = listener
        self.pending = collections.deque()
        self.kept = []
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self.pending.append((record.levelno, line))
        if (record.levelno >= logging.WARNING or self.listener.summary_started
                or self.listener.stopping_asked):
            self.kept.append((record.levelno, line))

    def drain(self):
        while True:
            try:
                yield self.pending.popleft()
            except IndexError:
                return


# Columns right of a download's name. Minimums: a value wider than its
# column takes room from the name, never from the edge of the screen
SIZE_WIDTH = 10
RATE_WIDTH = 12
ETA_WIDTH = 6
PCT_WIDTH = 4
MIN_NAME = 12
MIN_BAR = 8
MAX_BAR = 40


def bar_width(width):
    """The bar gets a quarter of the row, within reason"""
    return max(MIN_BAR, min(MAX_BAR, width // 4))


def row(label, size, rate, eta, bar, pct, width, label_style="", keep_start=False):
    """One row: the name gets whatever the rest leaves it. On a narrow
    screen the time left goes first, then the speed, then the size,
    so the bar and the percentage are always there"""
    columns = [
        (size.rjust(SIZE_WIDTH), "green"),
        (rate.rjust(RATE_WIDTH), "cyan"),
        (eta.rjust(ETA_WIDTH), "magenta"),
    ]
    while True:
        tail = Text(no_wrap=True)
        for text, style in columns:
            tail.append(" ")
            tail.append(text, style)
        tail.append(" ")
        tail.append_text(bar)
        tail.append(" ")
        tail.append(pct.rjust(PCT_WIDTH), "bold")
        if width - tail.cell_len >= MIN_NAME or not columns:
            break
        columns.pop()
    name_width = max(1, width - tail.cell_len)
    if keep_start:
        # A label that reads from the left, like the totals: its end
        # is what gives way
        name = label if len(label) <= name_width else label[:name_width - 1] + "…"
        name = name.ljust(name_width)
    else:
        # A file name, whose end is what tells it from its neighbours
        name = _fit(label, name_width)
    line = Text(name, style=label_style, no_wrap=True)
    line.append_text(tail)
    line.truncate(width)
    return line


def transfer_row(transfer, width, style, frame):
    rate = transfer.rate
    fraction = transfer.fraction
    return row(
        transfer.name,
        _human_size(transfer.total if transfer.total else transfer.done),
        (_human_size(rate) + "/s") if rate else "--",
        _format_clock(transfer.eta) if transfer.eta is not None else "--:--",
        MuncherBar(fraction, style, frame).text(bar_width(width)),
        "{0}%".format(int(fraction * 100)) if fraction is not None else "--",
        width,
    )


def idle_row(width):
    return row("", "", "", "", Text(" " * bar_width(width)), "", width)


def totals_row(snap, width):
    label = "Total {done}/{expected}".format(
        done=snap.done, expected=snap.expected if snap.expected else "?"
    )
    if snap.failed:
        label += " ({0} failed)".format(snap.failed)
    if snap.skipped:
        label += " ({0} skipped)".format(snap.skipped)
    if not snap.walk_done:
        label += " so far"
    fraction = snap.fraction
    return row(
        label,
        _human_size(snap.seen),
        (_human_size(snap.rate) + "/s") if snap.rate else "--",
        _format_clock(snap.elapsed),
        PlainBar(fraction).text(bar_width(width)) if fraction is not None
        else Text(" " * bar_width(width)),
        "{0}%".format(int(fraction * 100)) if fraction is not None else "--",
        width,
        label_style="bold",
        keep_start=True,
    )


def status_line(snap, width):
    parts = [Text(snap.phase, style="bold yellow" if snap.stopping else "bold")]
    if snap.bundles:
        parts.append(Text("{0} bundles checked".format(snap.bundles)))
    if snap.jobs:
        parts.append(Text("{0} at once".format(snap.jobs)))
    if snap.bundle:
        parts.append(Text(snap.bundle, style="italic"))
    line = Text(" · ", style="bright_black").join(parts)
    line.no_wrap = True
    line.truncate(width, overflow="ellipsis")
    return line


def transfers_block(snap, width, style, frame):
    """The downloads and the totals, as one renderable"""
    lines = []
    for transfer in snap.slots:
        if transfer is None:
            lines.append(idle_row(width))
        else:
            lines.append(transfer_row(transfer, width, style, frame))
    lines.append(totals_row(snap, width))
    return Group(*lines)


class Transfers(Widget):
    """The downloads in flight and the totals"""

    DEFAULT_CSS = """
    Transfers { height: auto; padding: 0 1; }
    """

    def __init__(self, style, **kwargs):
        super().__init__(**kwargs)
        self.progress_style = style
        self.snap = None
        self.frame = 0

    def show(self, snap, frame):
        self.snap = snap
        self.frame = frame
        self.refresh(layout=True)

    def render(self):
        if self.snap is None:
            return Text("")
        return transfers_block(
            self.snap, max(20, self.content_size.width), self.progress_style,
            self.frame,
        )


class SkipPicker(ModalScreen):
    """Which download to skip. Enter picks, Escape changes nothing"""

    DEFAULT_CSS = """
    SkipPicker { align: center middle; }
    SkipPicker > Vertical {
        width: 80%; max-width: 100; height: auto; max-height: 80%;
        border: round $warning; background: $surface; padding: 0 1;
    }
    SkipPicker OptionList { height: auto; max-height: 20; border: none; }
    """
    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, names):
        super().__init__()
        self.names = names

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                "Skip which download? It stops for this run, keeps what it "
                "has, and the next run carries on from there"
            )
            yield OptionList(*self.names)

    def on_option_list_option_selected(self, event):
        self.dismiss(self.names[event.option_index])

    def action_dismiss_none(self):
        self.dismiss(None)


class HbdApp(App):
    """The interface for one run of a DownloadLibrary"""

    TITLE = "hbd"
    # Nothing in it for a download run
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #status { height: 1; padding: 0 1; background: $boost; }
    #log { border-top: hkey $primary-darken-2; padding: 0 1; scrollbar-size-vertical: 1; }
    """
    BINDINGS = [
        Binding("ctrl+c", "stop", "Stop", priority=True),
        Binding("q", "stop", "Stop", show=False),
        Binding("p", "pause", "Pause/resume"),
        Binding("s", "skip", "Skip a download"),
        Binding("plus,equals_sign", "more", "More at once", key_display="+"),
        Binding("minus", "fewer", "Fewer", key_display="-"),
        Binding("end", "follow", "Follow log"),
    ]

    def __init__(self, library, listener, collector, style=DEFAULT_PROGRESS_STYLE,
                 fps=FPS):
        super().__init__()
        self.library = library
        self.listener = listener
        self.collector = collector
        self.progress_style = style
        self.fps = fps
        self.frame = 0
        # What library.start() ended with: None, or what it raised
        self.outcome = None
        self.finished = threading.Event()
        self.forced = False
        self._stop_asked = False
        self._run_over = False

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield Transfers(self.progress_style, id="transfers")
        yield RichLog(id="log", max_lines=5000, wrap=True, markup=False, highlight=False)
        yield Footer()

    def on_mount(self):
        self.set_interval(1 / self.fps, self.tick)
        self.tick()
        # A plain thread rather than a Textual worker: if a transfer is
        # wedged past saving, a second Ctrl-C must still be able to
        # leave without waiting for it
        threading.Thread(target=self._run, name="hbd-engine", daemon=True).start()

    def _run(self):
        try:
            self.library.start()
        except BaseException as error:  # noqa: B902, handed to the cli
            self.outcome = error
        finally:
            # The next tick sees it and closes the app. Told from here
            # instead, a run that ends as the app closes would be
            # telling an app that is no longer there
            self.finished.set()

    def tick(self):
        self.frame += 1
        snap = self.listener.snapshot()
        width = max(20, self.size.width - 2)
        self.query_one("#status", Static).update(status_line(snap, width))
        self.query_one("#transfers", Transfers).show(snap, self.frame)
        log = self.query_one("#log", RichLog)
        for level, line in self.collector.drain():
            log.write(Text(line, style=LEVEL_STYLES.get(level, "")))
        if self.finished.is_set() and not self._run_over:
            self._run_over = True
            self.exit()

    def action_stop(self):
        if self._stop_asked:
            # Again: do not wait for anything
            self.forced = True
            self.exit()
            return
        self._stop_asked = True
        self.notify(
            "Stopping once the downloads in flight have saved where they "
            "got to. Press Ctrl+C again to quit now",
            title="Stopping", timeout=8,
        )
        # Off the event loop: it logs, and logging is quick, but nothing
        # the engine does should ever hold up the interface
        threading.Thread(target=self.library.request_stop, daemon=True).start()

    def action_pause(self):
        library = self.library
        if library.is_paused:
            library.resume()
            self.notify("Resumed", timeout=3)
        else:
            library.pause()
            self.notify("Paused. Press p to carry on", timeout=5)
        self.tick()

    def action_more(self):
        self._change_jobs(+1)

    def action_fewer(self):
        self._change_jobs(-1)

    def _change_jobs(self, step):
        now = self.library.set_jobs(self.library.jobs_now + step)
        if now is None:
            self.notify(
                "This run downloads one file at a time (-j 1, --no-parallel or "
                "a dry run), so there is nothing to change",
                severity="warning", timeout=5,
            )
        else:
            self.notify("{0} at once".format(now), timeout=2)
        self.tick()

    def action_skip(self):
        snap = self.listener.snapshot()
        names = [t.name for t in snap.slots if t is not None]
        if not names:
            self.notify("Nothing is downloading", timeout=3)
            return

        def chosen(name):
            if name:
                self.library.skip(name)
                self.notify("Skipping " + name, timeout=4)

        self.push_screen(SkipPicker(names), chosen)

    def action_follow(self):
        log = self.query_one("#log", RichLog)
        log.auto_scroll = True
        log.scroll_end(animate=False)


def run(library, listener, style=DEFAULT_PROGRESS_STYLE, headless=False):
    """Run `library` under the interface, then hand the terminal back
    with the lines worth keeping printed in it. Raises whatever the run
    raised, so the cli treats it exactly as it would a plain run
    """
    root = logging.getLogger()
    collector = LogCollector(listener)
    collector.addFilter(RedactingFilter())
    # The terminal belongs to the interface while it runs: anything
    # that would have gone to it goes into the log pane instead
    displaced = [h for h in root.handlers if _to_terminal(h)]
    for handler in displaced:
        root.removeHandler(handler)
    root.addHandler(collector)

    app = HbdApp(library, listener, collector, style=style)
    try:
        app.run(headless=headless)
    finally:
        root.removeHandler(collector)
        for handler in displaced:
            root.addHandler(handler)

    _replay(collector, listener)

    if app.forced and not app.finished.is_set():
        sys.stderr.write("Quit without waiting for downloads in flight\n")
        sys.stderr.flush()
        library.force_stop()
    if app.outcome is not None:
        raise app.outcome


def _to_terminal(handler):
    stream = getattr(handler, "stream", None)
    try:
        return bool(stream is not None and stream.isatty())
    except Exception:
        return False


def _replay(collector, listener):
    for line in listener.held_output():
        print(line, file=sys.stdout)
    sys.stdout.flush()
    for _, line in collector.kept:
        print(line, file=sys.stderr)
    sys.stderr.flush()
