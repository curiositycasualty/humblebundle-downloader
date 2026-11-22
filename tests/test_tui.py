"""The interface: the run state it reads, what it draws, and the app
driven headless through Textual's own test harness"""
import asyncio
import logging
import threading
import time

import pytest
from rich.console import Console

from humblebundle_downloader import tui
from humblebundle_downloader.errors import RunStopped
from humblebundle_downloader.run_state import RunState


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def plain(renderable, width=100):
    console = Console(width=width, record=True, file=open("/dev/null", "w"))
    console.print(renderable)
    return console.export_text().rstrip("\n").split("\n")


###
# RunState
###
def test_slots_fill_and_empty_in_place():
    state = RunState()
    state.downloads_started(3)
    state.file_started("a.cbz", 100)
    state.file_started("b.cbz", 200)
    state.file_finished("a.cbz")
    state.file_started("c.cbz", None)
    snap = state.snapshot()
    assert [t.name if t else None for t in snap.slots] == ["c.cbz", "b.cbz", None]
    assert snap.done == 1 and snap.failed == 0


def test_rates_and_eta_come_from_recent_progress():
    clock = Clock()
    state = RunState(clock=clock)
    state.downloads_started(1)
    state.file_started("a.cbz", 1000)
    state.snapshot()
    clock.now += 1
    state.file_progress("a.cbz", 250)
    snap = state.snapshot()
    transfer = snap.slots[0]
    assert transfer.rate == 250 and transfer.fraction == 0.25
    assert transfer.eta == 3.0
    assert snap.rate == 250 and snap.seen == 250


def test_totals_only_have_a_fraction_once_the_walk_is_done():
    state = RunState()
    for _ in range(4):
        state.file_queued()
    state.file_skipped()
    state.file_started("a", 1)
    state.file_finished("a")
    assert state.snapshot().fraction is None
    state.traversal_finished()
    snap = state.snapshot()
    assert snap.expected == 3 and snap.fraction == pytest.approx(1 / 3)


def test_phases_follow_the_run():
    state = RunState()
    assert state.snapshot().phase == "Reading your library"
    state.bundle_started("Bundle One")
    snap = state.snapshot()
    assert snap.phase == "Checking bundles" and snap.bundle == "Bundle One" and snap.bundles == 1
    state.traversal_finished()
    assert state.snapshot().phase == "Downloading"
    state.stopping()
    state.bundle_started("Later")
    snap = state.snapshot()
    assert snap.phase == "Stopping" and snap.stopping


def test_auto_jobs_going_down_drops_idle_slots_from_the_end():
    state = RunState()
    state.downloads_started(6)
    state.file_started("a", 1)
    state.jobs_changed(4)
    snap = state.snapshot()
    assert len(snap.slots) == 4 and snap.jobs == 4


def test_a_resume_resets_the_size_and_rate():
    state = RunState()
    state.file_started("a", 100)
    state.file_progress("a", 40)
    state.file_started("a", 300)
    assert state.snapshot().slots[0].total == 300


def test_run_state_is_safe_from_many_threads():
    state = RunState()
    state.downloads_started(8)

    def work(n):
        for i in range(200):
            state.file_started("f%d" % n, 1000)
            state.file_progress("f%d" % n, i)
            state.file_finished("f%d" % n)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for _ in range(50):
        state.snapshot()
    for thread in threads:
        thread.join()
    assert state.snapshot().done == 1600


###
# Drawing
###
def busy_state(slots=2, fraction=0.5):
    clock = Clock()
    state = RunState(clock=clock)
    state.downloads_started(slots)
    for n in range(slots):
        state.file_queued()
        state.file_started("file_%d.cbz" % n, 1000)
    state.snapshot()
    clock.now += 1
    for n in range(slots):
        state.file_progress("file_%d.cbz" % n, int(1000 * fraction))
    return state


def test_rows_fit_the_width_exactly():
    snap = busy_state().snapshot()
    for width in (40, 80, 160):
        lines = plain(tui.transfers_block(snap, width, "fish", 0), width)
        assert len(lines) == 3
        assert all(len(line.rstrip()) <= width for line in lines)
        assert "file_0.cbz" in lines[0] and lines[0].rstrip().endswith("50%")
        assert lines[-1].startswith("Total 0/2" if width < 60 else "Total 0/2 so far")
    # Narrow: the time left and the speed give way first
    narrow = plain(tui.transfers_block(snap, 40, "fish", 0), 40)[0]
    assert "B/s" not in narrow and "1000 B" in narrow
    wide = plain(tui.transfers_block(snap, 100, "fish", 0), 100)[0]
    assert "500 B/s" in wide and "00:01" in wide


def test_a_long_name_keeps_its_end():
    state = RunState()
    state.file_started("a_very_long_name_indeed_that_will_not_fit_vol12.pdf", 10)
    line = plain(tui.transfers_block(state.snapshot(), 60, "fish", 0), 60)[0]
    assert line.startswith("…") and "vol12.pdf" in line


def test_the_status_line_says_what_is_happening():
    state = RunState()
    state.downloads_started(4)
    state.bundle_started("Humble Comic Bundle")
    line = tui.status_line(state.snapshot(), 100).plain
    assert line == "Checking bundles · 1 bundles checked · 4 at once · Humble Comic Bundle"


###
# The app, headless
###
class FakeLibrary:
    """Plays a short run to the listener, as the engine would"""

    def __init__(self, listener, hold=None, fail=None):
        self.events = listener
        self.hold = hold or threading.Event()
        self.fail = fail
        self.stop_asked = threading.Event()
        self.forced = False

    def start(self):
        log = logging.getLogger("hbd-test")
        events = self.events
        events.downloads_started(2)
        events.bundle_started("Bundle One")
        events.file_queued()
        events.file_started("one.pdf", 100)
        log.info("Checking bundle: Bundle One")
        events.file_progress("one.pdf", 50)
        events.output("https://cdn.example/one.pdf")
        while not self.hold.wait(0.02):
            if self.stop_asked.is_set():
                log.warning("Stopped, nothing further was downloaded")
                raise RunStopped()
        events.file_finished("one.pdf")
        events.traversal_finished()
        events.downloads_finished()
        log.warning("1 file(s) were a different version")
        events.run_ending()
        log.info("Took 2s")
        if self.fail:
            raise self.fail

    def request_stop(self):
        self.events.stopping()
        self.stop_asked.set()

    def force_stop(self):
        self.forced = True


@pytest.fixture(autouse=True)
def info_logging():
    # The cli logs at INFO; pytest leaves the root logger at WARNING
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.INFO)
    yield
    root.setLevel(level)


def make_app(library, listener, style="fish"):
    collector = tui.LogCollector(listener)
    logging.getLogger().addHandler(collector)
    app = tui.HbdApp(library, listener, collector, style=style, fps=50)
    return app, collector


def wait_for(condition, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_the_app_shows_the_run_and_closes_when_it_is_over():
    listener = tui.TuiListener()
    library = FakeLibrary(listener)
    app, collector = make_app(library, listener)

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.2)
            status = app.query_one("#status").render().plain
            assert status.startswith("Checking bundles")
            block = plain(app.query_one("#transfers").render(), 98)
            assert "one.pdf" in block[0] and "50%" in block[0]
            log = "\n".join(line.text for line in app.query_one("#log").lines)
            assert "Checking bundle: Bundle One" in log
            library.hold.set()
            await pilot.pause(0.3)
        return app.return_code

    try:
        asyncio.run(drive())
    finally:
        logging.getLogger().removeHandler(collector)
    assert app.finished.is_set() and app.outcome is None
    assert listener.held_output() == ["https://cdn.example/one.pdf"]
    kept = [line for _, line in collector.kept]
    assert kept == ["1 file(s) were a different version", "Took 2s"]


def test_ctrl_c_asks_the_engine_to_stop_and_a_second_quits_now():
    listener = tui.TuiListener()
    library = FakeLibrary(listener)
    app, collector = make_app(library, listener)
    # Slow the stop down, so the second press lands while it waits
    library.request_stop = lambda: (library.events.stopping(), time.sleep(0.5))

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert app.query_one("#status").render().plain.startswith("Stopping")
            assert not app.forced
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)

    try:
        asyncio.run(drive())
    finally:
        logging.getLogger().removeHandler(collector)
        library.hold.set()
    assert app.forced


def test_a_stop_ends_the_run_with_the_stopped_status():
    listener = tui.TuiListener()
    library = FakeLibrary(listener)
    app, collector = make_app(library, listener)

    async def drive():
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.1)
            await pilot.press("q")
            await pilot.pause(0.5)

    try:
        asyncio.run(drive())
    finally:
        logging.getLogger().removeHandler(collector)
    assert isinstance(app.outcome, RunStopped) and not app.forced
    assert [line for _, line in collector.kept] == ["Stopped, nothing further was downloaded"]


def test_run_hands_the_terminal_back_and_raises_what_the_run_raised(capsys):
    from humblebundle_downloader.errors import LibraryPathError

    listener = tui.TuiListener()
    library = FakeLibrary(listener, hold=threading.Event(), fail=LibraryPathError("gone"))
    library.hold.set()
    root = logging.getLogger()
    before = list(root.handlers)
    with pytest.raises(LibraryPathError):
        tui.run(library, listener, headless=True)
    assert root.handlers == before
    out = capsys.readouterr()
    assert out.out == "https://cdn.example/one.pdf\n"
    assert out.err == "1 file(s) were a different version\nTook 2s\n"


def test_a_whole_run_downloads_under_the_interface(tmp_path):
    from .test_site_downloads import FakeHumble, files_under, library, two_bundles

    listener = tui.TuiListener()
    dl = library(tmp_path, FakeHumble(*two_bundles()), jobs=2, listener=listener)
    app, collector = make_app(dl, listener, style="train")

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            assert await asyncio.to_thread(wait_for, app.finished.is_set)
            await pilot.pause(0.1)

    try:
        asyncio.run(drive())
    finally:
        logging.getLogger().removeHandler(collector)
    assert app.outcome is None
    assert len(files_under(tmp_path / "lib")) == 4
    assert any(line.startswith("Took ") for _, line in collector.kept)
