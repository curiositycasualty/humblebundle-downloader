"""The download engine driven by the rebuilt site, end to end, against
a fake site and a fake CDN"""
import json
import hashlib
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
        body = self.bodies[name]
        start = 0
        if headers and headers.get("Range"):
            start = int(headers["Range"].split("=")[1].rstrip("-"))
            return CdnResponse(body[start:], 206, url, {
                "Content-Range": "bytes {0}-{1}/{2}".format(start, len(body) - 1, len(body))},
                streamed=self.streamed)
        return CdnResponse(body, 200, url, streamed=self.streamed)

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


def test_a_bare_session_value_still_becomes_simpleauth(tmp_path):
    dl = DownloadLibrary(str(tmp_path), cookie_auth="abc==")
    assert dl.session.headers["cookie"] == "_simpleauth_sess=abc=="


def test_a_cookie_header_is_passed_through(tmp_path):
    dl = DownloadLibrary(str(tmp_path), cookie_auth="session=xyz; other=1")
    assert dl.session.headers["cookie"] == "session=xyz; other=1"
