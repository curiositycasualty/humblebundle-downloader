"""Files on disk: part files and their resume notes, checksums, names and sizes"""

import json
import os


# Below this fraction of the size it replaces, or of the size Humble
# lists, a file counts as much smaller: likely more heavily compressed
SMALLER_RATIO = 0.75


# Checksums of nothing at all. Humble lists these for some files, having
# hashed an empty file at some point; no download could ever match one
EMPTY_DIGESTS = frozenset((
    "d41d8cd98f00b204e9800998ecf8427e",                     # md5
    "da39a3ee5e6b4b0d3255bfef95601890afd80709",             # sha1
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
))


def _struct_hashes(entry):
    """Checksums the api offers for a file, strongest first.

    Everything is optional: entries without one are downloaded and kept
    exactly as before. A listed checksum of an empty file is treated as
    none, since it says nothing about the file actually offered
    """
    found = {}
    for name in ("sha1", "md5"):
        value = entry.get(name)
        if isinstance(value, str) and value.strip():
            value = value.strip().lower()
            if value not in EMPTY_DIGESTS:
                found[name] = value
    return found


def _digest_of(algorithm, hasher):
    """What was computed, ready to record in the cache"""
    if hasher is None or algorithm is None:
        return {}
    return {algorithm: hasher.hexdigest()}


def _much_smaller(new, old):
    """Whether new is so much smaller than old that it is likely a
    lesser copy of the same thing rather than the same thing"""
    return bool(new and old) and new < SMALLER_RATIO * old


def _resume_note(part_file):
    """Beside a part file: what the server called the file it came from"""
    return part_file + ".json"


def _write_resume_note(part_file, validator):
    """Record the validator a later run needs to carry on safely. With
    no validator there is no safe way to, so there is no note, and the
    part file is not kept"""
    note = _resume_note(part_file)
    if not validator:
        _remove_quietly(note)
        return
    try:
        with open(note, "w") as handle:
            json.dump({"validator": validator}, handle)
    except OSError:
        pass


def _resume_point(part_file):
    """(bytes held, validator) for a part file an earlier run left, or
    None when there is nothing safe to carry on from"""
    size = _file_size(part_file)
    if not size:
        return None
    try:
        with open(_resume_note(part_file)) as handle:
            validator = json.load(handle).get("validator")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(validator, str) or not validator:
        return None
    return size, validator


def _feed_hash_from_file(hasher, path):
    """Re-read what is already on disk so a resumed transfer's hash
    covers the whole file, not just the part that arrived this time
    """
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 256), b""):
            hasher.update(block)


def _write_fd(fd, text):
    """Write to a file descriptor directly, past Python's buffered
    streams and their locks. For a signal handler, which may have
    interrupted a thread that was in the middle of writing"""
    try:
        os.write(fd, text.encode("utf-8", "replace"))
    except OSError:
        pass


def _mount_hint(path):
    """Why a path might be missing, for the error message"""
    absolute = os.path.abspath(path)
    for mount_root in ("/Volumes/", "/media/", "/mnt/", "/run/media/"):
        if absolute.startswith(mount_root):
            return "is the drive plugged in and mounted?"
    parent = os.path.dirname(absolute)
    if parent and not os.path.exists(parent):
        return "{parent} does not exist either".format(parent=parent)
    return None


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _clean_name(dirty_str):
    allowed_chars = (" ", "_", ".", "-", "[", "]")
    clean = []
    for c in dirty_str.replace("+", "_").replace(":", " -"):
        if c.isalpha() or c.isdigit() or c in allowed_chars:
            clean.append(c)

    return "".join(clean).strip().rstrip(".")


def _file_ext(filename):
    """Lowercase extension of a filename, or '' when it has none"""
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def _struct_filename(file_type):
    """Filename of a download_struct entry, or None if it has no url"""
    if "url" not in file_type or "web" not in file_type["url"]:
        return None
    return file_type["url"]["web"].split("?")[0].split("/")[-1]


def _coerce_size(raw_size):
    """The api is not consistent about the type used for file sizes"""
    if raw_size is None:
        return None
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None
