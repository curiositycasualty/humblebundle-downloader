import os
import sys
import json
import time
import email
import queue
import parsel
import shutil
import signal
import logging
import datetime
import requests
import threading
import http.cookiejar
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .progress_styles import PROGRESS_STYLES, DEFAULT_PROGRESS_STYLE

logger = logging.getLogger(__name__)

# Retried at the transport layer. 429 is deliberately absent: it is
# handled explicitly so the whole pool backs off together
RETRY_STATUSES = (500, 502, 503, 504)
THROTTLE_STATUSES = (429,)
DEFAULT_COOLDOWN = 30

# Conventional exit status for "killed by SIGINT"
INTERRUPT_EXIT_CODE = 130
# How long a first Ctrl-C waits for transfers to wind themselves up
SHUTDOWN_GRACE = 10


class Interrupted(Exception):
    """Raised inside a transfer when the run is being shut down.

    Distinct from a failure: there is nothing to retry or resume, and it
    should not be reported as a download that went wrong.
    """


def _package_version():
    try:
        from importlib.metadata import version

        return version("humblebundle-downloader")
    except Exception:
        return "dev"


USER_AGENT = (
    "humblebundle-downloader/{version} "
    "(+https://github.com/xtream1101/humblebundle-downloader)"
).format(version=_package_version())


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


def _bar(fraction, width):
    if width < 3:
        return ""
    if fraction is None:
        return "[" + "?" * width + "]"
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _muncher_bar(fraction, width, style, frame):
    """A muncher eating its way along the track.

    With no known size it paces back and forth instead of creeping
    right, so the line still shows something is happening.
    """
    frames = style["muncher"]
    muncher = frames[frame % len(frames)]
    span = max(0, width - len(muncher))
    if span == 0:
        return "[" + muncher[:width] + "]"

    if fraction is None:
        # Bounce: 0..span..0
        cycle = span * 2
        step = frame % cycle if cycle else 0
        position = step if step <= span else cycle - step
        ahead = style["pellet"] * (span - position)
        behind = style["pellet"] * position
        return "[" + behind + muncher + ahead + "]"

    position = int(round(max(0.0, min(1.0, fraction)) * span))
    return "[{wake}{muncher}{pellets}]".format(
        wake=style["wake"] * position,
        muncher=muncher,
        pellets=style["pellet"] * (span - position),
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


class ProgressReporter:
    """A block of lines repainted in place: one per transfer in flight,
    plus a totals line, in the manner of a package manager.

    Workers only ever touch counters. One reporter thread owns the
    terminal block and is the only thing that writes to it, which is
    what stops concurrent transfers from shredding each other's output.
    """

    # Column widths for the numbers. These are minimums: a value wider
    # than its column pushes the name column in rather than the line out
    SIZE_WIDTH = 10
    RATE_WIDTH = 12
    ETA_WIDTH = 5
    PCT_WIDTH = 4
    # Everything right of the name, bar excluded: four gaps plus the
    # four columns above
    TAIL_WIDTH = 4 + SIZE_WIDTH + RATE_WIDTH + ETA_WIDTH + PCT_WIDTH
    MIN_NAME = 10
    MAX_BAR = 20
    MIN_BAR = 6

    def __init__(self, stream=None, slots=1, interval=0.2, window=3.0,
                 style=DEFAULT_PROGRESS_STYLE):
        self.stream = sys.stderr if stream is None else stream
        self.slots = max(1, int(slots))
        self.interval = interval
        self.window = window
        self.style = PROGRESS_STYLES.get(
            style, PROGRESS_STYLES[DEFAULT_PROGRESS_STYLE]
        )
        # Bumped every repaint, so the munchers chomp on their own even
        # when a transfer is barely moving
        self._frame = 0

        self._lock = threading.Lock()
        self._files = {}                      # name -> dict of counters
        self._slot_names = [None] * self.slots
        self._done = 0
        self._failed = 0
        self._skipped = 0
        self._queued = 0
        self._bytes = 0                       # bytes from finished files
        self._walk_done = False
        self._started_at = time.time()
        self._overall = []                    # (when, bytes seen so far)

        self._thread = None
        self._stop = threading.Event()
        self._painted = 0                     # lines currently on screen

    # -- lifecycle ---------------------------------------------------

    def enabled(self):
        try:
            return bool(self.stream.isatty())
        except Exception:
            return False

    def start(self):
        if not self.enabled() or self._thread is not None:
            return
        self._started_at = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        self.clear()

    # -- what the workers report -------------------------------------

    def file_queued(self):
        with self._lock:
            self._queued += 1

    def file_skipped(self):
        """Queued, then found not to need downloading after all"""
        with self._lock:
            self._skipped += 1

    def traversal_finished(self):
        """No more files will be queued, so the totals can show a bar"""
        with self._lock:
            self._walk_done = True

    def file_started(self, name, total):
        with self._lock:
            self._files[name] = {
                "done": 0, "total": total, "samples": [],
            }
            if name not in self._slot_names:
                if None in self._slot_names:
                    self._slot_names[self._slot_names.index(None)] = name
                else:
                    self._slot_names.append(name)

    def file_progress(self, name, downloaded):
        # Hot path: one dict write per chunk. Sampling for rates happens
        # in the reporter thread, not here
        with self._lock:
            entry = self._files.get(name)
            if entry is not None:
                entry["done"] = downloaded

    def file_finished(self, name, ok=True):
        with self._lock:
            entry = self._files.pop(name, None)
            if name in self._slot_names:
                self._slot_names[self._slot_names.index(name)] = None
            if ok:
                self._done += 1
                if entry is not None:
                    self._bytes += entry["done"]
            else:
                self._failed += 1

    # -- rates -------------------------------------------------------

    def _sample(self, now):
        """Called from the paint loop, so sampling costs the transfers
        nothing
        """
        seen = self._bytes
        for entry in self._files.values():
            seen += entry["done"]
            samples = entry["samples"]
            samples.append((now, entry["done"]))
            while len(samples) > 2 and now - samples[0][0] > self.window:
                samples.pop(0)

        self._overall.append((now, seen))
        while (len(self._overall) > 2
               and now - self._overall[0][0] > self.window):
            self._overall.pop(0)
        return seen

    @staticmethod
    def _rate(samples):
        if len(samples) < 2:
            return None
        elapsed = samples[-1][0] - samples[0][0]
        if elapsed <= 0:
            return None
        moved = samples[-1][1] - samples[0][1]
        return moved / elapsed if moved > 0 else 0.0

    # -- rendering ---------------------------------------------------

    def _layout(self, width):
        """Widest bar that still leaves room for a name.

        A row is name + space + TAIL_WIDTH + bar + 2 brackets, and one
        column is kept spare so the terminal does not wrap it, so the
        bar can have whatever is left over after MIN_NAME.
        """
        bar = max(self.MIN_BAR, min(
            self.MAX_BAR,
            width - self.MIN_NAME - self.TAIL_WIDTH - 4,
        ))
        name = width - self.TAIL_WIDTH - bar - 4
        return name, bar

    def _fits(self, width):
        return width >= self.MIN_NAME + self.TAIL_WIDTH + self.MIN_BAR + 4

    def _compose(self, label, size, rate, eta, bar, pct, width):
        """Lay out one row.

        The tail is built first and measured, and the name gets whatever
        is left. A number wider than its column then costs the name a
        character instead of pushing the whole line past the terminal.
        """
        tail = "{size:>{sw}} {rate:>{rw}} {eta:>{ew}} {bar} {pct:>{pw}}".format(
            size=size, sw=self.SIZE_WIDTH,
            rate=rate, rw=self.RATE_WIDTH,
            eta=eta, ew=self.ETA_WIDTH,
            bar=bar,
            pct=pct, pw=self.PCT_WIDTH,
        )
        name_width = max(self.MIN_NAME, width - len(tail) - 2)
        return (_fit(label, name_width) + " " + tail).rstrip()

    def _slot_line(self, name, entry, width):
        _, bar_width = self._layout(width)
        total = entry["total"]
        done = entry["done"]
        rate = self._rate(entry["samples"])

        fraction = (done / total) if total else None
        if total and rate:
            eta = _format_clock((total - done) / rate) if rate > 0 else "--:--"
        else:
            eta = "--:--"

        return self._compose(
            label=name,
            size=_human_size(total if total else done),
            rate=(_human_size(rate) + "/s") if rate else "--",
            eta=eta,
            bar=_muncher_bar(fraction, bar_width, self.style, self._frame),
            pct="{0}%".format(int(fraction * 100)) if fraction is not None
            else "--",
            width=width,
        )

    def _idle_line(self, width):
        _, bar_width = self._layout(width)
        return self._compose(
            label="", size="", rate="", eta="",
            bar=" " * (bar_width + 2), pct="", width=width,
        )

    def _total_line(self, seen, width):
        _, bar_width = self._layout(width)
        expected = max(0, self._queued - self._skipped)
        rate = self._rate(self._overall)

        label = "Total {done}/{expected}".format(
            done=self._done, expected=expected if expected else "?"
        )
        if self._failed:
            label += " ({failed} failed)".format(failed=self._failed)
        if not self._walk_done:
            label += " so far"

        if self._walk_done and expected:
            fraction = min(1.0, self._done / expected)
            bar = _bar(fraction, bar_width)
            right = "{0}%".format(int(fraction * 100))
        else:
            bar = " " * (bar_width + 2)
            right = "--"

        return self._compose(
            label=label,
            size=_human_size(seen),
            rate=(_human_size(rate) + "/s") if rate else "--",
            eta=_format_clock(time.time() - self._started_at),
            bar=bar,
            pct=right,
            width=width,
        )

    def render(self, width=80, height=24):
        """The whole block, as a list of lines"""
        now = time.time()
        with self._lock:
            seen = self._sample(now)
            names = list(self._slot_names)
            files = {n: dict(e) for n, e in self._files.items()}

            if not self._fits(width):
                return [self._compact(seen, width)]
            # Leave the shell a row to write on
            if height < len(names) + 3:
                return [self._compact(seen, width)]

            lines = []
            for name in names:
                entry = files.get(name)
                if entry is None:
                    lines.append(self._idle_line(width))
                else:
                    lines.append(self._slot_line(name, entry, width))
            lines.append(self._total_line(seen, width))
            return lines

    def _compact(self, seen, width):
        """One line, for a terminal too small for the block"""
        active = len(self._files)
        rate = self._rate(self._overall)
        parts = ["{0} done".format(self._done)]
        if self._failed:
            parts.append("{0} failed".format(self._failed))
        parts.append("{0} active".format(active))
        parts.append(_human_size(seen))
        if rate:
            parts.append(_human_size(rate) + "/s")
        line = " · ".join(parts)
        return line[:max(0, width - 1)]

    # -- terminal ----------------------------------------------------

    def _write(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except Exception:
            pass

    def clear(self):
        """Wipe the block so ordinary log output lands on clean rows"""
        if not self._painted or not self.enabled():
            self._painted = 0
            return
        out = ["\033[{0}A".format(self._painted)]
        out.extend(["\r\033[K\n"] * self._painted)
        out.append("\033[{0}A".format(self._painted))
        self._painted = 0
        self._write("".join(out))

    def paint(self):
        if not self.enabled():
            return
        try:
            size = shutil.get_terminal_size((80, 24))
            width, height = size.columns, size.lines
        except Exception:
            width, height = 80, 24

        self._frame += 1
        lines = self.render(width, height)

        out = []
        if self._painted:
            out.append("\033[{0}A".format(self._painted))
        for line in lines:
            out.append("\r\033[K" + line[:width - 1] + "\n")

        extra = self._painted - len(lines)
        if extra > 0:
            out.extend(["\r\033[K\n"] * extra)
            out.append("\033[{0}A".format(extra))

        self._painted = len(lines)
        self._write("".join(out))

    def _loop(self):
        while not self._stop.is_set():
            self.paint()
            self._stop.wait(self.interval)


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _content_range_total(response):
    """Total size out of a `Content-Range: bytes 100-999/1000` header"""
    try:
        raw = response.headers.get("Content-Range")
    except Exception:
        return None
    if not raw or "/" not in raw:
        return None
    return _coerce_size(raw.rsplit("/", 1)[-1].strip())


def _retry_after_seconds(response, default=DEFAULT_COOLDOWN):
    """Retry-After is either a number of seconds or an http date"""
    raw = None
    try:
        raw = response.headers.get("Retry-After")
    except Exception:
        return default

    if not raw:
        return default

    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        pass

    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return default
    if when is None:
        return default

    if when.tzinfo is None:
        delta = when - datetime.datetime.now()
    else:
        delta = when - datetime.datetime.now(when.tzinfo)
    return max(0, int(delta.total_seconds()))


def _clean_name(dirty_str):
    allowed_chars = (" ", "_", ".", "-", "[", "]")
    clean = []
    for c in dirty_str.replace("+", "_").replace(":", " -"):
        if c.isalpha() or c.isdigit() or c in allowed_chars:
            clean.append(c)

    return "".join(clean).strip().rstrip(".")


def _human_size(num_bytes):
    """Format a byte count the way a human would read it"""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            if unit == "B":
                return "{size:.0f} {unit}".format(size=size, unit=unit)
            return "{size:.2f} {unit}".format(size=size, unit=unit)
        size /= 1024


def _file_ext(filename):
    """Lowercase extension of a filename, or '' when it has none"""
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def _struct_filename(file_type):
    """Filename of a download_struct entry, or None if it has no url"""
    if "url" not in file_type or "web" not in file_type["url"]:
        return None
    return file_type["url"]["web"].split("?")[0].split("/")[-1]


def _order_error(order):
    """Best explanation available for an order with no product data"""
    if not isinstance(order, dict):
        return "the api returned {kind}, not an order".format(
            kind=type(order).__name__
        )

    for key in ("_errors", "error_code", "error", "message"):
        if order.get(key):
            return "the api said {detail}".format(detail=order[key])

    known = ", ".join(sorted(order.keys())) or "nothing"
    return "no product data in the response (keys: {known})".format(known=known)


def _coerce_size(raw_size):
    """The api is not consistent about the type used for file sizes"""
    if raw_size is None:
        return None
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


class DownloadLibrary:
    def __init__(
        self,
        library_path,
        cookie_path=None,
        cookie_auth=None,
        progress_bar=False,
        ext_include=None,
        ext_exclude=None,
        platform_include=None,
        prefer_format=None,
        purchase_keys=None,
        trove=False,
        update=False,
        dry_run=False,
        print_urls=False,
        jobs=1,
        retries=5,
        progress_style=DEFAULT_PROGRESS_STYLE,
    ):
        self.library_path = library_path
        self.jobs = max(1, int(jobs))
        self.retries = max(0, int(retries))
        self.progress_bar = progress_bar
        # Several \r progress bars writing at once is unreadable, so the
        # per-file bar is for sequential runs. In parallel one reporter
        # thread owns a single status line for the whole pool
        self._show_bar = progress_bar and self.jobs == 1
        self._show_status = progress_bar and self.jobs > 1
        self.progress_style = progress_style
        self._progress = (
            ProgressReporter(slots=self.jobs, style=progress_style)
            if self._show_status
            else None
        )

        self._queue = None
        self._workers = []
        self._stop = threading.Event()
        self._cache_lock = threading.Lock()
        self._thread_local = threading.local()

        # A 429 seen by any one worker pauses all of them: retrying per
        # request just means the other workers keep hammering while one
        # of them politely backs off
        self._cooldown_until = 0.0
        self._cooldown_lock = threading.Lock()
        self._throttled_count = 0

        self._notice_lock = threading.Lock()
        self._range_unsupported = False

        self.ext_include = (
            [] if ext_include is None else list(map(str.lower, ext_include))
        )
        self.ext_exclude = (
            [] if ext_exclude is None else list(map(str.lower, ext_exclude))
        )

        if platform_include is None or "all" in platform_include:
            # if 'all', then do not need to use this check
            platform_include = []
        self.platform_include = list(map(str.lower, platform_include))

        # Ordered: the first format with a match wins, so the last entry
        # acts as the default
        self.prefer_format = (
            []
            if prefer_format is None
            else [ext.lower().lstrip(".") for ext in prefer_format]
        )

        self.purchase_keys = purchase_keys
        self.trove = trove
        self.update = update
        self.dry_run = dry_run
        self.print_urls = print_urls

        # Tally of everything a real run would fetch, filled in by
        # _record_pending_download while self.dry_run is True
        self.pending_downloads = []
        self.unexpanded_asmjs = 0
        self.skipped_orders = []
        self._current_bundle = ""

        self.session = self._configure_session(requests.Session())
        if cookie_path:
            try:
                cookie_jar = http.cookiejar.MozillaCookieJar(cookie_path)
                cookie_jar.load()
                self.session.cookies = cookie_jar
            except http.cookiejar.LoadError:
                # Still support the original cookie method
                with open(cookie_path, "r") as f:
                    self.session.headers.update({"cookie": f.read().strip()})
        elif cookie_auth:
            self.session.headers.update(
                {"cookie": "_simpleauth_sess={}".format(cookie_auth)}
            )

    def start(self):
        self.cache_file = os.path.join(self.library_path, ".cache.json")
        self.cache_data = self._load_cache_data(self.cache_file)
        self.purchase_keys = (
            self.purchase_keys if self.purchase_keys else self._get_purchase_keys()
        )

        previous_handler = self._install_interrupt_handler()
        try:
            # One handler for the whole run, not just the walk: a Ctrl-C
            # is at least as likely to land while waiting on the workers
            # as during traversal, and it has to be caught either way
            try:
                self._start_workers()

                if self.trove is True:
                    logger.info("Only checking the Humble Trove...")
                    self._current_bundle = "Humble Trove"
                    for product in self._get_trove_products():
                        title = _clean_name(product["human-name"])
                        self._process_trove_product(title, product)
                else:
                    for order_id in self.purchase_keys:
                        self._process_order_id(order_id)

                if self._progress is not None:
                    # Everything is queued, so the totals have a real
                    # denominator now and can show a bar
                    self._progress.traversal_finished()

                self._stop_workers()

                if self._stop.is_set():
                    # A sequential transfer was interrupted
                    sys.exit(INTERRUPT_EXIT_CODE)
            except KeyboardInterrupt:
                self._stop.set()
                # Bounded: transfers check the stop flag between chunks,
                # so this is quick, and anything that will not stop gets
                # left behind rather than holding the run open
                self._stop_workers(timeout=SHUTDOWN_GRACE)
                logger.warning("Interrupted, nothing further was downloaded")
                sys.exit(INTERRUPT_EXIT_CODE)
        finally:
            self._restore_interrupt_handler(previous_handler)

        if self.dry_run is True:
            self._log_dry_run_summary()

        if self._throttled_count:
            logger.warning(
                "Throttled {count} time(s) during this run. Lower --jobs "
                "if it keeps happening".format(count=self._throttled_count)
            )

        if self.skipped_orders:
            logger.warning(
                "{count} order(s) were skipped and nothing from them was "
                "checked: {keys}".format(
                    count=len(self.skipped_orders),
                    keys=" ".join(self.skipped_orders),
                )
            )

    def _hard_exit(self):
        """Leave now, without waiting on anything that might be stuck.

        os._exit skips interpreter shutdown, which is the point: nothing
        it would wait for can keep us here. Part files are left where
        they are and the next run truncates them.
        """
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(INTERRUPT_EXIT_CODE)

    def _install_interrupt_handler(self):
        """First Ctrl-C asks for a tidy stop, second one insists.

        Signal handlers only run on the main thread, so this is where
        both presses are seen no matter which thread is transferring.
        """
        if threading.current_thread() is not threading.main_thread():
            return None

        def handler(signum, frame):
            if self._stop.is_set():
                if self._progress is not None:
                    self._progress.clear()
                sys.stderr.write(
                    "\nQuitting now. Partly downloaded files are left "
                    "behind and will be redone next run.\n"
                )
                self._hard_exit()
                return

            self._stop.set()
            if self._progress is not None:
                self._progress.clear()
            sys.stderr.write(
                "\nStopping. Waiting for downloads in flight, press "
                "Ctrl-C again to quit immediately.\n"
            )
            sys.stderr.flush()
            # Same as the default handler, so anything the main thread
            # is blocked on unwinds exactly as it used to
            raise KeyboardInterrupt

        try:
            return signal.signal(signal.SIGINT, handler)
        except ValueError:
            # Not the main thread of the main interpreter
            return None

    def _restore_interrupt_handler(self, previous):
        if previous is None:
            return
        try:
            signal.signal(signal.SIGINT, previous)
        except (ValueError, TypeError):
            pass

    def _configure_session(self, session):
        """Transport level retries, plus a user agent that says who we
        are. The default python-requests one gets treated more harshly
        by a lot of edge configurations
        """
        session.headers.update({"User-Agent": USER_AGENT})

        if self.retries == 0:
            return session

        retry_args = dict(
            total=self.retries,
            connect=self.retries,
            read=self.retries,
            status=self.retries,
            backoff_factor=1,
            status_forcelist=RETRY_STATUSES,
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        try:
            retry = Retry(allowed_methods=frozenset(["GET", "HEAD"]), **retry_args)
        except TypeError:
            # urllib3 < 1.26 spelled it differently
            retry = Retry(method_whitelist=frozenset(["GET", "HEAD"]), **retry_args)

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_maxsize=max(10, self.jobs),
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _wait_out_cooldown(self):
        """Block until any pool wide throttle pause has expired"""
        while not self._stop.is_set():
            with self._cooldown_lock:
                remaining = self._cooldown_until - time.time()
            if remaining <= 0:
                return
            # Woken early if the run is interrupted
            self._stop.wait(min(remaining, 1.0))

    def _enter_cooldown(self, seconds, remote_file):
        """Pause every worker, not just the one that got the 429"""
        with self._cooldown_lock:
            self._throttled_count += 1
            now = time.time()
            until = now + seconds
            if until <= self._cooldown_until:
                # Another worker already called for a longer pause
                return
            # Workers refused at the same moment would otherwise each
            # announce the same pause
            announce = self._cooldown_until <= now or (
                until - self._cooldown_until > 1
            )
            self._cooldown_until = until

        if not announce:
            return

        logger.warning(
            "Throttled by the server (429) on {name}, pausing all "
            "downloads for {seconds}s".format(
                name=os.path.basename(remote_file.split("?")[0]),
                seconds=seconds,
            )
        )

    def _start_workers(self):
        if self.jobs == 1 or self.dry_run is True:
            # Nothing to parallelise: a dry run makes no downloads
            return

        logger.info(
            "Downloading with {jobs} parallel jobs".format(jobs=self.jobs)
        )
        if self._progress is not None:
            self._progress.start()

        # Bounded, so traversal cannot run far ahead of the downloads
        self._queue = queue.Queue(maxsize=self.jobs * 4)
        for _ in range(self.jobs):
            worker = threading.Thread(target=self._worker, daemon=True)
            worker.start()
            self._workers.append(worker)

    def _stop_workers(self, timeout=None):
        if self._queue is None:
            return

        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                # Workers time out on get() and notice _stop anyway
                pass

        deadline = None if timeout is None else time.time() + timeout
        for worker in self._workers:
            if deadline is None:
                worker.join()
            else:
                worker.join(timeout=max(0, deadline - time.time()))

        stubborn = [worker for worker in self._workers if worker.is_alive()]

        self._workers = []
        self._queue = None

        if self._progress is not None:
            self._progress.stop()

        if stubborn:
            # A transfer is wedged somewhere uninterruptible. The user
            # asked to stop, so stop
            logger.warning(
                "{count} download(s) did not stop within {grace}s, "
                "quitting anyway".format(
                    count=len(stubborn), grace=timeout
                )
            )
            self._hard_exit()

    def _worker(self):
        while True:
            try:
                # Time limited so a shutdown is noticed even when the
                # sentinel cannot be delivered past a full queue
                job = self._queue.get(timeout=0.25)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue

            try:
                if job is None:
                    return
                if self._stop.is_set():
                    continue
                self._run_download_job(job)
            except Exception:
                logger.exception(
                    "Failed to download {remote_file}".format(
                        remote_file=job.get("remote_file")
                    )
                )
            finally:
                self._queue.task_done()

    def _session(self):
        """requests.Session is not documented as thread safe, so every
        worker thread gets its own, built from the authenticated one
        """
        if self._queue is None:
            return self.session

        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._new_session()
            self._thread_local.session = session
        return session

    def _new_session(self):
        session = self._configure_session(requests.Session())
        session.headers.update(self.session.headers)
        session.cookies.update(self.session.cookies)
        return session

    def _get_trove_download_url(self, machine_name, web_name):
        try:
            sign_r = self.session.post(
                "https://www.humblebundle.com/api/v1/user/download/sign",
                data={
                    "machine_name": machine_name,
                    "filename": web_name,
                },
            )
        except Exception:
            logger.error(
                "Failed to get download url for trove product {title}".format(
                    title=web_name
                )
            )
            return None

        logger.debug("Signed url response {sign_r}".format(sign_r=sign_r))
        if sign_r.json().get("_errors") == "Unauthorized":
            logger.critical("Your account does not have access to the Trove")
            sys.exit()
        signed_url = sign_r.json()["signed_url"]
        logger.debug("Signed url {signed_url}".format(signed_url=signed_url))
        return signed_url

    def _process_trove_product(self, title, product):
        for platform, download in product["downloads"].items():
            # Sometimes the name has a dir in it
            # Example is "Broken Sword 5 - the Serpent's Curse"
            # Only the windows file has a dir like
            # "revolutionsoftware/BS5_v2.2.1-win32.zip"
            if self._should_download_platform(platform) is False:
                logger.info(
                    "Skipping {platform} for {product_title}".format(
                        platform=platform, product_title=title
                    )
                )
                continue

            web_name = download["url"]["web"].split("/")[-1]
            if self._should_download_file_by_ext_and_log(web_name) is False:
                continue

            cache_file_key = "trove:{name}".format(name=web_name)
            file_info = {
                "uploaded_at": (
                    download.get("uploaded_at")
                    or download.get("timestamp")
                    or product.get("date_added", "0")
                ),
                "md5": download.get("md5", "UNKNOWN_MD5"),
            }
            cache_file_info = self.cache_data.get(cache_file_key, {})

            if cache_file_info != {} and self.update is not True:
                # Do not care about checking for updates at this time
                continue

            if file_info["uploaded_at"] != cache_file_info.get(
                "uploaded_at"
            ) and file_info["md5"] != cache_file_info.get("md5"):
                if self.dry_run is True:
                    # The trove api hands us the size up front, so only pay
                    # for the signing request when the url is being printed
                    signed_url = None
                    if self.print_urls is True:
                        signed_url = self._get_trove_download_url(
                            download["machine_name"],
                            web_name,
                        )
                    self._record_pending_download(
                        signed_url,
                        os.path.join("Humble Trove", title, web_name),
                        _coerce_size(
                            download.get("file_size") or download.get("size")
                        ),
                    )
                    continue

                product_folder = os.path.join(self.library_path, "Humble Trove", title)
                # Create directory to save the files to
                try:
                    os.makedirs(product_folder)
                except OSError:
                    pass
                local_filename = os.path.join(
                    product_folder,
                    web_name,
                )
                signed_url = self._get_trove_download_url(
                    download["machine_name"],
                    web_name,
                )
                if signed_url is None:
                    # Failed to get signed url. Error logged in fn
                    continue

                try:
                    product_r = self.session.get(signed_url, stream=True)
                except Exception:
                    logger.error(
                        "Failed to get trove product {title}".format(title=web_name)
                    )
                    continue

                if "uploaded_at" in cache_file_info:
                    uploaded_at = time.strftime(
                        "%Y-%m-%d", time.localtime(int(cache_file_info["uploaded_at"]))
                    )
                else:
                    uploaded_at = None

                self._process_download(
                    product_r,
                    cache_file_key,
                    file_info,
                    local_filename,
                    rename_str=uploaded_at,
                    remote_file=signed_url,
                )

    def _get_trove_products(self):
        trove_products = []
        idx = 0
        trove_base_url = "https://www.humblebundle.com/client/catalog?index={idx}"
        while True:
            logger.debug(
                "Collecting trove product data from api pg:{idx} ...".format(idx=idx)
            )
            trove_page_url = trove_base_url.format(idx=idx)
            try:
                trove_r = self.session.get(trove_page_url)
            except Exception:
                logger.error("Failed to get products from Humble Trove")
                return []

            page_content = trove_r.json()

            if len(page_content) == 0:
                break

            trove_products.extend(page_content)
            idx += 1

        return trove_products

    def _process_order_id(self, order_id):
        order_url = "https://www.humblebundle.com/api/v1/order/{order_id}?all_tpkds=true".format(
            order_id=order_id
        )
        try:
            order_r = self.session.get(
                order_url,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
            )
        except Exception:
            logger.error("Failed to get order key {order_id}".format(order_id=order_id))
            return

        logger.debug("Order request: {order_r}".format(order_r=order_r))

        try:
            order = order_r.json()
        except ValueError:
            self._skip_order(
                order_id,
                order_r,
                "the response was not json (is the cookie still valid?)",
            )
            return

        if not isinstance(order, dict) or "product" not in order:
            self._skip_order(order_id, order_r, _order_error(order))
            return

        bundle_title = _clean_name(order["product"]["human_name"])
        self._current_bundle = bundle_title
        logger.info("Checking bundle: " + str(bundle_title))
        for product in order.get("subproducts", []):
            self._process_product(order_id, bundle_title, product)

    def _skip_order(self, order_id, order_r, reason):
        """One unusable order must not take the whole run down with it"""
        self.skipped_orders.append(order_id)
        logger.error(
            "Skipping order {order_id} (http {status_code}): {reason}".format(
                order_id=order_id,
                status_code=order_r.status_code,
                reason=reason,
            )
        )

    def _rename_old_file(self, local_filename, append_str):
        # Check if older file exists, if so rename
        if os.path.isfile(local_filename) is True:
            filename_parts = local_filename.rsplit(".", 1)
            new_name = "{name}_{append_str}.{ext}".format(
                name=filename_parts[0], append_str=append_str, ext=filename_parts[1]
            )
            os.rename(local_filename, new_name)
            logger.info("Renamed older file to {new_name}".format(new_name=new_name))

    def _select_by_format(self, file_types, product_title, platform):
        """Narrow a platform's files down to the preferred format.

        Tries each --prefer-format extension in order and keeps every
        file of the first one that matches, so multi part items (a comic
        split across volumes) survive intact. When none of the preferred
        formats are present, falls back to the single largest file.
        Entries without a downloadable url (asm.js games, external
        links) are never dropped, since they have no format to compare.
        """
        if not self.prefer_format:
            return file_types

        passthrough = []
        candidates = []
        for file_type in file_types:
            filename = _struct_filename(file_type)
            if filename is None:
                passthrough.append(file_type)
            elif self._should_download_file_by_ext_and_log(filename) is True:
                candidates.append((file_type, filename, _file_ext(filename)))

        if len(candidates) < 2:
            # Nothing to choose between
            return passthrough + [entry[0] for entry in candidates]

        for wanted in self.prefer_format:
            chosen = [entry for entry in candidates if entry[2] == wanted]
            if chosen:
                self._log_format_choice(
                    product_title, platform, wanted, candidates, chosen
                )
                return passthrough + [entry[0] for entry in chosen]

        # No preferred format available, so take the biggest file there is
        largest = max(
            candidates,
            key=lambda entry: _coerce_size(entry[0].get("file_size")) or 0,
        )
        self._log_format_choice(
            product_title, platform, None, candidates, [largest]
        )
        return passthrough + [largest[0]]

    def _log_format_choice(
        self, product_title, platform, wanted, candidates, chosen
    ):
        dropped = [
            entry[1] for entry in candidates if entry not in chosen
        ]
        if not dropped:
            return
        if wanted is None:
            reason = "no preferred format available, taking the largest file"
        else:
            reason = "preferring {wanted}".format(wanted=wanted)
        logger.info(
            "{product_title} [{platform}]: {reason}, skipping {dropped}".format(
                product_title=product_title,
                platform=platform,
                reason=reason,
                dropped=", ".join(dropped),
            )
        )

    def _process_product(self, order_id, bundle_title, product):
        product_title = _clean_name(product["human_name"])
        # Get all types of download for a product
        for download_type in product["downloads"]:
            if self._should_download_platform(download_type["platform"]) is False:
                logger.info(
                    "Skipping {platform} for {product_title}".format(
                        platform=download_type["platform"], product_title=product_title
                    )
                )
                continue

            product_folder = os.path.join(
                self.library_path, bundle_title, product_title
            )
            # Create directory to save the files to
            if self.dry_run is False:
                try:
                    os.makedirs(product_folder)
                except OSError:
                    pass

            # Download each file type of a product
            for file_type in self._select_by_format(
                download_type["download_struct"],
                product_title,
                download_type["platform"],
            ):
                try:
                    if "url" in file_type and "web" in file_type["url"]:
                        # downloadable URL
                        url = file_type["url"]["web"]

                        url_filename = url.split("?")[0].split("/")[-1]

                        if (
                            self._should_download_file_by_ext_and_log(url_filename)
                            is False
                        ):
                            continue

                        cache_file_key = order_id + ":" + url_filename
                        try:
                            self._check_cache_and_download(
                                cache_file_key,
                                url,
                                product_folder,
                                url_filename,
                                file_size=_coerce_size(file_type.get("file_size")),
                            )
                        except FileExistsError:
                            continue
                        except Exception:
                            logger.exception("Failed to download {url}".format(url=url))
                    elif "asm_config" in file_type:
                        # asm.js game playable directly in the browser
                        game_name = file_type["asm_config"]["display_item"]
                        local_folder = os.path.join(product_folder, game_name)
                        # Create directory to save the files to
                        if self.dry_run is False:
                            try:
                                os.makedirs(local_folder, exist_ok=True)  # noqa: E701
                            except OSError:
                                pass  # noqa: E701

                        # get the HTML file that presents the game, used in the Humble web interface iframe
                        asmjs_html_filename = game_name + ".html"
                        asmjs_local_html_filename = game_name + ".local.html"
                        cache_file_key = order_id + ":" + asmjs_html_filename
                        # game_name might be "game" or "game_asm" but the path to the file here always uses the "game_asm" version
                        game_asm_name = file_type["asm_manifest"]["asmFile"].split("/")[
                            2
                        ]
                        asmjs_url = (
                            "https://www.humblebundle.com/play/asmjs/"
                            + game_asm_name
                            + "/"
                            + order_id
                        )

                        if (
                            self._should_download_file_by_ext_and_log(
                                asmjs_html_filename
                            )
                            is False
                        ):
                            continue

                        if self.dry_run is True:
                            # The data files this game needs are listed
                            # inside the html page, which a dry run does
                            # not fetch, so only the page itself is sized
                            try:
                                self._check_cache_and_download(
                                    cache_file_key,
                                    asmjs_url,
                                    local_folder,
                                    asmjs_html_filename,
                                )
                            except FileExistsError:
                                pass
                            except Exception:
                                logger.exception(
                                    "Failed to check {asmjs_url}".format(
                                        asmjs_url=asmjs_url
                                    )
                                )
                            self.unexpanded_asmjs += 1
                            continue

                        downloaded = False
                        try:
                            # Not queued: the manifest is read out of this
                            # page, so it has to be on disk before the
                            # data files below can even be named
                            downloaded = self._check_cache_and_download(
                                cache_file_key,
                                asmjs_url,
                                local_folder,
                                asmjs_html_filename,
                                synchronous=True,
                            )
                        except FileExistsError:
                            pass  # we should download the asm/data files even if the html file was previously downloaded
                        except Exception:
                            logger.exception(
                                "Failed to download {asmjs_url}".format(
                                    asmjs_url=asmjs_url
                                )
                            )
                            continue

                        # read from the html file a version of file_type['asm_manifest'] with HMAC/etc auth params on the URLs
                        with open(
                            os.path.join(local_folder, asmjs_html_filename), "r"
                        ) as asmjs_html:
                            asmjs_page = parsel.Selector(text=asmjs_html.read())
                            asm_player_data_text = asmjs_page.css(
                                "#webpack-asm-player-data::text"
                            ).get()  # noqa: E501
                            asm_player_data = json.loads(asm_player_data_text)

                        if downloaded:
                            # create the local playable version of the html file
                            # by replacing remote manifest URLs with the local filename
                            try:
                                with open(
                                    os.path.join(local_folder, asmjs_html_filename), "r"
                                ) as asmjs_html:
                                    with open(
                                        os.path.join(
                                            local_folder, asmjs_local_html_filename
                                        ),
                                        "w",
                                    ) as asmjs_local_html:
                                        for line in asmjs_html:
                                            for (
                                                local_filename,
                                                remote_file,
                                            ) in asm_player_data["asmOptions"][
                                                "manifest"
                                            ].items():
                                                line = line.replace(
                                                    f'"{local_filename}": "{remote_file}"',
                                                    f'"{local_filename}": "{local_filename}"',
                                                )
                                            asmjs_local_html.write(line)
                            except Exception:
                                logger.exception(
                                    "Failed to create local version of {asmjs_html_filename}".format(
                                        asmjs_html_filename=asmjs_html_filename
                                    )
                                )

                        # TODO deduplicate these files? Osmos example has 3 unique files and 2 dupes with different names
                        for local_filename, remote_file in asm_player_data[
                            "asmOptions"
                        ]["manifest"].items():
                            cache_file_key = (
                                order_id + ":" + game_name + ":" + local_filename
                            )
                            try:
                                self._check_cache_and_download(
                                    cache_file_key,
                                    remote_file,
                                    local_folder,
                                    local_filename,
                                )
                            except FileExistsError:
                                continue
                            except Exception:
                                logger.exception(
                                    "Failed to download {url}".format(url=url)
                                )
                                continue

                    elif "external_link" in file_type:
                        logger.info(
                            "External url found: {bundle_title}/{product_title} : {url}".format(
                                bundle_title=bundle_title,
                                product_title=product_title,
                                url=file_type["external_link"],
                            )
                        )

                    else:
                        logger.info(
                            "No downloadable url(s) found: {bundle_title}/{product_title}".format(
                                bundle_title=bundle_title, product_title=product_title
                            )
                        )
                        logger.info(file_type)
                        continue
                except Exception:
                    logger.exception(
                        "Failed to download this 'file':\n{file_type}".format(
                            file_type=file_type
                        )
                    )
                    continue

    def _log_url(self, url):
        if self.print_urls is True and url:
            # Straight to stdout, one per line, so the list stays usable
            # when piped into another tool. All logging goes to stderr
            print(url)

    def _get_remote_headers(self, remote_file):
        """Headers only, so nothing is transferred to size up a file"""
        for _ in range(self.retries + 1):
            self._wait_out_cooldown()
            if self._stop.is_set():
                return None

            try:
                head_r = self._session().head(remote_file, allow_redirects=True)
            except Exception:
                logger.debug(
                    "Failed to get headers for {remote_file}".format(
                        remote_file=remote_file
                    )
                )
                return None

            if head_r.status_code not in THROTTLE_STATUSES:
                break

            self._enter_cooldown(_retry_after_seconds(head_r), remote_file)
        else:
            return None

        if head_r.status_code != 200:
            logger.debug(
                "File unavailable {remote_file} status code {status_code}".format(
                    remote_file=remote_file, status_code=head_r.status_code
                )
            )
            return None

        return head_r.headers

    def _check_pending_download(
        self, remote_file, local_file, file_size, cache_file_info
    ):
        """Work out if a real run would download this file, and how big
        it is, without fetching any of its content
        """
        cached_modified = cache_file_info.get("url_last_modified")
        headers = None
        if file_size is None or cached_modified is not None:
            headers = self._get_remote_headers(remote_file)

        if headers is not None:
            if (
                cached_modified is not None
                and headers.get("Last-Modified") == cached_modified
            ):
                # Unchanged since the last run, a real run would skip it
                return

            if file_size is None:
                file_size = _coerce_size(headers.get("Content-Length"))

        self._record_pending_download(remote_file, local_file, file_size)

    def _record_pending_download(self, remote_file, local_file, file_size):
        self.pending_downloads.append(
            {
                "bundle": self._current_bundle,
                "local_file": local_file,
                "url": remote_file,
                "file_size": file_size,
            }
        )
        if file_size is None:
            logger.info(
                "Would download: {local_file} (size unknown)".format(
                    local_file=local_file
                )
            )
        else:
            logger.info(
                "Would download: {local_file} ({size})".format(
                    local_file=local_file, size=_human_size(file_size)
                )
            )
        self._log_url(remote_file)

    def _log_dry_run_summary(self):
        bundles = {}
        total_size = 0
        unknown_count = 0
        for pending in self.pending_downloads:
            bundle_size, bundle_files = bundles.get(pending["bundle"], (0, 0))
            file_size = pending["file_size"]
            if file_size is None:
                unknown_count += 1
                file_size = 0
            total_size += file_size
            bundles[pending["bundle"]] = (
                bundle_size + file_size,
                bundle_files + 1,
            )

        logger.info("\n" + ("=" * 60))
        logger.info("Dry run: nothing was downloaded")
        logger.info("=" * 60)

        for bundle, (bundle_size, bundle_files) in sorted(
            bundles.items(), key=lambda item: item[1][0], reverse=True
        ):
            logger.info(
                "{size:>10}  {files:>4} file(s)  {bundle}".format(
                    size=_human_size(bundle_size),
                    files=bundle_files,
                    bundle=bundle or "Unknown bundle",
                )
            )

        logger.info("-" * 60)
        logger.info(
            "{files} file(s) to download, {size} ({raw:,} bytes)".format(
                files=len(self.pending_downloads),
                size=_human_size(total_size),
                raw=total_size,
            )
        )
        if unknown_count > 0:
            logger.info(
                "{count} of those file(s) did not report a size, so the "
                "real total will be larger".format(count=unknown_count)
            )
        if self.unexpanded_asmjs > 0:
            logger.info(
                "{count} asm.js game(s) were not sized: their data files "
                "are only listed inside the game page, which a dry run "
                "does not download".format(count=self.unexpanded_asmjs)
            )

    def _update_cache_data(self, cache_file_key, file_info):
        # Update cache file with newest data so if the script
        # quits it can keep track of the progress.
        # The whole file is rewritten each time, so the mutation and the
        # write are held together under one lock: without it, parallel
        # jobs interleave and truncate each other's json
        with self._cache_lock:
            self.cache_data[cache_file_key] = file_info
            with open(self.cache_file, "w") as outfile:
                json.dump(
                    self.cache_data,
                    outfile,
                    sort_keys=True,
                    indent=4,
                )

    def _check_cache_and_download(
        self,
        cache_file_key,
        remote_file,
        local_folder,
        local_filename,
        file_size=None,
        synchronous=False,
    ):
        cache_file_info = self.cache_data.get(cache_file_key, {})

        if cache_file_info != {} and self.update is not True:
            # Do not care about checking for updates at this time
            raise FileExistsError

        if self.dry_run is True:
            self._check_pending_download(
                remote_file,
                os.path.join(local_folder, local_filename),
                file_size,
                cache_file_info,
            )
            return False

        job = {
            "cache_file_key": cache_file_key,
            "remote_file": remote_file,
            "local_folder": local_folder,
            "local_filename": local_filename,
            "cache_file_info": cache_file_info,
        }

        if self._progress is not None:
            self._progress.file_queued()

        if self._queue is not None and synchronous is False:
            # Hand it to a worker. Blocks once the queue is full, which
            # keeps traversal from running away from the downloads
            self._queue.put(job)
            return False

        return self._run_download_job(job)

    def _note_skip(self):
        """Queued, then turned out not to need downloading"""
        if self._progress is not None:
            self._progress.file_skipped()

    def _get_with_backoff(self, remote_file, stream=True, range_start=None):
        """GET that honours the pool wide cooldown and handles a 429.

        Connection errors and 5xx are retried inside the session's
        transport adapter. A 429 is handled here instead, so that every
        worker pauses rather than only the one that was refused.

        range_start asks the server to resume from that byte. Whether it
        agrees is its business: the caller checks the status code.
        """
        headers = None
        if range_start:
            headers = {"Range": "bytes={start}-".format(start=range_start)}

        for _ in range(self.retries + 1):
            self._wait_out_cooldown()
            if self._stop.is_set():
                return None

            try:
                response = self._session().get(
                    remote_file, stream=stream, headers=headers
                )
            except Exception:
                logger.exception(
                    "Failed to download {remote_file}".format(
                        remote_file=remote_file
                    )
                )
                return None

            if response.status_code not in THROTTLE_STATUSES:
                return response

            self._enter_cooldown(_retry_after_seconds(response), remote_file)
            try:
                response.close()
            except Exception:
                pass

        logger.error(
            "Giving up on {remote_file}: still throttled after {count} "
            "attempts".format(
                remote_file=remote_file, count=self.retries + 1
            )
        )
        return None

    def _run_download_job(self, job):
        cache_file_key = job["cache_file_key"]
        remote_file = job["remote_file"]
        local_folder = job["local_folder"]
        local_filename = job["local_filename"]
        cache_file_info = job["cache_file_info"]

        remote_file_r = self._get_with_backoff(remote_file)
        if remote_file_r is None:
            self._note_skip()
            return False

        # Check to see if the file still exists
        if remote_file_r.status_code != 200:
            logger.debug(
                "File unavailable {remote_file} status code {status_code}".format(
                    remote_file=remote_file, status_code=remote_file_r.status_code
                )
            )
            self._note_skip()
            return False

        logger.debug(
            "Item request: {remote_file_r}, Url: {remote_file}".format(
                remote_file_r=remote_file_r, remote_file=remote_file
            )
        )
        file_info = {}
        if "Last-Modified" in remote_file_r.headers:
            file_info["url_last_modified"] = remote_file_r.headers["Last-Modified"]
            if file_info["url_last_modified"] == cache_file_info.get(
                "url_last_modified"
            ):
                self._note_skip()
                return False
        if "url_last_modified" in cache_file_info:
            last_modified = datetime.datetime.strptime(
                cache_file_info["url_last_modified"], "%a, %d %b %Y %H:%M:%S %Z"
            ).strftime("%Y-%m-%d")
        else:
            last_modified = None

        local_file = os.path.join(local_folder, local_filename)
        # Create directory to save the file to, which might not exist if there's a subdirectory included
        try:
            os.makedirs(os.path.dirname(local_file), exist_ok=True)  # noqa: E701
        except OSError:
            raise  # noqa: E701

        self._log_url(remote_file)

        return self._process_download(
            remote_file_r,
            cache_file_key,
            file_info,
            local_file,
            rename_str=last_modified,
            remote_file=remote_file,
        )

    def _process_download(
        self,
        open_r,
        cache_file_key,
        file_info,
        local_filename,
        rename_str=None,
        remote_file=None,
    ):
        started = time.time()
        # Content lands in <file>.part and is only moved into place once
        # it is whole, so an interrupted transfer can be resumed and can
        # never be mistaken for a finished file
        part_file = local_filename + ".part"
        label = os.path.basename(local_filename)
        written = 0
        try:
            if rename_str:
                self._rename_old_file(local_filename, rename_str)

            written = self._download_to_part(
                open_r, part_file, remote_file, local_filename
            )
            os.replace(part_file, local_filename)

        except (Exception, KeyboardInterrupt) as e:
            if self._show_bar:
                # Do not overwrite the progress bar on next print
                print()

            if isinstance(e, Interrupted):
                # Asked to stop, so this is not a failure worth shouting
                logger.debug(
                    "Stopped downloading {local_filename}".format(
                        local_filename=local_filename
                    )
                )
            else:
                logger.error(
                    "Failed to download file {local_filename}".format(
                        local_filename=local_filename
                    )
                )

            if self._progress is not None:
                self._progress.file_finished(label, ok=False)
                self._progress.clear()

            # Clean up the partial transfer. Any previously downloaded
            # copy of the file is left untouched
            _remove_quietly(part_file)

            if type(e).__name__ == "KeyboardInterrupt":
                # A worker cannot exit the process, so flag it and let
                # start() shut the run down once the workers are joined
                self._stop.set()
                if threading.current_thread() is threading.main_thread():
                    sys.exit(INTERRUPT_EXIT_CODE)

            result = False

        else:
            if self._show_bar:
                # Do not overwrite the progress bar on next print
                print()
            if "url_last_modified" not in file_info:
                # no Last-Modified header so we set the time of the current download
                # this will result in the file not being re-downloaded by default later
                file_info["url_last_modified"] = datetime.datetime.now().strftime(
                    "%a, %d %b %Y %H:%M:%S %Z"
                )
            if self._progress is not None:
                self._progress.file_finished(label, ok=True)

            self._update_cache_data(cache_file_key, file_info)
            self._log_completed(local_filename, written, time.time() - started)
            result = True

        finally:
            # Since its a stream connection, make sure to close it
            try:
                open_r.connection.close()
            except Exception:
                pass

        return result

    def _log_completed(self, local_filename, written, elapsed):
        """In parallel mode the start lines interleave, so say what
        finished, and how big it turned out to be
        """
        if self.jobs == 1:
            return
        if self._progress is not None:
            # Land this line on a clean row; the reporter repaints after
            self._progress.clear()
        logger.info(
            "Downloaded {name} ({size} in {elapsed:.1f}s)".format(
                name=os.path.basename(local_filename),
                size=_human_size(written or 0),
                elapsed=elapsed,
            )
        )

    def _download_to_part(self, response, part_file, remote_file, local_filename):
        """Stream into the part file, picking the transfer back up where
        it broke off when the server allows it.

        Returns the size of the finished part file.
        """
        logger.info(
            "Downloading: {local_filename}".format(local_filename=local_filename)
        )

        append = False
        already = 0
        total = _coerce_size(response.headers.get("Content-Length"))
        attempt = 0
        label = os.path.basename(local_filename)

        if self._progress is not None:
            self._progress.file_started(label, total)

        while True:
            try:
                self._stream_to_file(
                    response, part_file, append, already, total, label
                )
                return _file_size(part_file)
            except (KeyboardInterrupt, Interrupted):
                # Shutting down: nothing to resume, nothing to report
                raise
            except Exception as error:
                attempt += 1
                already = _file_size(part_file)
                if (
                    remote_file is None
                    or attempt > self.retries
                    or self._stop.is_set()
                ):
                    raise

                logger.warning(
                    "{name} stopped after {size} ({error}), picking it "
                    "back up".format(
                        name=os.path.basename(local_filename),
                        size=_human_size(already),
                        error=error,
                    )
                )

                resumed = self._reopen_for_resume(
                    remote_file, part_file, already
                )
                if resumed is None:
                    raise
                response, append, total = resumed
                if append is False:
                    already = 0
                if self._progress is not None:
                    # A resume changes what the whole file weighs
                    self._progress.file_started(label, total)
                    self._progress.file_progress(label, already)

    def _reopen_for_resume(self, remote_file, part_file, already):
        """Ask for the rest of the file.

        The server decides whether that is possible, and we believe the
        status code rather than assuming support: 206 means the Range was
        honoured and we append, 200 means it was ignored and the file
        starts again from nothing.

        Returns (response, append, total), or None if it cannot go on.
        """
        response = self._get_with_backoff(
            remote_file, range_start=already or None
        )
        if response is None:
            return None

        status = getattr(response, "status_code", 200)

        if status == 206:
            total = _content_range_total(response)
            if total is None:
                length = _coerce_size(response.headers.get("Content-Length"))
                total = None if length is None else already + length
            return response, True, total

        if status == 416:
            # We hold at least as much as is on offer, so the part file
            # is stale. Throw it away and take the file from the top
            _remove_quietly(part_file)
            response = self._get_with_backoff(remote_file)
            if response is None:
                return None
            if getattr(response, "status_code", 200) != 200:
                return None
            return (
                response,
                False,
                _coerce_size(response.headers.get("Content-Length")),
            )

        if status == 200:
            self._note_range_unsupported()
            return (
                response,
                False,
                _coerce_size(response.headers.get("Content-Length")),
            )

        return None

    def _note_range_unsupported(self):
        with self._notice_lock:
            if self._range_unsupported:
                return
            self._range_unsupported = True

        logger.info(
            "The server ignored a Range request, so interrupted files "
            "start over instead of resuming"
        )

    def _stream_to_file(
        self, response, part_file, append, already, total, label=None
    ):
        """One pass of the transfer. `already` is what the part file
        holds when appending, so the progress bar counts the whole file
        """
        written = already
        with open(part_file, "ab" if append else "wb") as outfile:
            for data in response.iter_content(chunk_size=4096):
                if self._stop.is_set():
                    # Checked between chunks so a Ctrl-C stops a large
                    # transfer promptly instead of at the end of it
                    raise Interrupted()
                written += len(data)
                outfile.write(data)
                if self._show_bar:
                    self._draw_bar(written, total)
                elif self._progress is not None and label is not None:
                    self._progress.file_progress(label, written)

        if total is not None and written < total:
            raise ValueError(
                "Download did not complete, {written} of {total} bytes".format(
                    written=written, total=total
                )
            )
        if total is not None and written > total:
            if self._show_bar:
                print()
            logger.warning("Downloaded more content than expected")

        return written

    def _draw_bar(self, written, total):
        if total is None:  # no content length header
            print("\t{written}".format(written=written), end="\r")
            return

        pb_width = 50
        done = int(pb_width * written / total)
        print(
            "\t{percent}% [{filler}{space}]".format(
                percent=int(done * (100 / pb_width)),
                filler="=" * min(max(done, 0), pb_width),
                space=" " * min(max((pb_width - done), 0), pb_width),
            ),
            end="\r",
        )

    def _load_cache_data(self, cache_file):
        try:
            with open(cache_file, "r") as f:
                cache_data = json.load(f)
        except FileNotFoundError:
            cache_data = {}

        return cache_data

    def _get_purchase_keys(self):
        try:
            library_r = self.session.get("https://www.humblebundle.com/home/library")
        except Exception:
            logger.exception("Failed to get list of purchases")
            return []

        logger.debug("Library request: " + str(library_r))
        library_page = parsel.Selector(text=library_r.text)
        user_data = (
            library_page.css("#user-home-json-data").xpath("string()").extract_first()
        )
        if user_data is None:
            raise Exception("Unable to download user-data, cookies missing?")
        orders_json = json.loads(user_data)
        return orders_json["gamekeys"]

    def _should_download_platform(self, platform):
        platform = platform.lower()
        if self.platform_include and platform not in self.platform_include:
            return False
        return True

    def _should_download_file_by_ext_and_log(self, filename):
        if self._should_download_file_by_ext(filename) is False:
            logger.info("Skipping the file {filename}".format(filename=filename))
            return False
        return True

    def _should_download_file_by_ext(self, filename):
        return self._should_download_ext(_file_ext(filename))

    def _should_download_ext(self, ext):
        ext = ext.lower()
        if self.ext_include != []:
            return ext in self.ext_include
        elif self.ext_exclude != []:
            return ext not in self.ext_exclude
        return True
