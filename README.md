# Humble Bundle Downloader

[![PyPI](https://img.shields.io/pypi/v/humblebundle-downloader.svg)](https://pypi.python.org/pypi/humblebundle-downloader)
[![PyPI](https://img.shields.io/pypi/l/humblebundle-downloader.svg)](https://pypi.python.org/pypi/humblebundle-downloader)

**Download all of your content from your Humble Bundle Library!**  

The first time this runs it may take a while because it will download everything.
After that it will only download the content that has been updated or is missing.  


## Features

- works with Humble's rebuilt website (2026), which removed the api older versions relied on
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
    - Sign in at humblebundle.com, open your browser's developer tools, go to the **Network** tab and
      reload your library page. Click the request for `library`, find the `Cookie` request header, and
      copy its whole value. Pass it using `-s 'COOKIE HEADER'`, single quotes included.
    - This works whatever the session cookie is called. Since the 2026 rebuild Humble signs you in on
      `auth.humblebundle.com`, so the cookie older instructions named may not be the one that matters.
    - A bare `_simpleauth_sess` value is still accepted, as before: anything that is not
      `name=value` is taken to be one. Some suggestions for common issues can be found in
      [issue #50](https://github.com/xtream1101/humblebundle-downloader/issues/50)

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

If a run stops with a message about signing in, the cookie has expired: sign in again in your
browser and pass the new one. That is reported as an error rather than as an empty library.

The library path is checked before anything else happens. If it does not exist it is created; if it
cannot be created or written to, the run stops right there with one line saying why, rather than
reaching the network and then failing once per file hundreds of files later. Paths under
`/Volumes`, `/media`, `/mnt` and `/run/media` get a reminder to check the drive is mounted, since
that is usually what has gone wrong.

That check is repeated cheaply as the run goes: if the library folder disappears mid-run, which on
an external or network drive means it was unmounted, the run stops instead of quietly rebuilding
the folder tree inside the empty mountpoint and filling your boot disk.


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

The estimate honours every other flag, so `-n` combined with `--include`, `--platform`, `--keys`
or `--update` tells you the size of exactly that subset.
Sizes come from your library pages, the same ones the website shows, so a dry run asks Humble for
no download urls at all and transfers nothing. For the few files with no size listed, a `HEAD`
request is used instead, and anything that still comes back without a size is counted separately at
the end of the summary.


### 4. Listing the download urls

Add `--print-urls` to print the url of each file being collected, one per line.
The urls go to stdout while all other output goes to stderr, so the list can be piped straight into
another tool:

`hbd --cookie-file cookies.txt --library-path "Downloaded Library" --dry-run --print-urls > urls.txt`

This works during a real download too, in which case it lists each file as it is fetched.
Humble only hands out a download url on request, so with `--dry-run` this asks for one per item,
which takes about half a second each: a whole library takes a while. The urls are signed and stop
working after roughly a day, so a saved list is only good for a short time.


### 5. Parallel downloads

Four files download at once by default. Change the number with `--jobs`/`-j`:

`hbd --cookie-file cookies.txt --library-path "Comics" -j 8`

`--no-parallel` goes back to one file at a time, and overrides `--jobs`, so it still works when
`-j` is baked into a shell alias.

`--progress` gives each transfer in flight its own line, repainted in place, with a totals line
underneath. One reporter thread owns the whole block, so concurrent transfers cannot shred each
other's output:

```
Monstress Volume 1.cbz              1.00 MiB 964.52 KiB/s 00:03 [~~~~~~~~~><>········]  51%
Descender Volume 2.cbz              1.13 MiB 968.39 KiB/s 00:06 [~~~~~~><>···········]  34%
Black Hammer Omnibus.pdf            1.25 MiB 956.79 KiB/s 00:09 [~~~~><>·············]  20%
Saga Volume 5.cbz                 898.44 KiB 957.58 KiB/s 00:01 [~~~~~~~~~~~~><>·····]  72%
Total 4/9                           4.06 MiB   3.74 MiB/s 00:11 [#########-----------]  44%
```

Each line carries the file, its size, current speed, ETA and a muncher eating its way along the
bar, chomping as it goes. A file whose size the api does not report gets a muncher pacing back and
forth instead of a percentage. Slots are stable, so a finished transfer leaves its row empty for
the next one rather than making the other lines jump about. The totals line counts files rather
than bytes, and only grows a bar once everything has been queued and the denominator is final.

`--progress-style` picks the muncher: `fish` (the default), `snail`, `bars` or `blocks`. They live
in `progress_styles.py`, which is a plain dict with no imports, so adding your own is a two line
change — the only rule is that every frame of a style has to be the same width, or the bar jitters
as it animates.

Each file also announces itself as it lands, on its own line above the block:

```
Downloaded Some Comic Vol1.cbz (24.10 MiB in 3.4s)
Downloaded Another Book.epub (8.40 MiB in 1.1s)
```

Running sequentially (`--no-parallel`) keeps the original per-file bar. The block is only drawn to
a terminal: piped or redirected output gets the completion lines alone, with no escape sequences to
clean up afterwards. A terminal too narrow or too short for the block falls back to a single
summary line automatically.


### Stopping a run

**Ctrl+C** asks for a tidy stop. Transfers check between chunks, so they give up promptly rather
than at the end of whatever multi-gigabyte file they happen to be on. Partly downloaded `.part`
files are removed, the cache keeps everything already finished, and the run exits with status
`130`. A second run picks up where this one left off.

**Ctrl+C again** quits immediately, whatever state anything is in. Partly downloaded files are left
behind, and the next run replaces them.

If a transfer is wedged somewhere that cannot be interrupted, the first Ctrl+C waits ten seconds
for it before leaving anyway, so one press is always enough eventually and two are enough at once.
`--exit-timeout SECONDS` changes that wait: `--exit-timeout 0` makes the first press quit straight
away, a longer value gives slow drives and flaky connections more room to finish what they were
doing. A second Ctrl+C ignores it entirely and quits at once however long it was set to.
Either way the exit status is `130`, the usual convention for "stopped by SIGINT" — note that
upstream exited `0` here, so a wrapper script can now tell an interrupted run from a complete one.

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

Transfers that break partway through are picked back up rather than started again. Content is
written to `<file>.part` and only moved into place once it is whole, so an interrupted download is
never mistaken for a finished one, and a previously downloaded copy is left untouched until the new
one is complete.

Whether a transfer can actually resume is the server's call, not an assumption: the downloader asks
with a `Range` header and believes the reply. `206 Partial Content` means it appends to what it
already has; `200` means the server ignored the request, so the file starts again from nothing and
that is noted once in the log. Either way the file you end up with is correct. Runs of
`--retries` apply here too, so a large file that keeps dropping is retried rather than abandoned on
the first stumble.

The resume also carries `If-Range`, holding the `ETag` or `Last-Modified` of the response the
transfer started from. That asks the server for the rest of **this** file rather than the rest of
whatever is at that url now, and a server that notices the difference answers with the whole of the
new file instead of a slice of it. Without that, a file re-generated upstream mid-transfer would
have its new second half stitched onto the old first half, giving a file of exactly the right
length and entirely the wrong contents.


### 6. Checking what arrived

Humble reports a checksum for most files. Each download is hashed as it streams past, costing no
extra pass over the data, and checked against what was promised. A file that does not match is not
kept: the partial file is discarded and it is downloaded again from scratch rather than resumed,
since resuming would keep the bad bytes. `--retries` governs how many attempts it gets, and a file
that never matches is left out of the library and out of the cache, so the next run tries again.

`sha1` is used when it is offered and `md5` otherwise, and the result is recorded in `.cache.json`
so a later run can tell a file that changed on disk from one that changed upstream. Items the api
gives no checksum for are downloaded and kept exactly as before.

This is the backstop for the resume case above: `If-Range` is what prevents a mismatched splice,
and the checksum is what catches one if a server ignores it. `--no-verify` turns the checking off.


### 7. Picking one format per item

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


## How it reads the rebuilt site

In 2026 Humble rebuilt its website and removed the json api this tool used to read the library
from. The website now gets everything through its own server actions: functions the page calls by
name. This tool calls the same ones, in the same way the page does:

- the whole library is listed a thousand entries at a time, so a large library takes a few requests
- each bundle's page supplies every file in it, with its size and checksums, in one request
- a download url is only asked for when a file is about to be downloaded, never ahead of time.
  They expire, and a large library takes longer to download than they last. A url that is refused
  part way through is simply asked for again

Server actions are addressed by ids that change every time Humble updates the site, so none are
written into this tool. Each run finds them by name in the site's own scripts, and only a change to
those names would break it.

Library entries that are keys for Steam, Epic, GOG and so on are left alone, as they always were.

### Upgrading from a version before the rebuild

The rebuilt site identifies bundles by new ids, so none of the entries in an older `.cache.json`
match any more. Rather than download everything again, a file already on disk at the place this run
would save it, and at exactly the size the site lists, is recorded in the cache as downloaded and
left alone. The run reports how many files that applied to. Run with `--dry-run` first to see what
would still be downloaded.

The first `--update` run to reach such a file records the date the server reports for it, without
downloading it, so later updates have something to compare against.

### The Humble Trove

The Trove is gone from the rebuilt site (it became the subscriber vault) and both endpoints `--trove`
used now answer 404, so `--trove` stops straight away with a message saying so.


## Notes

- Inside your library folder a file named `.cache.json` is saved and keeps track of the files that have been downloaded.
  This way running the download command again pointing to the same directory will only download new or updated files.
- Use `--help` with all `hbd` commands to see available options
- Every file a filter passes over is logged, which is useful for working out why something did not
  arrive and a wall of noise on a run that filters most of a library out. `--hide-skipped` turns
  those lines off; they drop to debug rather than disappearing, so `HBD_LOGLEVEL=DEBUG` still shows
  them. Orders that could not be read are reported either way, since that is a problem rather than
  a choice you made
- Find supported platforms for the `--platform` flag by visiting your Humble Bundle Library
  and look under the **Platform** dropdown
- Download select bundles by using the `-k` or `--keys` flag.
  These are the ids at the end of a bundle's page in your library, `/home/library/<id>`. The
  thirteen character purchase keys older versions used do not appear on the rebuilt site.
  A key given more than once is only used once, so nothing is fetched or downloaded twice.
