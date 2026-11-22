"""Downloading one file: requests with backoff, resuming, checksums, and what is kept"""

import datetime
import hashlib
import logging
import os
import threading
import time

from .errors import ChecksumMismatch, Interrupted, RepeatedMismatch, RunStopped, Skipped
from .files import (
    _coerce_size,
    _digest_of,
    _feed_hash_from_file,
    _file_size,
    _much_smaller,
    _remove_quietly,
    _resume_note,
    _resume_point,
    _write_resume_note,
)
from .formatting import _human_size
from .humble_site import NotSignedIn, SignedUrl
from .web import (
    THROTTLE_STATUSES,
    _content_range_total,
    _retry_after_seconds,
    _validator_for,
)

# Under the engine's name, as before the split, so anything filtering
# hbd's log by logger still sees all of it
logger = logging.getLogger("humblebundle_downloader.download_library")


# In the library, the running list of such files
SMALLER_REPORT = ".hbd-smaller-than-listed.tsv"


class TransferMixin:
    """Part of DownloadLibrary: see download_library.py. Uses the
    state DownloadLibrary.__init__ sets up"""

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

    def _get_remote_headers(self, remote_file):
        """Headers only, so nothing is transferred to size up a file"""
        head_r = self._request_with_backoff(
            "head", remote_file, quiet=True, allow_redirects=True
        )
        if head_r is None:
            return None

        if head_r.status_code != 200:
            logger.debug(
                "File unavailable {remote_file} status code {status_code}".format(
                    remote_file=remote_file, status_code=head_r.status_code
                )
            )
            return None

        return head_r.headers

    def _library_vanished(self):
        """The library root is no longer there.

        Every remaining file would hit this, so stop the run rather than
        say the same thing several hundred times
        """
        if self._fatal:
            return
        self._fatal = self._path_problem(
            os.path.abspath(self.library_path),
            "it went away while the run was going",
        )
        self._stop.set()

    def _folder_unusable(self, local_file, error):
        """Could not make somewhere to put a file"""
        self._note_skip()
        logger.error(
            "Cannot create a folder for {name}: {reason}".format(
                name=os.path.basename(local_file),
                reason=error.strerror or error,
            )
        )

        if not os.path.isdir(self.library_path):
            self._library_vanished()

    def _get_with_backoff(self, remote_file, stream=True, range_start=None,
                          if_range=None):
        """GET that honours the pool wide cooldown and handles a 429.

        Connection errors and 5xx are retried inside the session's
        transport adapter. A 429 is handled here instead, so that every
        worker pauses rather than only the one that was refused.

        range_start asks the server to resume from that byte. Whether it
        agrees is its business: the caller checks the status code.

        if_range carries the validator from the first response, so that
        a file which changed underneath us comes back whole, as a 200,
        instead of a slice that would be spliced onto the wrong bytes.
        """
        headers = None
        if range_start:
            headers = {"Range": "bytes={start}-".format(start=range_start)}
            if if_range:
                headers["If-Range"] = if_range

        return self._request_with_backoff(
            "get", remote_file, stream=stream, headers=headers
        )

    def _request_with_backoff(self, method, url, quiet=False, **kwargs):
        """Send a request, waiting out any pool wide cooldown first and
        treating a 429 as everyone's problem rather than this caller's.

        url may be a SignedUrl, signed here, at the last moment. Humble
        keeps handing out the same signed url until it lapses, so one
        that is refused is signed again, once, and tried again
        """
        if not isinstance(url, SignedUrl):
            return self._send_with_backoff(method, url, quiet=quiet, **kwargs)

        resolved = self._resolve_signed(url)
        if resolved is None:
            return None
        response = self._send_with_backoff(method, resolved, quiet=quiet, **kwargs)
        if response is None or response.status_code not in (401, 403):
            return response

        try:
            response.close()
        except Exception:
            pass
        logger.debug(
            "The download url for {name} was refused, signing a fresh "
            "one".format(name=url.filename)
        )
        resolved = self._resolve_signed(url, refresh=True)
        if resolved is None:
            return None
        return self._send_with_backoff(method, resolved, quiet=quiet, **kwargs)

    def _resolve_signed(self, url, refresh=False):
        """A real url for a SignedUrl, or None when there is not one"""
        try:
            return url.resolve(session=self._session(), refresh=refresh)
        except NotSignedIn as error:
            # Every remaining file would get the same answer
            if not self._fatal:
                self._fatal = error
            self._stop.set()
        except Exception as error:
            logger.error(
                "Could not get a download url for {name}: {error}".format(
                    name=url.filename, error=error
                )
            )
        return None

    def _send_with_backoff(self, method, url, quiet=False, **kwargs):
        """The GET that streams a file and the HEAD that sizes one had
        the same loop twice over, differing only in the verb and in how
        loudly they complain when it does not work.
        """
        for _ in range(self.retries + 1):
            self._wait_out_cooldown()
            if self._stop.is_set():
                return None

            try:
                response = getattr(self._session(), method)(url, **kwargs)
            except Exception:
                trouble = "Failed to {method} {url}".format(
                    method=method.upper(), url=url
                )
                if quiet:
                    logger.debug(trouble)
                else:
                    logger.exception(trouble)
                return None

            if response.status_code not in THROTTLE_STATUSES:
                return response

            self._enter_cooldown(_retry_after_seconds(response), url)
            try:
                response.close()
            except Exception:
                pass

        exhausted = (
            "Giving up on {url}: still throttled after {count} "
            "attempts".format(url=url, count=self.retries + 1)
        )
        if quiet:
            logger.debug(exhausted)
        else:
            logger.error(exhausted)
        return None

    def _run_download_job(self, job):
        cache_file_key = job["cache_file_key"]
        remote_file = job["remote_file"]
        local_folder = job["local_folder"]
        local_filename = job["local_filename"]
        cache_file_info = job["cache_file_info"]

        part_file = os.path.join(local_folder, local_filename) + ".part"
        resume = None
        held = _resume_point(part_file)
        if held is not None:
            already, validator = held
            opened = self._reopen_for_resume(
                remote_file, part_file, already, validator
            )
            if opened is not None:
                remote_file_r, append, total = opened
                resume = (append, already if append else 0, total)
                if append:
                    logger.info(
                        "Picking {name} up from {size}, where an earlier run "
                        "stopped".format(
                            name=local_filename, size=_human_size(already)
                        )
                    )
            else:
                remote_file_r = None
        else:
            remote_file_r = self._get_with_backoff(remote_file)
        if remote_file_r is None:
            self._note_skip()
            self._record_failure(
                os.path.join(local_folder, local_filename), "no answer from the server"
            )
            return False

        # Check to see if the file still exists
        if remote_file_r.status_code not in (200, 206):
            logger.debug(
                "File unavailable {remote_file} status code {status_code}".format(
                    remote_file=remote_file, status_code=remote_file_r.status_code
                )
            )
            self._note_skip()
            self._record_failure(
                os.path.join(local_folder, local_filename),
                "the server answered http {0}".format(remote_file_r.status_code),
            )
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
            if cache_file_info.get("adopted") and not cache_file_info.get(
                "url_last_modified"
            ):
                # Found on disk with no date to compare against. Take
                # this one as the baseline instead of calling it a change
                try:
                    remote_file_r.close()
                except Exception:
                    pass
                self._update_cache_data(
                    cache_file_key, dict(cache_file_info, **file_info)
                )
                self._note_skip()
                return False
        local_file = os.path.join(local_folder, local_filename)

        if cache_file_info and not self.accept_smaller:
            # An update that would put a much smaller file in place of
            # the one on disk. The old one would only survive as a dated
            # copy beside it, so keep it where it is instead
            on_disk = _file_size(local_file)
            if remote_file_r.status_code == 206:
                served = _content_range_total(remote_file_r)
            else:
                served = _coerce_size(remote_file_r.headers.get("Content-Length"))
            if on_disk and served and _much_smaller(served, on_disk):
                try:
                    remote_file_r.close()
                except Exception:
                    pass
                self._keep_larger(local_file, on_disk, served)
                self._update_cache_data(
                    cache_file_key,
                    dict(cache_file_info, kept_larger=served, **file_info),
                )
                self._note_skip()
                return False

        if "url_last_modified" in cache_file_info:
            last_modified = self._cache_rename_date(cache_file_info)
        else:
            last_modified = None

        # The library root going missing mid run usually means a drive
        # was unmounted. makedirs below would cheerfully rebuild the
        # tree inside the empty mountpoint and fill the boot disk
        # instead, so check before writing rather than after
        if not os.path.isdir(self.library_path):
            self._note_skip()
            self._library_vanished()
            return False

        # Create directory to save the file to, which might not exist if there's a subdirectory included
        try:
            os.makedirs(os.path.dirname(local_file), exist_ok=True)  # noqa: E701
        except OSError as error:
            self._folder_unusable(local_file, error)
            self._record_failure(local_file, "its folder could not be made")
            return False

        self._log_url(remote_file)

        return self._process_download(
            remote_file_r,
            cache_file_key,
            file_info,
            local_file,
            rename_str=last_modified,
            remote_file=remote_file,
            hashes=job.get("hashes"),
            listed_size=job.get("listed_size"),
            resume=resume,
        )

    def _process_download(
        self,
        open_r,
        cache_file_key,
        file_info,
        local_filename,
        rename_str=None,
        remote_file=None,
        hashes=None,
        listed_size=None,
        resume=None,
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

            written, digests = self._download_to_part(
                open_r, part_file, remote_file, local_filename, hashes,
                listed_size=listed_size,
                resume=resume,
            )
            # Recorded so a later run can tell a file that changed on
            # disk from one that changed upstream
            file_info.update(digests)
            os.replace(part_file, local_filename)
            _remove_quietly(_resume_note(part_file))

        except (Exception, KeyboardInterrupt) as e:
            stopped = isinstance(e, (Interrupted, KeyboardInterrupt))
            skipped = isinstance(e, Skipped)
            if skipped:
                with self._notice_lock:
                    self._skipping.discard(label)
            if isinstance(e, Interrupted):
                # Asked to stop, so this is not a failure worth shouting,
                # but how far it got and how fast is worth a line
                self._log_finished(
                    local_filename,
                    _file_size(part_file),
                    time.time() - started,
                    total=_content_range_total(open_r) or _coerce_size(
                        (getattr(open_r, "headers", None) or {}).get("Content-Length")
                    ),
                    stopped=True,
                    resumable=os.path.exists(_resume_note(part_file)),
                    verb="Skipped" if skipped else "Stopped",
                )
            else:
                logger.error(
                    "Failed to download file {local_filename}".format(
                        local_filename=local_filename
                    )
                )
                self._record_failure(local_filename, str(e) or type(e).__name__)

            self.events.file_finished(label, ok=False, skipped=skipped)

            # A stopped transfer keeps what it has, for the next run to
            # carry on from. A failed one is thrown away: what it holds
            # is not to be trusted. Any previously downloaded copy of
            # the file is left untouched either way
            if not stopped or not os.path.exists(_resume_note(part_file)):
                _remove_quietly(part_file)
                _remove_quietly(_resume_note(part_file))

            if type(e).__name__ == "KeyboardInterrupt":
                # A worker cannot exit the process, so flag it and let
                # start() shut the run down once the workers are joined
                self._stop.set()
                if threading.current_thread() is threading.main_thread():
                    raise RunStopped() from None

            result = False

        else:
            if "url_last_modified" not in file_info:
                # no Last-Modified header so we set the time of the current download
                # this will result in the file not being re-downloaded by default later
                file_info["url_last_modified"] = datetime.datetime.now().strftime(
                    "%a, %d %b %Y %H:%M:%S %Z"
                )
            self.events.file_finished(label, ok=True)

            file_info["path"] = self._library_relpath(local_filename)
            self._update_cache_data(cache_file_key, file_info)
            self._log_finished(local_filename, written, time.time() - started)
            result = True

        finally:
            # Since its a stream connection, make sure to close it
            try:
                open_r.connection.close()
            except Exception:
                pass

        return result

    def _log_finished(self, local_filename, done, elapsed, total=None,
                      stopped=False, resumable=False, verb="Stopped"):
        """One line per file once it is over: how much, how long, and how
        fast on average, whether it finished or was stopped part way.
        The progress block, when there is one, keeps it above itself
        """
        name = os.path.basename(local_filename)
        done = done or 0
        rate = done / elapsed if elapsed > 0 else None
        speed = ", avg {0}/s".format(_human_size(rate)) if rate else ""
        if not stopped:
            line = "Downloaded {name}: {size} in {elapsed:.1f}s{speed}".format(
                name=name, size=_human_size(done), elapsed=elapsed, speed=speed
            )
        elif total:
            line = (
                "{verb} {name} at {pct}% ({done} of {total}) after "
                "{elapsed:.1f}s{speed}".format(
                    verb=verb,
                    name=name,
                    pct=int(100 * min(1.0, done / total)),
                    done=_human_size(done),
                    total=_human_size(total),
                    elapsed=elapsed,
                    speed=speed,
                )
            )
        else:
            line = "{verb} {name} after {done} in {elapsed:.1f}s{speed}".format(
                verb=verb, name=name, done=_human_size(done), elapsed=elapsed, speed=speed
            )
        if stopped and resumable and done:
            line += "; the next run carries on from there"
        logger.info(line)

    def _hash_algorithm(self, hashes):
        """Strongest checksum the api gave us that we can actually use.
        With none listed, sha1 anyway: there is nothing to check it
        against now, but recorded in the cache it lets --verify tell
        later whether the file on disk is still the one downloaded"""
        if not self.verify:
            return None
        for name in ("sha1", "md5"):
            if (hashes or {}).get(name):
                try:
                    hashlib.new(name)
                except ValueError:
                    continue
                return name
        return "sha1"

    def _download_to_part(self, response, part_file, remote_file,
                          local_filename, hashes=None, listed_size=None,
                          resume=None):
        """Stream into the part file, picking the transfer back up where
        it broke off when the server allows it.

        Returns (size of the finished file, checksums computed for it).
        """
        # The block already shows what is in flight, and a line per start
        # on top of a line per finish just scrolls the history away
        (logger.debug if self.events.shows_transfers else logger.info)(
            "Downloading: {local_filename}".format(local_filename=local_filename)
        )

        if resume is not None:
            # Carrying on from an earlier run's part file
            append, already, total = resume
        else:
            append = False
            already = 0
            total = _coerce_size(response.headers.get("Content-Length"))
        attempt = 0
        label = os.path.basename(local_filename)

        # Taken from the first response, so a resume can ask for the
        # rest of *this* file rather than a slice of whatever is there
        # now. Hashing happens as the bytes go past, costing a pass over
        # data we are already walking
        validator = _validator_for(response)
        algorithm = self._hash_algorithm(hashes)
        hasher = hashlib.new(algorithm) if algorithm else None
        if hasher is not None and append and already:
            # The hash has to cover the bytes an earlier run fetched too
            _feed_hash_from_file(hasher, part_file)

        # Written as the transfer starts, not when it stops: a second
        # Ctrl+C ends the process on the spot, and the part file it
        # leaves is only resumable if this is already there
        _write_resume_note(part_file, validator)

        self.events.file_started(label, total)
        if already:
            self.events.file_progress(label, already)

        # What the last download that failed its checksum hashed to
        mismatched = None
        while True:
            try:
                self._stream_to_file(
                    response, part_file, append, already, total, label, hasher
                )
                try:
                    self._check_digest(algorithm, hasher, hashes, label)
                except ChecksumMismatch as error:
                    if not self._listing_is_stale(label, part_file, listed_size):
                        if error.actual == mismatched:
                            # The same wrong bytes twice: the server is
                            # sending this file consistently, so another
                            # try would only fetch them a third time
                            raise RepeatedMismatch(
                                "{0}, and the same again when downloaded a "
                                "second time".format(error)
                            ) from error
                        mismatched = error.actual
                        raise
                return _file_size(part_file), _digest_of(algorithm, hasher)
            except (KeyboardInterrupt, Interrupted, RepeatedMismatch):
                # Shutting down: nothing to resume, nothing to report
                raise
            except Exception as error:
                attempt += 1
                spoiled = isinstance(error, ChecksumMismatch)
                if spoiled:
                    # The bytes on disk are wrong, so there is nothing
                    # worth resuming onto
                    _remove_quietly(part_file)
                    already = 0
                else:
                    already = _file_size(part_file)

                if (
                    remote_file is None
                    or attempt > self.retries
                    or self._stop.is_set()
                ):
                    raise

                logger.warning(
                    "{name} {what} ({error}), {action}".format(
                        name=label,
                        what="is not what was promised" if spoiled
                        else "stopped after " + _human_size(already),
                        error=error,
                        action="downloading it again" if spoiled
                        else "picking it back up",
                    )
                )

                resumed = self._reopen_for_resume(
                    remote_file, part_file, already, validator
                )
                if resumed is None:
                    raise
                response, append, total = resumed
                if append is False:
                    already = 0

                # Whatever the server decided, the hash has to cover
                # exactly what ends up in the file
                if hasher is not None:
                    hasher = hashlib.new(algorithm)
                    if append and already:
                        _feed_hash_from_file(hasher, part_file)
                validator = _validator_for(response) or validator

                # A resume changes what the whole file weighs
                self.events.file_started(label, total)
                self.events.file_progress(label, already)

    def _record_failure(self, local_file, reason):
        """A file this run gave up on. A run being stopped is not a
        failure of the files it did not get to"""
        if self._stop.is_set():
            return
        with self._notice_lock:
            self.failed_files.append((local_file, reason))

    def _keep_larger(self, local_file, on_disk, served):
        with self._notice_lock:
            self.smaller_files.append((local_file, on_disk, served, "kept"))
        logger.info(
            "Keeping your {name} ({mine}): Humble now sends a much smaller "
            "version ({theirs}). --accept-smaller takes it anyway".format(
                name=os.path.basename(local_file),
                mine=_human_size(on_disk),
                theirs=_human_size(served),
            )
        )

    def _report_smaller(self):
        """Say which files are much smaller than they were, and keep a
        running list in the library, one line per file, newest wins"""
        if not self.smaller_files:
            return
        downloaded = [f for f in self.smaller_files if f[3] == "downloaded"]
        kept = [f for f in self.smaller_files if f[3] == "kept"]
        if downloaded:
            logger.warning(
                "{count} file(s) arrived much smaller than Humble lists "
                "({before} listed, {after} received), likely more heavily "
                "compressed than the originals".format(
                    count=len(downloaded),
                    before=_human_size(sum(f[1] for f in downloaded)),
                    after=_human_size(sum(f[2] for f in downloaded)),
                )
            )
        if kept:
            logger.info(
                "{count} file(s) of yours were kept rather than replaced by "
                "much smaller versions ({mine} kept, {theirs} on offer)".format(
                    count=len(kept),
                    mine=_human_size(sum(f[1] for f in kept)),
                    theirs=_human_size(sum(f[2] for f in kept)),
                )
            )
        if self.dry_run is True:
            return
        report = os.path.join(self.library_path, SMALLER_REPORT)
        rows = {}
        try:
            with open(report) as handle:
                for line in handle:
                    if line.startswith("#") or not line.strip():
                        continue
                    rows[line.split("\t", 1)[0]] = line.rstrip("\n")
        except OSError:
            pass
        today = datetime.date.today().isoformat()
        for path, before, after, kind in self.smaller_files:
            rel = os.path.relpath(path, self.library_path)
            rows[rel] = "\t".join(
                [rel, kind, str(before), str(after),
                 "{0:.0f}%".format(100.0 * after / before), today]
            )
        try:
            with open(report, "w") as handle:
                handle.write(
                    "# path\twhat happened\tsize before\tsize now\tnow as % "
                    "of before\tdate\n"
                )
                for rel in sorted(rows):
                    handle.write(rows[rel] + "\n")
            logger.info("The list is in {0}".format(report))
        except OSError as error:
            logger.warning("Could not write {0}: {1}".format(report, error))

    def _listing_is_stale(self, label, part_file, listed_size):
        """Whether a checksum that does not match is Humble's listing
        being out of date rather than the download being damaged.

        Humble replaces files (a new edition, a fixed scan) without always
        updating the size and checksums listed for them. The file arrives
        whole, at its new size, and is checked against the old one's
        checksum. A file at the listed size that does not match is
        damaged; one at a different size is a different file
        """
        arrived = _file_size(part_file)
        if not listed_size or arrived == listed_size:
            return False
        with self._notice_lock:
            self.stale_listings += 1
            if _much_smaller(arrived, listed_size):
                self.smaller_files.append(
                    (part_file[: -len(".part")], listed_size, arrived, "downloaded")
                )
        logger.warning(
            "{name} is not the version Humble lists ({listed}, the server "
            "sent {arrived}): keeping what the server sent, with its own "
            "checksum".format(
                name=label,
                listed=_human_size(listed_size),
                arrived=_human_size(arrived),
            )
        )
        return True

    @staticmethod
    def _check_digest(algorithm, hasher, hashes, label):
        if hasher is None:
            return
        expected = (hashes or {}).get(algorithm, "").lower()
        actual = hasher.hexdigest().lower()
        if expected and actual != expected:
            error = ChecksumMismatch(
                "{algorithm} of {label} is {actual}, expected "
                "{expected}".format(
                    algorithm=algorithm, label=label,
                    actual=actual, expected=expected,
                )
            )
            error.actual = actual
            raise error

    def _reopen_for_resume(self, remote_file, part_file, already,
                           validator=None):
        """Ask for the rest of the file.

        The server decides whether that is possible, and we believe the
        status code rather than assuming support: 206 means the Range was
        honoured and we append, 200 means it was ignored, or the file
        changed since If-Range was taken, and it starts again from
        nothing.

        Returns (response, append, total), or None if it cannot go on.
        """
        response = self._get_with_backoff(
            remote_file, range_start=already or None, if_range=validator
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
            if not already:
                # We asked for the whole file on purpose, so there is
                # nothing surprising to report
                pass
            elif validator:
                # We asked for the rest only if the file was unchanged,
                # and got the whole thing, so it changed underneath us
                logger.info(
                    "{name} changed on the server, taking it from the "
                    "top".format(
                        name=os.path.basename(part_file).replace(".part", "")
                    )
                )
            else:
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
        self, response, part_file, append, already, total, label=None,
        hasher=None,
    ):
        """One pass of the transfer. `already` is what the part file
        holds when appending, so the progress bar counts the whole file
        """
        written = already
        with open(part_file, "ab" if append else "wb") as outfile:
            for data in response.iter_content(chunk_size=4096):
                if self._paused.is_set():
                    self._wait_while_paused(label)
                if self._stop.is_set():
                    # Checked between chunks so a Ctrl-C stops a large
                    # transfer promptly instead of at the end of it
                    raise Interrupted()
                if label is not None and label in self._skipping:
                    raise Skipped()
                written += len(data)
                outfile.write(data)
                self._count_bytes(len(data))
                if hasher is not None:
                    hasher.update(data)
                if label is not None:
                    self.events.file_progress(label, written)

        if total is not None and written < total:
            raise ValueError(
                "Download did not complete, {written} of {total} bytes".format(
                    written=written, total=total
                )
            )
        if total is not None and written > total:
            logger.warning("Downloaded more content than expected")

        return written
