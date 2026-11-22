"""The cache: .cache.json, which says what has been downloaded, and from which version"""

import datetime
import json
import logging
import os
import tempfile
import time

from .errors import LibraryPathError
from .files import _coerce_size, _much_smaller, _remove_quietly, _resume_note

# Under the engine's name, as before the split, so anything filtering
# hbd's log by logger still sees all of it
logger = logging.getLogger("humblebundle_downloader.download_library")


# Longest the cache goes unwritten while files are being recorded
CACHE_FLUSH_SECONDS = 5.0


class CacheMixin:
    """Part of DownloadLibrary: see download_library.py. Uses the
    state DownloadLibrary.__init__ sets up"""

    def _update_cache_data(self, cache_file_key, file_info):
        """Record a file in the cache, writing it out now and then.

        Rewriting the whole file after every download meant thousands of
        full rewrites of a growing file, each flushed to disk, with every
        other worker waiting on it: gigabytes of writes over a large
        library, and a first run adopting files back to back did nothing
        else. Now it is written at most every CACHE_FLUSH_SECONDS, and
        once more when the run ends however it ends. A run killed outright
        loses those last seconds of entries, for files that are complete
        on disk, which the next run recognises and records without
        downloading them again
        """
        with self._cache_lock:
            self.cache_data[cache_file_key] = file_info
            self._cache_dirty = True
            if time.time() - self._cache_written_at >= CACHE_FLUSH_SECONDS:
                self._flush_cache_locked()

    def _flush_cache(self, wait=None):
        """Write the cache out if anything has changed since it last was.
        wait bounds how long to wait for the lock, for a run leaving in a
        hurry; past it the write is skipped rather than hanging
        """
        if not self._cache_lock.acquire(timeout=-1 if wait is None else wait):
            return
        try:
            if self._cache_dirty:
                self._flush_cache_locked()
        finally:
            self._cache_lock.release()

    def _flush_cache_locked(self):
        self._write_cache_data()
        self._cache_dirty = False
        self._cache_written_at = time.time()

    def _write_cache_data(self):
        """Write somewhere else, then rename over the top.

        The whole file is rewritten each time, so writing in place
        leaves a window in which the cache is half written. A run killed in that window, which is exactly what a
        second Ctrl-C does, used to leave a truncated or empty
        .cache.json that the next run could not read at all. os.replace
        is atomic, so what is on disk is always one whole version or the
        other.
        """
        folder = os.path.dirname(self.cache_file) or "."
        handle, temp_path = tempfile.mkstemp(
            prefix=".cache.json.", suffix=".tmp", dir=folder
        )
        try:
            with os.fdopen(handle, "w") as outfile:
                json.dump(
                    self.cache_data,
                    outfile,
                    sort_keys=True,
                    indent=4,
                )
                outfile.flush()
                os.fsync(outfile.fileno())
            os.replace(temp_path, self.cache_file)
        except Exception:
            _remove_quietly(temp_path)
            raise

    def _adopt(self, cache_file_key, local_file, file_size, remote_file=None):
        """Take a file already on disk as downloaded, when the cache has
        never heard of it but it is exactly the size it should be.

        The rebuilt site identifies bundles by new ids, so after it no
        old cache entry matches anything and a whole library would be
        downloaded again. Size is the check rather than a checksum: the
        old cache trusted its entries without one, and hashing a large
        library on a slow drive costs hours.

        The entry records no Last-Modified, since none was seen. The
        first --update to reach it fills that in rather than treating
        the mismatch as a change, so later updates compare like for like
        """
        if not file_size:
            return False
        kept = None
        try:
            on_disk = os.path.getsize(local_file)
        except OSError:
            return False
        if on_disk != file_size:
            # Perhaps a newer version than Humble lists, which is what
            # the server sends now. One HEAD request costs far less than
            # downloading it again to find out
            if remote_file is None or not on_disk:
                return False
            headers = self._get_remote_headers(remote_file)
            served = _coerce_size((headers or {}).get("Content-Length"))
            if served != on_disk:
                if not (served and _much_smaller(served, on_disk)) or self.accept_smaller:
                    return False
                # What is on disk is far bigger than what Humble sends now,
                # which since its files were regenerated usually means a
                # more heavily compressed one. Downloading would overwrite
                # the original with no copy kept
                kept = served
                self._keep_larger(local_file, on_disk, served)

        self.adopted_count += 1
        logger.debug(
            "Already have {local_file}, recording it".format(local_file=local_file)
        )
        if self.dry_run is not True:
            # Whatever an earlier run left half done is not needed now
            _remove_quietly(local_file + ".part")
            _remove_quietly(_resume_note(local_file + ".part"))
        if self.dry_run is not True:
            entry = {"adopted": True, "path": self._library_relpath(local_file)}
            if kept is not None:
                entry["kept_larger"] = kept
            self._update_cache_data(cache_file_key, entry)
        return True

    def _library_relpath(self, local_file):
        """Where a file is in the library, as the cache records it so
        --verify can find it again: relative, with forward slashes"""
        return os.path.relpath(local_file, self.library_path).replace(os.sep, "/")

    def _reset_cache(self, cache_file):
        """--reset-cache: start from an empty cache.

        The old one is set aside rather than deleted. It holds the
        checksums --verify checks against, which a fresh start cannot
        get back, so it stays to be put back by hand if wanted. Files
        already on disk are not downloaded again: with no cache entry,
        each one found at the right size is recorded afresh. A dry run
        pretends the cache is empty and leaves the file as it is.
        """
        if not os.path.exists(cache_file):
            logger.info("--reset-cache: there was no cache, starting from nothing")
            return {}
        if self.dry_run is True:
            logger.info(
                "--reset-cache: acting as if the cache were empty (a dry run "
                "leaves it as it is)"
            )
            return {}
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        aside = "{0}.reset-{1}.bak".format(cache_file, stamp)
        try:
            os.replace(cache_file, aside)
        except OSError as error:
            raise LibraryPathError(
                "--reset-cache could not set {0} aside: {1}".format(
                    cache_file, error.strerror or error
                )
            ) from error
        logger.warning(
            "--reset-cache: starting from an empty cache. The old one is kept "
            "as {0}; files already on disk at the right size are recorded "
            "again rather than downloaded".format(os.path.basename(aside))
        )
        return {}

    def _load_cache_data(self, cache_file):
        self._clear_stale_cache_temps(cache_file)

        try:
            with open(cache_file, "r") as f:
                cache_data = json.load(f)
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as error:
            # A cache we cannot read is not worth dying over: the run
            # can rebuild it. Keep the old one in case it is wanted
            self._set_aside_bad_cache(cache_file, error)
            return {}

        if not isinstance(cache_data, dict):
            self._set_aside_bad_cache(
                cache_file, "it does not contain an object"
            )
            return {}

        return self._usable_cache_entries(cache_data)

    @staticmethod
    def _usable_cache_entries(cache_data):
        """Keep the entries that can be used, drop the ones that cannot.

        Everything downstream treats an entry as a mapping: a stray
        string or list would raise AttributeError on the traversal
        thread and take the run down, and because any non empty value
        counts as "already downloaded" it would also quietly skip that
        file forever.

        Dropping the whole cache over one bad entry means downloading
        the library again, so only the bad entries go.
        """
        usable = {}
        dropped = 0
        for key, value in cache_data.items():
            if isinstance(key, str) and isinstance(value, dict):
                usable[key] = value
            else:
                dropped += 1

        if dropped:
            logger.warning(
                "Ignoring {dropped} unreadable entr{plural} in the cache; "
                "those files will be downloaded again".format(
                    dropped=dropped, plural="y" if dropped == 1 else "ies"
                )
            )
        return usable

    @staticmethod
    def _cache_rename_date(cache_file_info):
        """The date to tag an older copy of a file with.

        A cache written by another version, or edited by hand, can hold
        anything at all here. Not knowing the date is only worth losing
        the rename over, not the download
        """
        stamp = cache_file_info.get("url_last_modified")
        if not isinstance(stamp, str):
            return None
        try:
            return datetime.datetime.strptime(
                stamp, "%a, %d %b %Y %H:%M:%S %Z"
            ).strftime("%Y-%m-%d")
        except ValueError:
            logger.debug(
                "Cache holds an unparseable date: {stamp!r}".format(stamp=stamp)
            )
            return None

    def _clear_stale_cache_temps(self, cache_file):
        """Tidy up half written caches left by a run that was killed.

        os.replace means they never became the cache, but a hard kill
        cannot unlink them either, so they would otherwise pile up
        """
        folder = os.path.dirname(cache_file) or "."
        prefix = os.path.basename(cache_file) + "."
        try:
            leftovers = os.listdir(folder)
        except OSError:
            return

        for name in leftovers:
            if name.startswith(prefix) and name.endswith(".tmp"):
                _remove_quietly(os.path.join(folder, name))

    def _set_aside_bad_cache(self, cache_file, reason):
        spoiled = "{cache_file}.unreadable-{stamp}".format(
            cache_file=cache_file,
            stamp=datetime.datetime.now().strftime("%Y%m%d-%H%M%S"),
        )
        try:
            os.replace(cache_file, spoiled)
            kept = " Kept as {spoiled}.".format(
                spoiled=os.path.basename(spoiled)
            )
        except OSError:
            kept = ""

        logger.warning(
            "Could not read {name}: {reason}.{kept} Starting a new one, so "
            "files already downloaded will be fetched again".format(
                name=os.path.basename(cache_file), reason=reason, kept=kept
            )
        )
