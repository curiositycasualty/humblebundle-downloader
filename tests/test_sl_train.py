import os

import pytest

from humblebundle_downloader import sl_train
from humblebundle_downloader.download_library import ProgressReporter

from .test_progress_screen import TTY


def test_the_licence_notice_travels_with_the_art():
    source = open(sl_train.__file__, encoding="utf-8").read()
    assert "Copyright 1993,1998,2014 Toyoda Masashi" in source
    assert "unless you try to pretend that you wrote it" in source


def test_every_piece_is_the_same_width():
    pieces = list(sl_train._BODY) + list(sl_train._COAL) + list(sl_train._CAR)
    pieces += [line for pair in sl_train._WHEELS for line in pair]
    assert {len(p) for p in pieces} == {sl_train.PIECE}


def test_the_train_faces_right_engine_last():
    rows = sl_train.train(0, cars=0)
    assert len(rows) == sl_train.HEIGHT
    assert {len(r) for r in rows} == {sl_train.PIECE * 2}
    # The funnel is at the front, which is now the right
    assert rows[0].rstrip().endswith("++")
    assert rows[0].index("++") > rows[0].index("____")


@pytest.mark.parametrize("frame", range(6))
def test_the_wheels_turn(frame):
    rows = sl_train.train(frame, cars=0)
    other = sl_train.train(frame + 1, cars=0)
    assert rows[:4] == other[:4]
    assert rows[4:] != other[4:]


@pytest.mark.parametrize("width", [60, 90, 120, 183])
@pytest.mark.parametrize("fraction", [None, 0.0, 0.3, 0.75, 1.0])
def test_a_scene_is_always_exactly_its_size(width, fraction):
    rows = sl_train.scene(fraction, width, 3)
    assert len(rows) == sl_train.HEIGHT + 1
    assert {len(r) for r in rows} == {width}
    assert rows[-1].startswith("═") and rows[-1].endswith("╧")


def test_the_train_starts_in_view_and_ends_in_the_tunnel():
    width = 150
    mouth = width - sl_train.TUNNEL
    start = sl_train.scene(0.0, width, 0)
    end = sl_train.scene(1.0, width, 0)
    assert "++" in start[0][:mouth]
    assert start[0][:sl_train.PIECE * 4].strip()
    # All of it gone into the hill
    assert all(not row[:mouth].strip() for row in end[:-1])
    assert all(row[mouth:] == sl_train._TUNNEL[i] for i, row in enumerate(end[:-1]))


def test_further_along_means_further_right():
    def nose(rows):
        return rows[0].rstrip().rfind("+")

    positions = [nose(sl_train.scene(f, 200, 0)) for f in (0.0, 0.1, 0.2, 0.3)]
    assert positions == sorted(positions) and len(set(positions)) == 4


def test_narrow_terminals_get_a_shorter_train_or_just_track():
    assert sl_train.cars_for(200) == 2
    assert sl_train.cars_for(90) == 1
    assert sl_train.cars_for(70) == 0
    assert sl_train.cars_for(50) == -1
    rows = sl_train.scene(0.5, 50, 0)
    assert all(not r[:50 - sl_train.TUNNEL].strip() for r in rows[:-1])


def test_an_empty_scene_is_just_track_and_tunnel():
    rows = sl_train.scene(0.5, 120, 0, empty=True)
    assert all(not r[:120 - sl_train.TUNNEL].strip() for r in rows[:-1])


###
# in the progress block
###
def paint(reporter, tty, width, height, monkeypatch, files=(("a.pdf", 0.6), ("b.cbz", 0.2))):
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda fallback=None: os.terminal_size((width, height))
    )
    for name, fraction in files:
        reporter.file_started(name, 1000)
        reporter.file_progress(name, int(1000 * fraction))
    for _ in range(4):
        reporter.paint()
    return [r for r in tty.screen.rows if r.strip()]


def block(style="train", big=False, slots=3):
    tty = TTY()
    return tty, ProgressReporter(stream=tty, slots=slots, style=style, big_trains=big)


def count_engines(lines):
    return sum(1 for line in lines if line.rstrip().endswith("++") or "++ " in line)


def test_the_totals_get_a_train(monkeypatch):
    tty, reporter = block()
    lines = paint(reporter, tty, 120, 40, monkeypatch)
    assert sum(1 for l in lines if l.startswith("Total")) == 1
    assert sum(1 for l in lines if set(l.rstrip()) <= {"═", "╧"} and l.strip()) == 1
    assert len(reporter.render(120, 40)) == 3 + 1 + sl_train.HEIGHT + 1


def test_every_download_gets_a_train_when_asked_and_there_is_room(monkeypatch):
    tty, reporter = block(big=True)
    paint(reporter, tty, 120, 40, monkeypatch)
    lines = reporter.render(120, 40)
    tall = sl_train.HEIGHT + 1
    assert len(lines) == 3 * (1 + tall) + 1 + tall
    assert sum(1 for l in lines if set(l.rstrip()) <= {"═", "╧"} and l.strip()) == 4


@pytest.mark.parametrize("height,rows", [
    (40, 3 * 8 + 1 + 7),     # everything
    (30, 3 + 1 + 7),         # one line per download, train for the totals
    (10, 3 + 1),             # plain rows
    (4, 1),                  # the one line summary
])
def test_it_falls_back_as_the_terminal_gets_shorter(monkeypatch, height, rows):
    tty, reporter = block(big=True)
    paint(reporter, tty, 120, height, monkeypatch)
    assert len(reporter.render(120, height)) == rows


def test_other_styles_never_draw_a_train(monkeypatch):
    tty, reporter = block(style="fish", big=True)
    paint(reporter, tty, 120, 60, monkeypatch)
    assert len(reporter.render(120, 60)) == 3 + 1


def test_the_tall_block_repaints_in_place(monkeypatch):
    tty, reporter = block(big=True)
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda fallback=None: os.terminal_size((120, 40))
    )
    names = ["a.pdf", "b.cbz", "c.epub", "d.zip"]
    for step, name in enumerate(names):
        reporter.file_started(name, 1000)
        for done in (100, 400, 900):
            reporter.file_progress(name, done)
            reporter.paint()
        if step:
            reporter.file_finished(names[step - 1])
            reporter.paint()
    final = reporter.render(120, 40)

    def steady(lines):
        # Rates and clocks move between one paint and the next
        import re

        return [re.sub(r"[\d.]+ \S+/s|\d\d:\d\d", "", l).rstrip() for l in lines if l.strip()]

    on_screen = steady(tty.screen.rows)
    assert on_screen == steady(final)
    # And nothing left over above it from earlier, taller or shorter paints
    assert len(on_screen) == len(steady(final))
