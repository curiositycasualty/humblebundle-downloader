"""Going through the library bundle by bundle, deciding what each file needs"""

import json
import logging
import os
import queue

import parsel

from .files import _clean_name, _coerce_size
from .formatting import _human_size
from .humble_site import SignedUrl
from .models import ASMJS, EXTERNAL, FILE, Order

# Under the engine's name, as before the split, so anything filtering
# hbd's log by logger still sees all of it
logger = logging.getLogger("humblebundle_downloader.download_library")


def _unique_keys(keys):
    """Keep the first of each key, in the order they were given"""
    seen = set()
    unique = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


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


class WalkMixin:
    """Part of DownloadLibrary: see download_library.py. Uses the
    state DownloadLibrary.__init__ sets up"""

    def _all_orders(self):
        """The library's bundles, then the vault when asked for. --trove
        meant only the Trove, so it now means only the vault, which is
        what the Trove became
        """
        if self.trove is not True:
            for order_id, order in self.site.orders(keys=self.purchase_keys or None):
                yield order_id, order
        if self.trove is True or self.vault is True:
            yield self.site.vault()

    def _process_order(self, order_id, order):
        """Queue everything in one bundle, given in the old api's shape"""
        if not isinstance(order, dict) or "product" not in order:
            self._skip_order(order_id, _order_error(order))
            return
        try:
            order = Order.from_legacy(order_id, order)
        except (KeyError, TypeError, AttributeError) as error:
            self._skip_order(order_id, "unreadable ({0!r})".format(error))
            return

        bundle_title = _clean_name(order.title)
        self._current_bundle = bundle_title
        logger.info("Checking bundle: " + str(bundle_title))
        self.events.bundle_started(bundle_title)
        for product in order.products:
            self._process_product(order.id, bundle_title, product)

    def _skip_order(self, order_id, reason):
        """One unusable order must not take the whole run down with it"""
        self.skipped_orders.append(order_id)
        logger.error(
            "Skipping order {order_id}: {reason}".format(
                order_id=order_id, reason=reason
            )
        )

    def _rename_old_file(self, local_filename, append_str):
        # Check if older file exists, if so rename
        if os.path.isfile(local_filename) is True:
            # Keeps the previous version beside the new one, by design.
            # Split on the name only: a dot in a folder name, or a file
            # with no extension at all (some game downloads), must not
            # end up in the wrong place or crash it
            folder, name = os.path.split(local_filename)
            stem, dot, ext = name.rpartition(".")
            if not dot or not stem:
                stem, ext = name, ""
            new_name = os.path.join(
                folder,
                "{stem}_{append_str}{ext}".format(
                    stem=stem, append_str=append_str, ext="." + ext if ext else ""
                ),
            )
            os.rename(local_filename, new_name)
            logger.info("Renamed older file to {new_name}".format(new_name=new_name))

    def _process_product(self, order_id, bundle_title, product):
        product_title = _clean_name(product.title)
        for download in product.downloads:
            if self._should_download_platform(download.platform) is False:
                self._log_skip(
                    "Skipping {platform} for {product_title}".format(
                        platform=download.platform, product_title=product_title
                    )
                )
                continue

            product_folder = os.path.join(
                self.library_path, bundle_title, product_title
            )
            if self.dry_run is False:
                try:
                    os.makedirs(product_folder)
                except OSError:
                    pass

            for entry in self._select_by_format(
                download.files, product_title, download.platform
            ):
                try:
                    if entry.kind == FILE:
                        self._process_file(order_id, product_folder, entry)
                    elif entry.kind == ASMJS:
                        self._process_asmjs(order_id, product_folder, entry)
                    elif entry.kind == EXTERNAL:
                        logger.info(
                            "External url found: {bundle_title}/{product_title} : {url}".format(
                                bundle_title=bundle_title,
                                product_title=product_title,
                                url=entry.url,
                            )
                        )
                    else:
                        logger.info(
                            "No downloadable url(s) found: {bundle_title}/{product_title}".format(
                                bundle_title=bundle_title, product_title=product_title
                            )
                        )
                        logger.info(entry.struct)
                except Exception:
                    logger.exception(
                        "Failed to download this 'file':\n{struct}".format(
                            struct=entry.struct
                        )
                    )

    def _process_file(self, order_id, product_folder, entry):
        """An ordinary file: queue it if it needs downloading"""
        filename = entry.filename
        url = entry.url
        if entry.signing is not None:
            # The site gives a filename, not a url. One is minted in the
            # worker, right before it is used
            url = SignedUrl(
                self._signer,
                entry.signing.entitlement,
                entry.signing.download_key,
                filename,
                vault=entry.signing.vault,
            )
        if self._should_download_file_by_ext_and_log(filename) is False:
            return
        try:
            self._check_cache_and_download(
                order_id + ":" + filename,
                url,
                product_folder,
                filename,
                file_size=entry.listed_size,
                hashes=entry.hashes,
            )
        except FileExistsError:
            pass
        except Exception:
            logger.exception("Failed to download {url}".format(url=url))

    def _process_asmjs(self, order_id, product_folder, entry):
        """A game playable in the browser: its page, then the data files
        the page names"""
        file_type = entry.struct
        game_name = file_type["asm_config"]["display_item"]
        local_folder = os.path.join(product_folder, game_name)
        if self.dry_run is False:
            try:
                os.makedirs(local_folder, exist_ok=True)
            except OSError:
                pass

        # The page that presents the game, as Humble's own iframe shows it
        asmjs_html_filename = game_name + ".html"
        asmjs_local_html_filename = game_name + ".local.html"
        cache_file_key = order_id + ":" + asmjs_html_filename
        # game_name might be "game" or "game_asm", but the path to the
        # file always uses the "game_asm" one
        game_asm_name = file_type["asm_manifest"]["asmFile"].split("/")[2]
        asmjs_url = (
            "https://www.humblebundle.com/play/asmjs/" + game_asm_name + "/" + order_id
        )

        if self._should_download_file_by_ext_and_log(asmjs_html_filename) is False:
            return

        if self.dry_run is True:
            # The data files this game needs are listed inside the html
            # page, which a dry run does not fetch, so only the page
            # itself is sized
            try:
                self._check_cache_and_download(
                    cache_file_key, asmjs_url, local_folder, asmjs_html_filename
                )
            except FileExistsError:
                pass
            except Exception:
                logger.exception(
                    "Failed to check {asmjs_url}".format(asmjs_url=asmjs_url)
                )
            self.unexpanded_asmjs += 1
            return

        downloaded = False
        try:
            # Not queued: the manifest is read out of this page, so it has
            # to be on disk before the data files below can even be named
            downloaded = self._check_cache_and_download(
                cache_file_key,
                asmjs_url,
                local_folder,
                asmjs_html_filename,
                synchronous=True,
            )
        except FileExistsError:
            # The data files are still wanted when the page was
            # downloaded before
            pass
        except Exception:
            logger.exception(
                "Failed to download {asmjs_url}".format(asmjs_url=asmjs_url)
            )
            return

        # The page carries the manifest with the auth parameters on its urls
        with open(os.path.join(local_folder, asmjs_html_filename), "r") as asmjs_html:
            asmjs_page = parsel.Selector(text=asmjs_html.read())
            asm_player_data_text = asmjs_page.css(
                "#webpack-asm-player-data::text"
            ).get()
            asm_player_data = json.loads(asm_player_data_text)
        manifest = asm_player_data["asmOptions"]["manifest"]

        if downloaded:
            # A copy of the page that plays from the files beside it, with
            # the remote manifest urls swapped for the local names
            try:
                with open(os.path.join(local_folder, asmjs_html_filename), "r") as asmjs_html, \
                        open(os.path.join(local_folder, asmjs_local_html_filename), "w") as local_html:
                    for line in asmjs_html:
                        for local_filename, remote_file in manifest.items():
                            line = line.replace(
                                f'"{local_filename}": "{remote_file}"',
                                f'"{local_filename}": "{local_filename}"',
                            )
                        local_html.write(line)
            except Exception:
                logger.exception(
                    "Failed to create local version of {name}".format(
                        name=asmjs_html_filename
                    )
                )

        # TODO deduplicate these files? Osmos example has 3 unique files
        # and 2 dupes with different names
        for local_filename, remote_file in manifest.items():
            try:
                self._check_cache_and_download(
                    order_id + ":" + game_name + ":" + local_filename,
                    remote_file,
                    local_folder,
                    local_filename,
                )
            except FileExistsError:
                continue
            except Exception:
                logger.exception("Failed to download {url}".format(url=remote_file))
                continue

    def _log_url(self, url):
        if self.print_urls is True and url:
            if isinstance(url, SignedUrl):
                # Asking for the list is asking for these to be signed
                url = self._resolve_signed(url)
                if url is None:
                    return
            # Straight to stdout, one per line, so the list stays usable
            # when piped into another tool. All logging goes to stderr
            self.events.output(str(url))

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
                "url": str(remote_file),
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

    def _check_cache_and_download(
        self,
        cache_file_key,
        remote_file,
        local_folder,
        local_filename,
        file_size=None,
        synchronous=False,
        hashes=None,
    ):
        if self._stop.is_set():
            # Stopping: nothing more is started, whatever the walk finds
            return False
        cache_file_info = self.cache_data.get(cache_file_key, {})

        if cache_file_info != {} and self.update is not True:
            # Do not care about checking for updates at this time
            raise FileExistsError

        local_file = os.path.join(local_folder, local_filename)

        if cache_file_info == {} and self._adopt(
            cache_file_key, local_file, file_size, remote_file
        ):
            raise FileExistsError

        if not self._claim(local_file):
            logger.debug(
                "Already handled {local_file} this run".format(
                    local_file=local_file
                )
            )
            raise FileExistsError

        self._print_checksum(local_file, hashes)

        if self.dry_run is True:
            self._check_pending_download(
                remote_file,
                local_file,
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
            "hashes": hashes or {},
            "listed_size": file_size,
        }

        self.events.file_queued()

        if self._queue is not None and synchronous is False:
            # Hand it to a worker. Waits while the queue is full, which
            # keeps traversal from running away from the downloads, but
            # not past a stop
            while True:
                try:
                    self._queue.put(job, timeout=0.5)
                    return False
                except queue.Full:
                    if self._stop.is_set():
                        return False

        return self._run_download_job(job)

    def _claim(self, local_file):
        """First caller to ask for a destination gets it.

        Two jobs writing the same file would interleave into one .part
        and produce something corrupt that looks finished
        """
        with self._claim_lock:
            if local_file in self._claimed:
                return False
            self._claimed.add(local_file)
            return True

    def _dedupe_keys(self, keys):
        unique = _unique_keys(keys)
        repeated = len(keys) - len(unique)
        if repeated:
            logger.info(
                "Ignoring {repeated} repeated purchase key(s)".format(
                    repeated=repeated
                )
            )
        return unique

    def _note_skip(self):
        """Queued, then turned out not to need downloading"""
        self.events.file_skipped()

    def _print_checksum(self, local_file, hashes):
        """The checksum Humble lists for a file about to be downloaded, as
        shasum -c reads it, to stdout like --print-urls. Paths are
        relative to the library, so the list checks from inside it
        """
        if not self.print_checksums:
            return
        path = os.path.relpath(local_file, self.library_path)
        sha1 = (hashes or {}).get("sha1")
        with self._notice_lock:
            header = not self._checksum_header_done
            self._checksum_header_done = True
        if header:
            self.events.output(
                "# sha1 as Humble lists them. To check, from inside the "
                "library: shasum -c <this list>. A file Humble has "
                "replaced since will not match its listing"
            )
        if sha1:
            self.events.output("{sha1}  {path}".format(sha1=sha1, path=path))
        else:
            self.events.output("# no sha1 listed: {path}".format(path=path))
