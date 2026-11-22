"""hbd --diagnose: what hbd can and cannot do with Humble's site, right now.

Humble rebuilt its site once and broke the tool; it will change again.
This walks through every step a run depends on, one request each where
it can, and says which work. It downloads nothing but one file's
headers.

The report is safe to paste anywhere: it gives cookie names but never
their values, and counts and field names rather than bundle ids, keys
or download urls. Hosts are shown, tokens never.
"""
import datetime
import platform
import sys
from dataclasses import dataclass
from importlib import metadata
from urllib.parse import urlsplit

from .formatting import _human_size
from .humble_site import (
    DOWNLOADABLE,
    LIBRARY_PATH,
    LIST_ACTION,
    SIGN_ACTION,
    VAULT_SIGN_ACTION,
    NotSignedIn,
)

PASS, FAIL, SKIP = "ok", "failed", "skipped"
MARKS = {PASS: "✓", FAIL: "✗", SKIP: "–"}


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "?"


def versions():
    return "hbd {hbd} · Python {py} on {os} · requests {req} · textual {tx}".format(
        hbd=_version("humblebundle-downloader"),
        py=platform.python_version(),
        os=platform.system() or sys.platform,
        req=_version("requests"),
        tx=_version("textual"),
    )


def run(site, session, report=None):
    """Every check, in order, each passed to report(Check) as it is
    done. Returns them all. A check whose groundwork failed is skipped"""
    checks = []

    def note(name, status, detail):
        check = Check(name, status, detail)
        checks.append(check)
        if report is not None:
            report(check)
        return status == PASS

    # 1. The site answers, and does not send us to sign in
    try:
        response = site._get(LIBRARY_PATH)
        final = urlsplit(getattr(response, "url", "") or "").netloc or "the site"
        ok = response.status_code == 200
        note("Site", PASS if ok else FAIL, "{0} answered http {1}".format(final, response.status_code))
    except NotSignedIn:
        note("Site", FAIL, "the library page sent us to sign in: the cookie is not accepted")
        ok = False
    except Exception as error:
        note("Site", FAIL, "could not reach it: {0}".format(_short(error)))
        ok = False

    # 2. Which cookies are sent (names only)
    sent, elsewhere = site._cookie_names()
    if sent:
        detail = ", ".join(sorted(sent))
        if elsewhere:
            detail += " (also held, for other hosts: {0})".format(", ".join(sorted(elsewhere)))
        note("Cookies", PASS, detail)
    else:
        note("Cookies", FAIL, "none are sent to the site" + (
            " (held for other hosts: {0})".format(", ".join(sorted(elsewhere))) if elsewhere else ""))

    # 3. The server actions hbd calls, found by name in the site's scripts
    actions = {}
    if ok:
        try:
            actions.update(site._actions_on(LIBRARY_PATH))
        except Exception as error:
            note("Actions", FAIL, "could not read the library page's scripts: {0}".format(_short(error)))
    # 4. Signed in, and the listing reads
    entries = []
    if ok and LIST_ACTION in actions:
        try:
            value = site.list_page(1, limit=50)
            entries = [e for e in value["results"] if isinstance(e, dict)]
            pagination = value.get("pagination") or {}
            total = pagination.get("totalDocs")
            types = {}
            for entry in entries:
                types[entry.get("type")] = types.get(entry.get("type"), 0) + 1
            listed = "{0} entries".format("{:,}".format(total) if isinstance(total, int) else "?")
            kinds = ", ".join("{0} {1}".format(n, t) for t, n in sorted(types.items(), key=lambda x: -x[1]))
            note("Signed in", PASS, "the library lists {0}; the first {1}: {2}".format(listed, len(entries), kinds or "none"))
        except NotSignedIn:
            note("Signed in", FAIL, "the listing says not signed in: the cookie has expired or is wrong")
        except Exception as error:
            note("Signed in", FAIL, "the listing failed: {0}".format(_short(error)))
    elif ok:
        note("Signed in", SKIP, "no listing action to ask with")
    else:
        note("Signed in", SKIP, "the site did not answer")

    # 5. A bundle page, and what its files look like
    entitlement = None
    bundle = next((e.get("productEntitlementId") for e in entries
                   if e.get("type") == DOWNLOADABLE and e.get("productEntitlementId")), None)
    if bundle:
        try:
            files = site.page_files(LIBRARY_PATH + "/" + bundle)
            with_files = {k: v for k, v in files.items() if v.get("downloads")}
            structs = [s for v in with_files.values() for d in v["downloads"]
                       for s in d.get("downloadStruct") or [] if isinstance(s, dict)]
            fields = sorted({k for s in structs for k in s})
            entitlement = next(iter(with_files), None)
            if structs:
                note("Bundle page", PASS, "{0} items with files, {1} files; file fields: {2}".format(
                    len(with_files), len(structs), ", ".join(fields)))
            else:
                note("Bundle page", FAIL, "a bundle page read, but no files were found in it: its layout has changed")
        except Exception as error:
            note("Bundle page", FAIL, "could not read one: {0}".format(_short(error)))
    else:
        note("Bundle page", SKIP, "no downloadable bundle among the first entries listed")

    # The signing action lives with an item page's scripts
    if entitlement:
        try:
            actions.update(site._actions_on(LIBRARY_PATH + "/content/" + entitlement))
        except Exception:
            pass
    if not ok:
        note("Actions", SKIP, "the site did not answer")
    else:
        wanted = (LIST_ACTION, SIGN_ACTION, VAULT_SIGN_ACTION)
        missing = [name for name in wanted if name not in actions]
        if not missing:
            note("Actions", PASS, "found all three: " + ", ".join(wanted))
        else:
            found = [name for name in wanted if name in actions]
            note("Actions", FAIL, "not found: {0}{1}. Humble has renamed or removed them".format(
                ", ".join(missing), "; found: " + ", ".join(found) if found else ""))

    # 6. A download url, signed
    url = None
    if entitlement and SIGN_ACTION in actions:
        try:
            urls, expires = site.sign([entitlement])
            if urls:
                url = next(iter(urls.values()))
                hosts = sorted({urlsplit(u).netloc for u in urls.values()})
                until = (datetime.datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M")
                         if expires else "an unstated time")
                note("Signing", PASS, "{0} urls signed, on {1}, good until {2}".format(
                    len(urls), ", ".join(hosts), until))
            else:
                note("Signing", FAIL, "Humble signed nothing for an item that has files")
        except Exception as error:
            note("Signing", FAIL, _short(error))
    else:
        note("Signing", SKIP, "nothing to sign" if not entitlement else "no signing action")

    # 7. The download server answers for that url, headers only
    if url:
        try:
            head = session.head(url, allow_redirects=True, timeout=30)
            size = head.headers.get("Content-Length")
            ranges = head.headers.get("Accept-Ranges", "not said")
            detail = "http {0}, {1}, resumable: {2}".format(
                head.status_code,
                _human_size(int(size)) if size and size.isdigit() else "size not given",
                ranges,
            )
            note("Download server", PASS if head.status_code == 200 else FAIL, detail)
        except Exception as error:
            note("Download server", FAIL, _short(error))
    else:
        note("Download server", SKIP, "no signed url to try")

    return checks


def _short(error):
    """The innermost reason, which is the useful part of requests'
    nested messages, without any url's query string"""
    from .logs import redact

    while error.__cause__ is not None or error.__context__ is not None:
        error = error.__cause__ or error.__context__
    text = redact(str(error) or type(error).__name__)
    return text if len(text) <= 160 else text[:159] + "…"


def format_check(check):
    return "{mark} {name}: {detail}".format(
        mark=MARKS[check.status], name=check.name, detail=check.detail)
