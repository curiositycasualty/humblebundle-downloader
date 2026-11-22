import requests

from humblebundle_downloader import diagnose
from humblebundle_downloader.humble_site import HumbleSite

from .test_site_downloads import FakeHumble, two_bundles

SECRET = "s3cr3t-cookie-value"


def signed_in_fake():
    fake = FakeHumble(*two_bundles())
    jar = requests.cookies.RequestsCookieJar()
    jar.set("_simpleauth_sess", SECRET, domain="www.humblebundle.com", path="/")
    fake.cookies = jar
    return fake


def run(fake):
    lines = []
    checks = diagnose.run(HumbleSite(fake), fake, report=lambda c: lines.append(diagnose.format_check(c)))
    return {c.name: c for c in checks}, "\n".join(lines)


def test_everything_working(capsys):
    checks, text = run(signed_in_fake())
    assert {name: c.status for name, c in checks.items()} == {
        "Site": "ok", "Cookies": "ok", "Signed in": "ok", "Bundle page": "ok",
        "Actions": "ok", "Signing": "ok", "Download server": "ok",
    }
    assert checks["Cookies"].detail == "_simpleauth_sess"
    assert "the library lists 3 entries" in checks["Signed in"].detail
    assert "file fields:" in checks["Bundle page"].detail and "sha1" in checks["Bundle page"].detail
    assert "on cdn.example" in checks["Signing"].detail
    assert checks["Download server"].detail.startswith("http 200")


def test_nothing_secret_is_shown():
    fake = signed_in_fake()
    _, text = run(fake)
    assert SECRET not in text
    assert "?t=" not in text
    for bundle_or_item in ("b1", "b2", "e1", "e2", "e3"):
        assert " {0}".format(bundle_or_item) not in text


def test_signed_out_fails_at_the_listing_and_skips_what_needs_it():
    fake = signed_in_fake()
    fake.signed_out = True
    checks, _ = run(fake)
    assert checks["Site"].status == "failed" and "sign in" in checks["Site"].detail
    assert checks["Signed in"].status == "skipped"
    assert checks["Download server"].status == "skipped"


def test_a_renamed_action_is_named():
    fake = signed_in_fake()
    fake.list_id = None
    original = fake.get

    def get(url, headers=None, **kwargs):
        response = original(url, headers=headers, **kwargs)
        if url.endswith("lib-1.js"):
            response.text = response.text.replace("filterUserLibrary", "listEverything")
        return response

    fake.get = get
    checks, _ = run(fake)
    assert checks["Actions"].status == "failed" and "filterUserLibrary" in checks["Actions"].detail
    assert checks["Signed in"].status == "skipped"


def test_versions_line():
    assert diagnose.versions().startswith("hbd ")


def test_the_command_needs_no_library_and_reports_failure_cleanly(monkeypatch, capsys):
    import sys

    import pytest

    from humblebundle_downloader import cli as cli_module

    monkeypatch.setattr(sys, "argv", ["hbd", "--diagnose", "-s", SECRET])
    with pytest.raises(SystemExit) as done:
        cli_module.cli()
    out = capsys.readouterr().out
    # No network in tests, so the very first step fails, and says so
    assert done.value.code == 1
    assert out.splitlines()[0].startswith("hbd ")
    assert "✗ Site: could not reach it" in out
    assert "checks failed" in out
    assert SECRET not in out
