import os

import queue
from humblebundle_downloader.progress_styles import (
    PROGRESS_STYLES,
    DEFAULT_PROGRESS_STYLE,
)
import signal
import logging

import io

import pytest

import time
import email
import datetime

import json
import threading

from humblebundle_downloader.download_library import (
    DownloadLibrary,
    Interrupted,
    ProgressReporter,
    _coerce_size,
    _file_ext,
    _human_size,
    _content_range_total,
    _order_error,
    _muncher_bar,
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


def _reporter(slots=2, tty=True, style="fish"):
    return ProgressReporter(
        stream=FakeTTY() if tty else io.StringIO(), slots=slots, style=style
    )


def test_block_has_a_line_per_slot_plus_totals():
    reporter = _reporter(slots=3)
    lines = reporter.render(100, 24)
    assert len(lines) == 4
    assert lines[-1].startswith("Total")


def test_active_transfer_shows_name_and_percentage():
    reporter = _reporter()
    reporter.file_started("one.cbz", 1000)
    reporter.file_progress("one.cbz", 620)
    block = "\n".join(reporter.render(100, 24))
    assert "one.cbz" in block
    assert "62%" in block


def test_finished_transfer_frees_its_slot():
    reporter = _reporter(slots=2)
    reporter.file_started("one.cbz", 1000)
    reporter.file_progress("one.cbz", 1000)
    reporter.file_finished("one.cbz")
    block = "\n".join(reporter.render(100, 24))
    assert "one.cbz" not in block
    assert "Total 1/" in block
    assert reporter._bytes == 1000


def test_slots_are_stable_as_transfers_come_and_go():
    reporter = _reporter(slots=2)
    reporter.file_started("first.cbz", 10)
    reporter.file_started("second.cbz", 10)
    assert reporter._slot_names == ["first.cbz", "second.cbz"]
    reporter.file_finished("first.cbz")
    assert reporter._slot_names == [None, "second.cbz"]
    reporter.file_started("third.cbz", 10)
    # Takes the free slot rather than shuffling the other line about
    assert reporter._slot_names == ["third.cbz", "second.cbz"]


def test_totals_show_a_bar_once_the_walk_is_done():
    reporter = _reporter()
    for _ in range(4):
        reporter.file_queued()
    reporter.file_started("one.cbz", 10)
    reporter.file_finished("one.cbz")

    assert "so far" in reporter.render(100, 24)[-1]
    reporter.traversal_finished()
    totals = reporter.render(100, 24)[-1]
    assert "so far" not in totals
    assert "Total 1/4" in totals
    assert "25%" in totals


def test_skipped_files_come_off_the_denominator():
    reporter = _reporter()
    for _ in range(5):
        reporter.file_queued()
    reporter.file_skipped()
    reporter.file_skipped()
    reporter.traversal_finished()
    assert "Total 0/3" in reporter.render(100, 24)[-1]


def test_totals_report_failures():
    reporter = _reporter()
    reporter.file_started("bad.bin", 10)
    reporter.file_finished("bad.bin", ok=False)
    assert "1 failed" in reporter.render(100, 24)[-1]
    assert reporter._bytes == 0


def test_unknown_size_gets_a_pacing_muncher_and_no_percentage():
    reporter = _reporter()
    reporter.file_started("mystery.bin", None)
    reporter.file_progress("mystery.bin", 2048)
    line = reporter.render(100, 24)[0]
    assert "mystery.bin" in line
    assert "2.00 KiB" in line
    assert "%" not in line


def test_narrow_terminal_falls_back_to_one_line():
    reporter = _reporter(slots=4)
    lines = reporter.render(30, 24)
    assert len(lines) == 1
    assert "done" in lines[0]
    assert len(lines[0]) <= 29


def test_short_terminal_falls_back_to_one_line():
    reporter = _reporter(slots=8)
    assert len(reporter.render(100, 6)) == 1


def test_lines_are_never_wider_than_the_terminal():
    reporter = _reporter(slots=3)
    for i in range(3):
        name = "a-really-quite-long-file-name-{0}.cbz".format(i)
        reporter.file_started(name, 1000)
        reporter.file_progress(name, 500)
    for width in (60, 80, 120):
        for line in reporter.render(width, 24):
            assert len(line) <= width, (width, len(line), line)


def test_reporter_is_disabled_off_a_terminal():
    reporter = _reporter(tty=False)
    assert reporter.enabled() is False
    reporter.start()
    reporter.paint()
    reporter.stop()
    assert reporter.stream.getvalue() == ""


def test_paint_then_clear_wipes_the_block():
    reporter = _reporter(slots=2)
    reporter.file_started("one.cbz", 1000)
    reporter.paint()
    assert reporter._painted == 3
    assert reporter.stream.getvalue() != ""
    reporter.clear()
    assert reporter._painted == 0
    assert reporter.stream.getvalue().endswith("\033[3A")


def test_repaint_moves_back_over_the_previous_block():
    reporter = _reporter(slots=2)
    reporter.paint()
    reporter.stream.truncate(0)
    reporter.stream.seek(0)
    reporter.paint()
    assert reporter.stream.getvalue().startswith("\033[3A")


def test_only_used_for_parallel_runs_with_progress():
    assert DownloadLibrary("x", jobs=1, progress_bar=True)._progress is None
    assert DownloadLibrary("x", jobs=4, progress_bar=False)._progress is None
    parallel = DownloadLibrary("x", jobs=4, progress_bar=True)
    assert parallel._progress is not None
    assert parallel._progress.slots == 4
    assert parallel._show_bar is False


###
# _muncher_bar
###
def test_muncher_starts_at_the_left_and_ends_at_the_right():
    style = PROGRESS_STYLES["bars"]
    assert _muncher_bar(0.0, 10, style, 0) == "[#---------]"
    assert _muncher_bar(1.0, 10, style, 0) == "[##########]"


def test_muncher_leaves_a_wake_and_eats_the_pellets_ahead():
    line = _muncher_bar(0.5, 12, PROGRESS_STYLES["fish"], 0)
    assert line.startswith("[~")
    assert line.endswith("\u00b7]")
    assert "><>" in line


def test_muncher_animates_between_frames():
    style = PROGRESS_STYLES["fish"]
    first = _muncher_bar(0.5, 20, style, 0)
    second = _muncher_bar(0.5, 20, style, 1)
    assert first != second, "the muncher never chomps"
    assert len(first) == len(second), "chomping changes the bar width"


def test_every_style_keeps_a_constant_width():
    for name, style in PROGRESS_STYLES.items():
        widths = {
            len(_muncher_bar(0.5, 20, style, frame))
            for frame in range(len(style["muncher"]) * 2)
        }
        assert widths == {22}, (name, widths)


def test_muncher_paces_when_the_size_is_unknown():
    style = PROGRESS_STYLES["fish"]
    seen = {_muncher_bar(None, 16, style, frame) for frame in range(12)}
    assert len(seen) > 2, "an unknown size should still look alive"
    for line in seen:
        assert len(line) == 18


def test_muncher_copes_with_a_tiny_bar():
    for width in range(0, 6):
        line = _muncher_bar(0.5, width, PROGRESS_STYLES["fish"], 0)
        assert line.startswith("[") and line.endswith("]")


def test_unknown_style_name_falls_back_to_the_default():
    reporter = ProgressReporter(stream=io.StringIO(), style="no-such-style")
    assert reporter.style is PROGRESS_STYLES[DEFAULT_PROGRESS_STYLE]


###
# interrupt handling
###
def test_stream_to_file_stops_between_chunks(tmp_path):
    dl = DownloadLibrary(str(tmp_path))
    dl._stop.set()
    with pytest.raises(Interrupted):
        dl._stream_to_file(
            ChunkedResponse(b"abcdefgh"), str(tmp_path / "f.part"),
            False, 0, 8,
        )


def test_interrupted_transfer_is_not_reported_as_a_failure(tmp_path, caplog):
    dl = DownloadLibrary(str(tmp_path))
    dl.cache_file = str(tmp_path / ".cache.json")
    dl.cache_data = {}
    dl._stop.set()

    target = str(tmp_path / "thing.bin")
    with caplog.at_level(logging.ERROR):
        result = dl._process_download(
            ChunkedResponse(b"abcdefgh", {"Content-Length": "8"}),
            "key", {}, target, remote_file="https://x/thing.bin",
        )

    assert result is False
    assert not os.path.exists(target + ".part")
    assert "Failed to download" not in caplog.text
    assert dl.cache_data == {}


def test_interrupted_transfer_leaves_an_existing_file_alone(tmp_path):
    keeper = tmp_path / "thing.bin"
    keeper.write_bytes(b"the copy from last time")
    dl = DownloadLibrary(str(tmp_path))
    dl.cache_file = str(tmp_path / ".cache.json")
    dl.cache_data = {}
    dl._stop.set()

    dl._process_download(
        ChunkedResponse(b"abcdefgh", {"Content-Length": "8"}),
        "key", {}, str(keeper), remote_file="https://x/thing.bin",
    )
    assert keeper.read_bytes() == b"the copy from last time"


def test_stop_workers_forces_an_exit_when_a_worker_will_not_stop():
    dl = DownloadLibrary("fake_library_path", jobs=1)
    forced = []
    dl._hard_exit = lambda: forced.append(True)

    release = threading.Event()
    dl._queue = queue.Queue()
    wedged = threading.Thread(target=release.wait, daemon=True)
    wedged.start()
    dl._workers = [wedged]

    dl._stop_workers(timeout=0.2)
    assert forced == [True]
    release.set()
    wedged.join(timeout=5)


def test_stop_workers_does_not_force_an_exit_normally():
    dl = DownloadLibrary("fake_library_path", jobs=2)
    forced = []
    dl._hard_exit = lambda: forced.append(True)
    dl._start_workers()
    dl._stop_workers(timeout=5)
    assert forced == []
    assert dl._workers == []


def test_interrupt_handler_is_installed_and_restored():
    dl = DownloadLibrary("fake_library_path")
    before = signal.getsignal(signal.SIGINT)
    previous = dl._install_interrupt_handler()
    assert signal.getsignal(signal.SIGINT) is not before
    dl._restore_interrupt_handler(previous)
    assert signal.getsignal(signal.SIGINT) is before


def test_first_interrupt_sets_stop_and_raises():
    dl = DownloadLibrary("fake_library_path")
    previous = dl._install_interrupt_handler()
    handler = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
        assert dl._stop.is_set()
    finally:
        dl._restore_interrupt_handler(previous)


def test_second_interrupt_exits_hard():
    dl = DownloadLibrary("fake_library_path")
    forced = []
    dl._hard_exit = lambda: forced.append(True)
    previous = dl._install_interrupt_handler()
    handler = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)   # no raise: it leaves instead
        assert forced == [True]
    finally:
        dl._restore_interrupt_handler(previous)
