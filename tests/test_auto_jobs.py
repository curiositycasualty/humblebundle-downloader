import threading
import time

import pytest

from humblebundle_downloader.auto_jobs import JobGate, JobTuner


def feed(tuner, *rates, **kwargs):
    return [tuner.observe(rate, **kwargs) for rate in rates]


###
# deciding
###
def test_it_climbs_while_more_at_once_keeps_paying():
    tuner = JobTuner(start=4, maximum=16, step=2, gain=1.10, hold=3)
    limits = [limit for limit, _ in feed(tuner, 10, 12, 14, 16)]
    assert limits == [6, 8, 10, 12]


def test_it_steps_back_when_a_step_does_not_pay():
    tuner = JobTuner(start=4, step=2, gain=1.10, hold=3)
    feed(tuner, 10)                         # tries 6
    limit, reason = tuner.observe(10.5)     # 5% better is not enough
    assert limit == 4
    assert "no faster" in reason


def test_it_waits_before_trying_again_and_then_does():
    tuner = JobTuner(start=4, step=2, gain=1.10, hold=3)
    feed(tuner, 10, 10)                     # up, then back to 4
    held = feed(tuner, 10, 10, 10)
    assert [limit for limit, _ in held] == [4, 4, 4]
    assert all(reason is None for _, reason in held)
    assert tuner.observe(10) == (6, "trying more at once")


def test_being_throttled_halves_it_at_once():
    tuner = JobTuner(start=4, step=2, hold=3)
    feed(tuner, 10, 12, 14)                 # climbs to 10
    limit, reason = tuner.observe(1, throttled=True)
    assert limit == 5
    assert "slow down" in reason
    # and it does not climb straight back
    assert [l for l, _ in feed(tuner, 50, 50, 50)] == [5, 5, 5]


def test_a_starved_measurement_teaches_it_nothing():
    tuner = JobTuner(start=4)
    assert tuner.observe(1, busy=False) == (4, None)
    assert tuner.observe(0, busy=False) == (4, None)
    assert tuner.best is None


def test_it_stops_at_the_top_and_bottom():
    tuner = JobTuner(start=14, maximum=16, step=2, gain=1.0, hold=1)
    limits = [l for l, _ in feed(tuner, 10, 20, 30, 40)]
    assert max(limits) == 16
    low = JobTuner(start=1, minimum=1)
    for _ in range(5):
        low.observe(1, throttled=True)
    assert low.limit == 1


def test_it_remembers_the_best_it_saw():
    tuner = JobTuner(start=4, step=2, gain=1.10, hold=3)
    feed(tuner, 10, 30, 20)
    assert tuner.best == (30, 6)


###
# the gate
###
def test_the_gate_lets_only_the_limit_through():
    gate = JobGate(2)
    peak = []
    lock = threading.Lock()
    inside = [0]

    def work():
        gate.acquire()
        with lock:
            inside[0] += 1
            peak.append(inside[0])
        time.sleep(0.02)
        with lock:
            inside[0] -= 1
        gate.release()

    threads = [threading.Thread(target=work) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(peak) == 2


def test_raising_the_limit_lets_waiters_in_straight_away():
    gate = JobGate(1)
    gate.acquire()
    got = threading.Event()
    threading.Thread(target=lambda: (gate.acquire(), got.set()), daemon=True).start()
    assert not got.wait(0.2)
    gate.set_limit(2)
    assert got.wait(2)


def test_lowering_the_limit_waits_for_downloads_to_finish():
    gate = JobGate(3)
    for _ in range(3):
        gate.acquire()
    gate.set_limit(1)
    assert gate.active == 3                 # nothing in flight is stopped
    got = threading.Event()
    threading.Thread(target=lambda: (gate.acquire(), got.set()), daemon=True).start()
    gate.release()
    gate.release()
    assert not got.wait(0.2)                # still one in, at a limit of one
    gate.release()
    assert got.wait(2)


def test_a_waiting_worker_gives_up_when_the_run_stops():
    gate = JobGate(1)
    gate.acquire()
    stop = threading.Event()
    result = []
    t = threading.Thread(target=lambda: result.append(gate.acquire(stop, poll=0.01)))
    t.start()
    stop.set()
    t.join(2)
    assert result == [False]


###
# the run
###
def test_a_whole_run_with_auto_downloads_everything(tmp_path):
    from .test_site_downloads import BODIES, FakeHumble, files_under, library, two_bundles

    fake = FakeHumble(*two_bundles())
    dl = library(tmp_path, fake, jobs="auto")
    assert dl.auto_jobs and dl.jobs == 16 and dl._gate.limit == 4
    dl.start()
    assert len(files_under(tmp_path / "lib")) == len(BODIES)


def test_the_tuner_thread_moves_the_gate_and_says_why(tmp_path, caplog):
    import logging

    from humblebundle_downloader.download_library import DownloadLibrary

    caplog.set_level(logging.INFO)
    dl = DownloadLibrary(str(tmp_path), jobs="auto")
    for _ in range(dl._gate.limit):
        dl._gate.acquire()                  # every slot busy

    def traffic():
        while not dl._tuner_stop.is_set():
            dl._count_bytes(1_000_000)
            time.sleep(0.01)

    threading.Thread(target=traffic, daemon=True).start()
    tuner = threading.Thread(target=dl._tune, kwargs={"interval": 0.3, "tick": 0.05})
    tuner.start()
    deadline = time.time() + 5
    while dl._gate.limit == 4 and time.time() < deadline:
        time.sleep(0.05)
    dl._tuner_stop.set()
    tuner.join(2)
    assert dl._gate.limit == 6
    assert any("Now downloading 6 at once, was 4" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value,expected", [("auto", "auto"), ("AUTO", "auto"), ("8", 8)])
def test_the_cli_takes_a_number_or_auto(value, expected):
    from humblebundle_downloader.cli import parse_args

    assert parse_args(["-l", "x", "-s", "y", "-j", value]).jobs == expected


@pytest.mark.parametrize("value", ["0", "-2", "lots"])
def test_the_cli_refuses_anything_else(value, capsys):
    from humblebundle_downloader.cli import parse_args

    with pytest.raises(SystemExit):
        parse_args(["-l", "x", "-s", "y", "-j", value])


def test_no_parallel_still_wins_over_auto():
    from humblebundle_downloader.cli import parse_args, resolve_jobs

    args = parse_args(["-l", "x", "-s", "y", "-j", "auto", "--no-parallel"])
    assert resolve_jobs(args) == 1
