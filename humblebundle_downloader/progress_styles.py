"""Looks for the per-file progress bar.

Kept in its own module with no imports of its own so the cli can offer
the choices without pulling in the download machinery to do it.

Each style is a muncher that eats its way along a track: `muncher` is
the animation, one entry per frame, cycled as the display repaints;
`pellet` is the track ahead of it and `wake` what it leaves behind.
An optional `end` is drawn at the far end of the track, for the muncher
to arrive at, `exits` has it carry on off the end so 100% is all wake,
`enters` has it start off the left so 0% is all track, and `scene` names a full sized picture to draw as well
when there is room, which only the train has.

Add your own. The only rule is that every frame of a style has to be
the same width, or the bar will jitter as it animates.
"""

PROGRESS_STYLES = {
    # A little fish swimming along, eating the pellets in front of it
    "fish": {"muncher": ("><>", ">=>"), "pellet": "·", "wake": "~", "exits": True, "enters": True},
    # A snail, leaving a slime trail. Slow things look right in it
    "snail": {"muncher": ("@¬", "@~"), "pellet": "·", "wake": "=", "exits": True, "enters": True},
    # A plain filling bar
    "bars": {"muncher": ("#",), "pellet": "-", "wake": "#"},
    # Solid blocks, for terminals with a good font
    "blocks": {"muncher": ("█",), "pellet": "░", "wake": "█"},
    # A light cycle racing across the grid, its rider tucking in and
    # out, leaving a solid wall of light behind it
    "cycle": {
        "muncher": ("O='`o", "O=`'o"),
        "pellet": "·",
        "wake": "━",
        # Rides off the end of the bar rather than stopping at it
        "exits": True,
        "enters": True,
    },
    # A mouse nibbling its way along the crumbs to the cheese
    "mouse": {"muncher": ("~( 3:>", "~( 3;>"), "pellet": "·", "wake": " ", "end": "◣"},
    # An arrow in flight, which thunks into the target as the file lands
    "arrow": {"muncher": (">>--->", ">>--->"), "pellet": " ", "wake": " ", "end": "(◎)", "enters": True},
    # A worm wriggling along to the apple
    "worm": {"muncher": ("~~:)", "-~:)"), "pellet": "·", "wake": " ", "end": "Q"},
    # A rocket, exhaust behind it, off into space
    "rocket": {"muncher": ("=]>", "≡]>"), "pellet": "·", "wake": "~", "exits": True, "enters": True},
    # Someone sprinting, arms pumping, straight off the end
    "runner": {"muncher": ("ε=┌(^-^)┘", "ε=└(^-^)┐"), "pellet": " ", "wake": " ", "exits": True, "enters": True},
    # A duck paddling along, quacking, ripples spreading behind it
    "duck": {"muncher": ("_(')>", "_(')="), "pellet": " ", "wake": "~", "exits": True, "enters": True},
    # A car with its wheels turning, past the lane markings
    "car": {"muncher": ("[o=o]>", "[0=0]>"), "pellet": "-", "wake": "_", "exits": True, "enters": True},
    # A comet twinkling through the stars, its tail behind it
    "comet": {"muncher": ("*", "✦"), "pellet": "·", "wake": "=", "exits": True, "enters": True},
    # A flying saucer, lights blinking, leaving a beam behind it
    "ufo": {"muncher": ("<o>", "<*>"), "pellet": " ", "wake": "'", "exits": True, "enters": True},
    # A little train running along the rails into a tunnel at the end,
    # couplings stretching and bunching as it chuffs along. With this
    # style the totals get the full sized train from sl, on its own
    # track: see sl_train, and --train-per-download to give every
    # download one
    "train": {
        "muncher": ("[]=[]=[]>", "[]-[]-[]>"),
        "pellet": "═",
        "wake": "═",
        "end": "▐▓",
        # In from the left, and into the tunnel rather than stopping at
        # its mouth
        "enters": True,
        "exits": True,
        "scene": "sl",
    },
}

DEFAULT_PROGRESS_STYLE = "fish"
