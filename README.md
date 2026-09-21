# Humble Bundle Downloader

[![PyPI](https://img.shields.io/pypi/v/humblebundle-downloader.svg)](https://pypi.python.org/pypi/humblebundle-downloader)
[![PyPI](https://img.shields.io/pypi/l/humblebundle-downloader.svg)](https://pypi.python.org/pypi/humblebundle-downloader)

**Download all of your content from your Humble Bundle Library!**  

The first time this runs it may take a while because it will download everything.
After that it will only download the content that has been updated or is missing.  


## Features

- support for Humble Trove _(`--trove` flag)_
- downloads new and updated content from your Humble Bundle Library on each run _(only check for updates if using `--update`)_
- cli command for easy use (downloading will also work on a headless system)
- works for SSO and 2FA accounts
- downloads several files at once, 4 by default _(`--jobs`/`--no-parallel` flags)_
- optional progress bar for each item downloaded _(`--progress` flag)_
- optional dry run that reports how much there is to download before downloading any of it _(`--dry-run` flag)_
- optional listing of the url of every file being collected _(`--print-urls` flag)_
- optional filter by file types using an include _or_ exclude list _(`--include/--exclude` flag)_
- optional filter by platform types like video, ebook, etc... _(`--platform` flag)_
- optional single format per item, with an ordered preference and a largest-file fallback _(`--prefer-format` flag)_


## Install


### Using PIP

`pip install humblebundle-downloader`


### Using docker

Remember to mount your download directory in the container using dockers `-v` argument.
`docker run ghcr.io/xtream1101/humblebundle-downloader -h`


## Instructions


### 1. Getting cookies

First thing to do is get your account cookies.
This can be done by getting a browser extension that lets you see or export your cookies.

- **Method 1 (recommended)**
    - Get the value of the cookie called `_simpleauth_sess` and pass that value using `-s 'COOKIE_VALUE'`
    - Note: The quotes in the cookie value are part of the value, you might need to wrap the entire value
      (including double quotes) in single quotes. Some suggestions for common issues can be found in [issue #50](https://github.com/xtream1101/humblebundle-downloader/issues/50)

- **Method 2**
    - Export the cookies in the Netscape format using an extension.  
      If your exported cookie file is not working, it may be a formatting issue.
      This can be fixed by running the command `curl -b cookies.orig.txt --cookie-jar cookies.txt http://bogus`


### 2. Downloading your library

Use the following command to download your Humble Bundle Library:  
`hbd --cookie-file cookies.txt --library-path "Downloaded Library" --progress`  
_If using the docker image, exclude the `hbd` part of the command_

This directory structure will be used:  
`Downloaded Library/Purchase Name/Item Name/downloaded_file.ext`


### 3. Checking the size before downloading

Add `--dry-run` (or `-n`) to see what a run would fetch without fetching any of it:  
`hbd --cookie-file cookies.txt --library-path "Downloaded Library" --dry-run`

Nothing is written to disk, no directories are created and the `.cache.json` file is left alone.
Each pending file is listed with its size, followed by a per-bundle breakdown and a grand total:

```
============================================================
Dry run: nothing was downloaded
============================================================
  3.40 GiB     2 file(s)  Game Bundle
 15.00 MiB     2 file(s)  Book Bundle - Sci-Fi
------------------------------------------------------------
4 file(s) to download, 3.41 GiB (3,663,212,288 bytes)
```

The estimate honours every other flag, so `-n` combined with `--include`, `--platform`, `--keys`,
`--trove` or `--update` tells you the size of exactly that subset.
Sizes come from the Humble Bundle api itself, so this costs no bandwidth beyond the api calls a real
run already makes. For the few files the api does not report a size for, a `HEAD` request is used
instead, and anything that still comes back without a size is counted separately at the end of the
summary.


### 4. Listing the download urls

Add `--print-urls` to print the url of each file being collected, one per line.
The urls go to stdout while all other output goes to stderr, so the list can be piped straight into
another tool:

`hbd --cookie-file cookies.txt --library-path "Downloaded Library" --dry-run --print-urls > urls.txt`

This works during a real download too, in which case it lists each file as it is fetched.
Note that Humble Bundle urls are signed and expire after a while, so a saved list is only good for
a short time.


### 5. Parallel downloads

Four files download at once by default. Change the number with `--jobs`/`-j`:

`hbd --cookie-file cookies.txt --library-path "Comics" -j 8`

`--no-parallel` goes back to one file at a time, and overrides `--jobs`, so it still works when
`-j` is baked into a shell alias.

Because several `\r` progress bars writing at once is unreadable, `--progress` draws the per-file
bar only when running sequentially. In parallel each file reports once, as it finishes:

```
Downloaded Some Comic Vol1.cbz (24.10 MiB in 3.4s)
Downloaded Another Book.epub (8.40 MiB in 1.1s)
```

A few notes on what parallelism does and does not touch. Each worker gets its own
`requests.Session`, built from the authenticated one, since `requests.Session` is not documented as
thread safe. `.cache.json` is rewritten in full after every completed file, so the update and the
write are held under one lock — without it, concurrent jobs truncate each other's json. asm.js games
still download their page sequentially, because the list of data files to fetch is read out of that
page. Dry runs stay single threaded, having nothing to download.

If Humble starts refusing connections or throttling you, lower `-j` before assuming anything else
is wrong. The downloader handles throttling on its own, though:

- Connection errors and 5xx responses are retried with exponential backoff at the transport layer.
  `--retries` sets how many times (default 5, `0` to fail on the first error).
- A `429 Too Many Requests` pauses **every** worker, not only the one that was refused, for as long
  as the server's `Retry-After` header asks (30s when it does not say). Retrying per request would
  just mean one worker backing off politely while the rest keep hammering.
- The run ends by telling you how many times it was throttled, so a slow run is not a mystery.

Requests identify themselves as `humblebundle-downloader/<version>` rather than the default
`python-requests`, which tends to be treated more harshly by the machinery in front of a CDN.


### 6. Picking one format per item

Bundles usually ship the same book or comic several times over: `.cbz`, `.epub`, `.pdf` and `.mobi`
of the same thing. `--prefer-format` (or `-f`) keeps one format instead of all of them:

`hbd --cookie-file cookies.txt --library-path "Comics" --prefer-format cbz epub pdf`

The extensions are tried **in order**, so the last one you list acts as the default:

1. every `.cbz` in the item, if it has any
2. otherwise every `.epub`
3. otherwise every `.pdf`
4. otherwise the single largest file, whatever its format

That last step is what stops an item disappearing when it ships in some format you did not think to
list, like `.cbr` or `.djvu`.

Selection happens per item **and per platform**, so a game that ships a Windows and a Linux build
still gets one file for each, rather than one file overall.

When the winning format has more than one file in an item, all of them are kept. A comic split
into `Vol1.cbz` and `Vol2.cbz` comes down as both volumes, not just the larger one — the flag
collapses format variants, never content.

`--prefer-format` composes with the other filters, and `--include`/`--exclude` are applied first, so
`-e cbz -f cbz epub` will never pick a `.cbz` and moves on to `.epub`. Combine it with `--dry-run`
to see exactly which file wins for each item before downloading anything.


## Notes

- Inside your library folder a file named `.cache.json` is saved and keeps track of the files that have been downloaded.
  This way running the download command again pointing to the same directory will only download new or updated files.
- Use `--help` with all `hbd` commands to see available options
- Find supported platforms for the `--platform` flag by visiting your Humble Bundle Library
  and look under the **Platform** dropdown
- Download select bundles by using the `-k` or `--keys` flag.
  Find these keys by going to your _Purchases_ section,
  click on a products and there should be a `downloads?key=XXXX` in the url.
