"""Which files are wanted: platforms, extensions and --prefer-format"""

import logging
from concurrent.futures import ThreadPoolExecutor

from .files import _coerce_size, _file_ext
from .formatting import _human_size
from .humble_site import SignedUrl

# Under the engine's name, as before the split, so anything filtering
# hbd's log by logger still sees all of it
logger = logging.getLogger("humblebundle_downloader.download_library")


# --prefer-format keyword: the biggest file, whatever its format
LARGEST = "largest"
# How many of an item's formats are sized up at once. Items rarely come
# in more than four
SIZE_CHECKS = 4


class SelectionMixin:
    """Part of DownloadLibrary: see download_library.py. Uses the
    state DownloadLibrary.__init__ sets up"""

    def _select_by_format(self, entries, product_title, platform):
        """Narrow a platform's files (FileEntry) down to the preferred
        format.

        Tries each --prefer-format extension in order and keeps every
        file of the first one that matches, so multi part items (a comic
        split across volumes) survive intact. The keyword "largest"
        stands for the single biggest file of whatever is offered, and is
        also what happens when none of the preferred formats are present.
        Entries without a file to compare (asm.js games, external links)
        are never dropped.
        """
        if not self.prefer_format:
            return list(entries)

        passthrough = []
        candidates = []
        for entry in entries:
            if entry.filename is None:
                passthrough.append(entry)
            elif self._should_download_file_by_ext_and_log(entry.filename) is True:
                candidates.append(entry)

        if len(candidates) < 2:
            # Nothing to choose between
            return passthrough + candidates

        for wanted in self.prefer_format:
            if wanted == LARGEST:
                return passthrough + self._take_largest(
                    candidates, product_title, platform, LARGEST
                )
            chosen = [entry for entry in candidates if _file_ext(entry.filename) == wanted]
            if chosen:
                self._log_format_choice(
                    product_title, platform, wanted, candidates, chosen
                )
                return passthrough + chosen

        # No preferred format available, so take the biggest file there is
        return passthrough + self._take_largest(
            candidates, product_title, platform, None
        )

    def _take_largest(self, candidates, product_title, platform, wanted):
        """The biggest of the candidates, by what the server sends now
        rather than what Humble lists. Each is asked about at the same
        time, so an item in four formats costs one round trip, not four
        """
        sizes = self._real_sizes(candidates)
        biggest = max(range(len(candidates)), key=lambda i: sizes[i] or 0)
        largest = candidates[biggest]
        self._log_format_choice(
            product_title, platform, wanted, candidates, [largest], sizes
        )
        return [largest]

    def _real_sizes(self, entries):
        if len(entries) < 2 or not any(e.signing for e in entries):
            return [self._real_size(entry) for entry in entries]
        return list(self._size_pool().map(self._real_size, entries))

    def _size_pool(self):
        pool = getattr(self, "_sizer", None)
        if pool is None:
            pool = self._sizer = ThreadPoolExecutor(
                max_workers=SIZE_CHECKS, thread_name_prefix="hbd-size"
            )
        return pool

    def _close_size_pool(self):
        pool = getattr(self, "_sizer", None)
        if pool is not None:
            self._sizer = None
            pool.shutdown(wait=False, cancel_futures=True)

    def _real_size(self, entry):
        """A file's size as the server has it now.

        Humble's listing gives the sizes of files from before it
        regenerated them, sometimes three times what is sent now, so
        "the largest" by the listing can be the wrong choice. For files
        from the rebuilt site one HEAD settles it; entries carrying a
        real url, as the old api gave, were listed accurately and are
        not asked about. When the server cannot say, the listing will do
        """
        if entry.signing is None or self._signer is None:
            return entry.listed_size
        url = SignedUrl(
            self._signer,
            entry.signing.entitlement,
            entry.signing.download_key,
            entry.url,
            vault=entry.signing.vault,
        )
        headers = self._get_remote_headers(url)
        return _coerce_size((headers or {}).get("Content-Length")) or entry.listed_size

    def _log_format_choice(
        self, product_title, platform, wanted, candidates, chosen, sizes=None
    ):
        dropped = [entry.filename for entry in candidates if entry not in chosen]
        if not dropped:
            return
        if wanted in (None, LARGEST):
            if wanted is None:
                reason = "no preferred format available, taking the largest file"
            else:
                reason = "taking the largest file"
            if sizes and all(sizes):
                reason += " ({0})".format(", ".join(
                    "{0} {1}".format(_file_ext(entry.filename) or entry.filename, _human_size(size))
                    for entry, size in zip(candidates, sizes, strict=True)
                ))
        else:
            reason = "preferring {wanted}".format(wanted=wanted)
        self._log_skip(
            "{product_title} [{platform}]: {reason}, skipping {dropped}".format(
                product_title=product_title,
                platform=platform,
                reason=reason,
                dropped=", ".join(dropped),
            )
        )

    def _should_download_platform(self, platform):
        platform = platform.lower()
        if self.platform_include and platform not in self.platform_include:
            return False
        return True

    def _log_skip(self, message):
        """Say what a filter passed over.

        Useful when working out why something did not arrive, and a
        wall of noise on a run that filters most of a library out, so
        it drops to debug rather than disappearing when turned off
        """
        if self.log_skipped:
            logger.info(message)
        else:
            logger.debug(message)

    def _should_download_file_by_ext_and_log(self, filename):
        if self._should_download_file_by_ext(filename) is False:
            self._log_skip(
                "Skipping the file {filename}".format(filename=filename)
            )
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
