"""The little train from sl, running along a track into a tunnel.

The train is the "little" one sl draws with -l, from sl.h, under sl's
licence, which asks that this notice go with every copy:

    Copyright 1993,1998,2014 Toyoda Masashi (mtoyoda@acm.org)

    Everyone is permitted to do anything on this program including copying,
    modifying, and improving, unless you try to pretend that you wrote it.
    i.e., the above copyright notice has to appear in all copies.
    THE AUTHOR DISCLAIMS ANY RESPONSIBILITY WITH REGARD TO THIS SOFTWARE.

sl's train faces left and runs right to left. Here it is mirrored, so
it runs left to right the way a progress bar fills, and its progress is
how far it has gone into the tunnel at the right of the track.
"""

# From sl.h: LOGO1-4, LWHL11-62, LCOAL1-6, LCAR1-6. Each piece is 21
# columns wide, and the wheels have six positions
_BODY = (
    "     ++      +------ ",
    "     ||      |+-+ |  ",
    "   /---------|| | |  ",
    "  + ========  +-+ |  ",
)
_WHEELS = (
    (" _|--O========O~\\-+  ", "//// \\_/      \\_/    "),
    (" _|--/O========O\\-+  ", "//// \\_/      \\_/    "),
    (" _|--/~O========O-+  ", "//// \\_/      \\_/    "),
    (" _|--/~\\------/~\\-+  ", "//// \\_O========O    "),
    (" _|--/~\\------/~\\-+  ", "//// \\O========O/    "),
    (" _|--/~\\------/~\\-+  ", "//// O========O_/    "),
)
_COAL = (
    "____                 ",
    "|   \\@@@@@@@@@@@     ",
    "|    \\@@@@@@@@@@@@@_ ",
    "|                  | ",
    "|__________________| ",
    "   (O)       (O)     ",
)
_CAR = (
    "____________________ ",
    "|  ___ ___ ___ ___ | ",
    "|  |_| |_| |_| |_| | ",
    "|__________________| ",
    "|__________________| ",
    "   (O)        (O)    ",
)

HEIGHT = len(_COAL)
PIECE = len(_CAR[0])

_MIRROR = str.maketrans("/\\()[]<>{}", "\\/)(][><}{")

# The tunnel: a hillside with the mouth on its left face, which the
# train runs into. One row per train row, then the track's
_TUNNEL = (
    "▗▄▄▄▄▄▄",
    "▐▓▓▓▓▓▓",
    "▐▓▓▓▓▓▓",
    "▐▓▓▓▓▓▓",
    "▐▓▓▓▓▓▓",
    "▐▓▓▓▓▓▓",
)
TUNNEL = len(_TUNNEL[0])
RAIL = "═"
TUNNEL_RAIL = "╧"


def _mirror(line):
    return line[::-1].translate(_MIRROR)


def train(frame, cars=2):
    """The train's rows, facing right: cars, then coal, then engine.

    cars is how many passenger cars to pull, so a narrow terminal can
    have a shorter train. frame picks the wheel position
    """
    top, bottom = _WHEELS[frame % len(_WHEELS)]
    engine = _BODY + (top, bottom)
    pieces = [_CAR] * max(0, cars) + [_COAL, engine]
    # sl lays the pieces out engine first, facing left. Mirroring each
    # row of that whole train turns it round, engine last
    return [
        _mirror("".join(piece[row] for piece in reversed(pieces)))
        for row in range(HEIGHT)
    ]


# Least track the train should have to cross, beyond its own length
MIN_RUN = 20


def cars_for(width):
    """As many cars, up to two, as still leave the train some track to
    cross. -1 when not even the engine and coal fit"""
    room = width - TUNNEL
    for cars in (2, 1, 0):
        if room >= PIECE * (cars + 2) + MIN_RUN:
            return cars
    return -1


def scene(fraction, width, frame, empty=False):
    """The track, the tunnel, and the train fraction of the way into it.

    Returns HEIGHT + 1 rows, each exactly width columns. At 0 the whole
    train sits at the start of the track; at 1 the last of it has gone
    into the tunnel. With no fraction it waits at the start, wheels
    turning. Empty, or too narrow for even the engine, and there is
    just the track
    """
    width = max(width, TUNNEL + 1)
    mouth = width - TUNNEL
    rows = [[" "] * mouth for _ in range(HEIGHT)]

    cars = cars_for(width)
    if cars >= 0 and not empty:
        body = train(frame, cars)
        progress = 0.0 if fraction is None else max(0.0, min(1.0, fraction))
        # From fully in view at the start, to fully inside the tunnel
        left = int(round(progress * mouth))
        for row, line in enumerate(body):
            for col, ch in enumerate(line):
                x = left + col
                if 0 <= x < mouth and ch != " ":
                    rows[row][x] = ch

    out = ["".join(row) + _TUNNEL[i] for i, row in enumerate(rows)]
    out.append(RAIL * mouth + TUNNEL_RAIL * TUNNEL)
    return out
