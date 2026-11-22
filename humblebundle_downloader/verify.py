"""hbd --verify: is every file in the library still the one downloaded?

Each file is hashed and compared with the checksum the cache recorded
when it was downloaded. Nothing is fetched and no cookie is needed: it
reads the library and its .cache.json, and nothing else.

Downloads from 0.6.0 on record where they went, so they are found by
path. Older cache entries only know the file's name and bundle id, so
those are matched by name; a name that appears in several bundles
counts as unchanged if it matches any of them.
"""
import datetime
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .logs import LOG_DIR

CHUNK = 1024 * 1024
# Hashing is mostly waiting on the drive; a few at once keeps it busy
# without thrashing a spinning disk
WORKERS = 4

OK = "unchanged"
CHANGED = "changed"
MISSING = "missing"
UNCHECKED = "no checksum"

# Not library content: hbd's own files, part files, and the clutter
# operating systems leave
_SKIP_NAMES = {".cache.json", ".DS_Store", "Thumbs.db", "desktop.ini"}
_SKIP_SUFFIXES = (".part", ".part.json", ".tmp")


@dataclass(frozen=True)
class Result:
    path: str                    # relative to the library, with /
    status: str
    algorithm: str = ""
    expected: str = ""
    actual: str = ""
    size: int = 0


class NothingToVerify(Exception):
    pass


def load_cache(library_path):
    path = os.path.join(library_path, ".cache.json")
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise NothingToVerify(
            "There is no .cache.json in {0}, so nothing to check the files "
            "against. Is that the library?".format(library_path)
        ) from None
    except (OSError, ValueError) as error:
        raise NothingToVerify("Could not read {0}: {1}".format(path, error)) from error
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}


def _digests(entry):
    return {name: entry[name].lower() for name in ("sha1", "md5")
            if isinstance(entry.get(name), str) and entry[name]}


def index(cache):
    """(by path, by name): what each file should hash to. A path maps
    to one entry's digests, a name to every entry's with that name"""
    by_path, by_name = {}, {}
    for key, entry in cache.items():
        digests = _digests(entry)
        path = entry.get("path")
        if isinstance(path, str) and path:
            by_path[path] = digests
        elif digests:
            name = key.rsplit(":", 1)[-1]
            by_name.setdefault(name, []).append(digests)
    return by_path, by_name


def library_files(library_path):
    """(relative path, absolute path, size) of every file worth checking"""
    for root, dirs, files in os.walk(library_path):
        # hbd's logs, virtualenvs, .git, the Trash: none of it is content
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name in _SKIP_NAMES or name.endswith(_SKIP_SUFFIXES) or name.startswith((".hbd-", "._")):
                continue
            full = os.path.join(root, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            rel = os.path.relpath(full, library_path).replace(os.sep, "/")
            yield rel, full, size


def _hash(path, algorithms, progress=None):
    hashers = {name: hashlib.new(name) for name in algorithms}
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            for hasher in hashers.values():
                hasher.update(chunk)
            if progress is not None:
                progress(len(chunk))
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}


def _check(rel, full, size, expected, progress):
    """expected: a list of digest dicts, any of which is a match"""
    algorithms = sorted({name for digests in expected for name in digests},
                        key=lambda n: n != "sha1")
    actual = _hash(full, algorithms, progress)
    for digests in expected:
        for name, value in digests.items():
            if actual.get(name) == value:
                return Result(rel, OK, name, value, value, size)
    name = algorithms[0]
    first = next((d[name] for d in expected if name in d), "")
    return Result(rel, CHANGED, name, first, actual[name], size)


def verify_library(library_path, workers=WORKERS, progress=None, planned=None):
    """Every file's Result, files in the cache missing from disk too.

    progress(bytes) is called as bytes are hashed, from several threads;
    planned(files, bytes) once, with how much there is to hash
    """
    cache = load_cache(library_path)
    by_path, by_name = index(cache)

    todo, results, seen = [], [], set()
    for rel, full, size in library_files(library_path):
        seen.add(rel)
        if rel in by_path:
            expected = [by_path[rel]] if by_path[rel] else []
        else:
            expected = by_name.get(rel.rsplit("/", 1)[-1], [])
        if expected:
            todo.append((rel, full, size, expected))
        else:
            results.append(Result(rel, UNCHECKED, size=size))

    for rel in sorted(by_path):
        if rel not in seen:
            results.append(Result(rel, MISSING))

    if planned is not None:
        planned(len(todo), sum(item[2] for item in todo))
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="hbd-verify") as pool:
        results.extend(pool.map(lambda item: _check(*item, progress), todo))
    return sorted(results, key=lambda r: r.path)


def write_report(library_path, results, now=None):
    """Every result as a tab separated file in the library's logs
    folder. Returns its path, or None when it cannot be written"""
    folder = os.path.join(library_path, LOG_DIR)
    stamp = (now or datetime.datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(folder, "verify-{0}.tsv".format(stamp))
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# status\tpath\talgorithm\texpected\tactual\n")
            for r in results:
                handle.write("\t".join((r.status, r.path, r.algorithm, r.expected, r.actual)) + "\n")
    except OSError:
        return None
    return path


def summary(results):
    counts = {status: 0 for status in (OK, CHANGED, MISSING, UNCHECKED)}
    for r in results:
        counts[r.status] += 1
    return counts
