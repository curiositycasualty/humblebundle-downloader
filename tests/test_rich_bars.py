"""The Rich renderables draw exactly what the plain functions do, in
colour, at the width they are given"""
import pytest
from rich.console import Console

from humblebundle_downloader.progress_styles import (
    PROGRESS_STYLES,
    _muncher_bar,
    muncher_parts,
    style_colors,
)
from humblebundle_downloader.rich_bars import MuncherBar, PlainBar


def rendered(renderable, width):
    console = Console(width=width, color_system="truecolor", record=True, file=open("/dev/null", "w"))
    console.print(renderable)
    return console.export_text().rstrip("\n")


@pytest.mark.parametrize("name", sorted(PROGRESS_STYLES))
@pytest.mark.parametrize("fraction", [None, 0.0, 0.01, 0.5, 0.93, 1.0])
def test_a_bar_is_the_plain_bar_in_colour(name, fraction):
    style = PROGRESS_STYLES[name]
    for width in (12, 30):
        bar = MuncherBar(fraction, name, frame=3)
        text = bar.text(width)
        assert text.plain == _muncher_bar(fraction, width - 2, style, 3)
        assert text.cell_len == width


def test_parts_join_to_the_plain_bar_and_carry_their_colours():
    style = PROGRESS_STYLES["mouse"]
    parts = muncher_parts(0.5, 20, style, 0)
    assert "".join(t for _, t in parts) == _muncher_bar(0.5, 20, style, 0)
    kinds = [k for k, _ in parts]
    assert kinds[0] == kinds[-1] == "bracket"
    assert "muncher" in kinds and "end" in kinds
    text = MuncherBar(0.5, "mouse").text(22)
    colors = style_colors(style)
    muncher = style["muncher"][0]
    start = text.plain.index(muncher)
    styled = [str(span.style) for span in text.spans if span.start == start]
    assert styled == [colors["muncher"]]


def test_a_bar_fills_the_width_rich_offers():
    assert len(rendered(MuncherBar(0.5, "fish"), 40)) == 40
    assert len(rendered(MuncherBar(0.5, "fish", width=16), 40)) == 16


def test_an_unknown_style_falls_back_to_the_default():
    assert MuncherBar(0.5, "no-such-style").text(20).plain == MuncherBar(0.5, "fish").text(20).plain


def test_the_totals_bar_is_plain():
    assert PlainBar(0.5).text(12).plain == "[#####-----]"
