"""Defaults from a config file, so a plain `hbd` does the right thing.

~/.config/hbd/config.toml (or $XDG_CONFIG_HOME/hbd/config.toml, or
wherever HBD_CONFIG or --config points) holds any of hbd's long options
as keys:

    library-path = "/Volumes/Books/Humble"
    cookie-file = "~/.config/hbd/cookies.txt"
    jobs = "auto"
    prefer-format = ["cbz", "largest"]
    progress-style = "cycle"

Anything given on the command line wins. What a run is (--verify,
--diagnose, --dry-run) is never taken from the file: a default that
quietly turns every run into a dry run would be a trap. Nor is
--reset-cache, which would set the cache aside on every run.
"""
import os
import stat
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

# Options that choose what a run does, or which config to read
NOT_IN_CONFIG = {"verify", "diagnose", "dry_run", "reset_cache", "config", "no_config", "help"}


class ConfigError(Exception):
    pass


def default_path(environ=None):
    """Where the config file is, or None when HBD_CONFIG is set empty"""
    environ = os.environ if environ is None else environ
    if "HBD_CONFIG" in environ:
        return os.path.expanduser(environ["HBD_CONFIG"]) or None
    base = environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "hbd", "config.toml")


def load(path):
    """The file's settings, or {} when there is no file"""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as error:
        raise ConfigError("{0} is not valid TOML: {1}".format(path, error)) from error
    except OSError as error:
        raise ConfigError("Could not read {0}: {1}".format(path, error.strerror or error)) from error
    return data


def defaults(parser, data, path):
    """Turn the file's keys into argparse defaults, checked against the
    options the parser has. Returns {dest: value}"""
    actions = {}
    for action in parser._actions:
        for option in action.option_strings:
            if option.startswith("--"):
                actions[option[2:]] = action
    out = {}
    for key, value in data.items():
        name = key.replace("_", "-")
        action = actions.get(name)
        if action is None or action.dest in NOT_IN_CONFIG:
            hint = " (that is chosen on the command line)" if action is not None else ""
            raise ConfigError("{0}: '{1}' is not a setting hbd has{2}".format(path, key, hint))
        out[action.dest] = _convert(action, value, path, key)
    return out


def _convert(action, value, path, key):
    def bad(expected):
        return ConfigError("{0}: '{1}' should be {2}, not {3!r}".format(path, key, expected, value))

    if action.nargs == 0:                       # store_true flags
        if not isinstance(value, bool):
            raise bad("true or false")
        return value
    if action.nargs in ("*", "+"):
        items = value.split() if isinstance(value, str) else value
        if not isinstance(items, list) or not all(isinstance(i, (str, int)) for i in items):
            raise bad("a list")
        items = [str(i) for i in items]
        return [action.type(i) for i in items] if callable(action.type) and action.type is not str else items
    if isinstance(value, (list, dict)):
        raise bad("a single value")
    if action.type is not None and action.type is not str:
        try:
            # As the command line would give it: every type hbd uses
            # takes text
            value = action.type(str(value).lower() if isinstance(value, bool) else str(value))
        except Exception:
            raise bad("a valid value") from None
    if action.choices is not None and value not in action.choices:
        raise bad("one of " + ", ".join(map(str, action.choices)))
    if isinstance(value, str) and action.dest in ("library_path", "cookie_file"):
        value = os.path.expanduser(value)
    return value


def readable_by_others(path):
    """Whether anyone but its owner can read the file (posix only)"""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return os.name == "posix" and bool(mode & (stat.S_IRGRP | stat.S_IROTH))
