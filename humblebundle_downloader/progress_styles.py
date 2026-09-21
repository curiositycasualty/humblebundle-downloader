"""Looks for the per-file progress bar.

Kept in its own module with no imports of its own so the cli can offer
the choices without pulling in the download machinery to do it.

Each style is a muncher that eats its way along a track: `muncher` is
the animation, one entry per frame, cycled as the display repaints;
`pellet` is the track ahead of it and `wake` what it leaves behind.

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
}

DEFAULT_PROGRESS_STYLE = "fish"
