"""The typed values the walk works with, made from the old api's shape
that humble_site still hands over"""
from humblebundle_downloader.humble_site import legacy_order
from humblebundle_downloader.models import (
    ASMJS,
    EXTERNAL,
    FILE,
    UNKNOWN,
    Download,
    FileEntry,
    Order,
    Signing,
)

from .test_humble_site import download, entitlement, struct


def test_a_file_entry_from_the_rebuilt_site():
    raw = {
        "url": {"web": "wytches_vol1.pdf"},
        "file_size": "141303793",
        "sha1": "AB" * 20,
        "md5": None,
        "hbd_sign": {"entitlement": "e1", "download_key": "k1"},
    }
    entry = FileEntry.from_struct(raw)
    assert entry.kind == FILE
    assert entry.filename == "wytches_vol1.pdf"
    assert entry.listed_size == 141303793
    assert entry.hashes == {"sha1": "ab" * 20}
    assert entry.signing == Signing("e1", "k1", False)
    assert entry.struct is raw


def test_an_old_style_url_keeps_its_name_and_has_no_signing():
    entry = FileEntry.from_struct({"url": {"web": "https://dl.example/a/book.epub?t=1"}})
    assert entry.kind == FILE and entry.filename == "book.epub" and entry.signing is None


def test_other_kinds():
    assert FileEntry.from_struct({"asm_config": {"display_item": "g"}}).kind == ASMJS
    link = FileEntry.from_struct({"external_link": "https://example.com"})
    assert link.kind == EXTERNAL and link.url == "https://example.com"
    assert FileEntry.from_struct({"name": "nothing"}).kind == UNKNOWN
    assert FileEntry.from_struct({"url": "not a dict"}).kind == UNKNOWN


def test_a_vault_file_is_signed_as_one():
    entry = FileEntry.from_struct({
        "url": {"web": "game.zip"},
        "hbd_sign": {"entitlement": "m", "download_key": "m", "vault": True},
    })
    assert entry.signing.vault is True


def test_an_order_from_what_the_site_code_makes():
    ent = entitlement("e1", "Book One", download("k1", "ebook", struct("one.pdf"), struct("one.epub")))
    order = Order.from_legacy("b1", legacy_order("b1", "Bundle One", [("e1", "Book One", "c1", ent["downloads"])]))
    assert order.id == "b1" and order.title == "Bundle One"
    (product,) = order.products
    assert product.title == "Book One"
    (dl,) = product.downloads
    assert isinstance(dl, Download) and dl.platform == "ebook"
    assert [f.filename for f in dl.files] == ["one.pdf", "one.epub"]
    assert all(f.signing is not None for f in dl.files)


def test_an_unreadable_order_is_skipped_not_fatal(tmp_path):
    from humblebundle_downloader.download_library import DownloadLibrary

    dl = DownloadLibrary(str(tmp_path))
    dl._process_order("bad", {"product": {"human_name": "B"}, "subproducts": [{"no": "title"}]})
    assert dl.skipped_orders == ["bad"]
