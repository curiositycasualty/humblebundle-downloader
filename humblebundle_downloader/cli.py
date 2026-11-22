import os
import sys
import logging
import argparse

import requests

from .progress_styles import PROGRESS_STYLES, DEFAULT_PROGRESS_STYLE

logger = logging.getLogger(__name__)

LOG_LEVEL = os.environ.get("HBD_LOGLEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(message)s",
)
# Ignore unwanted logs from the requests lib when debuging
logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)


def parse_args(args):
    if len(args) > 0 and args[0].lower() == "download":
        args = args[1:]
        raise DeprecationWarning("`download` argument is no longer used")

    parser = argparse.ArgumentParser()

    cookie = parser.add_mutually_exclusive_group(required=True)
    cookie.add_argument(
        "-c",
        "--cookie-file",
        type=str,
        help="Location of the cookies file",
    )
    cookie.add_argument(
        "-s",
        "--session-auth",
        type=str,
        help=(
            "Value of the _simpleauth_sess cookie, or a whole cookie header "
            "(name=value; name2=value2) for a different cookie. WRAP IN QUOTES"
        ),
    )
    parser.add_argument(
        "-l",
        "--library-path",
        type=str,
        help="Folder to download all content to",
        required=True,
    )
    parser.add_argument(
        "--vault",
        action="store_true",
        help=(
            "Also download the subscriber vault, the games that come with a "
            "Humble Choice membership"
        ),
    )
    parser.add_argument(
        "-t",
        "--trove",
        action="store_true",
        help=(
            "Only the subscriber vault, which is what the Humble Trove "
            "became. Same as --vault without the rest of the library"
        ),
    )
    parser.add_argument(
        "-u",
        "--update",
        action="store_true",
        help=("Check to see if products have been updated " "(still get new products)"),
    )
    parser.add_argument(
        "-p",
        "--platform",
        type=str,
        nargs="*",
        help=(
            "Only get content in a platform. Values can be seen in your "
            "humble bundle's library dropdown. Ex: -p ebook video"
        ),
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help=(
            "No longer needed: progress is always shown when hbd runs in "
            "a terminal. Accepted so existing commands keep working"
        ),
    )
    parser.add_argument(
        "--progress-style",
        type=str,
        default=DEFAULT_PROGRESS_STYLE,
        choices=sorted(PROGRESS_STYLES),
        help=(
            "Look of each download's progress bar (default: {default})".format(
                default=DEFAULT_PROGRESS_STYLE
            )
        ),
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=jobs_value,
        default=4,
        metavar="N|auto",
        help=(
            "Number of files to download at once, or 'auto' to start at 4 "
            "and find the fastest number as it goes, up to 16 (default: 4)"
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help=(
            "How many times to retry a file the server refuses or drops "
            "(default: 5). Use 0 to fail on the first error"
        ),
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help=(
            "Do not keep this run's log in the library's .hbd-logs folder "
            "(by default the newest {0} runs' logs are kept there)".format(20)
        ),
    )
    parser.add_argument(
        "--hide-skipped",
        action="store_true",
        help=(
            "Do not log every file a filter passes over. Handy when "
            "--include or --platform leaves most of a library out"
        ),
    )
    parser.add_argument(
        "--exit-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "How long the first Ctrl+C waits for downloads in flight "
            "before quitting anyway (default: 300, five minutes). 0 quits "
            "straight away; a second Ctrl+C always does"
        ),
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help=(
            "Do not check downloaded files against the checksum the api "
            "reports. Files with no checksum are unaffected either way"
        ),
    )
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help=(
            "Download one file at a time. Overrides --jobs"
        ),
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help=(
            "Do not download anything. List what would be downloaded "
            "and report the total size first"
        ),
    )
    parser.add_argument(
        "--print-urls",
        action="store_true",
        help=(
            "Print the url of each file being collected to stdout, "
            "one per line (all other output goes to stderr)"
        ),
    )
    parser.add_argument(
        "--accept-smaller",
        action="store_true",
        help=(
            "Let a new version much smaller than the file on disk (under "
            "75%%) replace it. By default the file on disk is kept, since "
            "Humble's regenerated files are often more heavily compressed"
        ),
    )
    parser.add_argument(
        "--print-checksums",
        action="store_true",
        help=(
            "Print the sha1 Humble lists for each file to be downloaded, to "
            "stdout, in the format shasum -c reads. Works with --dry-run"
        ),
    )
    parser.add_argument(
        "-f",
        "--prefer-format",
        type=str,
        nargs="*",
        help=(
            "Only get one format per item, trying these extensions in "
            "order and falling back to the largest file when none of "
            "them are available. 'largest' takes the biggest file, "
            "judged by what the server sends now. "
            "Ex: -f cbz epub pdf, -f largest, -f cbz largest"
        ),
    )
    filter_ext = parser.add_mutually_exclusive_group()
    filter_ext.add_argument(
        "-e",
        "--exclude",
        type=str,
        nargs="*",
        help=("File extensions to ignore when downloading files. " "Ex: -e pdf mobi"),
    )
    filter_ext.add_argument(
        "-i",
        "--include",
        type=str,
        nargs="*",
        help="Only download files with these extensions. Ex: -i pdf mobi",
    )
    parser.add_argument(
        "-k",
        "--keys",
        type=str,
        nargs="*",
        help=(
            "Only these bundles, by the id at the end of the bundle's "
            "library page url (/home/library/<id>). Can set multiple"
        ),
    )

    return parser.parse_args(args)


def jobs_value(text):
    """A whole number of at least 1, or auto"""
    if text.strip().lower() == "auto":
        return "auto"
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "expected a number or 'auto', not {0!r}".format(text)
        ) from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def resolve_jobs(cli_args):
    """--no-parallel wins over --jobs, so an alias carrying -j can still
    be overridden on the command line without an argparse conflict
    """
    return 1 if cli_args.no_parallel else cli_args.jobs


def _connection_reason(error):
    """The innermost reason a connection failed, which is the useful
    part of requests' nested messages"""
    reason = error
    while reason.__context__ is not None or reason.__cause__ is not None:
        reason = reason.__cause__ or reason.__context__
    text = str(reason) or type(reason).__name__
    return text if len(text) <= 200 else text[:199] + "…"


def start_run_log(cli_args):
    """Keep this run's whole log in the library, and redact urls in
    every line logged anywhere. Returns what finish_run_log needs"""
    from .logs import RedactingFilter, open_run_log

    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(RedactingFilter())
    if cli_args.no_log_file or cli_args.dry_run:
        return None
    if not os.path.isdir(cli_args.library_path):
        # Not created here: hbd takes care not to make a library on a
        # drive that is not mounted, and this must not get round that.
        # The run creates it, and the next run logs
        return None
    handler, path = open_run_log(cli_args.library_path)
    if handler is None:
        logger.warning("Not keeping a log of this run: {0}".format(path))
        return None
    root.addHandler(handler)
    return handler, path


def finish_run_log(run_log):
    if run_log is None:
        return
    handler, path = run_log
    logger.info("This run's full log is in {0}".format(path))
    logging.getLogger().removeHandler(handler)
    handler.close()


def make_listener(interactive):
    """What shows the run: the interface when there is a terminal to
    draw it on, and only log lines when there is not (cron, a pipe, a
    log file)"""
    if interactive:
        from .tui import TuiListener

        return TuiListener()
    from .events import Listener

    return Listener()


def cli():
    cli_args = parse_args(sys.argv[1:])

    if cli_args.retries < 0:
        sys.exit("--retries cannot be negative")

    if cli_args.exit_timeout is not None and cli_args.exit_timeout < 0:
        sys.exit("--exit-timeout cannot be negative")

    jobs = resolve_jobs(cli_args)

    from .download_library import DownloadLibrary, LibraryPathError, FAILED_EXIT_CODE
    from .humble_site import SiteError

    from .tui import can_run

    interactive = can_run()
    listener = make_listener(interactive)
    run_log = start_run_log(cli_args)

    try:
        library = DownloadLibrary(
            cli_args.library_path,
            cookie_path=cli_args.cookie_file,
            cookie_auth=cli_args.session_auth,
            ext_include=cli_args.include,
            ext_exclude=cli_args.exclude,
            platform_include=cli_args.platform,
            prefer_format=cli_args.prefer_format,
            purchase_keys=cli_args.keys,
            trove=cli_args.trove,
            vault=cli_args.vault,
            update=cli_args.update,
            dry_run=cli_args.dry_run,
            print_urls=cli_args.print_urls,
            print_checksums=cli_args.print_checksums,
            accept_smaller=cli_args.accept_smaller,
            jobs=jobs,
            retries=cli_args.retries,
            verify=not cli_args.no_verify,
            shutdown_grace=cli_args.exit_timeout,
            log_skipped=not cli_args.hide_skipped,
            listener=listener,
        )
        if interactive:
            from . import tui

            tui.run(
                library,
                listener,
                style=cli_args.progress_style,
            )
        else:
            library.start()
    except (LibraryPathError, SiteError) as error:
        # Nothing to retry and nothing to show a traceback for: the
        # path is wrong, or the site said no, and the message says how
        sys.exit(str(error))
    except requests.exceptions.ConnectionError as error:
        # No network, a proxy in the way, or Humble is down: the
        # traceback says nothing the one line does not
        sys.exit("Could not reach Humble Bundle: {0}".format(
            _connection_reason(error)))
    finally:
        finish_run_log(run_log)

    if library.failed_files:
        # Finished, but not everything: say so to whatever ran us
        sys.exit(FAILED_EXIT_CODE)
