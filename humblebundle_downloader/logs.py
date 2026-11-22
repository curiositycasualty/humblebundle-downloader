"""The log of each run, kept in the library.

The interface only puts warnings, errors and the summary back in the
terminal when it closes, so everything else (each file as it finished,
what was skipped and why) would be gone. Each run therefore also writes
its whole log to <library>/.hbd-logs/, and the newest few are kept.

Download urls carry a token that works for a day, so query strings are
taken out of any url before a line is written anywhere. --print-urls
still prints them whole, to stdout: asking for them is the point.
"""
import datetime
import logging
import os
import re

LOG_DIR = ".hbd-logs"
# How many run logs are kept; older ones are deleted as new ones start
KEEP = 20
FORMAT = "%(asctime)s %(levelname)-7s %(message)s"

_QUERY = re.compile(r"(https?://[^\s?#\"'<>]+)\?[^\s\"'<>)]*")


def redact(text):
    """Text with the query string taken off every url in it"""
    return _QUERY.sub(r"\1?…", text)


class RedactingFormatter(logging.Formatter):
    """Formats as usual, tracebacks included, then redacts the lot"""

    def format(self, record):
        return redact(super().format(record))


class RedactingFilter(logging.Filter):
    """For handlers whose formatter is not ours, like the interface's:
    the message is redacted before it is formatted"""

    def filter(self, record):
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True


def open_run_log(library_path, keep=KEEP, now=None):
    """Start this run's log file. Returns (handler, path), or (None,
    reason) when it cannot be written, which never stops a run"""
    folder = os.path.join(library_path, LOG_DIR)
    stamp = (now or datetime.datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(folder, "hbd-{0}.log".format(stamp))
    try:
        os.makedirs(folder, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as error:
        return None, error.strerror or str(error)
    handler.setFormatter(RedactingFormatter(FORMAT))
    handler.setLevel(logging.INFO)
    prune(folder, keep, current=path)
    return handler, path


def prune(folder, keep, current=None):
    """Delete all but the newest `keep` logs. Names sort by time"""
    try:
        logs = sorted(
            name for name in os.listdir(folder)
            if name.startswith("hbd-") and name.endswith(".log")
        )
    except OSError:
        return
    for name in logs[:-keep] if keep > 0 else logs:
        path = os.path.join(folder, name)
        if path == current:
            continue
        try:
            os.remove(path)
        except OSError:
            pass
