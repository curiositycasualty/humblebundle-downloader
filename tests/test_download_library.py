from humblebundle_downloader.download_library import (
    DownloadLibrary,
    _coerce_size,
    _human_size,
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
