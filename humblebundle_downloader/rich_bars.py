"""The progress styles as Rich renderables, in colour.

The drawing itself stays in progress_styles and sl_train as plain
functions of (fraction, width, frame), which is what the tests pin
down. This only fits them to the width Rich offers and colours them.
"""
from rich.measure import Measurement
from rich.text import Text

from . import sl_train
from .progress_styles import (
    DEFAULT_PROGRESS_STYLE,
    PROGRESS_STYLES,
    muncher_parts,
    style_colors,
)


def _style(style):
    if isinstance(style, dict):
        return style
    return PROGRESS_STYLES.get(style, PROGRESS_STYLES[DEFAULT_PROGRESS_STYLE])


class MuncherBar:
    """One download's bar in a progress style. Fills whatever width it
    is given, brackets included; fraction None means the size is not
    known, and the muncher paces back and forth"""

    MIN_WIDTH = 5

    def __init__(self, fraction, style=DEFAULT_PROGRESS_STYLE, frame=0, width=None):
        self.fraction = fraction
        self.style = _style(style)
        self.frame = frame
        self.width = width

    def text(self, width):
        colors = style_colors(self.style)
        track = max(1, width - 2)
        return Text.assemble(*[
            (text, colors.get(part, ""))
            for part, text in muncher_parts(self.fraction, track, self.style, self.frame)
        ], no_wrap=True, end="")

    def __rich_console__(self, console, options):
        yield self.text(self.width or options.max_width)

    def __rich_measure__(self, console, options):
        if self.width:
            return Measurement(self.width, self.width)
        return Measurement(self.MIN_WIDTH, options.max_width)


class PlainBar(MuncherBar):
    """The totals bar: filled, without a muncher"""

    def __init__(self, fraction, width=None):
        super().__init__(fraction, "bars", 0, width)


# Which characters of the sl scene are which, for colouring
_TUNNEL = set("▗▄▐▓╧")
_SMOKE = set("@")


class TrainScene:
    """sl's little train on its track, running into the tunnel as the
    fraction grows. `height` rows tall"""

    # The train, and the track under it
    height = sl_train.HEIGHT + 1

    def __init__(self, fraction, frame=0, empty=False):
        self.fraction = fraction
        self.frame = frame
        self.empty = empty

    @staticmethod
    def fits(width):
        return sl_train.cars_for(width) >= 0

    def lines(self, width):
        return sl_train.scene(self.fraction, width, self.frame, empty=self.empty)

    def text(self, width):
        out = Text(no_wrap=True, end="")
        for row, line in enumerate(self.lines(width)):
            if row:
                out.append("\n")
            for char in line:
                if char in _TUNNEL:
                    out.append(char, "grey50")
                elif char == sl_train.RAIL:
                    out.append(char, "bright_black")
                elif char in _SMOKE:
                    out.append(char, "grey70")
                else:
                    out.append(char, "bold bright_white")
        return out

    def __rich_console__(self, console, options):
        yield self.text(options.max_width)

    def __rich_measure__(self, console, options):
        return Measurement(options.max_width, options.max_width)
