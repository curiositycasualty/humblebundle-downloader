import json
import threading

from humblebundle_downloader.download_library import (
    DownloadLibrary,
    _coerce_size,
    _file_ext,
    _human_size,
    _order_error,
)


###
# _should_download_ext
###
def test_include_logic_has_values():
    dl = DownloadLibrary(
        "fake_library_path",
        ext_include=["pdf", "EPub"],
    )
    assert dl._should_download_ext("pdf") is True
    assert dl._should_download_ext("df") is False
    assert dl._should_download_ext("ePub") is True
    assert dl._should_download_ext("mobi") is False


def test_include_logic_empty():
    dl = DownloadLibrary(
        "fake_library_path",
        ext_include=[],
    )
    assert dl._should_download_ext("pdf") is True
    assert dl._should_download_ext("df") is True
    assert dl._should_download_ext("EPub") is True
    assert dl._should_download_ext("mobi") is True


def test_exclude_logic_has_values():
    dl = DownloadLibrary(
        "fake_library_path",
        ext_exclude=["pdf", "EPub"],
    )
    assert dl._should_download_ext("pdf") is False
    assert dl._should_download_ext("df") is True
    assert dl._should_download_ext("ePub") is False
    assert dl._should_download_ext("mobi") is True


def test_exclude_logic_empty():
    dl = DownloadLibrary(
        "fake_library_path",
        ext_exclude=[],
    )
    assert dl._should_download_ext("pdf") is True
    assert dl._should_download_ext("df") is True
    assert dl._should_download_ext("EPub") is True
    assert dl._should_download_ext("mobi") is True


###
# _should_download_platform
###
def test_download_platform_filter_none():
    dl = DownloadLibrary(
        "fake_library_path",
        platform_include=None,
    )
    assert dl._should_download_platform("ebook") is True
    assert dl._should_download_platform("audio") is True


def test_download_platform_filter_blank():
    dl = DownloadLibrary(
        "fake_library_path",
        platform_include=[],
    )
    assert dl._should_download_platform("ebook") is True
    assert dl._should_download_platform("audio") is True


def test_download_platform_filter_audio():
    dl = DownloadLibrary(
        "fake_library_path",
        platform_include=["audio"],
    )
    assert dl._should_download_platform("ebook") is False
    assert dl._should_download_platform("audio") is True


###
# _human_size / _coerce_size
###
def test_human_size():
    assert _human_size(0) == "0 B"
    assert _human_size(512) == "512 B"
    assert _human_size(1024) == "1.00 KiB"
    assert _human_size(5 * 1024 * 1024) == "5.00 MiB"
    assert _human_size(1536 * 1024 * 1024) == "1.50 GiB"


def test_coerce_size():
    assert _coerce_size(None) is None
    assert _coerce_size("not a number") is None
    assert _coerce_size(-1) is None
    assert _coerce_size("2048") == 2048
    assert _coerce_size(2048) == 2048


###
# dry run tallying
###
class FakeHeadResponse:
    def __init__(self, headers=None, status_code=200):
        self.headers = headers or {}
        self.status_code = status_code


class FakeHeadSession:
    def __init__(self, headers=None, status_code=200):
        self._headers = headers
        self._status_code = status_code
        self.head_calls = []

    def head(self, url, **kwargs):
        self.head_calls.append(url)
        return FakeHeadResponse(self._headers, self._status_code)


def _dry_run_library(session=None, update=False):
    dl = DownloadLibrary("fake_library_path", dry_run=True, update=update)
    dl.cache_data = {}
    if session is not None:
        dl.session = session
    return dl


def test_dry_run_uses_size_from_api_without_any_request():
    session = FakeHeadSession()
    dl = _dry_run_library(session)
    dl._check_pending_download("https://h.b/f.pdf", "f.pdf", 1024, {})
    assert session.head_calls == []
    assert dl.pending_downloads[0]["file_size"] == 1024


def test_dry_run_falls_back_to_head_for_unknown_size():
    session = FakeHeadSession(headers={"Content-Length": "4096"})
    dl = _dry_run_library(session)
    dl._check_pending_download("https://h.b/f.pdf", "f.pdf", None, {})
    assert session.head_calls == ["https://h.b/f.pdf"]
    assert dl.pending_downloads[0]["file_size"] == 4096


def test_dry_run_records_unknown_size_when_head_fails():
    session = FakeHeadSession(status_code=404)
    dl = _dry_run_library(session)
    dl._check_pending_download("https://h.b/f.pdf", "f.pdf", None, {})
    assert len(dl.pending_downloads) == 1
    assert dl.pending_downloads[0]["file_size"] is None


def test_dry_run_skips_file_unchanged_since_last_run():
    last_modified = "Mon, 01 Jan 2024 00:00:00 GMT"
    session = FakeHeadSession(headers={"Last-Modified": last_modified})
    dl = _dry_run_library(session, update=True)
    dl._check_pending_download(
        "https://h.b/f.pdf",
        "f.pdf",
        1024,
        {"url_last_modified": last_modified},
    )
    assert dl.pending_downloads == []


def test_dry_run_counts_file_changed_since_last_run():
    session = FakeHeadSession(
        headers={"Last-Modified": "Tue, 02 Jan 2024 00:00:00 GMT"}
    )
    dl = _dry_run_library(session, update=True)
    dl._check_pending_download(
        "https://h.b/f.pdf",
        "f.pdf",
        1024,
        {"url_last_modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
    )
    assert len(dl.pending_downloads) == 1


###
# _file_ext
###
def test_file_ext():
    assert _file_ext("book.CBZ") == "cbz"
    assert _file_ext("book.tar.gz") == "gz"
    assert _file_ext("README") == ""


###
# _select_by_format
###
def _web(name, size=None):
    entry = {"url": {"web": "https://dl.hb.com/" + name}}
    if size is not None:
        entry["file_size"] = size
    return entry


def _names(selected):
    return [
        entry["url"]["web"].rsplit("/", 1)[-1]
        for entry in selected
        if "url" in entry
    ]


def _select(struct, **kwargs):
    dl = DownloadLibrary("fake_library_path", **kwargs)
    return dl._select_by_format(struct, "Item", "ebook")


def test_prefer_format_off_keeps_everything():
    struct = [_web("a.pdf", 10), _web("a.cbz", 20)]
    assert _select(struct) == struct


def test_prefer_format_picks_first_available():
    struct = [_web("a.pdf", 10), _web("a.cbz", 20), _web("a.epub", 5)]
    selected = _select(struct, prefer_format=["cbz", "epub", "pdf"])
    assert _names(selected) == ["a.cbz"]


def test_prefer_format_order_matters():
    struct = [_web("a.pdf", 10), _web("a.cbz", 20), _web("a.epub", 5)]
    selected = _select(struct, prefer_format=["epub", "cbz"])
    assert _names(selected) == ["a.epub"]


def test_prefer_format_keeps_every_file_of_the_winning_format():
    struct = [_web("v1.cbz", 10), _web("v2.cbz", 11), _web("all.pdf", 90)]
    selected = _select(struct, prefer_format=["cbz", "pdf"])
    assert _names(selected) == ["v1.cbz", "v2.cbz"]


def test_prefer_format_falls_back_to_largest_file():
    struct = [_web("a.cbr", 15), _web("a.djvu", 55), _web("a.txt", 1)]
    selected = _select(struct, prefer_format=["cbz", "epub"])
    assert _names(selected) == ["a.djvu"]


def test_prefer_format_fallback_without_sizes_keeps_one_file():
    struct = [_web("a.cbr"), _web("a.djvu")]
    selected = _select(struct, prefer_format=["cbz"])
    assert len(_names(selected)) == 1


def test_prefer_format_leaves_single_file_alone():
    struct = [_web("only.azw3", 5)]
    selected = _select(struct, prefer_format=["cbz"])
    assert _names(selected) == ["only.azw3"]


def test_prefer_format_never_drops_entries_without_a_url():
    external = {"external_link": "https://example.com/x"}
    struct = [_web("a.pdf", 10), _web("a.cbz", 20), external]
    selected = _select(struct, prefer_format=["cbz"])
    assert external in selected
    assert _names(selected) == ["a.cbz"]


def test_prefer_format_respects_exclude():
    struct = [_web("a.pdf", 10), _web("a.cbz", 20)]
    selected = _select(struct, prefer_format=["cbz", "pdf"], ext_exclude=["cbz"])
    assert _names(selected) == ["a.pdf"]


def test_prefer_format_respects_include():
    struct = [_web("a.pdf", 10), _web("a.cbz", 20), _web("a.epub", 5)]
    selected = _select(
        struct, prefer_format=["cbz", "epub"], ext_include=["epub", "pdf"]
    )
    assert _names(selected) == ["a.epub"]


def test_prefer_format_normalises_dots_and_case():
    struct = [_web("a.pdf", 10), _web("a.CBZ", 20)]
    selected = _select(struct, prefer_format=[".CbZ"])
    assert _names(selected) == ["a.CBZ"]


###
# _order_error / skipping unusable orders
###
def test_order_error_reports_api_error_field():
    assert "Unauthorized" in _order_error({"_errors": "Unauthorized"})
    assert "missing_order" in _order_error({"error_code": "missing_order"})


def test_order_error_reports_wrong_type():
    assert "list" in _order_error([])


def test_order_error_lists_keys_when_nothing_else_is_known():
    reason = _order_error({"gamekey": "abc", "uploaded_at": "x"})
    assert "gamekey" in reason and "uploaded_at" in reason


class FakeOrderResponse:
    def __init__(self, payload, status_code=200, valid_json=True):
        self._payload = payload
        self.status_code = status_code
        self._valid_json = valid_json

    def json(self):
        if not self._valid_json:
            raise ValueError("not json")
        return self._payload


GOOD_ORDER = {
    "product": {"human_name": "Real Bundle"},
    "subproducts": [],
}


class OrderSession:
    def __init__(self, responses):
        self.responses = responses
        self.headers = {}
        self.cookies = {}

    def get(self, url, **kwargs):
        for key, response in self.responses.items():
            if "/order/" + key in url:
                return response
        raise AssertionError("unexpected url " + url)


def _run_orders(responses):
    dl = DownloadLibrary(
        "fake_library_path", purchase_keys=list(responses), dry_run=True
    )
    dl.session = OrderSession(responses)
    dl.cache_data = {}
    dl.cache_file = "fake_library_path/.cache.json"
    for order_id in dl.purchase_keys:
        dl._process_order_id(order_id)
    return dl


def test_order_without_product_is_skipped_not_fatal():
    dl = _run_orders({"bad": FakeOrderResponse({"_errors": "Unauthorized"})})
    assert dl.skipped_orders == ["bad"]


def test_order_that_is_not_json_is_skipped():
    dl = _run_orders(
        {"bad": FakeOrderResponse(None, status_code=503, valid_json=False)}
    )
    assert dl.skipped_orders == ["bad"]


def test_one_bad_order_does_not_stop_the_others():
    dl = _run_orders({
        "bad": FakeOrderResponse({"_errors": "Unauthorized"}),
        "good": FakeOrderResponse(GOOD_ORDER),
    })
    assert dl.skipped_orders == ["bad"]
    assert dl._current_bundle == "Real Bundle"


def test_order_without_subproducts_does_not_crash():
    dl = _run_orders(
        {"thin": FakeOrderResponse({"product": {"human_name": "Thin"}})}
    )
    assert dl.skipped_orders == []
    assert dl._current_bundle == "Thin"


###
# parallel downloads
###
def test_jobs_defaults_to_sequential():
    dl = DownloadLibrary("fake_library_path")
    assert dl.jobs == 1
    assert dl._queue is None


def test_jobs_is_clamped_to_at_least_one():
    assert DownloadLibrary("fake_library_path", jobs=0).jobs == 1
    assert DownloadLibrary("fake_library_path", jobs=-4).jobs == 1


def test_progress_bar_is_kept_when_sequential():
    dl = DownloadLibrary("fake_library_path", progress_bar=True, jobs=1)
    assert dl._show_bar is True


def test_progress_bar_is_dropped_when_parallel():
    dl = DownloadLibrary("fake_library_path", progress_bar=True, jobs=4)
    assert dl._show_bar is False


def test_session_is_shared_when_sequential():
    dl = DownloadLibrary("fake_library_path", jobs=1)
    assert dl._session() is dl.session


def test_dry_run_starts_no_workers():
    dl = DownloadLibrary("fake_library_path", jobs=8, dry_run=True)
    dl._start_workers()
    assert dl._queue is None
    assert dl._workers == []


def test_workers_start_and_stop_cleanly():
    dl = DownloadLibrary("fake_library_path", jobs=3)
    dl._start_workers()
    assert len(dl._workers) == 3
    dl._stop_workers()
    assert dl._workers == []
    assert dl._queue is None


def test_cache_file_stays_valid_under_concurrent_writes(tmp_path):
    dl = DownloadLibrary(str(tmp_path), jobs=8)
    dl.cache_file = str(tmp_path / ".cache.json")
    dl.cache_data = {}

    def write_many(offset):
        for i in range(25):
            dl._update_cache_data(
                "key-{0}-{1}".format(offset, i), {"url_last_modified": "x"}
            )

    threads = [
        threading.Thread(target=write_many, args=(n,)) for n in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with open(dl.cache_file) as handle:
        written = json.load(handle)
    assert len(written) == 200
