import pytest
from humblebundle_downloader.cli import parse_args, resolve_jobs


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


def test_jobs_defaults_to_four():
    args = parse_args(["-l", "some_path", "-c", "fake_cookie"])
    assert args.jobs == 4
    assert args.no_parallel is False
    assert resolve_jobs(args) == 4


def test_jobs_can_be_set():
    args = parse_args(["-l", "some_path", "-c", "fake_cookie", "-j", "12"])
    assert resolve_jobs(args) == 12


def test_no_parallel_overrides_jobs():
    args = parse_args(
        ["-l", "some_path", "-c", "fake_cookie", "-j", "12", "--no-parallel"]
    )
    assert args.jobs == 12
    assert resolve_jobs(args) == 1


def test_exit_timeout_defaults_to_unset():
    args = parse_args(["-l", "some_path", "-c", "fake_cookie"])
    assert args.exit_timeout is None


def test_exit_timeout_accepts_a_fraction():
    args = parse_args(
        ["-l", "some_path", "-c", "fake_cookie", "--exit-timeout", "2.5"]
    )
    assert args.exit_timeout == 2.5


def test_exit_timeout_accepts_zero():
    args = parse_args(
        ["-l", "some_path", "-c", "fake_cookie", "--exit-timeout", "0"]
    )
    assert args.exit_timeout == 0


def test_skip_lines_are_shown_by_default():
    args = parse_args(["-l", "some_path", "-c", "fake_cookie"])
    assert args.hide_skipped is False


def test_skip_lines_can_be_hidden():
    args = parse_args(
        ["-l", "some_path", "-c", "fake_cookie", "--hide-skipped"]
    )
    assert args.hide_skipped is True
