import logging
import os
import sys

import pytest

from humblebundle_downloader import config
from humblebundle_downloader.cli import parse_args


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("HBD_CONFIG", str(path))

    def write(text):
        path.write_text(text)
        return path

    return write


def test_the_file_fills_in_what_the_command_line_leaves_out(settings, tmp_path):
    settings('''
library-path = "~/Humble"
session-auth = "from-the-file"
jobs = "auto"
prefer-format = ["cbz", "largest"]
progress-style = "cycle"
hide-skipped = true
retries = 2
''')
    args = parse_args([])
    assert args.library_path == os.path.expanduser("~/Humble")
    assert args.session_auth == "from-the-file"
    assert args.jobs == "auto"
    assert args.prefer_format == ["cbz", "largest"]
    assert args.progress_style == "cycle"
    assert args.hide_skipped is True and args.retries == 2
    assert args.config_used == str(tmp_path / "config.toml") and args.config_has_cookie


def test_the_command_line_wins(settings):
    settings('library-path = "/a"\njobs = 8\nprefer-format = "pdf epub"\nsession-auth = "file"')
    args = parse_args(["-l", "/b", "-j", "2", "-f", "cbz"])
    assert (args.library_path, args.jobs, args.prefer_format) == ("/b", 2, ["cbz"])
    assert parse_args([]).prefer_format == ["pdf", "epub"]


def test_a_cookie_on_the_command_line_replaces_the_files_of_either_kind(settings):
    settings('library-path = "/a"\nsession-auth = "file"')
    args = parse_args(["-c", "cookies.txt"])
    assert args.cookie_file == "cookies.txt" and args.session_auth is None


def test_mistakes_in_the_file_are_named(settings, capsys):
    for text, said in [
        ("libary-path = '/a'", "'libary-path' is not a setting"),
        ("verify = true", "chosen on the command line"),
        ("jobs = 'lots'", "'jobs' should be a valid value"),
        ("progress-style = 'zeppelin'", "'progress-style' should be one of"),
        ("hide-skipped = 'yes'", "should be true or false"),
        ("library-path = ['/a']", "should be a single value"),
        ("library-path = ", "is not valid TOML"),
        ("cookie-file = 'c'\nsession-auth = 's'\nlibrary-path = '/a'", "not both"),
    ]:
        settings(text)
        with pytest.raises(SystemExit):
            parse_args(["-l", "/x", "-s", "y"] if "cookie" not in text else [])
        assert said in capsys.readouterr().err, text


def test_no_config_and_an_empty_hbd_config_read_nothing(settings):
    settings('library-path = "/a"\nsession-auth = "s"')
    assert parse_args(["--no-config", "-l", "/b", "-s", "t"]).config_used is None
    os.environ["HBD_CONFIG"] = ""
    assert config.default_path() is None


def test_an_explicit_config_must_exist(tmp_path, capsys):
    with pytest.raises(SystemExit):
        parse_args(["--config", str(tmp_path / "nope.toml"), "-l", "/a", "-s", "x"])
    assert "no settings file" in capsys.readouterr().err


def test_where_the_file_is_looked_for():
    assert config.default_path({"HBD_CONFIG": "~/h.toml"}) == os.path.expanduser("~/h.toml")
    assert config.default_path({"XDG_CONFIG_HOME": "/x"}) == "/x/hbd/config.toml"
    assert config.default_path({}).endswith(os.path.join(".config", "hbd", "config.toml"))


def test_a_cookie_in_a_file_others_can_read_is_warned_about(settings, monkeypatch, caplog):
    from humblebundle_downloader import cli as cli_module

    path = settings('library-path = "/nonexistent-lib"\nsession-auth = "s"')
    os.chmod(path, 0o644)

    class Library:
        failed_files = []

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("humblebundle_downloader.download_library.DownloadLibrary", Library)
    monkeypatch.setattr("humblebundle_downloader.tui.can_run", lambda: False)
    monkeypatch.setattr(sys, "argv", ["hbd"])
    caplog.set_level(logging.INFO)
    cli_module.cli()
    assert "chmod 600" in caplog.text
    os.chmod(path, 0o600)
    caplog.clear()
    cli_module.cli()
    assert "chmod 600" not in caplog.text and "Settings from" in caplog.text
