import pytest
from humblebundle_downloader.cli import parse_args


def test_old_action_format():
    with pytest.raises(DeprecationWarning):
        _ = parse_args(["download", "-l", "some_path", "-c", "fake_cookie"])


def test_no_action():
    args = parse_args(["-l", "some_path", "-c", "fake_cookie"])
    assert args.library_path == "some_path"
    assert args.cookie_file == "fake_cookie"


def test_no_args():
    with pytest.raises(SystemExit) as ex:
        parse_args([])
    assert ex.value.code == 2


def test_dry_run_defaults_off():
    args = parse_args(["-l", "some_path", "-c", "fake_cookie"])
    assert args.dry_run is False
    assert args.print_urls is False


def test_dry_run_flags():
    args = parse_args(
        ["-l", "some_path", "-c", "fake_cookie", "--dry-run", "--print-urls"]
    )
    assert args.dry_run is True
    assert args.print_urls is True


def test_dry_run_short_flag():
    args = parse_args(["-l", "some_path", "-c", "fake_cookie", "-n"])
    assert args.dry_run is True
