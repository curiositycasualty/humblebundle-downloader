import datetime
import logging
import os
import sys

import pytest

from humblebundle_downloader import logs


def test_urls_lose_their_query_strings():
    line = ("Failed to GET https://cdn.humble.com/humblebundle/wytches_vol1.pdf"
            "?t=abc123&ttl=86400 after 3 tries")
    assert logs.redact(line) == (
        "Failed to GET https://cdn.humble.com/humblebundle/wytches_vol1.pdf?… after 3 tries")
    assert logs.redact("no urls here?t=1") == "no urls here?t=1"
    assert logs.redact("'https://a.b/c?x=1', https://d.e/f") == "'https://a.b/c?…', https://d.e/f"


def test_tracebacks_are_redacted_too(tmp_path):
    handler, path = logs.open_run_log(str(tmp_path))
    log = logging.getLogger("hbd-log-test")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        try:
            raise ValueError("refused https://cdn.example/x.pdf?t=SECRET")
        except ValueError:
            log.exception("Failed https://cdn.example/x.pdf?t=SECRET")
    finally:
        log.removeHandler(handler)
        handler.close()
    text = open(path).read()
    assert "SECRET" not in text
    assert "ValueError: refused https://cdn.example/x.pdf?…" in text


def test_the_interface_filter_redacts_before_formatting():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "got %s", ("https://a/b?t=1",), None)
    logs.RedactingFilter().filter(record)
    assert record.getMessage() == "got https://a/b?…"


def test_old_logs_are_pruned_to_the_newest(tmp_path):
    folder = tmp_path / logs.LOG_DIR
    folder.mkdir()
    for day in range(1, 26):
        (folder / "hbd-2026-11-{0:02d}_10-00-00.log".format(day)).write_text("x")
    (folder / "notes.txt").write_text("mine")
    handler, path = logs.open_run_log(str(tmp_path), keep=20, now=datetime.datetime(2026, 11, 30, 9))
    handler.close()
    kept = sorted(os.listdir(folder))
    assert "notes.txt" in kept
    assert len([n for n in kept if n.endswith(".log")]) == 20
    assert os.path.basename(path) in kept
    assert "hbd-2026-11-01_10-00-00.log" not in kept


def test_an_unwritable_library_means_no_log_not_a_failed_run(tmp_path):
    blocker = tmp_path / "lib"
    blocker.write_text("a file, not a folder")
    handler, reason = logs.open_run_log(str(blocker))
    assert handler is None and reason


###
# In the cli
###
@pytest.fixture
def fake_cli(monkeypatch):
    from humblebundle_downloader import cli as cli_module

    class Library:
        def __init__(self, *args, **kwargs):
            self.failed_files = []

        def start(self):
            logging.getLogger("humblebundle_downloader.download_library").info(
                "Downloaded a.pdf from https://cdn.example/a.pdf?t=SECRET")

    monkeypatch.setattr("humblebundle_downloader.download_library.DownloadLibrary", Library)
    monkeypatch.setattr("humblebundle_downloader.tui.can_run", lambda: False)
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    root.setLevel(logging.INFO)

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["hbd", "-s", "y"] + list(args))
        cli_module.cli()

    yield run
    root.setLevel(level)
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for handler in root.handlers:
        handler.filters = [f for f in handler.filters if not isinstance(f, logs.RedactingFilter)]


def test_a_run_keeps_its_log_in_the_library(tmp_path, fake_cli, caplog):
    fake_cli("-l", str(tmp_path))
    (log_file,) = (tmp_path / logs.LOG_DIR).iterdir()
    text = log_file.read_text()
    assert "Downloaded a.pdf from https://cdn.example/a.pdf?…" in text
    assert "SECRET" not in text
    assert "This run's full log is in " + str(log_file) in caplog.text


def test_no_log_file_and_dry_runs_keep_no_log(tmp_path, fake_cli):
    fake_cli("-l", str(tmp_path), "--no-log-file")
    fake_cli("-l", str(tmp_path), "--dry-run")
    assert not (tmp_path / logs.LOG_DIR).exists()


def test_a_missing_library_is_not_created_for_the_log(tmp_path, fake_cli):
    missing = tmp_path / "Volumes" / "Books"
    fake_cli("-l", str(missing))
    assert not missing.exists()
