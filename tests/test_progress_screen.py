"""What the progress block actually leaves on screen.

The reporter talks to the terminal in cursor movements, so checking the
text it writes says little. This replays that text through a minimal
terminal and checks the screen a person would be looking at.
"""
import io
import re
import logging
import threading

from humblebundle_downloader.download_library import ProgressReporter

CSI = re.compile(r"\x1b\[(\d*)([AK])")


class Screen:
    """Enough of a VT100 for \\r, \\n, ESC[nA and ESC[K"""

    def __init__(self):
        self.rows = [""]
        self.row = 0
        self.col = 0

    def feed(self, text):
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\x1b":
                match = CSI.match(text, i)
                count, op = int(match.group(1) or 1), match.group(2)
                if op == "A":
                    self.row = max(0, self.row - count)
                else:
                    self.rows[self.row] = self.rows[self.row][:self.col]
                i = match.end()
                continue
            if ch == "\r":
                self.col = 0
            elif ch == "\n":
                self.row += 1
                if self.row == len(self.rows):
                    self.rows.append("")
            else:
                line = self.rows[self.row].ljust(self.col)
                self.rows[self.row] = line[:self.col] + ch + line[self.col + 1:]
                self.col += 1
            i += 1

    def lines(self):
        return [r.rstrip() for r in self.rows if r.strip()]


class TTY(io.StringIO):
    """Records what was written and replays it onto a Screen"""

    def __init__(self):
        io.StringIO.__init__(self)
        self.screen = Screen()
        self.lock = threading.Lock()

    def isatty(self):
        return True

    def write(self, text):
        with self.lock:
            self.screen.feed(text)
        return len(text)

    def flush(self):
        pass


def reporter_with_log(slots=3):
    tty = TTY()
    reporter = ProgressReporter(stream=tty, slots=slots, style="bars")
    handler = logging.StreamHandler(tty)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("test_progress_screen")
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)
    return tty, reporter, log, handler


def names_on(lines, names):
    return [n for line in lines for n in names if line.startswith(n)]


def test_log_lines_between_repaints_do_not_leave_stale_rows(monkeypatch):
    tty, reporter, log, handler = reporter_with_log()
    monkeypatch.setattr(logging.getLogger(), "handlers", [handler])
    reporter.attach_logging()
    try:
        names = ["a.pdf", "b.cbz", "c.epub", "d.zip"]
        for step, name in enumerate(names):
            reporter.file_started(name, 1000)
            reporter.paint()
            log.info("Downloading: %s", name)
            reporter.file_progress(name, 500)
            reporter.paint()
            if step:
                done = names[step - 1]
                reporter.file_finished(done)
                log.info("Downloaded %s", done)
                reporter.paint()
    finally:
        reporter.detach_logging()

    lines = tty.screen.lines()
    rows = [l for l in lines if not l.startswith(("Downloading", "Downloaded"))]
    on_screen = names_on(rows, names)
    # Every file in at most one row, and only those still in flight
    assert sorted(on_screen) == sorted(set(on_screen)), lines
    assert set(on_screen) == {"d.zip"}, lines
    # The log lines survive, above the block rather than painted over
    for name in ("a.pdf", "b.cbz", "c.epub"):
        assert "Downloaded " + name in lines, lines
    assert lines.index("Downloaded b.cbz") < lines.index(rows[0]), lines
    assert rows[-1].startswith("Total"), lines


def test_logging_from_workers_while_painting_keeps_one_block(monkeypatch):
    import sys

    tty, reporter, log, handler = reporter_with_log(slots=4)
    monkeypatch.setattr(logging.getLogger(), "handlers", [handler])
    # Switch threads as often as possible and paint flat out, so that a
    # repaint and a log line meeting halfway is near certain rather than
    # a matter of luck. Without the terminal lock this fails every time
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    reporter.interval = 0
    reporter.start()
    try:
        def worker(n):
            for i in range(60):
                name = "w{0}-{1}.pdf".format(n, i)
                reporter.file_started(name, 100)
                log.info("Downloading: %s", name)
                reporter.file_progress(name, 50)
                reporter.file_finished(name)
                log.info("Downloaded %s", name)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        reporter.stop()
        sys.setswitchinterval(previous)

    lines = tty.screen.lines()
    # Every log line made it, whole, and nothing of the block is left
    for n in range(4):
        for i in range(60):
            assert "Downloaded w{0}-{1}.pdf".format(n, i) in lines
    assert not [l for l in lines if l.startswith(("w", "Total"))], lines[-10:]


def test_logging_is_left_as_it_was_after_stop(monkeypatch):
    tty, reporter, log, handler = reporter_with_log()
    monkeypatch.setattr(logging.getLogger(), "handlers", [handler])
    original = handler.emit
    reporter.start()
    assert handler.emit != original
    reporter.stop()
    assert handler.emit == original


def test_handlers_writing_elsewhere_are_left_alone(monkeypatch):
    tty, reporter, log, handler = reporter_with_log()
    to_file = logging.StreamHandler(io.StringIO())
    monkeypatch.setattr(logging.getLogger(), "handlers", [to_file, handler])
    original = to_file.emit
    reporter.attach_logging()
    assert to_file.emit == original
    reporter.detach_logging()
