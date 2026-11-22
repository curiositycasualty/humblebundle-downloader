"""DownloadLibrary: one run over a Humble library.

This module holds the run's lifecycle: setting up, the download workers,
-j auto, stopping, and the summary at the end. The rest of the class is
in mixins, one per concern:

- walk.py: going through the library bundle by bundle
- selection.py: which files are wanted
- transfer.py: downloading one file, resuming and checking it
- cache.py: .cache.json

and the helpers they share are in errors.py, files.py and web.py.
"""
import logging
import os
import queue
import signal
import sys
import threading
import time
import http.cookiejar

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import auto_jobs
from .auto_jobs import JobGate, JobTuner
from .cache import CacheMixin
from .errors import (
    FAILED_EXIT_CODE,
    INTERRUPT_EXIT_CODE,
    ChecksumMismatch,
    Interrupted,
    LibraryPathError,
    RepeatedMismatch,
    RunStopped,
)
from .events import Listener
from .files import _mount_hint, _remove_quietly, _write_fd
from .formatting import _human_duration, _human_size
from .humble_site import HumbleSite, SiteError, UrlSigner
from .selection import SelectionMixin
from .transfer import TransferMixin
from .walk import WalkMixin
from .web import RETRY_STATUSES, SITE_HOST, USER_AGENT, _jar_from_header, _looks_like_cookie_header

logger = logging.getLogger(__name__)

# What embedding code imports from here. The errors live in errors.py
# now, and stay importable from here as they always were
__all__ = [
    "DownloadLibrary",
    "ChecksumMismatch",
    "FAILED_EXIT_CODE",
    "INTERRUPT_EXIT_CODE",
    "Interrupted",
    "LibraryPathError",
    "RepeatedMismatch",
    "RunStopped",
]

# How many failed files the end of a run names, before "and N more"
FAILURES_LISTED = 20
# How long a first Ctrl-C waits for transfers to wind themselves up.
# Ordinary transfers stop at their next chunk, well inside a second, so
# this is only ever spent on one stuck somewhere it cannot notice
SHUTDOWN_GRACE = 300


class DownloadLibrary(WalkMixin, SelectionMixin, TransferMixin, CacheMixin):
    def __init__(
        self,
        library_path,
        cookie_path=None,
        cookie_auth=None,
        ext_include=None,
        ext_exclude=None,
        platform_include=None,
        prefer_format=None,
        purchase_keys=None,
        trove=False,
        vault=False,
        update=False,
        dry_run=False,
        print_urls=False,
        print_checksums=False,
        accept_smaller=False,
        jobs=1,
        retries=5,
        verify=True,
        shutdown_grace=None,
        log_skipped=True,
        listener=None,
    ):
        self.library_path = library_path
        # "auto" runs as many workers as it might ever want, and a gate
        # lets the number it has settled on download at once
        self.auto_jobs = isinstance(jobs, str) and jobs.lower() == "auto"
        self.jobs = auto_jobs.MAXIMUM if self.auto_jobs else max(1, int(jobs))
        self._gate = JobGate(auto_jobs.START if self.auto_jobs else self.jobs)
        self._tuner = JobTuner() if self.auto_jobs else None
        self._tuner_thread = None
        self._tuner_stop = threading.Event()
        # Every byte any worker writes, for the tuner to measure with
        self._bytes_moved = 0
        self._bytes_lock = threading.Lock()
        self.retries = max(0, int(retries))
        # Checked against what the api says, when it says anything
        self.verify = verify
        # Whether every file a filter passes over is announced
        self.log_skipped = log_skipped
        # How long a first Ctrl-C waits for transfers in flight. Read
        # from the module default when not given, so it stays one place
        # to change for anyone embedding this
        self.shutdown_grace = max(
            0.0,
            float(SHUTDOWN_GRACE if shutdown_grace is None else shutdown_grace),
        )
        # Whoever is showing the run. The engine only ever tells it what
        # happened (see events.py); how that looks is up to it
        self.events = listener if listener is not None else Listener()

        self._queue = None
        self._workers = []
        self._downloads_announced = False
        self._stop = threading.Event()
        self._paused = threading.Event()
        # Only ever waited on, for a short sleep that a stop can cut off
        self._paused_wake = threading.Event()
        self._skipping = set()
        self._cache_lock = threading.Lock()
        # The cache is written at most every CACHE_FLUSH_SECONDS, and
        # once more when the run ends, rather than after every file
        self._cache_dirty = False
        self._cache_written_at = 0.0
        self._thread_local = threading.local()

        # A 429 seen by any one worker pauses all of them: retrying per
        # request just means the other workers keep hammering while one
        # of them politely backs off
        self._cooldown_until = 0.0
        self._cooldown_lock = threading.Lock()
        self._throttled_count = 0

        self._notice_lock = threading.Lock()
        self._range_unsupported = False
        # Set by a worker when something has gone wrong that every
        # remaining file would hit too, so the run stops rather than
        # repeating the same error once per file
        self._fatal = None

        # Destinations already handed out this run. A repeated purchase
        # key would otherwise queue the same file twice, and in parallel
        # that means two workers writing one .part at the same time
        self._claimed = set()
        self._claim_lock = threading.Lock()

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
        self.vault = vault
        self.update = update
        self.dry_run = dry_run
        self.print_urls = print_urls
        self.print_checksums = print_checksums
        # Whether a much smaller new version may replace a file on disk
        self.accept_smaller = accept_smaller
        # (path, size before, size now, "downloaded" or "kept") for every
        # file much smaller than it was, for the end of the run
        self.smaller_files = []
        self._checksum_header_done = False

        # Tally of everything a real run would fetch, filled in by
        # _record_pending_download while self.dry_run is True
        self.pending_downloads = []
        self.unexpanded_asmjs = 0
        self.skipped_orders = []
        self._current_bundle = ""
        # Files already on disk at the expected size that the cache did
        # not know about, which is every file after the site rebuild:
        # bundles are keyed by new ids, so no old cache entry matches
        self.adopted_count = 0
        # Files Humble has replaced without updating its listing
        self.stale_listings = 0
        # (path, why) for every file this run gave up on, so the run can
        # say which, and exit non-zero rather than look like a success
        self.failed_files = []

        # The library as the rebuilt website presents it. Built in
        # start(), once the session has its cookies
        self.site = None
        self._signer = None

        self.session = self._configure_session(requests.Session())
        if cookie_path:
            try:
                cookie_jar = http.cookiejar.MozillaCookieJar(cookie_path)
                # Session cookies, which is what a sign in usually is,
                # are exported with no expiry or an expiry of 0. By
                # default the first are discarded and the second count
                # as expired, so the cookie that matters never loads.
                # A cookie that really is stale is just refused upstream
                cookie_jar.load(ignore_discard=True, ignore_expires=True)
                self.session.cookies = cookie_jar
            except http.cookiejar.LoadError:
                # Still support the original cookie method
                with open(cookie_path, "r") as f:
                    _jar_from_header(self.session.cookies, f.read().strip())
        elif cookie_auth:
            if _looks_like_cookie_header(cookie_auth):
                # A whole cookie header, name=value pairs and all, which
                # is how a cookie other than _simpleauth_sess gets in
                _jar_from_header(self.session.cookies, cookie_auth.strip())
            else:
                self.session.cookies.set(
                    "_simpleauth_sess", cookie_auth.strip(), domain=SITE_HOST
                )

    def _can_write(self, path):
        """Actually write something, rather than ask about permissions.

        os.access answers from the posix bits, which network shares,
        read only mounts and macOS access controls can all disagree with
        """
        probe = os.path.join(path, ".hbd-write-check")
        try:
            with open(probe, "w"):
                pass
        except OSError:
            return False
        finally:
            _remove_quietly(probe)
        return True

    def _check_library_path(self):
        """Confirm the library is usable before going near the network.

        Without this the first sign of trouble is an error from a worker
        thread, a few hundred files into a run, repeated once per file.
        """
        path = self.library_path
        shown = os.path.abspath(path)

        if os.path.exists(path) and not os.path.isdir(path):
            raise LibraryPathError(
                "Library path is not a folder: {shown}".format(shown=shown)
            )

        if not os.path.isdir(path):
            if self.dry_run is True:
                # A dry run writes nothing, so this is worth saying but
                # not worth stopping for
                logger.warning(
                    "Library path does not exist yet: {shown}".format(shown=shown)
                )
                return
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as error:
                raise LibraryPathError(
                    self._path_problem(shown, error.strerror or error)
                ) from error
            logger.info("Created library folder {shown}".format(shown=shown))
            return

        if self.dry_run is not True and not self._can_write(path):
            raise LibraryPathError(
                self._path_problem(shown, "cannot write to it")
            )

    @staticmethod
    def _path_problem(shown, reason):
        message = "Cannot use library path {shown}: {reason}".format(
            shown=shown, reason=reason
        )
        hint = _mount_hint(shown)
        if hint:
            message += " ({hint})".format(hint=hint)
        return message

    def start(self):
        began = time.monotonic()
        # The thread running the walk uses self.session; every other
        # thread gets one of its own (see _session)
        self._owner = threading.current_thread()
        try:
            self._run()
        finally:
            self._close_size_pool()
            # Last, however the run ended: finished, failed or interrupted
            self._log_runtime(time.monotonic() - began)

    def _log_runtime(self, seconds):
        line = "Took {0}".format(_human_duration(seconds))
        moved = self._bytes_moved
        if moved:
            line += ", downloading {0}".format(_human_size(moved))
            if seconds >= 1:
                line += " at {0}/s on average".format(_human_size(moved / seconds))
        logger.info(line)

    def _run(self):
        self._check_library_path()

        self.cache_file = os.path.join(self.library_path, ".cache.json")
        self.cache_data = self._load_cache_data(self.cache_file)
        if self.purchase_keys:
            self.purchase_keys = self._dedupe_keys(self.purchase_keys)

        if self.site is None:
            self.site = HumbleSite(
                self.session, session_factory=lambda: self._new_session()
            )
        if self._signer is None:
            self._signer = UrlSigner(self.site)

        previous_handler = self._install_interrupt_handler()
        try:
            # One handler for the whole run, not just the walk: a Ctrl-C
            # is at least as likely to land while waiting on the workers
            # as during traversal, and it has to be caught either way
            try:
                self._start_workers()
                if self.dry_run is not True:
                    self._downloads_announced = True
                    self.events.downloads_started(self._gate.limit)

                for order_id, order in self._all_orders():
                    if self._stop.is_set():
                        # Interrupted, or something a worker found that
                        # the rest of the run would hit too
                        break
                    self._process_order(order_id, order)

                # Everything is queued, so the totals have a real
                # denominator now and can show a bar
                self.events.traversal_finished()

                # A stop asked for from outside gets the same bounded
                # wait as a Ctrl-C: transfers stop at their next chunk,
                # and one that will not is left behind
                self._stop_workers(
                    timeout=self.shutdown_grace if self._stop.is_set() else None
                )

                if isinstance(self._fatal, Exception):
                    raise self._fatal
                if self._fatal:
                    raise LibraryPathError(self._fatal)

                if self._stop.is_set():
                    # Asked to stop from outside, or a sequential transfer
                    # was interrupted
                    logger.warning("Stopped, nothing further was downloaded")
                    raise RunStopped()
            except SiteError:
                # Reading the library failed partway, usually an expired
                # cookie. Downloads in flight get to finish their chunk
                self._stop.set()
                self._stop_workers(timeout=self.shutdown_grace)
                raise
            except KeyboardInterrupt:
                self._stop.set()
                # Bounded: transfers check the stop flag between chunks,
                # so this is quick, and anything that will not stop gets
                # left behind rather than holding the run open
                self._stop_workers(timeout=self.shutdown_grace)
                logger.warning("Interrupted, nothing further was downloaded")
                raise RunStopped() from None
        finally:
            # However the run ends, what it learned is kept
            try:
                self._flush_cache()
            except Exception as error:
                logger.error("Could not save the cache: {0}".format(error))
            self._restore_interrupt_handler(previous_handler)

        self.events.run_ending()
        if self.dry_run is True:
            self._log_dry_run_summary()

        if self._tuner is not None and self._tuner.best:
            rate, limit = self._tuner.best
            logger.info(
                "Finished at {now} downloads at once. The fastest seen was "
                "{rate}/s, with {limit}: -j {limit} is a good fixed "
                "setting for this connection".format(
                    now=self._gate.limit, rate=_human_size(rate), limit=limit
                )
            )

        if self.failed_files:
            logger.error(
                "{count} file(s) could not be downloaded, and will be tried "
                "again next run:".format(count=len(self.failed_files))
            )
            for local_file, reason in self.failed_files[:FAILURES_LISTED]:
                logger.error(
                    "  {path} ({reason})".format(
                        path=os.path.relpath(local_file, self.library_path),
                        reason=reason,
                    )
                )
            if len(self.failed_files) > FAILURES_LISTED:
                logger.error(
                    "  ...and {more} more".format(
                        more=len(self.failed_files) - FAILURES_LISTED
                    )
                )

        self._report_smaller()

        if self.stale_listings:
            logger.warning(
                "{count} file(s) were a different version from the one "
                "Humble lists, so did not match its checksums. They were "
                "kept, and their own checksums recorded in the cache".format(
                    count=self.stale_listings
                )
            )

        if self.adopted_count:
            logger.info(
                "{count} file(s) were already on disk at the right size and "
                "were recorded in the cache rather than downloaded "
                "again".format(count=self.adopted_count)
            )

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
        try:
            self._flush_cache(wait=2)
        except Exception:
            pass
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(INTERRUPT_EXIT_CODE)

    def request_stop(self):
        """Stop as tidily as a first Ctrl-C does: start nothing new, let
        transfers in flight stop at their next chunk, keep their part
        files to resume from, and save the cache. Safe from any thread.
        start() then raises RunStopped"""
        if self._stop.is_set():
            return
        self._stop.set()
        self.events.stopping()
        logger.warning("Stopping. Waiting for downloads in flight")

    # -- live controls, for the interface -----------------------------

    def set_jobs(self, limit):
        """Download `limit` files at once from now on, starting more
        workers if there are not enough. Taking over from -j auto stops
        it changing the number. Returns the number now in force, or None
        when the run is not downloading in parallel (-j 1, a dry run)"""
        if self._queue is None:
            return None
        limit = max(1, min(auto_jobs.MAXIMUM, int(limit)))
        if self._tuner_thread is not None:
            # Set by hand: -j auto no longer has a say
            self._tuner_stop.set()
            logger.info("-j auto stopped: the number at once is now set by hand")
        while len(self._workers) < limit:
            worker = threading.Thread(target=self._worker, daemon=True)
            worker.start()
            self._workers.append(worker)
        old = self._gate.limit
        self._gate.set_limit(limit)
        if limit != old:
            logger.info("Now downloading {new} at once, was {old}".format(new=limit, old=old))
            self.events.jobs_changed(limit)
        return limit

    @property
    def jobs_now(self):
        return self._gate.limit

    def pause(self):
        """Hold every transfer where it is, and start no new ones.
        Connections may time out while paused; each picks up where it
        was once resumed, as after any dropped connection"""
        if not self._paused.is_set():
            self._paused.set()
            logger.info("Paused")
            self.events.paused(True)

    def resume(self):
        if self._paused.is_set():
            self._paused.clear()
            logger.info("Resumed")
            self.events.paused(False)

    @property
    def is_paused(self):
        return self._paused.is_set()

    def skip(self, name):
        """Give up on the download of `name` (as the interface shows it)
        for this run. What it has is kept for the next run to carry on
        from, and it does not count as a failure"""
        with self._notice_lock:
            self._skipping.add(name)

    def _wait_while_paused(self, name=None):
        while self._paused.is_set() and not self._stop.is_set():
            if name is not None and name in self._skipping:
                return
            self._paused_wake.wait(0.2)

    def force_stop(self):
        """Leave now, as a second Ctrl-C does"""
        self._hard_exit()

    def _install_interrupt_handler(self):
        """First Ctrl-C asks for a tidy stop, second one insists.

        Signal handlers only run on the main thread, so this is where
        both presses are seen no matter which thread is transferring.
        """
        if threading.current_thread() is not threading.main_thread():
            return None

        def handler(signum, frame):
            self._stop.set()
            # The second press is the operating system's, not ours. A
            # Python handler only runs when the main thread gets to it,
            # and quitting from one meant taking the terminal lock and
            # writing through sys.stderr, either of which another thread
            # can be holding at the time. Then the press that was meant
            # to quit whatever state things are in would wait on them
            # instead. With the default action, the kernel ends the
            # process the moment it arrives
            try:
                signal.signal(signal.SIGINT, signal.SIG_DFL)
            except (ValueError, OSError):
                pass
            # Straight to the descriptor: no locks, no buffer another
            # thread might be in the middle of. The block is drawn afresh
            # below this rather than over it
            self.events.stopping()
            _write_fd(
                2,
                "\nStopping. Waiting for downloads in flight, press "
                "Ctrl-C again to quit immediately.\n",
            )
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


    def _start_workers(self):
        if self.jobs == 1 or self.dry_run is True:
            # Nothing to parallelise: a dry run makes no downloads
            return

        if self.auto_jobs:
            logger.info(
                "Downloading {start} at a time to begin with, finding the "
                "best number as it goes (up to {most})".format(
                    start=self._gate.limit, most=self.jobs
                )
            )
        else:
            logger.info(
                "Downloading with {jobs} parallel jobs".format(jobs=self.jobs)
            )

        # Bounded, so traversal cannot run far ahead of the downloads
        self._queue = queue.Queue(maxsize=self.jobs * 4)
        for _ in range(self.jobs):
            worker = threading.Thread(target=self._worker, daemon=True)
            worker.start()
            self._workers.append(worker)

        if self.auto_jobs:
            self._tuner_stop.clear()
            self._tuner_thread = threading.Thread(target=self._tune, daemon=True)
            self._tuner_thread.start()

    def _count_bytes(self, count):
        with self._bytes_lock:
            self._bytes_moved += count

    def _tune(self, interval=None, tick=1.0):
        """Measure total speed every so often and let the tuner move the
        number of downloads at once. A measurement only counts when every
        slot had something to download most of the time: a slow library
        read is no reason to think more at once would not help
        """
        interval = auto_jobs.INTERVAL if interval is None else interval
        started = time.time()
        with self._bytes_lock:
            moved = self._bytes_moved
        throttled = self._throttled_count
        samples = busy = 0

        while not self._tuner_stop.wait(tick):
            samples += 1
            if self._gate.active >= self._gate.limit:
                busy += 1
            now = time.time()
            if now - started < interval:
                continue

            with self._bytes_lock:
                total = self._bytes_moved
            rate = (total - moved) / (now - started)
            old = self._gate.limit
            new, reason = self._tuner.observe(
                rate,
                busy=busy >= 0.8 * samples,
                throttled=self._throttled_count > throttled,
            )
            if new != old:
                self._gate.set_limit(new)
                logger.info(
                    "Now downloading {new} at once, was {old}: {reason} "
                    "({rate}/s in total)".format(
                        new=new, old=old, reason=reason,
                        rate=_human_size(rate),
                    )
                )
            self.events.jobs_changed(new)

            started, moved = now, total
            throttled = self._throttled_count
            samples = busy = 0

    def _stop_workers(self, timeout=None):
        self._tuner_stop.set()
        if self._tuner_thread is not None:
            self._tuner_thread.join(timeout=2)
            self._tuner_thread = None
        if self._queue is None:
            self._announce_downloads_finished()
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

        self._announce_downloads_finished()

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

    def _announce_downloads_finished(self):
        if self._downloads_announced:
            self._downloads_announced = False
            self.events.downloads_finished()

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
                # Paused: nothing new starts until resumed
                self._wait_while_paused()
                if not self._gate.acquire(self._stop):
                    continue
                try:
                    self._run_download_job(job)
                finally:
                    self._gate.release()
            except Exception as error:
                logger.exception(
                    "Failed to download {remote_file}".format(
                        remote_file=job.get("remote_file")
                    )
                )
                self._record_failure(
                    os.path.join(job.get("local_folder", ""), job.get("local_filename", "")),
                    str(error) or type(error).__name__,
                )
            finally:
                self._queue.task_done()

    def _session(self):
        """requests.Session is not documented as thread safe, so every
        thread but the one running the walk gets its own, built from the
        authenticated one: download workers, and the threads that size
        up an item's formats side by side
        """
        if threading.current_thread() is getattr(self, "_owner", None) or (
            self._queue is None and getattr(self, "_owner", None) is None
        ):
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


