import json
import logging
import os
import sys

import pytest

from humblebundle_downloader import verify
from humblebundle_downloader.verify import CHANGED, MISSING, OK, UNCHECKED

from .test_site_downloads import FakeHumble, library, two_bundles


@pytest.fixture
def downloaded(tmp_path):
    """A library hbd downloaded, as a run leaves it"""
    library(tmp_path, FakeHumble(*two_bundles()), jobs=2).start()
    return tmp_path / "lib"


def statuses(results):
    return {r.path: r.status for r in results}


def test_a_fresh_download_checks_out(downloaded):
    cache = json.loads((downloaded / ".cache.json").read_text())
    assert all("path" in entry and "sha1" in entry for entry in cache.values())
    results = verify.verify_library(str(downloaded))
    assert statuses(results) == {
        "Bundle One/Book One/one.epub": OK,
        "Bundle One/Book One/one.pdf": OK,
        "Bundle One/Book Two/two.pdf": OK,
        "Bundle Two/Game/game.exe": OK,
    }


def test_changed_missing_and_unknown_files_are_named(downloaded):
    (downloaded / "Bundle One/Book One/one.pdf").write_bytes(b"not what was downloaded")
    os.remove(downloaded / "Bundle Two/Game/game.exe")
    (downloaded / "Bundle One/notes.txt").write_text("mine")
    (downloaded / ".hbd-logs").mkdir()
    (downloaded / ".hbd-logs/hbd-x.log").write_text("log")
    (downloaded / ".venv").mkdir()
    (downloaded / ".venv/python").write_text("x")
    (downloaded / "Bundle One/Book Two/two.pdf.part").write_text("partial")
    found = statuses(verify.verify_library(str(downloaded)))
    assert found == {
        "Bundle One/Book One/one.epub": OK,
        "Bundle One/Book One/one.pdf": CHANGED,
        "Bundle One/Book Two/two.pdf": OK,
        "Bundle One/notes.txt": UNCHECKED,
        "Bundle Two/Game/game.exe": MISSING,
    }


def test_older_entries_are_matched_by_name(downloaded):
    cache_file = downloaded / ".cache.json"
    cache = json.loads(cache_file.read_text())
    for entry in cache.values():
        entry.pop("path")
    # The same name in another bundle, with another checksum: either will do
    cache["other:one.pdf"] = {"sha1": "0" * 40}
    cache_file.write_text(json.dumps(cache))
    found = statuses(verify.verify_library(str(downloaded)))
    assert set(found.values()) == {OK}
    (downloaded / "Bundle One/Book One/one.pdf").write_bytes(b"changed")
    assert statuses(verify.verify_library(str(downloaded)))["Bundle One/Book One/one.pdf"] == CHANGED


def test_md5_only_entries_are_checked_with_md5(downloaded):
    import hashlib

    cache_file = downloaded / ".cache.json"
    cache = json.loads(cache_file.read_text())
    body = (downloaded / "Bundle One/Book One/one.pdf").read_bytes()
    cache["b1:one.pdf"] = {"md5": hashlib.md5(body).hexdigest(), "path": "Bundle One/Book One/one.pdf"}
    cache_file.write_text(json.dumps(cache))
    (result,) = [r for r in verify.verify_library(str(downloaded)) if r.path.endswith("one.pdf")]
    assert result.status == OK and result.algorithm == "md5"


def test_progress_is_reported(downloaded):
    seen, planned = [], []
    verify.verify_library(str(downloaded), progress=seen.append, planned=lambda f, b: planned.append((f, b)))
    assert planned == [(4, sum(seen))]


def test_no_cache_is_said_plainly(tmp_path):
    with pytest.raises(verify.NothingToVerify):
        verify.verify_library(str(tmp_path))


def test_the_report_lists_everything(downloaded):
    results = verify.verify_library(str(downloaded))
    path = verify.write_report(str(downloaded), results)
    lines = open(path).read().splitlines()
    assert lines[0].startswith("# status") and len(lines) == 5


###
# hbd --verify
###
def run_cli(monkeypatch, *args):
    from humblebundle_downloader import cli as cli_module

    monkeypatch.setattr(sys, "argv", ["hbd"] + list(args))
    with pytest.raises(SystemExit) as done:
        cli_module.cli()
    return done.value.code


def test_verify_needs_no_cookie_and_exits_by_what_it_found(downloaded, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    assert run_cli(monkeypatch, "-l", str(downloaded), "--verify") == 0
    assert "Checked 4 files" in caplog.text and "4 unchanged" in caplog.text
    (downloaded / "Bundle One/Book One/one.pdf").write_bytes(b"x")
    assert run_cli(monkeypatch, "-l", str(downloaded), "--verify") == 1
    assert "changed: Bundle One/Book One/one.pdf" in caplog.text


def test_downloading_still_needs_a_cookie(monkeypatch, capsys):
    assert run_cli(monkeypatch, "-l", "x") == 2
    assert "-c/--cookie-file -s/--session-auth is required" in capsys.readouterr().err
