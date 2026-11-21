"""Looks for the per-file progress bar.

Kept in its own module with no imports of its own so the cli can offer
the choices without pulling in the download machinery to do it.

Each style is a muncher that eats its way along a track: `muncher` is
the animation, one entry per frame, cycled as the display repaints;
`pellet` is the track ahead of it and `wake` what it leaves behind.
An optional `end` is drawn at the far end of the track, for the muncher
to arrive at, and `scene` names a full sized picture to draw as well
when there is room, which only the train has.

Add your own. The only rule is that every frame of a style has to be
the same width, or the bar will jitter as it animates.
"""

PROGRESS_STYLES = {
    # A little fish swimming along, eating the pellets in front of it
    "fish": {"muncher": ("><>", ">=>"), "pellet": "·", "wake": "~"},
    # A snail, leaving a slime trail. Slow things look right in it
    "snail": {"muncher": ("@¬", "@~"), "pellet": "·", "wake": "="},
    # A plain filling bar
    "bars": {"muncher": ("#",), "pellet": "-", "wake": "#"},
    # Solid blocks, for terminals with a good font
    "blocks": {"muncher": ("█",), "pellet": "░", "wake": "█"},
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
        "scene": "sl",
    },
}

DEFAULT_PROGRESS_STYLE = "fish"
