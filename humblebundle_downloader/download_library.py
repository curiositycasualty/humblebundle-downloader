import os
import sys
import json
import time
import queue
import parsel
import logging
import datetime
import requests
import threading
import http.cookiejar

logger = logging.getLogger(__name__)


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
    ):
        self.library_path = library_path
        self.jobs = max(1, int(jobs))
        self.progress_bar = progress_bar
        # Several \r progress bars writing at once is unreadable, so in
        # parallel mode each file reports once, when it finishes
        self._show_bar = progress_bar and self.jobs == 1

        self._queue = None
        self._workers = []
        self._stop = threading.Event()
        self._cache_lock = threading.Lock()
        self._thread_local = threading.local()
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

        self.session = requests.Session()
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

        self._start_workers()
        try:
            if self.trove is True:
                logger.info("Only checking the Humble Trove...")
                self._current_bundle = "Humble Trove"
                for product in self._get_trove_products():
                    title = _clean_name(product["human-name"])
                    self._process_trove_product(title, product)
            else:
                for order_id in self.purchase_keys:
                    self._process_order_id(order_id)
        except KeyboardInterrupt:
            self._stop.set()
            logger.warning("Interrupted, waiting for downloads in flight...")
            self._stop_workers()
            sys.exit()

        self._stop_workers()

        if self._stop.is_set():
            # A worker hit Ctrl-C, so the run is not complete
            sys.exit()

        if self.dry_run is True:
            self._log_dry_run_summary()

        if self.skipped_orders:
            logger.warning(
                "{count} order(s) were skipped and nothing from them was "
                "checked: {keys}".format(
                    count=len(self.skipped_orders),
                    keys=" ".join(self.skipped_orders),
                )
            )

    def _start_workers(self):
        if self.jobs == 1 or self.dry_run is True:
            # Nothing to parallelise: a dry run makes no downloads
            return

        logger.info(
            "Downloading with {jobs} parallel jobs".format(jobs=self.jobs)
        )
        # Bounded, so traversal cannot run far ahead of the downloads
        self._queue = queue.Queue(maxsize=self.jobs * 4)
        for _ in range(self.jobs):
            worker = threading.Thread(target=self._worker, daemon=True)
            worker.start()
            self._workers.append(worker)

    def _stop_workers(self):
        if self._queue is None:
            return

        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join()

        self._workers = []
        self._queue = None

    def _worker(self):
        while True:
            job = self._queue.get()
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
        session = requests.Session()
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
        try:
            head_r = self.session.head(remote_file, allow_redirects=True)
        except Exception:
            logger.debug(
                "Failed to get headers for {remote_file}".format(
                    remote_file=remote_file
                )
            )
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

        if self._queue is not None and synchronous is False:
            # Hand it to a worker. Blocks once the queue is full, which
            # keeps traversal from running away from the downloads
            self._queue.put(job)
            return False

        return self._run_download_job(job)

    def _run_download_job(self, job):
        cache_file_key = job["cache_file_key"]
        remote_file = job["remote_file"]
        local_folder = job["local_folder"]
        local_filename = job["local_filename"]
        cache_file_info = job["cache_file_info"]

        try:
            remote_file_r = self._session().get(remote_file, stream=True)
        except Exception:
            logger.exception(
                "Failed to download {remote_file}".format(remote_file=remote_file)
            )
            return False

        # Check to see if the file still exists
        if remote_file_r.status_code != 200:
            logger.debug(
                "File unavailable {remote_file} status code {status_code}".format(
                    remote_file=remote_file, status_code=remote_file_r.status_code
                )
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
        )

    def _process_download(
        self, open_r, cache_file_key, file_info, local_filename, rename_str=None
    ):
        started = time.time()
        try:
            if rename_str:
                self._rename_old_file(local_filename, rename_str)

            written = self._download_file(open_r, local_filename)

        except (Exception, KeyboardInterrupt) as e:
            if self._show_bar:
                # Do not overwrite the progress bar on next print
                print()
            logger.error(
                "Failed to download file {local_filename}".format(
                    local_filename=local_filename
                )
            )

            # Clean up broken downloaded file
            try:
                os.remove(local_filename)
            except OSError:
                pass

            if type(e).__name__ == "KeyboardInterrupt":
                # A worker cannot exit the process, so flag it and let
                # start() shut the run down once the workers are joined
                self._stop.set()
                if threading.current_thread() is threading.main_thread():
                    sys.exit()

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
        logger.info(
            "Downloaded {name} ({size} in {elapsed:.1f}s)".format(
                name=os.path.basename(local_filename),
                size=_human_size(written or 0),
                elapsed=elapsed,
            )
        )

    def _download_file(self, product_r, local_filename):
        logger.info(
            "Downloading: {local_filename}".format(local_filename=local_filename)
        )

        with open(local_filename, "wb") as outfile:
            total_length = product_r.headers.get("content-length")
            if total_length is None:  # no content length header
                dl = 0
                for data in product_r.iter_content(chunk_size=4096):
                    dl += len(data)
                    outfile.write(data)
                    if self._show_bar:
                        print(
                            "\t{dl}".format(dl=dl),
                            end="\r",
                        )
            else:
                dl = 0
                total_length = int(total_length)
                for data in product_r.iter_content(chunk_size=4096):
                    dl += len(data)
                    outfile.write(data)
                    pb_width = 50
                    done = int(pb_width * dl / total_length)
                    if self._show_bar:
                        print(
                            "\t{percent}% [{filler}{space}]".format(
                                percent=int(done * (100 / pb_width)),
                                filler="=" * min(max(done, 0), pb_width),
                                space=" " * min(max((pb_width - done), 0), pb_width),
                            ),
                            end="\r",
                        )

                if dl < total_length:
                    raise ValueError("Download did not complete")
                if dl > total_length:
                    if self._show_bar:
                        print()
                    logger.warning("Downloaded more content than expected")

        return dl

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
