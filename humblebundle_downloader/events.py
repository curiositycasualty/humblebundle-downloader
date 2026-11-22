"""What a run tells whoever is showing it.

The engine (DownloadLibrary) knows nothing about terminals. It calls
these methods as things happen, and a display does with them what it
likes: the interactive interface draws them, the plain one ignores
most of them because log lines already say enough.

Calls come from whichever thread did the thing: the library walk, a
download worker, the -j auto tuner, or the Ctrl-C handler. A listener
has to be safe to call from all of them at once, and quick, since a
download calls file_progress once per chunk.
"""
import sys
import threading


class Listener:
    """Hears everything and does nothing, apart from printing output.

    Subclass it and override what matters to the display.
    """

    # Whether the display shows each transfer while it runs. When it
    # does, the "Downloading: x" line a file would otherwise get when it
    # starts only adds noise, so it drops to debug
    shows_transfers = False

    def __init__(self):
        self._output_lock = threading.Lock()

    # -- the run as a whole -------------------------------------------

    def downloads_started(self, slots):
        """Downloads are about to begin, up to `slots` at once"""

    def jobs_changed(self, limit):
        """The number of downloads at once changed, by -j auto or by
        hand from the interface"""

    def downloads_finished(self):
        """Every worker has stopped. The summary is logged after this"""

    def paused(self, paused):
        """Every transfer was held (True), or let go again (False)"""

    def stopping(self):
        """A stop was asked for, by Ctrl-C or the interface.

        Called from a signal handler too, so it must not take locks a
        worker might hold
        """

    def run_ending(self):
        """The run's summary lines follow: failures, totals, runtime"""

    # -- the library walk ---------------------------------------------

    def bundle_started(self, title):
        """The walk has moved on to another bundle"""

    def file_queued(self):
        """A file was found that may need downloading"""

    def file_skipped(self):
        """A queued file turned out not to need downloading after all"""

    def traversal_finished(self):
        """No more files will be queued, so totals are final"""

    # -- each download ------------------------------------------------

    def file_started(self, name, total):
        """A transfer began, or began again at a new size after a resume.
        total is None when the server does not say"""

    def file_progress(self, name, downloaded):
        """How much of `name` is on disk so far. Called once per chunk"""

    def file_finished(self, name, ok=True, skipped=False):
        """The transfer of `name` is over: done, or not (ok False), and
        when not, whether it was skipped on purpose"""

    # -- what goes to stdout ------------------------------------------

    def output(self, line):
        """A line meant for stdout, like --print-urls and
        --print-checksums give: kept apart from logging, which goes to
        stderr, so the list stays usable when piped elsewhere"""
        with self._output_lock:
            print(line, file=sys.stdout, flush=True)
