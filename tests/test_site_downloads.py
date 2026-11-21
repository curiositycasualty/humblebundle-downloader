"""The download engine driven by the rebuilt site, end to end, against
a fake site and a fake CDN"""
import json
import hashlib
import os
import threading

import pytest

from humblebundle_downloader.download_library import (
    DownloadLibrary,
    _looks_like_cookie_header,
)
from humblebundle_downloader.humble_site import NotSignedIn, SiteError

from .test_humble_site import (
    FakeSite,
    bundle_page,
    download,
    entitlement,
    entry,
)

LAST_MODIFIED = "Thu, 08 Oct 2026 16:35:42 GMT"

BODIES = {
    "one.pdf": b"the first book, " * 40,
    "one.epub": b"the first book as an epub " * 10,
    "two.pdf": b"the second book " * 25,
    "game.exe": b"MZ a game " * 60,
}


def real_struct(name, body=None, kind="PDF"):
    body = BODIES[name] if body is None else body
    return {
        "name": kind,
        "fileSize": len(body),
        "md5": hashlib.md5(body).hexdigest(),
        "sha1": hashlib.sha1(body).hexdigest(),
        "url": {"web": name, "bittorrent": "t"},
    }


class CdnResponse:
    def __init__(self, body, status_code=200, url="", headers=None, streamed=None):
        self.status_code = status_code
        self._streamed = streamed
        self._name = url.split("?")[0].rsplit("/", 1)[-1]
        self._body = body
        self.url = url
        self.headers = {"Content-Length": str(len(body)), "Last-Modified": LAST_MODIFIED}
        self.headers.update(headers or {})
        self.connection = self
        self.closed = False

    def iter_content(self, chunk_size=4096):
        if self._streamed is not None:
            self._streamed.append(self._name)
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeHumble(FakeSite):
    """The fake site plus a CDN behind it.

    Tokens are numbered by signing call; any in `revoked` are refused
    with a 403, the way an expired or tampered url is
    """

    def __init__(self, entries, pages, bodies=None, sign_reply=None):
        self.sign_calls = []
        self.revoked = set()
        self.cdn_gets = []
        # Names of files whose body was actually read
        self.streamed = []
        self.bodies = dict(BODIES if bodies is None else bodies)
        self._lock = threading.Lock()
        self._sign_reply = sign_reply
        FakeSite.__init__(self, entries, pages, signer=self._sign)

    def _sign(self, ids):
        with self._lock:
            self.sign_calls.append(list(ids))
            token = len(self.sign_calls)
        if self._sign_reply is not None:
            return self._sign_reply(ids)
        urls = {}
        for path, text in self.pages.items():
            if not path.startswith("/home/library/") or "/content/" in path:
                continue
            for ent_id in ids:
                if '"id": "{0}"'.format(ent_id) not in text:
                    continue
                # Every file of the entitlement, as the site does
                for line in text.splitlines():
                    if ent_id not in line:
                        continue
                    for name in self.bodies:
                        if '"web": "{0}"'.format(name) in line:
                            urls.setdefault("m", {}).setdefault("k-" + name, {})[name] = (
                                "https://cdn.example/{0}?t={1}".format(name, token)
                            )
        return {"ok": True, "data": {"signedUrls": urls, "expiresAt": "2030-01-01T00:00:00Z"}}

    def get(self, url, headers=None, stream=False, **kwargs):
        if not url.startswith("https://cdn.example/"):
            return FakeSite.get(self, url, headers=headers, **kwargs)
        name, _, token = url[len("https://cdn.example/"):].partition("?t=")
        with self._lock:
            self.cdn_gets.append((name, token, dict(headers or {})))
        if int(token) in self.revoked:
            return CdnResponse(b"", 403, url)
        if name in getattr(self, "missing", ()):
            return CdnResponse(b"", 404, url)
        body = self.bodies[name]
        start = 0
        changed = getattr(self, "last_modified", LAST_MODIFIED)
        if headers and headers.get("If-Range") and headers["If-Range"] != changed:
            # Not the file the caller started on: all of the new one
            return CdnResponse(body, 200, url, {"Last-Modified": changed},
                               streamed=self.streamed)
        if headers and headers.get("Range"):
            start = int(headers["Range"].split("=")[1].rstrip("-"))
            return CdnResponse(body[start:], 206, url, {
                "Content-Range": "bytes {0}-{1}/{2}".format(start, len(body) - 1, len(body)),
                "Last-Modified": changed},
                streamed=self.streamed)
        return CdnResponse(body, 200, url, {"Last-Modified": changed}, streamed=self.streamed)

    def head(self, url, **kwargs):
        return self.get(url, **kwargs)


def two_bundles():
    entries = [
        entry("e1", "b1", "Book One", "Bundle One"),
        entry("e2", "b1", "Book Two", "Bundle One"),
        entry("e3", "b2", "Game", "Bundle Two"),
    ]
    pages = {
        "/home/library/b1": bundle_page(
            "b1", "Bundle One",
            entitlement("e1", "Book One", download("k-one", "ebook",
                        real_struct("one.pdf"), real_struct("one.epub", kind="EPUB"))),
            entitlement("e2", "Book Two", download("k-two", "ebook", real_struct("two.pdf"))),
        ),
        "/home/library/b2": bundle_page(
            "b2", "Bundle Two",
            entitlement("e3", "Game", download("k-game", "windows", real_struct("game.exe"))),
        ),
    }
    return entries, pages


def library(tmp_path, fake, **kwargs):
    kwargs.setdefault("retries", 0)
    dl = DownloadLibrary(str(tmp_path / "lib"), **kwargs)
    dl.session = fake
    dl._new_session = lambda: fake
    return dl


def files_under(root):
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*")
        if p.is_file() and p.name != ".cache.json"
    )


def cache_of(tmp_path):
    return json.loads((tmp_path / "lib" / ".cache.json").read_text())


@pytest.mark.parametrize("jobs", [1, 3])
def test_whole_library_downloads(tmp_path, jobs):
    fake = FakeHumble(*two_bundles())
    library(tmp_path, fake, jobs=jobs).start()

    root = tmp_path / "lib"
    assert files_under(root) == [
        "Bundle One/Book One/one.epub",
        "Bundle One/Book One/one.pdf",
        "Bundle One/Book Two/two.pdf",
        "Bundle Two/Game/game.exe",
    ]
    for path in root.rglob("*"):
        if path.is_file() and path.name in BODIES:
            assert path.read_bytes() == BODIES[path.name]
    assert set(cache_of(tmp_path)) == {
        "b1:one.pdf", "b1:one.epub", "b1:two.pdf", "b2:game.exe",
    }
    # One signing per item, not per file
    assert sorted(i for call in fake.sign_calls for i in call) == ["e1", "e2", "e3"]


def test_checksums_from_the_site_are_verified(tmp_path):
    entries, pages = two_bundles()
    fake = FakeHumble(entries, pages, bodies=dict(BODIES, **{"two.pdf": b"x" * len(BODIES["two.pdf"])}))
    library(tmp_path, fake, jobs=1).start()
    root = tmp_path / "lib"
    assert "Bundle One/Book Two/two.pdf" not in files_under(root)
    assert "b1:two.pdf" not in cache_of(tmp_path)
    assert "b1:one.pdf" in cache_of(tmp_path)


def test_a_second_run_downloads_nothing(tmp_path):
    fake = FakeHumble(*two_bundles())
    library(tmp_path, fake, jobs=2).start()
    before = len(fake.cdn_gets)
    signs = len(fake.sign_calls)
    library(tmp_path, fake, jobs=2).start()
    assert len(fake.cdn_gets) == before
    assert len(fake.sign_calls) == signs


def test_dry_run_signs_nothing_and_sizes_everything(tmp_path):
    fake = FakeHumble(*two_bundles())
    dl = library(tmp_path, fake, dry_run=True)
    dl.start()
    assert fake.sign_calls == []
    assert fake.cdn_gets == []
    assert sum(p["file_size"] for p in dl.pending_downloads) == sum(len(b) for b in BODIES.values())
    assert all("t=" not in p["url"] for p in dl.pending_downloads)


def test_dry_run_print_urls_signs_them(tmp_path, capsys):
    fake = FakeHumble(*two_bundles())
    library(tmp_path, fake, dry_run=True, print_urls=True).start()
    printed = capsys.readouterr().out.split()
    assert sorted(u.split("?")[0].rsplit("/", 1)[1] for u in printed) == sorted(BODIES)
    assert all(u.startswith("https://cdn.example/") for u in printed)


def test_filters_still_apply(tmp_path):
    fake = FakeHumble(*two_bundles())
    library(tmp_path, fake, platform_include=["ebook"], prefer_format=["epub", "pdf"]).start()
    assert files_under(tmp_path / "lib") == [
        "Bundle One/Book One/one.epub",
        "Bundle One/Book Two/two.pdf",
    ]
    # Nothing was signed for the game the filter left out
    assert "e3" not in [i for call in fake.sign_calls for i in call]


def test_keys_limit_the_run_to_some_bundles(tmp_path):
    fake = FakeHumble(*two_bundles())
    library(tmp_path, fake, purchase_keys=["b2", "b2"]).start()
    assert files_under(tmp_path / "lib") == ["Bundle Two/Game/game.exe"]


def test_a_refused_url_is_signed_again(tmp_path):
    fake = FakeHumble(*two_bundles())
    fake.revoked = {1}
    library(tmp_path, fake, jobs=1, purchase_keys=["b2"]).start()
    assert files_under(tmp_path / "lib") == ["Bundle Two/Game/game.exe"]
    assert [t for _, t, _ in fake.cdn_gets] == ["1", "2"]


def test_a_url_refused_twice_is_a_failed_file_not_a_crash(tmp_path):
    fake = FakeHumble(*two_bundles())
    fake.revoked = {1, 2, 3, 4, 5, 6, 7, 8}
    library(tmp_path, fake, jobs=1, purchase_keys=["b2"]).start()
    assert files_under(tmp_path / "lib") == []


def test_signed_out_mid_run_stops_the_run(tmp_path):
    fake = FakeHumble(*two_bundles(), sign_reply=lambda ids: {"ok": False, "code": "not_authenticated"})
    with pytest.raises(NotSignedIn):
        library(tmp_path, fake, jobs=2).start()
    assert files_under(tmp_path / "lib") == []
    # It noticed straight away rather than trying every file
    assert len(fake.sign_calls) <= 3


def test_signed_out_from_the_start_is_reported(tmp_path):
    fake = FakeHumble(*two_bundles())
    fake.signed_out = True
    with pytest.raises(NotSignedIn):
        library(tmp_path, fake).start()


def test_files_already_on_disk_are_adopted_not_downloaded(tmp_path):
    fake = FakeHumble(*two_bundles())
    have = tmp_path / "lib" / "Bundle One" / "Book One" / "one.pdf"
    have.parent.mkdir(parents=True)
    have.write_bytes(BODIES["one.pdf"])
    wrong = tmp_path / "lib" / "Bundle One" / "Book Two" / "two.pdf"
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(b"short")

    dl = library(tmp_path, fake, jobs=1)
    dl.start()

    fetched = [name for name, _, _ in fake.cdn_gets]
    assert "one.pdf" not in fetched
    assert "two.pdf" in fetched
    assert wrong.read_bytes() == BODIES["two.pdf"]
    assert cache_of(tmp_path)["b1:one.pdf"] == {"adopted": True}
    assert dl.adopted_count == 1


def test_dry_run_counts_adoptable_files_but_writes_nothing(tmp_path):
    fake = FakeHumble(*two_bundles())
    have = tmp_path / "lib" / "Bundle Two" / "Game" / "game.exe"
    have.parent.mkdir(parents=True)
    have.write_bytes(BODIES["game.exe"])
    dl = library(tmp_path, fake, dry_run=True)
    dl.start()
    assert dl.adopted_count == 1
    assert "game.exe" not in [p["local_file"].rsplit("/", 1)[1] for p in dl.pending_downloads]
    assert not (tmp_path / "lib" / ".cache.json").exists()


def test_update_takes_an_adopted_file_as_its_baseline(tmp_path):
    fake = FakeHumble(*two_bundles())
    have = tmp_path / "lib" / "Bundle Two" / "Game" / "game.exe"
    have.parent.mkdir(parents=True)
    have.write_bytes(BODIES["game.exe"])
    library(tmp_path, fake, purchase_keys=["b2"]).start()
    assert cache_of(tmp_path)["b2:game.exe"] == {"adopted": True}

    library(tmp_path, fake, purchase_keys=["b2"], update=True).start()
    entry_now = cache_of(tmp_path)["b2:game.exe"]
    assert entry_now["url_last_modified"] == LAST_MODIFIED
    # Only the headers were looked at: the body was never fetched
    assert fake.streamed == []
    # Nothing renamed or rewritten
    assert files_under(tmp_path / "lib") == ["Bundle Two/Game/game.exe"]

    # And a later update compares like for like, so skips it quietly
    gets = len(fake.cdn_gets)
    library(tmp_path, fake, purchase_keys=["b2"], update=True).start()
    assert files_under(tmp_path / "lib") == ["Bundle Two/Game/game.exe"]
    assert len(fake.cdn_gets) == gets + 1
    assert fake.streamed == []


def test_trove_says_it_is_gone(tmp_path):
    with pytest.raises(SiteError, match="vault"):
        DownloadLibrary(str(tmp_path), trove=True).start()


@pytest.mark.parametrize("value,header", [
    ("_simpleauth_sess=eyJabc", True),
    ("a=b; c=d", True),
    ("eyJhbGciOiJIUzI1NiJ9abc==", False),
    ("eyJhbGciOiJIUzI1NiJ9abc=", False),
    ('"eyJhbGc|1234|abcd"', False),
    ("", False),
])
def test_cookie_header_or_bare_value(value, header):
    assert _looks_like_cookie_header(value) is header


def _jar(dl):
    return {(c.name, c.value, c.domain) for c in dl.session.cookies}


def test_a_bare_session_value_still_becomes_simpleauth(tmp_path):
    dl = DownloadLibrary(str(tmp_path), cookie_auth="abc==")
    assert _jar(dl) == {("_simpleauth_sess", "abc==", "www.humblebundle.com")}
    assert "cookie" not in {k.lower() for k in dl.session.headers}


def test_a_cookie_header_goes_into_the_jar(tmp_path):
    dl = DownloadLibrary(str(tmp_path), cookie_auth=" session=xyz; other=1=2 ;")
    assert _jar(dl) == {
        ("session", "xyz", "www.humblebundle.com"),
        ("other", "1=2", "www.humblebundle.com"),
    }
    assert "cookie" not in {k.lower() for k in dl.session.headers}


def test_an_old_style_raw_cookie_file_goes_into_the_jar(tmp_path):
    raw = tmp_path / "cookie.txt"
    raw.write_text("_simpleauth_sess=abc; other=1\n")
    dl = DownloadLibrary(str(tmp_path / "lib"), cookie_path=str(raw))
    assert ("_simpleauth_sess", "abc", "www.humblebundle.com") in _jar(dl)


class RedirectOnce:
    """A real requests transport: the first fetch of a page is answered
    with a 307 to the same page plus ?_rsc=, as Next.js now does, and the
    second is only served the library if the session cookie came along"""

    def __init__(self):
        self.seen = []

    def send(self, request, **kwargs):
        import requests

        self.seen.append((request.url, request.headers.get("Cookie")))
        response = requests.Response()
        response.request = request
        response.url = request.url
        if "_rsc=" not in request.url:
            response.status_code = 307
            response.headers["Location"] = request.url + "?_rsc=abc"
            response._content = b""
        else:
            signed_in = "ory_session_x=y" in (request.headers.get("Cookie") or "")
            response.status_code = 200
            response._content = (
                b'1:{"userLibraryItem":{"entitlements":{"downloads":[{"id":"e1",'
                b'"title":"Book","type":"download_drm_free","downloads":[{"key":'
                b'"k","platform":"ebook","downloadStruct":[{"name":"PDF",'
                b'"fileSize":1,"url":{"web":"b.pdf"}}]}]}]}}}\n'
                if signed_in else b'1:{"signedOut":true}\n'
            )
        return response

    def close(self):
        pass


def test_cookies_survive_the_rsc_redirect(tmp_path):
    from humblebundle_downloader.humble_site import HumbleSite

    dl = DownloadLibrary(str(tmp_path), cookie_auth="ory_session_x=y; other=1")
    transport = RedirectOnce()
    dl.session.mount("https://", transport)
    files = HumbleSite(dl.session).page_files("/home/library/b1")
    assert list(files) == ["e1"]
    assert [bool(cookie) for _, cookie in transport.seen] == [True, True]


def test_cookies_are_not_sent_to_the_download_cdn(tmp_path):
    dl = DownloadLibrary(str(tmp_path), cookie_auth="ory_session_x=y")
    transport = RedirectOnce()
    dl.session.mount("https://", transport)
    dl.session.get("https://cdn.humble.com/humblebundle/b.pdf?_rsc=1&t=tok")
    assert transport.seen[-1][1] is None


def test_a_cookie_file_keeps_session_cookies(tmp_path):
    """Exported session cookies have no expiry, or 0, and are HttpOnly"""
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.humblebundle.com\tTRUE\t/\tTRUE\t0\tsession_zero\tA\n"
        ".humblebundle.com\tTRUE\t/\tTRUE\t\tsession_blank\tB\n"
        "www.humblebundle.com\tFALSE\t/\tTRUE\t4102444800\tlong_lived\tC\n"
    )
    dl = DownloadLibrary(str(tmp_path / "lib"), cookie_path=str(cookies))
    assert sorted(c.name for c in dl.session.cookies) == [
        "long_lived", "session_blank", "session_zero",
    ]


###
# Humble's listing out of date
###
NEWER = b"a newer edition of the second book, rescanned " * 40


def test_a_newer_file_than_the_listing_is_kept_not_retried(tmp_path, caplog):
    import logging

    caplog.set_level(logging.WARNING)
    entries, pages = two_bundles()
    fake = FakeHumble(entries, pages, bodies=dict(BODIES, **{"two.pdf": NEWER}))
    dl = library(tmp_path, fake, jobs=1, purchase_keys=["b1"])
    dl.start()

    saved = tmp_path / "lib" / "Bundle One" / "Book Two" / "two.pdf"
    assert saved.read_bytes() == NEWER
    assert [n for n, _, _ in fake.cdn_gets].count("two.pdf") == 1
    entry = cache_of(tmp_path)["b1:two.pdf"]
    assert entry["sha1"] == hashlib.sha1(NEWER).hexdigest()
    assert dl.stale_listings == 1
    assert "not the version Humble lists" in caplog.text
    assert "1 file(s) were a different version" in caplog.text


def test_a_damaged_file_at_the_listed_size_is_still_rejected(tmp_path):
    entries, pages = two_bundles()
    damaged = b"z" * len(BODIES["two.pdf"])
    fake = FakeHumble(entries, pages, bodies=dict(BODIES, **{"two.pdf": damaged}))
    dl = library(tmp_path, fake, jobs=1, purchase_keys=["b1"], retries=1)
    dl.start()
    assert not (tmp_path / "lib" / "Bundle One" / "Book Two" / "two.pdf").exists()
    assert dl.stale_listings == 0


def test_a_newer_edition_already_on_disk_is_adopted_after_one_head(tmp_path):
    entries, pages = two_bundles()
    fake = FakeHumble(entries, pages, bodies=dict(BODIES, **{"two.pdf": NEWER}))
    have = tmp_path / "lib" / "Bundle One" / "Book Two" / "two.pdf"
    have.parent.mkdir(parents=True)
    have.write_bytes(NEWER)

    dl = library(tmp_path, fake, jobs=1, purchase_keys=["b1"])
    dl.start()
    assert "two.pdf" not in fake.streamed
    assert cache_of(tmp_path)["b1:two.pdf"] == {"adopted": True}
    assert dl.adopted_count == 1


def test_a_file_on_disk_matching_neither_is_downloaded_again(tmp_path):
    entries, pages = two_bundles()
    fake = FakeHumble(entries, pages, bodies=dict(BODIES, **{"two.pdf": NEWER}))
    have = tmp_path / "lib" / "Bundle One" / "Book Two" / "two.pdf"
    have.parent.mkdir(parents=True)
    have.write_bytes(b"half a file")
    library(tmp_path, fake, jobs=1, purchase_keys=["b1"]).start()
    assert have.read_bytes() == NEWER


###
# --print-checksums
###
def test_print_checksums_lists_what_humble_expects(tmp_path, capsys):
    fake = FakeHumble(*two_bundles())
    library(tmp_path, fake, dry_run=True, print_checksums=True).start()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("# sha1 as Humble lists them")
    listed = dict(line.split("  ", 1)[::-1] for line in lines[1:])
    assert listed == {
        os.path.join("Bundle One", "Book One", "one.pdf"): hashlib.sha1(BODIES["one.pdf"]).hexdigest(),
        os.path.join("Bundle One", "Book One", "one.epub"): hashlib.sha1(BODIES["one.epub"]).hexdigest(),
        os.path.join("Bundle One", "Book Two", "two.pdf"): hashlib.sha1(BODIES["two.pdf"]).hexdigest(),
        os.path.join("Bundle Two", "Game", "game.exe"): hashlib.sha1(BODIES["game.exe"]).hexdigest(),
    }


def test_printed_checksums_verify_the_downloaded_files(tmp_path, capsys):
    import subprocess

    fake = FakeHumble(*two_bundles())
    library(tmp_path, fake, jobs=2, print_checksums=True).start()
    listing = tmp_path / "SHA1SUMS"
    listing.write_text(capsys.readouterr().out)
    tool = ["shasum", "-c"] if subprocess.call(["which", "shasum"], stdout=subprocess.DEVNULL) == 0 else ["sha1sum", "-c"]
    result = subprocess.run(tool + [str(listing)], cwd=tmp_path / "lib", capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count(": OK") == 4


def test_a_file_without_a_listed_sha1_says_so(tmp_path, capsys):
    entries, pages = two_bundles()
    pages["/home/library/b2"] = pages["/home/library/b2"].replace(
        '"sha1": "' + hashlib.sha1(BODIES["game.exe"]).hexdigest() + '"', '"sha1": null'
    )
    fake = FakeHumble(entries, pages)
    library(tmp_path, fake, dry_run=True, print_checksums=True, purchase_keys=["b2"]).start()
    out = capsys.readouterr().out
    assert "# no sha1 listed: " + os.path.join("Bundle Two", "Game", "game.exe") in out


def test_files_finished_before_a_run_fails_are_still_saved_to_the_cache(tmp_path):
    entries, pages = two_bundles()
    holder = {}

    def sign(ids):
        if "e3" in ids:
            return {"ok": False, "code": "not_authenticated"}
        return FakeHumble._sign(holder["fake"], ids)

    fake = FakeHumble(entries, pages)
    holder["fake"] = fake
    fake._sign_reply = None
    fake.signer = sign
    with pytest.raises(NotSignedIn):
        library(tmp_path, fake, jobs=1).start()
    saved = cache_of(tmp_path)
    assert {"b1:one.pdf", "b1:one.epub", "b1:two.pdf"} <= set(saved)
    assert "b2:game.exe" not in saved



###
# failed files
###
def test_a_file_the_server_does_not_have_is_a_named_failure(tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO)
    fake = FakeHumble(*two_bundles())
    fake.missing = {"one.epub"}
    dl = library(tmp_path, fake, jobs=2)
    dl.start()
    assert [(os.path.basename(p), why) for p, why in dl.failed_files] == [
        ("one.epub", "the server answered http 404")
    ]
    assert "1 file(s) could not be downloaded" in caplog.text
    assert os.path.join("Bundle One", "Book One", "one.epub") in caplog.text
    assert "b1:one.epub" not in cache_of(tmp_path)
    assert "b1:one.pdf" in cache_of(tmp_path)


def test_a_damaged_file_is_a_named_failure(tmp_path):
    entries, pages = two_bundles()
    damaged = b"z" * len(BODIES["two.pdf"])
    fake = FakeHumble(entries, pages, bodies=dict(BODIES, **{"two.pdf": damaged}))
    dl = library(tmp_path, fake, jobs=1, retries=0)
    dl.start()
    assert len(dl.failed_files) == 1
    path, why = dl.failed_files[0]
    assert path.endswith("two.pdf") and "sha1" in why


def test_a_clean_run_has_no_failures(tmp_path):
    dl = library(tmp_path, FakeHumble(*two_bundles()), jobs=3)
    dl.start()
    assert dl.failed_files == []


def test_files_not_reached_because_the_run_stopped_are_not_failures(tmp_path):
    dl = DownloadLibrary(str(tmp_path))
    dl._stop.set()
    dl._record_failure("/x/y.pdf", "whatever")
    assert dl.failed_files == []


@pytest.mark.parametrize("failed,code", [([], None), ([("a.pdf", "http 404")], 1)])
def test_the_cli_exits_non_zero_only_when_something_failed(monkeypatch, failed, code):
    from humblebundle_downloader import cli as cli_module

    def start(self):
        self.failed_files = list(failed)

    monkeypatch.setattr(DownloadLibrary, "start", start)
    monkeypatch.setattr(cli_module.sys, "argv", ["hbd", "-s", "x", "-l", "lib"])
    if code is None:
        assert cli_module.cli() is None
    else:
        with pytest.raises(SystemExit) as stopped:
            cli_module.cli()
        assert stopped.value.code == code



###
# carrying on from an earlier run
###
BIG = bytes(range(256)) * 200          # 51,200 bytes: a dozen chunks


def one_big_file(body=BIG):
    entries = [entry("g1", "bg", "Big Game", "Big Bundle")]
    pages = {"/home/library/bg": bundle_page(
        "bg", "Big Bundle",
        entitlement("g1", "Big Game", download("k-big.zip", "windows", real_struct("big.zip", body=body))),
    )}
    return entries, pages


class StopAfter:
    """Has the CDN ask the run to stop after so many chunks of a file"""

    def __init__(self, fake, chunks):
        self.fake, self.chunks, self.dl = fake, chunks, None
        original = CdnResponse.iter_content
        hook = self

        def iter_content(response, chunk_size=4096):
            for n, chunk in enumerate(original(response, chunk_size)):
                if hook.dl is not None and n == hook.chunks:
                    hook.dl._stop.set()
                yield chunk

        self.patch = iter_content


def stopped_part_way(tmp_path, monkeypatch, chunks=4, body=BIG):
    fake = FakeHumble(*one_big_file(body), bodies={"big.zip": body})
    stopper = StopAfter(fake, chunks)
    monkeypatch.setattr(CdnResponse, "iter_content", stopper.patch)
    dl = library(tmp_path, fake, jobs=1)
    stopper.dl = dl
    with pytest.raises(SystemExit) as stopped:
        dl.start()
    assert stopped.value.code == 130
    monkeypatch.undo()
    return fake


def test_a_stopped_transfer_keeps_its_part_file_and_note(tmp_path, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO)
    stopped_part_way(tmp_path, monkeypatch)
    assert "the next run carries on from there" in caplog.text
    folder = tmp_path / "lib" / "Big Bundle" / "Big Game"
    part = folder / "big.zip.part"
    assert part.exists() and 0 < part.stat().st_size < len(BIG)
    assert json.loads((folder / "big.zip.part.json").read_text()) == {"validator": LAST_MODIFIED}
    assert not (folder / "big.zip").exists()


def test_the_next_run_carries_on_where_it_stopped(tmp_path, monkeypatch):
    stopped_part_way(tmp_path, monkeypatch)
    folder = tmp_path / "lib" / "Big Bundle" / "Big Game"
    held = (folder / "big.zip.part").stat().st_size

    fake = FakeHumble(*one_big_file(), bodies={"big.zip": BIG})
    library(tmp_path, fake, jobs=1).start()

    assert (folder / "big.zip").read_bytes() == BIG
    assert not (folder / "big.zip.part").exists()
    assert not (folder / "big.zip.part.json").exists()
    # One request for the rest, not a second go from the top
    assert len(fake.cdn_gets) == 1
    name, _, headers = fake.cdn_gets[0]
    assert headers.get("Range") == "bytes={0}-".format(held)
    assert headers.get("If-Range") == LAST_MODIFIED
    # And the checksum, over earlier bytes and new, matched the listing
    assert cache_of(tmp_path)["bg:big.zip"]["sha1"] == hashlib.sha1(BIG).hexdigest()


def test_a_file_changed_since_starts_again_from_the_top(tmp_path, monkeypatch):
    stopped_part_way(tmp_path, monkeypatch)
    folder = tmp_path / "lib" / "Big Bundle" / "Big Game"
    newer = bytes(reversed(range(256))) * 200

    fake = FakeHumble(*one_big_file(newer), bodies={"big.zip": newer})
    fake.last_modified = "Fri, 20 Nov 2026 09:00:00 GMT"
    library(tmp_path, fake, jobs=1).start()
    assert (folder / "big.zip").read_bytes() == newer


def test_a_failed_transfer_throws_its_part_away(tmp_path):
    entries, pages = one_big_file()
    fake = FakeHumble(entries, pages, bodies={"big.zip": b"q" * len(BIG)})
    library(tmp_path, fake, jobs=1, retries=0).start()
    folder = tmp_path / "lib" / "Big Bundle" / "Big Game"
    assert not (folder / "big.zip.part").exists()
    assert not (folder / "big.zip.part.json").exists()


def test_a_part_file_without_a_note_is_not_trusted(tmp_path):
    folder = tmp_path / "lib" / "Big Bundle" / "Big Game"
    folder.mkdir(parents=True)
    (folder / "big.zip.part").write_bytes(b"left by an older version")
    fake = FakeHumble(*one_big_file(), bodies={"big.zip": BIG})
    library(tmp_path, fake, jobs=1).start()
    assert (folder / "big.zip").read_bytes() == BIG
    assert "Range" not in fake.cdn_gets[0][2]


def test_adopting_a_file_clears_what_an_earlier_run_left(tmp_path):
    folder = tmp_path / "lib" / "Big Bundle" / "Big Game"
    folder.mkdir(parents=True)
    (folder / "big.zip").write_bytes(BIG)
    (folder / "big.zip.part").write_bytes(BIG[:100])
    (folder / "big.zip.part.json").write_text('{"validator": "x"}')
    library(tmp_path, FakeHumble(*one_big_file(), bodies={"big.zip": BIG}), jobs=1).start()
    assert sorted(p.name for p in folder.iterdir()) == ["big.zip"]
