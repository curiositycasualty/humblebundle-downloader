import os
import sys
import logging
import argparse

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
        help="Value of the cookie _simpleauth_sess. WRAP IN QUOTES",
    )
    parser.add_argument(
        "-l",
        "--library-path",
        type=str,
        help="Folder to download all content to",
        required=True,
    )
    parser.add_argument(
        "-t",
        "--trove",
        action="store_true",
        help="Only check and download Humble Trove content",
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
        help="Display progress bar for downloads",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=4,
        help="Number of files to download at once (default: 4)",
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
        "--no-parallel",
        action="store_true",
        help=(
            "Download one file at a time. Overrides --jobs, and gets "
            "you the per-file progress bar back"
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
        "-f",
        "--prefer-format",
        type=str,
        nargs="*",
        help=(
            "Only get one format per item, trying these extensions in "
            "order and falling back to the largest file when none of "
            "them are available. Ex: -f cbz epub pdf"
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
            "The purchase download key. Find in the url on the "
            "products/bundle download page. Can set multiple"
        ),
    )

    return parser.parse_args(args)


def resolve_jobs(cli_args):
    """--no-parallel wins over --jobs, so an alias carrying -j can still
    be overridden on the command line without an argparse conflict
    """
    return 1 if cli_args.no_parallel else cli_args.jobs


def cli():
    cli_args = parse_args(sys.argv[1:])

    if cli_args.jobs < 1:
        sys.exit("--jobs must be at least 1")

    if cli_args.retries < 0:
        sys.exit("--retries cannot be negative")

    jobs = resolve_jobs(cli_args)

    from .download_library import DownloadLibrary

    DownloadLibrary(
        cli_args.library_path,
        cookie_path=cli_args.cookie_file,
        cookie_auth=cli_args.session_auth,
        progress_bar=cli_args.progress,
        ext_include=cli_args.include,
        ext_exclude=cli_args.exclude,
        platform_include=cli_args.platform,
        prefer_format=cli_args.prefer_format,
        purchase_keys=cli_args.keys,
        trove=cli_args.trove,
        update=cli_args.update,
        dry_run=cli_args.dry_run,
        print_urls=cli_args.print_urls,
        jobs=jobs,
        retries=cli_args.retries,
    ).start()
