import json
import threading
import time

import pytest

from humblebundle_downloader.humble_site import (
    LIST_ACTION,
    SIGN_ACTION,
    VAULT_SIGN_ACTION,
    HumbleSite,
    NotSignedIn,
    SignedUrl,
    SiteError,
    UrlSigner,
    action_result,
    entitlement_files,
    legacy_order,
    parse_rsc,
)

# Invented, but shaped like what the live site sends
MD5 = "0123456789abcdef0123456789abcdef"
SHA1 = "0123456789abcdef0123456789abcdef01234567"
LIST_ID = "60" + "a" * 40
SIGN_ID = "70" + "b" * 40
OTHER_ID = "40" + "c" * 40
VAULT_ID = "40" + "e" * 40


def struct(web, size=100, name="PDF"):
    return {
        "id": "s-" + web,
        "arch": None,
        "fileSize": size,
        "md5": MD5,
        "sha1": SHA1,
        "name": name,
        "size": None,
        "url": {"web": web, "bittorrent": "torrent-token"},
    }


def download(key, platform, *structs):
    return {
        "id": "d-" + key,
        "key": key,
        "platform": platform,
        "deliveryMethods": ["download"],
        "downloadStruct": list(structs),
    }


def entitlement(ent_id, title, *downloads):
    return {
        "id": ent_id,
        "title": title,
        "type": "download_drm_free",
        "redemptionData": {},
        "downloads": list(downloads),
        "expired": False,
    }


def rsc(*rows):
    """Rows as the stream carries them, including ones that are not json"""
    lines = ['0:{"a":"$@1","q":"","i":false,"b":"build"}']
    lines.append('2:I["/_next/static/chunks/abc.js",[],"default"]')
    lines.append("3:HL[\"/_next/static/css/x.css\",\"style\"]")
    for number, row in enumerate(rows, start=4):
        lines.append("{:x}:{}".format(number, json.dumps(row)))
    return "\n".join(lines) + "\n"


def action_response(value):
    return '0:{"a":"$@1","q":"","i":false,"b":"build"}\n1:' + json.dumps(value) + "\n"


def bundle_page(bundle_id, bundle_title, *ents):
    """A bundle page: the entitlements sit deep in component props, and
    each download is rendered twice (list and detail)"""
    item = {
        "id": bundle_id,
        "type": "bundle",
        "title": bundle_title,
        "entitlements": {"downloads": list(ents), "keys": []},
    }
    first = ["$", "div", None, {"children": [["$", "$L6", None, {"userLibraryItem": item}]]}]
    again = ["$", "$L7", None, {"items": [{"download": d} for e in ents for d in e["downloads"]],
                                "entitlement": ents[0] if ents else None}]
    return rsc(first, again)


###
# the stream format
###
def test_parse_rsc_keeps_json_rows_only():
    rows = parse_rsc(rsc({"x": 1}, [1, 2]))
    assert rows["0"]["a"] == "$@1"
    assert rows["4"] == {"x": 1}
    assert rows["5"] == [1, 2]
    assert "2" not in rows and "3" not in rows


def test_parse_rsc_ignores_broken_rows():
    assert parse_rsc('0:{"a":\n1:{nope}\nnot a row\n') == {}


def test_action_result_follows_the_reference():
    text = '0:{"a":"$@3"}\n1:{"wrong":true}\n3:{"right":true}\n'
    assert action_result(text) == {"right": True}


def test_action_result_falls_back_to_row_one():
    assert action_result('1:{"ok":true}\n') == {"ok": True}


def test_action_result_of_nothing_is_none():
    assert action_result("<html>sign in</html>") is None


###
# finding files in a page
###
def test_entitlement_files_finds_each_download_once():
    page = bundle_page(
        "b1", "Bundle",
        entitlement("e1", "Book One", download("one_ebook", "ebook", struct("one.pdf"), struct("one.epub"))),
        entitlement("e2", "Book Two", download("two_ebook", "ebook", struct("two.pdf"))),
    )
    found = entitlement_files(parse_rsc(page))
    assert set(found) == {"e1", "e2"}
    assert found["e1"]["title"] == "Book One"
    assert len(found["e1"]["downloads"]) == 1
    assert [s["url"]["web"] for s in found["e1"]["downloads"][0]["downloadStruct"]] == [
        "one.pdf", "one.epub",
    ]


def test_entitlement_files_keeps_each_platform():
    page = bundle_page(
        "b1", "Bundle",
        entitlement(
            "g1", "Game",
            download("game_windows", "windows", struct("game.exe")),
            download("game_linux", "linux", struct("game.sh")),
        ),
    )
    found = entitlement_files(parse_rsc(page))
    assert sorted(d["platform"] for d in found["g1"]["downloads"]) == ["linux", "windows"]


def test_entitlement_files_ignores_downloads_with_no_owner():
    stray = {"download": download("x_ebook", "ebook", struct("x.pdf"))}
    assert entitlement_files(parse_rsc(rsc(stray))) == {}


###
# the old api's shape
###
def test_legacy_order_matches_the_old_api():
    ent = entitlement("e1", "Book", download("book_ebook", "ebook", struct("book.pdf", 42)))
    order = legacy_order("b1", "Bundle", [("e1", "Book", "book", ent["downloads"])])
    assert order["gamekey"] == "b1"
    assert order["product"]["human_name"] == "Bundle"
    sub = order["subproducts"][0]
    assert sub["human_name"] == "Book"
    dl = sub["downloads"][0]
    assert dl["platform"] == "ebook"
    entry = dl["download_struct"][0]
    assert entry["file_size"] == 42
    assert entry["md5"] == MD5 and entry["sha1"] == SHA1
    assert entry["url"] == {"web": "book.pdf"}
    assert entry["hbd_sign"] == {"entitlement": "e1", "download_key": "book_ebook"}


def test_legacy_order_leaves_real_urls_alone():
    ent = download("k", "ebook", struct("https://cdn.example/x.pdf?t=1"))
    order = legacy_order("b", "B", [("e", "T", "c", [ent])])
    entry = order["subproducts"][0]["downloads"][0]["download_struct"][0]
    assert entry["url"]["web"].startswith("https://")
    assert "hbd_sign" not in entry


def test_legacy_order_copes_with_a_struct_without_url():
    bare = {"name": "Stream", "fileSize": None, "hdStreamUrl": None}
    order = legacy_order("b", "B", [("e", "T", "c", [download("k", "video", bare)])])
    entry = order["subproducts"][0]["downloads"][0]["download_struct"][0]
    assert "url" not in entry and "hbd_sign" not in entry


###
# talking to the site
###
class Response:
    def __init__(self, text="", status_code=200, url="https://www.humblebundle.com/x"):
        self.text = text
        self.status_code = status_code
        self.url = url


def script_with(*actions):
    calls = ",".join(
        '(0,n.createServerReference)("{id}",n.callServer,void 0,n.findSourceMapURL,"{name}")'.format(
            id=action_id, name=name
        )
        for action_id, name in actions
    )
    return "var a=1;" + calls + ";"


class FakeSite:
    """Just enough of www.humblebundle.com.

    entries: what the list action returns, in order
    pages: path -> rsc text for bundle and item pages
    """

    def __init__(self, entries=(), pages=None, page_size_seen=None, signer=None):
        self.entries = list(entries)
        self.pages = pages or {}
        self.signer = signer or (lambda ids: {"ok": True, "data": {"signedUrls": {}, "expiresAt": None}})
        self.vault_signer = lambda ids: {"ok": False, "code": "denied"}
        self.list_id = LIST_ID
        self.sign_id = SIGN_ID
        self.signed_out = False
        self.gets = []
        self.posts = []
        self.headers = {}
        self.cookies = {}

    def get(self, url, headers=None, **kwargs):
        path = url.replace("https://www.humblebundle.com", "")
        self.gets.append(path)
        if self.signed_out:
            return Response("<html>login</html>", url="https://auth.humblebundle.com/login?flow=x")
        if path == "/home/library" and (headers or {}).get("RSC"):
            return Response(self.pages.get("vault", rsc()))
        if path == "/home/library":
            return Response('<script src="/_next/static/chunks/lib-1.js"></script>'
                            '<script src="/_next/static/chunks/shared.js"></script>')
        if path.startswith("/home/library/content/") and not (headers or {}).get("RSC"):
            return Response('<script src="/_next/static/chunks/item-9.js"></script>'
                            '<script src="/_next/static/chunks/shared.js"></script>')
        if path == "/_next/static/chunks/lib-1.js":
            return Response(script_with(
                (self.list_id, LIST_ACTION), (OTHER_ID, "recordDownload"),
                (VAULT_ID, VAULT_SIGN_ACTION),
            ))
        if path == "/_next/static/chunks/item-9.js":
            return Response(script_with((self.sign_id, SIGN_ACTION)))
        if path == "/_next/static/chunks/shared.js":
            return Response("var nothing;")
        if path in self.pages:
            return Response(self.pages[path])
        return Response("not found", status_code=404)

    def post(self, url, headers=None, data=None, files=None):
        path = url.replace("https://www.humblebundle.com", "")
        action = headers["Next-Action"]
        self.posts.append((path, action))
        if self.signed_out:
            return Response("<html>login</html>", url="https://auth.humblebundle.com/login?flow=x")
        if action == self.list_id and path == "/home/library":
            form = {name: value for name, (_, value) in files}
            limit = int(form["_1_limit"])
            cursor = int(form["_1_cursor"])
            chunk = self.entries[cursor:cursor + limit]
            pages = max(1, -(-len(self.entries) // limit))
            return Response(action_response({
                "results": chunk, "bundlesCount": 0, "success": True,
                "pagination": {"page": int(form["_1_page"]), "totalDocs": len(self.entries), "totalPages": pages},
            }))
        if action == self.sign_id and path.startswith("/home/library/content/"):
            return Response(action_response(self.signer(json.loads(data)[0])))
        if action == VAULT_ID and path == "/home/library":
            return Response(action_response(self.vault_signer(json.loads(data)[0])))
        return Response("unknown action", status_code=404)


def entry(ent_id, bundle_id, title, bundle_title, kind="download_drm_free"):
    return {
        "id": ent_id, "title": title, "subTitle": bundle_title,
        "type": kind, "itemType": "content-entitlement",
        "productEntitlementId": bundle_id, "contentId": title.lower().replace(" ", ""),
    }


def test_actions_are_found_by_name():
    fake = FakeSite()
    site = HumbleSite(fake)
    assert site._action_id(LIST_ACTION, "/home/library") == LIST_ID


def test_signing_action_is_looked_for_on_the_item_page():
    fake = FakeSite()
    site = HumbleSite(fake)
    site._action_id(LIST_ACTION, "/home/library")
    assert site._action_id(SIGN_ACTION, "/home/library/content/e1") == SIGN_ID
    # Each page's scripts are read once
    site._action_id(SIGN_ACTION, "/home/library/content/e2")
    assert fake.gets.count("/_next/static/chunks/item-9.js") == 1


def test_a_missing_action_says_the_site_changed():
    fake = FakeSite()
    fake.list_id = None
    fake.get = (lambda original: lambda url, **kw: Response("<html></html>")
                if url.endswith("/home/library") else original(url, **kw))(fake.get)
    with pytest.raises(SiteError, match="filterUserLibrary"):
        HumbleSite(fake).library()


def test_library_pages_through_everything():
    entries = [entry("e%d" % i, "b", "T%d" % i, "B") for i in range(25)]
    fake = FakeSite(entries)
    got = HumbleSite(fake, page_size=10).library()
    assert [e["id"] for e in got] == [e["id"] for e in entries]
    assert len([p for p in fake.posts if p[1] == LIST_ID]) == 3


def test_library_drops_repeats():
    entries = [entry("e1", "b", "T", "B"), entry("e1", "b", "T", "B")]
    assert len(HumbleSite(FakeSite(entries)).library()) == 1


def test_signed_out_is_reported_as_such():
    fake = FakeSite()
    fake.signed_out = True
    with pytest.raises(NotSignedIn):
        HumbleSite(fake).library()


def test_an_action_from_an_old_deploy_is_looked_up_again():
    fake = FakeSite([entry("e1", "b", "T", "B")])
    site = HumbleSite(fake)
    site.library()
    # Humble deploys: the old id now 404s, the scripts carry a new one
    fake.list_id = "61" + "d" * 40
    assert len(site.library()) == 1
    assert fake.posts[-1][1] == fake.list_id


def test_orders_reads_each_bundle_page_once():
    entries = [
        entry("e1", "b1", "Book One", "Bundle One"),
        entry("e2", "b1", "Book Two", "Bundle One"),
        entry("e3", "b2", "Game", "Bundle Two"),
        entry("k1", "b2", "Steam Key", "Bundle Two", kind="steam"),
    ]
    pages = {
        "/home/library/b1": bundle_page(
            "b1", "Bundle One",
            entitlement("e1", "Book One", download("one_ebook", "ebook", struct("one.pdf"))),
            entitlement("e2", "Book Two", download("two_ebook", "ebook", struct("two.pdf"))),
        ),
        "/home/library/b2": bundle_page(
            "b2", "Bundle Two",
            entitlement("e3", "Game", download("game_windows", "windows", struct("game.exe"))),
        ),
    }
    fake = FakeSite(entries, pages)
    orders = dict(HumbleSite(fake).orders())
    assert set(orders) == {"b1", "b2"}
    assert orders["b1"]["product"]["human_name"] == "Bundle One"
    assert [s["human_name"] for s in orders["b1"]["subproducts"]] == ["Book One", "Book Two"]
    assert [s["human_name"] for s in orders["b2"]["subproducts"]] == ["Game"]
    assert fake.gets.count("/home/library/b1") == 1
    assert not any(g.startswith("/home/library/content/") for g in fake.gets)


def test_an_item_missing_from_its_bundle_page_is_read_from_its_own():
    entries = [entry("e1", "b1", "Book One", "B"), entry("e2", "b1", "Late Book", "B")]
    pages = {
        "/home/library/b1": bundle_page(
            "b1", "B", entitlement("e1", "Book One", download("one_ebook", "ebook", struct("one.pdf"))),
        ),
        "/home/library/content/e2": rsc({"userLibraryItem": {"entitlements": {"downloads": [
            entitlement("e2", "Late Book", download("late_ebook", "ebook", struct("late.pdf")))]}}}),
    }
    orders = dict(HumbleSite(FakeSite(entries, pages)).orders())
    assert [s["human_name"] for s in orders["b1"]["subproducts"]] == ["Book One", "Late Book"]


def test_entries_with_no_files_are_counted_not_queued():
    entries = [entry("e1", "b1", "Book", "B"), entry("n1", "b1", "No Content", "B")]
    pages = {
        "/home/library/b1": bundle_page(
            "b1", "B", entitlement("e1", "Book", download("b_ebook", "ebook", struct("b.pdf"))),
        ),
        "/home/library/content/n1": rsc({"nothing": "here"}),
    }
    site = HumbleSite(FakeSite(entries, pages))
    orders = dict(site.orders())
    assert [s["human_name"] for s in orders["b1"]["subproducts"]] == ["Book"]
    assert site.empty_entries == ["No Content"]


def test_a_broken_bundle_page_falls_back_to_item_pages():
    entries = [entry("e1", "b1", "Book", "B")]
    pages = {
        "/home/library/content/e1": rsc({"x": entitlement(
            "e1", "Book", download("b_ebook", "ebook", struct("b.pdf")))}),
    }
    orders = dict(HumbleSite(FakeSite(entries, pages)).orders())
    assert [s["human_name"] for s in orders["b1"]["subproducts"]] == ["Book"]


def test_orders_can_be_limited_to_some_bundles(caplog):
    entries = [entry("e1", "b1", "A", "One"), entry("e2", "b2", "B", "Two")]
    pages = {
        "/home/library/b1": bundle_page("b1", "One", entitlement("e1", "A", download("a", "ebook", struct("a.pdf")))),
        "/home/library/b2": bundle_page("b2", "Two", entitlement("e2", "B", download("b", "ebook", struct("b.pdf")))),
    }
    fake = FakeSite(entries, pages)
    orders = dict(HumbleSite(fake).orders(keys=["b2", "nope"]))
    assert list(orders) == ["b2"]
    assert "/home/library/b1" not in fake.gets
    assert "nope" in caplog.text


###
# signing
###
def signed(*files, expires="2030-01-01T00:00:00.000Z"):
    urls = {}
    for key, name in files:
        urls.setdefault("machine", {}).setdefault(key, {})[name] = "https://cdn.example/" + name + "?t=tok"
        urls["machine"][key][name + ".torrent"] = "https://cdn.example/t/" + name + ".torrent?t=tok"
    return {"ok": True, "data": {"signedUrls": urls, "expiresAt": expires}}


def test_sign_returns_urls_by_key_and_filename_without_torrents():
    fake = FakeSite(signer=lambda ids: signed(("k", "a.pdf"), ("k", "a.epub")))
    urls, expires = HumbleSite(fake).sign(["e1"])
    assert urls == {
        ("k", "a.pdf"): "https://cdn.example/a.pdf?t=tok",
        ("k", "a.epub"): "https://cdn.example/a.epub?t=tok",
    }
    assert expires == pytest.approx(1893456000)


def test_sign_posts_to_the_item_page():
    fake = FakeSite(signer=lambda ids: signed())
    HumbleSite(fake).sign(["e7", "e8"])
    assert fake.posts[-1] == ("/home/library/content/e7", SIGN_ID)


def test_sign_refusal_for_auth_is_not_signed_in():
    fake = FakeSite(signer=lambda ids: {"ok": False, "code": "not_authenticated"})
    with pytest.raises(NotSignedIn):
        HumbleSite(fake).sign(["e1"])


def test_sign_refusal_for_anything_else_names_the_reason():
    fake = FakeSite(signer=lambda ids: {"ok": False, "code": "upstream_error"})
    with pytest.raises(SiteError, match="upstream_error"):
        HumbleSite(fake).sign(["e1"])


class CountingSite:
    def __init__(self, expires_in=3600, clock=None):
        self.calls = 0
        self.expires_in = expires_in
        self.clock = clock
        self.lock = threading.Lock()

    def sign(self, ids, session=None):
        with self.lock:
            self.calls += 1
            n = self.calls
        return (
            {("k", "a.pdf"): "https://cdn/a.pdf?t=%d" % n, ("k", "b.pdf"): "https://cdn/b.pdf?t=%d" % n},
            self.clock() + self.expires_in,
        )


def test_signer_signs_each_item_once():
    now = [1000.0]
    site = CountingSite(clock=lambda: now[0])
    signer = UrlSigner(site, margin=60, clock=lambda: now[0])
    assert signer.url_for("e1", "k", "a.pdf").endswith("t=1")
    assert signer.url_for("e1", "k", "b.pdf").endswith("t=1")
    assert site.calls == 1


def test_signer_signs_again_near_expiry():
    now = [1000.0]
    site = CountingSite(expires_in=120, clock=lambda: now[0])
    signer = UrlSigner(site, margin=60, clock=lambda: now[0])
    signer.url_for("e1", "k", "a.pdf")
    now[0] += 90
    assert signer.url_for("e1", "k", "a.pdf").endswith("t=2")


def test_signer_does_not_hammer_when_humble_returns_the_same_short_lived_url():
    now = [1000.0]
    site = CountingSite(expires_in=30, clock=lambda: now[0])
    signer = UrlSigner(site, margin=60, clock=lambda: now[0])
    for _ in range(5):
        signer.url_for("e1", "k", "a.pdf")
    assert site.calls == 1


def test_signer_refresh_forces_a_new_url():
    now = [1000.0]
    site = CountingSite(clock=lambda: now[0])
    signer = UrlSigner(site, clock=lambda: now[0])
    signer.url_for("e1", "k", "a.pdf")
    assert signer.url_for("e1", "k", "a.pdf", refresh=True).endswith("t=2")


def test_signer_matches_by_filename_when_the_key_differs():
    now = [1000.0]
    signer = UrlSigner(CountingSite(clock=lambda: now[0]), clock=lambda: now[0])
    assert "a.pdf" in signer.url_for("e1", "other_key", "a.pdf")


def test_signer_says_when_a_file_was_not_signed():
    now = [1000.0]
    signer = UrlSigner(CountingSite(clock=lambda: now[0]), clock=lambda: now[0])
    with pytest.raises(SiteError, match="c.pdf"):
        signer.url_for("e1", "k", "c.pdf")


def test_signer_signs_once_however_many_workers_ask():
    now = [1000.0]
    site = CountingSite(clock=lambda: now[0])
    signer = UrlSigner(site, clock=lambda: now[0])
    threads = [threading.Thread(target=signer.url_for, args=("e1", "k", "a.pdf")) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert site.calls == 1


def test_signed_url_resolves_late_and_prints_without_a_token():
    now = [1000.0]
    site = CountingSite(clock=lambda: now[0])
    url = SignedUrl(UrlSigner(site, clock=lambda: now[0]), "e1", "k", "a.pdf")
    assert site.calls == 0
    assert "t=" not in str(url) and "a.pdf" in str(url)
    assert url.resolve().startswith("https://cdn/a.pdf")
    assert site.calls == 1


###
# signed out
###
class SignedOutListing(FakeSite):
    """What Humble really does: 200, an empty library, and the reason"""

    def post(self, url, headers=None, data=None, files=None):
        return Response(action_response({
            "results": [], "bundlesCount": 0, "requestId": "1",
            "pagination": {"page": 1, "totalDocs": 0, "totalPages": 1},
            "success": False, "error": "not_authenticated", "retryable": False,
        }))


def test_an_empty_signed_out_listing_is_not_an_empty_library():
    with pytest.raises(NotSignedIn):
        HumbleSite(SignedOutListing()).library()


def test_signed_out_names_the_cookies_sent_but_not_their_values():
    fake = SignedOutListing()
    fake.headers = {"cookie": "_simpleauth_sess=SECRET1; csrf=SECRET2"}
    with pytest.raises(NotSignedIn) as raised:
        HumbleSite(fake).library()
    message = str(raised.value)
    assert "_simpleauth_sess" in message and "csrf" in message
    assert "SECRET" not in message


def test_signed_out_with_no_cookies_says_so():
    with pytest.raises(NotSignedIn, match="no cookies"):
        HumbleSite(SignedOutListing()).library()


def test_signed_out_separates_cookies_for_other_hosts():
    import requests

    fake = SignedOutListing()
    fake.cookies = requests.cookies.RequestsCookieJar()
    fake.cookies.set("ory_session_abc", "v", domain=".humblebundle.com")
    fake.cookies.set("site_only", "v", domain="www.humblebundle.com")
    fake.cookies.set("__Host-ory_kratos_browser_x", "v", domain="auth.humblebundle.com")
    with pytest.raises(NotSignedIn) as raised:
        HumbleSite(fake).library()
    message = str(raised.value)
    sent, _, other = message.partition("held but only for other hosts")
    assert "ory_session_abc" in sent and "site_only" in sent
    assert "__Host-ory_kratos_browser_x" in other
    assert "__Host-ory_kratos_browser_x" not in sent


def test_any_other_listing_error_is_reported():
    class Broken(FakeSite):
        def post(self, url, headers=None, data=None, files=None):
            return Response(action_response({"results": [], "success": False, "error": "upstream_error"}))

    with pytest.raises(SiteError, match="upstream_error"):
        HumbleSite(Broken()).library()


def test_sign_error_field_also_means_signed_out():
    fake = FakeSite(signer=lambda ids: {"ok": False, "error": "not_authenticated"})
    with pytest.raises(NotSignedIn):
        HumbleSite(fake).sign(["e1"])



###
# the stream format, properly
###
def text_row(row_id, text):
    """A T row: its length is in bytes, and nothing separates it from the
    row after"""
    body = text.encode("utf-8")
    return "{0}:T{1:x},{2}".format(row_id, len(body), text)


def test_rows_after_a_text_row_are_not_lost():
    stream = (
        '0:{"a":"$@1"}\n'
        + text_row("5", "<p>A blurb</p>\nover two lines")
        + '6:{"after": "the text"}\n'
        + '7:["also", "here"]\n'
    )
    rows = parse_rsc(stream)
    assert rows["6"] == {"after": "the text"}
    assert rows["7"] == ["also", "here"]
    assert "5" not in rows


def test_text_row_lengths_are_bytes_not_characters():
    stream = text_row("a", "Café — naïve 東京\n") + 'b:{"ok": 1}\n'
    assert parse_rsc(stream) == {"b": {"ok": 1}}


def test_a_broken_text_row_does_not_stop_the_rest():
    stream = "3:Tzz,garbage\n" + '4:{"fine": true}\n'
    assert parse_rsc(stream) == {"4": {"fine": True}}


###
# the vault
###
def vault_item(uuid, machine, title, *downloads):
    return {
        "id": uuid, "title": title + " (subscriber_vault)", "type": "$undefined",
        "collection": "subscriber-vault", "deliveryMethods": [],
        "entitlements": {"downloads": [{
            "id": machine, "title": title + " (subscriber_vault)", "type": None,
            "customCtaLabel": "", "downloads": list(downloads),
        }], "keys": []},
    }


def vault_page(*items):
    """Shaped like the real library page: text rows first, then the vault,
    straight after one of them on the same line"""
    return (
        '0:{"a":"$@1"}\n'
        + text_row("80", "<strong>In a game, someone</strong>\n<p>does things</p>")
        + text_row("81", "Another blurb — with an em dash, and no newline after")
        + "82:" + json.dumps(["$", "$L5", None, {"vaultTabVisible": True, "vaultItems": list(items)}]) + "\n"
    )


def test_the_vault_is_read_from_the_library_page():
    fake = FakeSite(pages={"vault": vault_page(
        vault_item("u1", "zodiacxx", "Zodiac XX",
                   download("zodiacxx_win", "windows", struct("zodiacxx_win.zip")),
                   download("zodiacxx_mac", "mac", struct("zodiacxx_mac.zip"))),
        vault_item("u2", "otherone", "Other One",
                   download("otherone_win", "windows", struct("otherone.exe"))),
    )})
    order_id, order = HumbleSite(fake).vault()
    assert order_id == "vault"
    assert order["product"]["human_name"] == "Humble Vault"
    assert [s["human_name"] for s in order["subproducts"]] == ["Zodiac XX", "Other One"]
    entry = order["subproducts"][0]["downloads"][0]["download_struct"][0]
    assert entry["hbd_sign"] == {"entitlement": "zodiacxx", "download_key": "zodiacxx_win", "vault": True}


def test_vault_items_are_signed_by_machine_name_on_the_library_page():
    fake = FakeSite()
    fake.vault_signer = lambda ids: {"ok": True, "data": {"signedUrls": {
        ids[0]: {"zodiacxx_win": {"zodiacxx_win.zip": "https://cdn.example/v.zip?t=1",
                                  "zodiacxx_win.zip.torrent": "https://cdn.example/t?t=1"}}},
        "expiresAt": "2030-01-01T00:00:00Z"}}
    urls, _ = HumbleSite(fake).sign(["zodiacxx"], vault=True)
    assert urls == {("zodiacxx_win", "zodiacxx_win.zip"): "https://cdn.example/v.zip?t=1"}
    assert fake.posts[-1] == ("/home/library", VAULT_ID)


def test_vault_and_content_signatures_are_cached_apart():
    calls = []

    class Site:
        def sign(self, ids, session=None, vault=False):
            calls.append((ids[0], vault))
            return {("k", "f.zip"): "https://x/{0}".format(vault)}, None

    signer = UrlSigner(Site())
    assert signer.url_for("same", "k", "f.zip") == "https://x/False"
    assert signer.url_for("same", "k", "f.zip", vault=True) == "https://x/True"
    assert calls == [("same", False), ("same", True)]


###
# Reading bundles ahead
###
class SlowSite(FakeSite):
    """Bundle and item pages take a while, and how many are being
    served at once is recorded"""

    def __init__(self, *args, delay=0.05, **kwargs):
        super().__init__(*args, **kwargs)
        self.delay = delay
        self.busy = 0
        self.most = 0
        self._lock = threading.Lock()

    def get(self, url, headers=None, **kwargs):
        slow = "/home/library/" in url and (headers or {}).get("RSC")
        if slow:
            with self._lock:
                self.busy += 1
                self.most = max(self.most, self.busy)
            time.sleep(self.delay)
        try:
            return super().get(url, headers=headers, **kwargs)
        finally:
            if slow:
                with self._lock:
                    self.busy -= 1


def many_bundles(count):
    entries, pages = [], {}
    for n in range(count):
        entries.append(entry("e%d" % n, "b%d" % n, "Book %d" % n, "Bundle %d" % n))
        pages["/home/library/b%d" % n] = bundle_page(
            "b%d" % n, "Bundle %d" % n,
            entitlement("e%d" % n, "Book %d" % n, download("k%d" % n, "ebook", struct("f%d.pdf" % n))),
        )
    return entries, pages


def test_bundle_pages_are_read_several_at_once_and_come_out_in_order():
    fake = SlowSite(*many_bundles(12))
    orders = list(HumbleSite(fake, readers=4).orders())
    assert [bundle_id for bundle_id, _ in orders] == ["b%d" % n for n in range(12)]
    assert [o["product"]["human_name"] for _, o in orders] == ["Bundle %d" % n for n in range(12)]
    assert fake.most == 4


def test_one_reader_reads_one_at_a_time():
    fake = SlowSite(*many_bundles(5), delay=0.01)
    assert len(list(HumbleSite(fake, readers=1).orders())) == 5
    assert fake.most == 1


def test_readers_use_sessions_of_their_own():
    fake = SlowSite(*many_bundles(8), delay=0.02)
    made = []

    def factory():
        made.append(threading.get_ident())
        return fake

    list(HumbleSite(fake, readers=3, session_factory=factory).orders())
    assert 1 <= len(made) <= 3
    assert len(set(made)) == len(made)


def test_signing_out_while_reading_ahead_still_stops_the_run():
    import pytest

    fake = SlowSite(*many_bundles(10), delay=0.01)
    site = HumbleSite(fake, readers=4)
    orders = site.orders()
    next(orders)
    fake.signed_out = True
    with pytest.raises(NotSignedIn):
        list(orders)


def test_stopping_early_does_not_read_the_whole_library():
    fake = SlowSite(*many_bundles(40), delay=0.02)
    orders = HumbleSite(fake, readers=4).orders()
    next(orders)
    orders.close()
    time.sleep(0.2)
    assert sum(1 for g in fake.gets if g.startswith("/home/library/b")) <= 4 * 2 + 1


def test_items_missing_from_a_bundle_page_are_read_several_at_once():
    count = 6
    entries = [entry("e%d" % n, "b1", "Book %d" % n, "B") for n in range(count)]
    pages = {"/home/library/b1": bundle_page("b1", "B")}
    for n in range(count):
        pages["/home/library/content/e%d" % n] = rsc({"userLibraryItem": {"entitlements": {"downloads": [
            entitlement("e%d" % n, "Book %d" % n, download("k%d" % n, "ebook", struct("f%d.pdf" % n)))]}}})
    fake = SlowSite(entries, pages)
    orders = dict(HumbleSite(fake, readers=3).orders())
    assert [s["human_name"] for s in orders["b1"]["subproducts"]] == ["Book %d" % n for n in range(count)]
    assert fake.most == 3
