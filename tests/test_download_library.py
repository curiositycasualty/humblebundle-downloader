import io

import pytest

import time
import email
import datetime

import json
import threading

from humblebundle_downloader.download_library import (
    DownloadLibrary,
    ProgressReporter,
    _coerce_size,
    _file_ext,
    _human_size,
    _content_range_total,
    _order_error,
    _retry_after_seconds,
    USER_AGENT,
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


###
# throttle handling
###
class FakeHeaders(dict):
    pass


def _response_with(retry_after):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return type("R", (), {"headers": headers})()


def test_retry_after_seconds_reads_a_number():
    assert _retry_after_seconds(_response_with("120")) == 120


def test_retry_after_seconds_defaults_when_absent():
    assert _retry_after_seconds(_response_with(None)) == 30


def test_retry_after_seconds_defaults_when_unparseable():
    assert _retry_after_seconds(_response_with("soon please")) == 30


def test_retry_after_seconds_reads_an_http_date():
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=90
    )
    stamp = email.utils.format_datetime(when)
    assert 80 <= _retry_after_seconds(_response_with(stamp)) <= 95


def test_session_carries_a_descriptive_user_agent():
    dl = DownloadLibrary("fake_library_path")
    agent = dl.session.headers.get("User-Agent")
    assert agent == USER_AGENT
    assert "humblebundle-downloader" in agent


def test_user_agent_is_set_even_without_retries():
    dl = DownloadLibrary("fake_library_path", retries=0)
    assert dl.session.headers.get("User-Agent") == USER_AGENT


def test_retry_adapter_leaves_429_to_the_pool():
    dl = DownloadLibrary("fake_library_path")
    retry = dl.session.get_adapter("https://example.com/").max_retries
    assert retry.total == 5
    assert 503 in retry.status_forcelist
    assert 429 not in retry.status_forcelist


def test_cooldown_blocks_until_it_expires():
    dl = DownloadLibrary("fake_library_path")
    dl._enter_cooldown(0.3, "https://dl.example.com/a.bin")
    started = time.time()
    dl._wait_out_cooldown()
    assert time.time() - started >= 0.25
    assert dl._throttled_count == 1


def test_cooldown_is_not_shortened_by_a_later_smaller_pause():
    dl = DownloadLibrary("fake_library_path")
    dl._enter_cooldown(30, "https://dl.example.com/a.bin")
    first = dl._cooldown_until
    dl._enter_cooldown(1, "https://dl.example.com/b.bin")
    assert dl._cooldown_until == first
    assert dl._throttled_count == 2


def test_cooldown_wait_returns_immediately_when_stopped():
    dl = DownloadLibrary("fake_library_path")
    dl._enter_cooldown(30, "https://dl.example.com/a.bin")
    dl._stop.set()
    started = time.time()
    dl._wait_out_cooldown()
    assert time.time() - started < 1


###
# range resume
###
def test_content_range_total():
    assert _content_range_total(_range_response("bytes 100-999/1000")) == 1000
    assert _content_range_total(_range_response("bytes */1000")) == 1000
    assert _content_range_total(_range_response(None)) is None
    assert _content_range_total(_range_response("bytes 0-1/unknown")) is None


def _range_response(content_range, status_code=206, length=None):
    headers = {}
    if content_range is not None:
        headers["Content-Range"] = content_range
    if length is not None:
        headers["Content-Length"] = str(length)
    return type(
        "R", (), {"headers": headers, "status_code": status_code}
    )()


def _library_with_response(response, second=None):
    dl = DownloadLibrary("fake_library_path")
    handed = []

    def fake_get(remote_file, stream=True, range_start=None):
        handed.append(range_start)
        if len(handed) == 1:
            return response
        return second

    dl._get_with_backoff = fake_get
    dl._handed = handed
    return dl


def test_resume_appends_when_server_answers_206():
    response = _range_response("bytes 500-999/1000", 206, length=500)
    dl = _library_with_response(response)
    got, append, total = dl._reopen_for_resume("https://x/y.bin", "y.part", 500)
    assert got is response
    assert append is True
    assert total == 1000
    assert dl._handed == [500]


def test_resume_derives_total_without_content_range():
    response = _range_response(None, 206, length=400)
    dl = _library_with_response(response)
    _, append, total = dl._reopen_for_resume("https://x/y.bin", "y.part", 600)
    assert append is True
    assert total == 1000


def test_resume_restarts_when_server_ignores_range():
    response = _range_response(None, 200, length=1000)
    dl = _library_with_response(response)
    got, append, total = dl._reopen_for_resume("https://x/y.bin", "y.part", 500)
    assert got is response
    assert append is False
    assert total == 1000
    assert dl._range_unsupported is True


def test_resume_takes_file_from_the_top_on_416(tmp_path):
    part = tmp_path / "y.part"
    part.write_bytes(b"stale")
    fresh = _range_response(None, 200, length=1000)
    dl = _library_with_response(_range_response(None, 416), second=fresh)
    got, append, total = dl._reopen_for_resume(
        "https://x/y.bin", str(part), 500
    )
    assert got is fresh
    assert append is False
    assert not part.exists(), "stale part should have been discarded"


def test_resume_gives_up_on_an_unexpected_status():
    dl = _library_with_response(_range_response(None, 404))
    assert dl._reopen_for_resume("https://x/y.bin", "y.part", 500) is None


def test_resume_gives_up_when_the_request_fails():
    dl = _library_with_response(None)
    assert dl._reopen_for_resume("https://x/y.bin", "y.part", 500) is None


class ChunkedResponse:
    def __init__(self, body, headers=None):
        self.headers = headers or {}
        self.status_code = 200
        self._body = body

    def iter_content(self, chunk_size=4096):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]


def test_stream_to_file_appends_to_what_is_already_there(tmp_path):
    part = tmp_path / "f.part"
    part.write_bytes(b"first half ")
    dl = DownloadLibrary("fake_library_path")
    written = dl._stream_to_file(
        ChunkedResponse(b"second half"), str(part), True, 11, 22
    )
    assert written == 22
    assert part.read_bytes() == b"first half second half"


def test_stream_to_file_truncates_when_not_appending(tmp_path):
    part = tmp_path / "f.part"
    part.write_bytes(b"leftovers from before")
    dl = DownloadLibrary("fake_library_path")
    dl._stream_to_file(ChunkedResponse(b"fresh"), str(part), False, 0, 5)
    assert part.read_bytes() == b"fresh"


def test_stream_to_file_rejects_a_short_transfer(tmp_path):
    part = tmp_path / "f.part"
    dl = DownloadLibrary("fake_library_path")
    with pytest.raises(ValueError):
        dl._stream_to_file(ChunkedResponse(b"only 6"), str(part), False, 0, 99)


###
# ProgressReporter
###
class FakeTTY(io.StringIO):
    def isatty(self):
        return True


def _reporter(tty=True):
    return ProgressReporter(stream=FakeTTY() if tty else io.StringIO())


def test_reporter_counts_and_percentages():
    reporter = _reporter()
    reporter.file_started("one.cbz", 1000)
    reporter.file_progress("one.cbz", 620)
    frame = reporter.render(100)
    assert "1 active" in frame
    assert "one.cbz 62%" in frame


def test_reporter_counts_finished_files_and_their_bytes():
    reporter = _reporter()
    reporter.file_started("one.cbz", 1000)
    reporter.file_progress("one.cbz", 1000)
    reporter.file_finished("one.cbz")
    frame = reporter.render(100)
    assert "1 done" in frame
    assert "0 active" in frame
    assert reporter._bytes == 1000


def test_reporter_reports_failures_separately():
    reporter = _reporter()
    reporter.file_started("bad.bin", 10)
    reporter.file_finished("bad.bin", ok=False)
    frame = reporter.render(100)
    assert "1 failed" in frame
    assert reporter._bytes == 0


def test_reporter_handles_an_unknown_size():
    reporter = _reporter()
    reporter.file_started("mystery.bin", None)
    reporter.file_progress("mystery.bin", 2048)
    frame = reporter.render(100)
    assert "mystery.bin 2.00 KiB" in frame
    assert "%" not in frame


def test_reporter_stays_within_the_terminal_width():
    reporter = _reporter()
    for i in range(12):
        name = "quite-a-long-name-{0}.cbz".format(i)
        reporter.file_started(name, 1000)
        reporter.file_progress(name, 500)
    for width in (30, 60, 100):
        assert len(reporter.render(width)) <= width - 1


def test_reporter_renders_a_single_line():
    reporter = _reporter()
    reporter.file_started("one.cbz", 1000)
    assert "\n" not in reporter.render(100)
    assert "\r" not in reporter.render(100)


def test_reporter_progress_for_an_unknown_file_is_ignored():
    reporter = _reporter()
    reporter.file_progress("never-started.bin", 500)
    assert "never-started" not in reporter.render(100)


def test_reporter_is_disabled_off_a_terminal():
    reporter = _reporter(tty=False)
    assert reporter.enabled() is False
    reporter.start()
    reporter.paint()
    reporter.stop()
    assert reporter.stream.getvalue() == ""


def test_reporter_writes_and_then_wipes_the_line():
    reporter = _reporter()
    reporter.file_started("one.cbz", 1000)
    reporter.paint()
    assert reporter.stream.getvalue() != ""
    reporter.clear()
    assert reporter.stream.getvalue().endswith("\033[K")


def test_reporter_only_used_for_parallel_runs_with_progress():
    assert DownloadLibrary("x", jobs=1, progress_bar=True)._progress is None
    assert DownloadLibrary("x", jobs=4, progress_bar=False)._progress is None
    parallel = DownloadLibrary("x", jobs=4, progress_bar=True)
    assert parallel._progress is not None
    assert parallel._show_bar is False
