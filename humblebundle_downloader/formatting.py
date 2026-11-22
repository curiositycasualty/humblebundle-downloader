"""How sizes, times and names are shown, wherever they are shown"""


def _human_size(num_bytes):
    """Format a byte count the way a human would read it"""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            if unit == "B":
                return "{size:.0f} {unit}".format(size=size, unit=unit)
            return "{size:.2f} {unit}".format(size=size, unit=unit)
        size /= 1024


def _human_duration(seconds):
    """1h 02m 03s, 4m 05s or 6s"""
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return "{0}h {1:02d}m {2:02d}s".format(hours, minutes, seconds)
    if minutes:
        return "{0}m {1:02d}s".format(minutes, seconds)
    return "{0}s".format(seconds)


def _format_clock(seconds):
    """mm:ss, or --:-- when there is nothing to go on"""
    if seconds is None or seconds < 0 or seconds != seconds:
        return "--:--"
    seconds = int(seconds)
    if seconds >= 3600:
        return "{hours:02d}:{minutes:02d}h".format(
            hours=seconds // 3600, minutes=(seconds % 3600) // 60
        )
    return "{minutes:02d}:{seconds:02d}".format(
        minutes=seconds // 60, seconds=seconds % 60
    )


def _fit(text, width):
    """Pad or truncate to exactly `width`, keeping the tail of a name
    since that is where a file's distinguishing part usually is
    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text.ljust(width)
    if width <= 3:
        return text[:width]
    return "…" + text[-(width - 1):]
