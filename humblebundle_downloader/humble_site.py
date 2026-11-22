"""Reading the library from Humble's rebuilt website.

The json api this tool was built on (``/api/v1/user/order`` and
``/api/v1/order/<key>``) went away when Humble rebuilt the site on
Next.js. What replaced it is not an api but the website's own server
actions: functions the browser calls by POSTing to a page with a
``Next-Action`` header naming the function.

Those names are hashes that change with every deploy (they changed
overnight while this was being written), so none are written down here.
Each run looks them up by their readable names in the same javascript
the browser downloads, which only breaks if Humble renames the functions
themselves.

What a run needs, and where it comes from:

* every library entry: ``filterUserLibrary``, a thousand at a time
* each entry's files, sizes and checksums: the bundle's page, one
  request for the whole bundle, falling back to the entry's own page
* a url to download from: ``getSignedUrlsForEntitlements``, asked for as
  late as possible, because the urls it hands out expire

Everything is turned into the shape the old order api returned, so the
rest of the tool (filters, format preference, the cache, verification)
works on it unchanged.
"""
import re
import json
import time
import logging
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

BASE_URL = "https://www.humblebundle.com"
LIBRARY_PATH = "/home/library"

LIST_ACTION = "filterUserLibrary"
SIGN_ACTION = "getSignedUrlsForEntitlements"
VAULT_SIGN_ACTION = "getVaultSignedUrls"

# Where vault files go, and their key in the cache
VAULT_ORDER = "vault"
VAULT_TITLE = "Humble Vault"

# The only entitlement type with files behind it. The others are keys
# for steam, epic, gog and so on, which this tool never handled
DOWNLOADABLE = "download_drm_free"

LIST_PAGE_SIZE = 1000
# Bundle pages read at once, ahead of the bundle being checked. Each is a
# second or so of Humble working out the page, little of it bandwidth
READERS = 4

# A signed url is used for as long as Humble says it is good for, less
# this much, so a transfer is not started on one about to lapse. Humble
# hands back the same url until it expires, so this cannot make one last
# longer, only avoid starting on a dying one when a fresh one is possible
SIGNED_URL_MARGIN = 10 * 60
RESIGN_INTERVAL = 60

CHUNK_RE = re.compile(r"/_next/static/chunks/[^\"'\s<>]+?\.js")
# (0,n.createServerReference)("<id>",n.callServer,void 0,n.findSourceMapURL,"<name>")
ACTION_RE = re.compile(
    r'createServerReference\)?\(\s*"([0-9a-f]{40,})"\s*,[^"()]{0,200}"([\w$]+)"\s*\)'
)
_ROW_START = re.compile(rb"([0-9a-f]+):")


class SiteError(Exception):
    """The site did not answer the way this module expects"""


class NotSignedIn(SiteError):
    """The cookie is missing, wrong, or has expired"""


def parse_rsc(text):
    """Rows of a React server component stream, keyed by row id.

    Most rows are ``<hex id>:<payload>`` up to the end of the line, and
    only the json ones are kept. Text rows are different:
    ``<hex id>:T<hex length>,<text>`` carries exactly that many bytes of
    text, newlines and all, and the next row follows straight on with
    no newline between. Reading the stream a line at a time loses every
    row after one, which on the library page included the whole vault
    """
    data = text.encode("utf-8") if isinstance(text, str) else text
    rows = {}
    i, end = 0, len(data)
    while i < end:
        if data[i:i + 1] in (b"\n", b"\r"):
            i += 1
            continue
        match = _ROW_START.match(data, i)
        if not match:
            # Not a row; skip to the next line and look again
            newline = data.find(b"\n", i)
            i = end if newline < 0 else newline + 1
            continue
        row_id = match.group(1).decode()
        i = match.end()
        if data[i:i + 1] == b"T":
            comma = data.find(b",", i)
            try:
                length = int(data[i + 1:comma], 16)
            except ValueError:
                newline = data.find(b"\n", i)
                i = end if newline < 0 else newline + 1
                continue
            i = comma + 1 + length
            continue
        newline = data.find(b"\n", i)
        stop = end if newline < 0 else newline
        payload = data[i:stop]
        i = stop + 1
        if payload[:1] in (b"{", b"["):
            try:
                rows[row_id] = json.loads(payload.decode("utf-8"))
            except ValueError:
                pass
    return rows


def action_result(text):
    """What a server action returned.

    Row 0 says which row holds the value (``"a": "$@1"``), and that row
    is the function's return value
    """
    rows = parse_rsc(text)
    root = rows.get("0")
    ref = root.get("a") if isinstance(root, dict) else None
    if isinstance(ref, str) and ref.startswith("$@"):
        return rows.get(ref[2:])
    return rows.get("1")


def _walk(value, visit, owner=None):
    """Visit every dict, passing along the nearest enclosing entitlement"""
    stack = [(value, owner)]
    while stack:
        item, owner = stack.pop()
        # Children go on reversed, so they come off in page order
        if isinstance(item, list):
            stack.extend((child, owner) for child in reversed(item))
            continue
        if not isinstance(item, dict):
            continue
        if _is_entitlement(item):
            owner = item
        visit(item, owner)
        stack.extend((child, owner) for child in reversed(list(item.values())))


def _is_entitlement(item):
    return (
        isinstance(item.get("id"), str)
        and isinstance(item.get("type"), str)
        and isinstance(item.get("title"), str)
        and isinstance(item.get("downloads"), list)
    )


def entitlement_files(rows):
    """Every entitlement with downloads in a page, keyed by its id.

    The same download can appear more than once in a page (it is
    rendered in more than one place), so each is kept once per
    entitlement, keyed by its download key and platform
    """
    found = {}

    def visit(item, owner):
        if owner is None or not isinstance(item.get("downloadStruct"), list):
            return
        entry = found.setdefault(
            owner["id"], {"title": owner["title"], "downloads": {}}
        )
        key = (item.get("key"), item.get("platform"))
        entry["downloads"].setdefault(key, item)

    for row in rows.values():
        _walk(row, visit)

    return {
        ent_id: {"title": entry["title"], "downloads": list(entry["downloads"].values())}
        for ent_id, entry in found.items()
    }


def _expiry_epoch(raw):
    """Unix time from Humble's ISO timestamp, or None"""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def legacy_struct(struct, entitlement_id, download_key):
    """One file, as the old api described it"""
    entry = {
        "name": struct.get("name"),
        "file_size": struct.get("fileSize"),
        "md5": struct.get("md5"),
        "sha1": struct.get("sha1"),
    }
    web = (struct.get("url") or {}).get("web")
    if isinstance(web, str) and web:
        entry["url"] = {"web": web}
        if not web.startswith(("http://", "https://")):
            # A bare filename: the real url is minted on request
            entry["hbd_sign"] = {
                "entitlement": entitlement_id,
                "download_key": download_key,
            }
    return entry


def legacy_order(bundle_id, bundle_title, items):
    """A bundle in the shape the old order api returned.

    items is a list of (entitlement id, title, content id, downloads)
    """
    subproducts = []
    for ent_id, title, content_id, downloads in items:
        legacy_downloads = []
        for download in downloads:
            key = download.get("key")
            legacy_downloads.append(
                {
                    "platform": download.get("platform") or "",
                    "machine_name": key,
                    "download_struct": [
                        legacy_struct(struct, ent_id, key)
                        for struct in download.get("downloadStruct") or []
                        if isinstance(struct, dict)
                    ],
                }
            )
        subproducts.append(
            {
                "human_name": title,
                "machine_name": content_id,
                "entitlement_id": ent_id,
                "downloads": legacy_downloads,
            }
        )
    return {
        "gamekey": bundle_id,
        "product": {"human_name": bundle_title},
        "subproducts": subproducts,
    }


class HumbleSite:
    """The library, read the way the website reads it"""

    def __init__(self, session, base_url=BASE_URL, page_size=LIST_PAGE_SIZE,
                 readers=READERS, session_factory=None):
        self.session = session
        self.readers = max(1, int(readers))
        # A session per reader thread, since requests does not promise
        # one can be shared. Without a factory they share this one
        self._session_factory = session_factory
        self._local = threading.local()
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self._actions = None
        self._read_pages = set()
        self._actions_lock = threading.Lock()
        # Entries with nothing to download (notices, coupons, licence
        # texts) are common, and worth one summary line, not one each
        self.empty_entries = []

    def _url(self, path):
        return self.base_url + path

    def _get(self, path, session=None, **kwargs):
        response = (session or self.session).get(self._url(path), **kwargs)
        self._check_signed_in(response)
        return response

    def _check_signed_in(self, response):
        """Some answers redirect to auth.humblebundle.com when signed out.
        Most do not: see library() for the one that always tells"""
        final = getattr(response, "url", "") or ""
        if "://auth." in final or "/login" in final:
            raise NotSignedIn(self._not_signed_in_message())

    def _cookie_names(self):
        """Names, never values: (sent to the site, held for other hosts).

        A cookie file holds cookies for every humblebundle.com host, but
        only those whose domain covers the site's own host are sent, and
        the difference matters when working out why a sign in failed
        """
        host = self.base_url.split("://", 1)[-1].split("/", 1)[0]
        sent, elsewhere = set(), set()
        try:
            for cookie in self.session.cookies:
                domain = (cookie.domain or "").lower()
                if domain.lstrip(".") == host or (
                    domain.startswith(".") and host.endswith(domain)
                ):
                    sent.add(cookie.name)
                elif "humblebundle.com" in domain:
                    elsewhere.add(cookie.name)
        except Exception:
            pass
        header = (getattr(self.session, "headers", None) or {}).get("cookie") or ""
        for part in header.split(";"):
            name = part.split("=", 1)[0].strip()
            if name and "=" in part:
                sent.add(name)
        return sorted(sent), sorted(elsewhere - sent)

    def _not_signed_in_message(self):
        sent, elsewhere = self._cookie_names()
        detail = (
            "the cookies sent were: " + ", ".join(sent)
            if sent
            else "no cookies for this site were sent at all"
        )
        if elsewhere:
            detail += "; held but only for other hosts: " + ", ".join(elsewhere)
        return (
            "Humble says this is not signed in ({detail}). The cookie is "
            "missing, is not the session one, or has expired. Sign in again "
            "in your browser and pass a fresh one".format(detail=detail)
        )

    def _call(self, name, path, session=None, data=None, files=None):
        """Run one of the site's server actions and return its value"""
        for attempt in (1, 2):
            action_id = self._action_id(name, path)
            headers = {"Accept": "text/x-component", "Next-Action": action_id}
            if data is not None:
                headers["Content-Type"] = "text/plain;charset=UTF-8"
            response = (session or self.session).post(
                self._url(path), headers=headers, data=data, files=files
            )
            self._check_signed_in(response)
            if response.status_code != 404:
                break
            # What an action id from an older deploy gets. A deploy mid
            # run is rare but not impossible: look them up afresh, once
            with self._actions_lock:
                self._actions = None
                self._read_pages = set()
            if attempt == 2:
                raise SiteError(
                    "Humble did not recognise its own '{name}' action (http "
                    "404), even after looking it up again".format(name=name)
                )
            logger.info("Humble's site changed mid run, looking it up again")

        if response.status_code != 200:
            raise SiteError(
                "'{name}' failed with http {status}".format(
                    name=name, status=response.status_code
                )
            )
        value = action_result(response.text)
        if value is None:
            raise SiteError(
                "'{name}' returned something unreadable".format(name=name)
            )
        return value

    def _action_id(self, name, path):
        """The current id of a server action.

        Each page only loads the scripts it needs, so an action is
        looked for in the scripts of the page it is about to be posted
        to: the list action lives with the library page, the signing
        action with an item page
        """
        with self._actions_lock:
            found = (self._actions or {}).get(name)
            if found is not None:
                return found
            # Shared so two workers do not both read the same page
            if self._actions is None:
                self._actions = {}
                self._read_pages = set()
            if path not in self._read_pages:
                self._read_pages.add(path)
                self._actions.update(self._actions_on(path))
            found = self._actions.get(name)

        if found is None:
            raise SiteError(
                "Humble's website has no '{name}' action any more, so it "
                "has changed in a way this version does not understand".format(
                    name=name
                )
            )
        return found

    def _actions_on(self, path):
        """Server action ids by name, from the scripts one page loads"""
        found = {}
        html = self._get(path).text
        for chunk in dict.fromkeys(CHUNK_RE.findall(html)):
            try:
                script = self.session.get(self._url(chunk)).text
            except Exception:
                logger.debug("Could not read {chunk}".format(chunk=chunk))
                continue
            for action_id, name in ACTION_RE.findall(script):
                found.setdefault(name, action_id)
        logger.debug(
            "Found {count} server actions on {path}".format(
                count=len(found), path=path
            )
        )
        return found

    def library(self):
        """Every entry in the library, a page of a thousand at a time"""
        entries = []
        seen = set()
        page = 1
        pages = 1
        while page <= pages:
            fields = {
                "requestId": str(page),
                "query": "",
                "sort": "mostRecent",
                "cursor": str((page - 1) * self.page_size),
                "page": str(page),
                "limit": str(self.page_size),
                "hideRevealed": "false",
                "hideExpired": "false",
                "dateExpires": "",
            }
            # The action is (previous state, form data): "$K1" points at
            # the form fields, which carry the _1_ prefix
            files = [("_1_" + name, (None, value)) for name, value in fields.items()]
            files.append(("0", (None, json.dumps([{}, "$K1"]))))
            value = self._call(LIST_ACTION, LIBRARY_PATH, files=files)

            if isinstance(value, dict) and (
                value.get("success") is False or value.get("error")
            ):
                # Signed out gets a 200 and an empty library, with the
                # reason alongside. Taking that at face value would look
                # like a successful run that found nothing to do
                if value.get("error") == "not_authenticated":
                    raise NotSignedIn(self._not_signed_in_message())
                raise SiteError(
                    "Humble would not list the library ({error})".format(
                        error=value.get("error") or "no reason given"
                    )
                )

            results = value.get("results") if isinstance(value, dict) else None
            if not isinstance(results, list):
                raise SiteError("The library listing came back without results")
            for entry in results:
                if isinstance(entry, dict) and entry.get("id") not in seen:
                    seen.add(entry.get("id"))
                    entries.append(entry)

            pagination = value.get("pagination") or {}
            try:
                pages = int(pagination.get("totalPages", page))
            except (TypeError, ValueError):
                pages = page
            page += 1

        return entries

    def _reader_session(self):
        if self._session_factory is None:
            return self.session
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = self._session_factory()
        return session

    def page_files(self, path, session=None):
        """Entitlement files rendered on one page of the library"""
        response = self._get(path, session=session, headers={"RSC": "1"})
        if response.status_code != 200:
            raise SiteError(
                "{path} answered http {status}".format(
                    path=path, status=response.status_code
                )
            )
        return entitlement_files(parse_rsc(response.text))

    def vault(self):
        """The subscriber vault, as one order. It replaced the Trove.

        Its items come in the library page itself, files and all, under
        collection "subscriber-vault". Each is signed by its machine name
        through getVaultSignedUrls rather than by entitlement id
        """
        response = self._get(LIBRARY_PATH, headers={"RSC": "1"})
        found = {}

        def visit(item, owner):
            if item.get("collection") == "subscriber-vault" and item.get("id"):
                found.setdefault(item["id"], item)

        for row in parse_rsc(response.text).values():
            _walk(row, visit)

        items = []
        for entry in found.values():
            downloads = ((entry.get("entitlements") or {}).get("downloads")) or []
            for game in downloads:
                machine = game.get("id")
                files = [d for d in game.get("downloads") or [] if d.get("downloadStruct")]
                if not machine or not files:
                    continue
                title = re.sub(
                    r"\s*\(subscriber_vault\)\s*$", "",
                    game.get("title") or entry.get("title") or machine,
                )
                items.append((machine, title, machine, files))

        logger.info("The vault has {count} item(s)".format(count=len(items)))
        order = legacy_order(VAULT_ORDER, VAULT_TITLE, items)
        for sub in order["subproducts"]:
            for download in sub["downloads"]:
                for struct in download["download_struct"]:
                    if "hbd_sign" in struct:
                        struct["hbd_sign"]["vault"] = True
        return VAULT_ORDER, order

    def orders(self, keys=None):
        """Yield (bundle id, order) for each bundle with files in it.

        keys limits the run to those bundles, by the id in the url of
        the bundle's library page. Bundles come out in library order,
        with the next few already being read meanwhile, so downloads can
        start while the rest of the library is still being read
        """
        entries = self.library()
        downloadable = [e for e in entries if e.get("type") == DOWNLOADABLE]
        logger.info(
            "Library has {total} entries, {count} of them downloadable".format(
                total=len(entries), count=len(downloadable)
            )
        )
        bundles = {}
        for entry in downloadable:
            bundles.setdefault(entry.get("productEntitlementId"), []).append(entry)

        if keys:
            wanted = set(keys)
            unknown = wanted - set(bundles)
            for key in sorted(unknown):
                logger.warning(
                    "No downloadable bundle with id {key} in the library".format(
                        key=key
                    )
                )
            bundles = {k: v for k, v in bundles.items() if k in wanted}

        for bundle_id, order in self._read_ahead(list(bundles.items())):
            yield bundle_id, order

        if self.empty_entries:
            logger.info(
                "{count} library entries have no files to download (notices, "
                "coupons and the like)".format(count=len(self.empty_entries))
            )

    def _read_ahead(self, bundles):
        """(bundle id, order) in library order, with the next few bundles'
        pages already being read while one is checked. Whatever is still
        running when the caller stops listening is abandoned
        """
        if self.readers < 2 or len(bundles) < 2:
            for bundle_id, members in bundles:
                yield bundle_id, self._bundle_order(bundle_id, members)
            return

        pool = ThreadPoolExecutor(
            max_workers=self.readers, thread_name_prefix="hbd-read"
        )
        pending = []
        upcoming = iter(bundles)
        try:
            while True:
                # Keep a couple of pages per reader in hand, not the
                # whole library: each holds a bundle's worth of listing
                while len(pending) < self.readers * 2:
                    try:
                        bundle_id, members = next(upcoming)
                    except StopIteration:
                        break
                    pending.append((bundle_id, pool.submit(
                        self._bundle_order, bundle_id, members,
                        session=None,
                    )))
                if not pending:
                    return
                bundle_id, future = pending.pop(0)
                yield bundle_id, future.result()
        finally:
            for _, future in pending:
                future.cancel()
            pool.shutdown(wait=False)

    def _bundle_order(self, bundle_id, members, session=None):
        session = session or self._reader_session()
        files = {}
        if bundle_id:
            try:
                files = self.page_files(LIBRARY_PATH + "/" + bundle_id, session)
            except NotSignedIn:
                raise
            except Exception as error:
                logger.debug(
                    "Bundle page {bundle_id} unusable ({error}), reading "
                    "its items one by one".format(bundle_id=bundle_id, error=error)
                )

        missing = [e for e in members if not (files.get(e["id"]) or {}).get("downloads")]
        if len(missing) > 1:
            # Each is its own request, about a second apiece. Worth
            # saying, or a large bundle looks like a stalled run
            logger.info(
                "{count} item(s) of this bundle are not on its page, reading "
                "each from its own".format(count=len(missing))
            )

        def own_page(entry):
            # Not on the bundle page: ask the item's own page
            try:
                return self.page_files(
                    LIBRARY_PATH + "/content/" + entry["id"],
                    self._reader_session(),
                ).get(entry["id"])
            except NotSignedIn:
                raise
            except Exception as error:
                logger.warning(
                    "Could not read {title}: {error}".format(
                        title=entry.get("title"), error=error
                    )
                )
                return None

        if len(missing) > 1 and self.readers > 1:
            with ThreadPoolExecutor(
                max_workers=self.readers, thread_name_prefix="hbd-item"
            ) as pool:
                read = dict(zip(
                    (e["id"] for e in missing), pool.map(own_page, missing)
                ))
        else:
            read = {e["id"]: own_page(e) for e in missing}

        items = []
        for entry in members:
            found = files.get(entry["id"])
            if not found or not found["downloads"]:
                found = read.get(entry["id"])
            if not found or not found["downloads"]:
                self.empty_entries.append(entry.get("title"))
                continue
            items.append(
                (
                    entry["id"],
                    found["title"] or entry.get("title") or entry["id"],
                    entry.get("contentId"),
                    found["downloads"],
                )
            )

        titles = [e.get("subTitle") for e in members if e.get("subTitle")]
        bundle_title = (
            max(set(titles), key=titles.count) if titles else bundle_id or "Library"
        )
        return legacy_order(bundle_id, bundle_title, items)

    def sign(self, entitlement_ids, session=None, vault=False):
        """Download urls for some entitlements, or with vault for some
        vault items by machine name.

        Returns ({(download key, filename): url}, expiry as unix time).
        Humble spends about half a second per entitlement and gives up
        at around ten seconds, so ask for a handful at a time at most
        """
        entitlement_ids = list(entitlement_ids)
        if vault:
            name, path = VAULT_SIGN_ACTION, LIBRARY_PATH
        else:
            name, path = SIGN_ACTION, LIBRARY_PATH + "/content/" + entitlement_ids[0]
        value = self._call(
            name,
            path,
            session=session,
            data=json.dumps([entitlement_ids]),
        )
        if not isinstance(value, dict) or not value.get("ok"):
            code = value.get("code") if isinstance(value, dict) else None
            if code == "not_authenticated":
                raise NotSignedIn(self._not_signed_in_message())
            if code is None and value.get("error") == "not_authenticated":
                raise NotSignedIn(self._not_signed_in_message())
            raise SiteError(
                "Humble would not sign a download url ({code})".format(
                    code=code or "no reason given"
                )
            )
        data = value.get("data") or {}
        urls = {}
        for keys in (data.get("signedUrls") or {}).values():
            for download_key, names in (keys or {}).items():
                for filename, url in (names or {}).items():
                    if isinstance(url, str) and not filename.endswith(".torrent"):
                        urls[(download_key, filename)] = url
        return urls, _expiry_epoch(data.get("expiresAt"))


class UrlSigner:
    """Signed urls, fetched once per entitlement and reused until they
    are about to lapse. Safe to call from every worker at once
    """

    def __init__(self, site, margin=SIGNED_URL_MARGIN, clock=time.time):
        self.site = site
        self.margin = margin
        self.clock = clock
        self._signed = {}
        self._locks = {}
        self._lock = threading.Lock()

    def _lock_for(self, entitlement_id):
        with self._lock:
            return self._locks.setdefault(entitlement_id, threading.Lock())

    def url_for(self, entitlement_id, download_key, filename, session=None,
                refresh=False, vault=False):
        slot = ("vault", entitlement_id) if vault else entitlement_id
        with self._lock_for(slot):
            signed = self._signed.get(slot)
            now = self.clock()
            stale = signed is None or refresh
            if not stale and signed[1] is not None and signed[1] - self.margin < now:
                # Close to lapsing. Humble hands back the same url until
                # it does, so asking again every time would only repeat
                # the same answer: ask at most once a minute
                stale = now - signed[2] >= RESIGN_INTERVAL
            if stale:
                if vault:
                    urls, expires = self.site.sign(
                        [entitlement_id], session=session, vault=True
                    )
                else:
                    urls, expires = self.site.sign([entitlement_id], session=session)
                signed = (urls, expires, now)
                self._signed[slot] = signed

        urls = signed[0]
        url = urls.get((download_key, filename))
        if url is None:
            # Keyed differently than expected: settle for the filename
            matches = [u for (_, name), u in urls.items() if name == filename]
            if len(matches) == 1:
                url = matches[0]
        if url is None:
            raise SiteError(
                "Humble signed {count} url(s) for this item, but none for "
                "{filename}".format(count=len(urls), filename=filename)
            )
        return url


class SignedUrl:
    """A download url that does not exist until it is asked for.

    Queued in place of a url, so that signing happens in the worker just
    before the transfer starts, not when the library is read. Urls signed
    up front would expire long before a large library finished
    """

    def __init__(self, signer, entitlement_id, download_key, filename,
                 vault=False):
        self.signer = signer
        self.entitlement_id = entitlement_id
        self.download_key = download_key
        self.filename = filename
        self.vault = vault

    def resolve(self, session=None, refresh=False):
        kwargs = {"vault": True} if self.vault else {}
        return self.signer.url_for(
            self.entitlement_id,
            self.download_key,
            self.filename,
            session=session,
            refresh=refresh,
            **kwargs
        )

    def __str__(self):
        return "{filename} (url signed when downloaded)".format(
            filename=self.filename
        )

    __repr__ = __str__
