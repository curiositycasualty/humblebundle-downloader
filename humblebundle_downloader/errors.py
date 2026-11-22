"""What can go wrong in a run, and the exit statuses that say so"""


# Conventional exit status for "killed by SIGINT"
INTERRUPT_EXIT_CODE = 130


# Exit status for a run that finished but gave up on some files
FAILED_EXIT_CODE = 1


class RunStopped(SystemExit):
    """The run was asked to stop and has. A SystemExit, with the
    conventional status for being interrupted, so a caller that does not
    catch it exits the way a Ctrl-C always has"""

    def __init__(self):
        super().__init__(INTERRUPT_EXIT_CODE)


class Interrupted(Exception):
    """Raised inside a transfer when the run is being shut down.

    Distinct from a failure: there is nothing to retry or resume, and it
    should not be reported as a download that went wrong.
    """


class RepeatedMismatch(Exception):
    """A file failed its checksum twice with the same wrong bytes, so it
    is given up on rather than downloaded yet again"""


class ChecksumMismatch(Exception):
    """What arrived is not what the api said it would be.

    Resuming would keep the bad bytes, so this always restarts the file
    from nothing rather than picking it back up.
    """


class LibraryPathError(Exception):
    """The library path cannot be written to.

    Nothing is worth attempting until it can, so this is raised before
    the run starts rather than discovered one file at a time.
    """
