# Change log


## Unreleased

- A run that could not download some files now lists them at the end, with why, and exits `1`
  instead of `0`, so a script or scheduled job can tell it from a clean run. A file the server
  answers 404 or similar for was skipped silently before, at debug level. Interrupted runs still
  exit `130`, and files a stopped run did not get to are not counted as failures
- The cache is now written at most every five seconds, and once more when the run ends however it
  ends, instead of being rewritten in full and flushed to disk after every file. Over a large
  library that was gigabytes of writes, with every other download waiting on each one. A run killed
  outright loses at most those few seconds of entries, for files that are complete on disk and are
  recognised on the next run
- Fixed every download failing its checksum against the rebuilt site. Humble's listing gives the
  size and checksums of an older version of many files than its servers now send, so each new
  edition was rejected and downloaded again until it ran out of retries, and nothing was saved. A
  file that arrives complete at a different size from the listing is now kept as a newer version,
  with its own checksum recorded and a warning; one at the listed size that does not match is still
  treated as damaged. Files on disk that are such newer versions are recognised with one `HEAD`
  request instead of being downloaded again
- Added `--print-checksums`, which prints the sha1 Humble lists for each file to be downloaded in
  the format `shasum -c` reads, with or without `--dry-run`
- The mouse now reaches the cheese at 90% and eats it over the last tenth, until at 100% there is
  none left
- Styles that leave the bar at 100% now also enter it: at 0% the track is empty, and from 1% the
  front of the train, bike, fish and so on comes in from the left. The arrow flies in too; the mouse
  and worm set out from the start of the bar as before
- Added nine progress styles: `rocket`, `runner`, `duck`, `car`, `comet` and `ufo`, which leave the bar
  as the file completes, and `mouse`, `arrow` and `worm`, which arrive at the cheese, the target and
  the apple at the end of it
- The train now runs into its tunnel, and the fish and snail off the end of the bar, at 100%, as the
  light cycle does, instead of stopping at the end. `bars` and `blocks` just fill
- Added `-j auto`: start at 4 downloads at once and find the fastest number while the run goes,
  up to 16. It adds two at a time while that makes the total at least 10% faster, takes a step back
  as soon as one does not, and halves when throttled; it only learns while every slot is busy.
  Changes are logged with their reason, the totals line shows the current number, and the run ends
  by naming the fastest it saw
- Every file now gets one line when it is over, with its size, time and average speed: `Downloaded <file>: <size> in <time>, avg <speed>`, or for one stopped by Ctrl+C, how far it got,
  `Stopped <file> at <n>% (<done> of <size>) after <time>, avg <speed>`. This is shown with or
  without `--progress` and with `-j 1` too, where previously nothing was said when a file finished
  one at a time and a stopped file was only mentioned at debug level
- Added a `cycle` progress style: a light cycle racing across the grid, leaving a wall of light behind it,
  and riding off the end of the bar as the file completes
- A second Ctrl+C now always quits, even with downloads in flight. It used to be handled in
  Python, which meant waiting for the main thread and taking the terminal lock first; with the
  progress display up, a thread holding that lock could keep either press from getting through.
  After the first press, the second is left to the operating system, which ends the process at
  once. The progress display also gives up after two seconds when a run is leaving, rather than
  waiting on a stuck terminal
- Added a `train` progress style. Each download's train runs along its rails into a tunnel at the
  end of its bar, and the totals get the little train from sl (Toyoda Masashi's, under sl's
  licence) crossing the screen into a tunnel as the run completes. `--train-per-download` gives
  every download its own sl train when the terminal is tall enough, stepping down to fewer trains,
  then none, as it gets shorter
- The first Ctrl+C now waits up to five minutes for a stuck transfer by default, up from ten
  seconds. Moving transfers still stop at their next chunk; `--exit-timeout` still overrides it and
  a second Ctrl+C still quits at once
- Works with Humble's rebuilt website. The json api the tool was built on (`/api/v1/user/order`,
  `/api/v1/order/<key>`) answers 404 since the 2026 rebuild, so nothing could be downloaded at all.
  The library is now read the way the website reads it, through its server actions, which are found
  by name in the site's own scripts at the start of each run rather than written into the tool: their
  ids change with every deploy, and did while this was being written
- Download urls are asked for just before each file is downloaded, by the worker about to download
  it, rather than all up front. They expire after about a day, and a large library takes longer than
  that. A url that is refused is asked for again, once
- A run that finds itself signed out, at the start or part way through, now stops with a message
  saying so and a non-zero exit, instead of finishing as though the library were empty
- Files already on disk where the run would put them, at exactly the size the site lists, are now
  recorded in the cache rather than downloaded again. Without this, upgrading would have meant
  downloading a whole library a second time, because the rebuilt site's bundle ids match no existing
  cache entry. The first `--update` to reach such a file records the server's date for it instead of
  treating the missing date as a change
- `--session-auth` also accepts a whole cookie header (`name=value; ...`), for when the session
  cookie is not `_simpleauth_sess`
- `--keys` now takes bundle ids from library page urls (`/home/library/<id>`); the old purchase keys
  do not exist on the rebuilt site
- `--trove` stops straight away with an explanation: the Trove was removed from the site, and its
  endpoints answer 404. The code for it has been removed
- Added `--hide-skipped` to turn off the line logged for every file a filter passes over, which is
  most of the output on a run that filters most of a library out. The lines drop to debug rather
  than disappearing, and orders that could not be read are still reported as errors
- Added `--exit-timeout` to set how long the first Ctrl+C waits for downloads in flight before
  quitting anyway, which was fixed at ten seconds. `0` quits straight away, and a second Ctrl+C
  still ignores it entirely
- Downloads are now checked against the checksum the api reports, hashed as they stream so there is
  no extra pass over the data. A file that does not match is discarded and downloaded again from
  scratch rather than resumed, and one that never matches is left out of the library and the cache.
  `sha1` is preferred over `md5`, the result is recorded in `.cache.json`, items with no checksum
  are unaffected, and `--no-verify` turns it off
- A resumed transfer now carries `If-Range`, so the server hands back the whole of a file that
  changed rather than a slice of it. Without that, a file re-generated upstream mid-transfer had its
  new second half stitched onto the old first half, producing a file of exactly the right length and
  entirely the wrong contents
- A purchase key given more than once, or returned more than once by the library page, is now used
  once. Previously the whole order was walked again, and in parallel the same file could be queued
  twice before the first copy finished, leaving two workers writing one `.part` at the same time
- The library path is now checked before the run starts: created if missing, and refused with one
  clear line if it cannot be created or written to. Previously an unusable path was first noticed
  by a worker thread partway through a run, as a traceback repeated once per file
- A library folder that disappears mid-run, which on an external or network drive means it was
  unmounted, now stops the run. `os.makedirs` would otherwise rebuild the folder tree inside the
  empty mountpoint and fill the boot disk instead
- Added `--dry-run`/`-n` to report the number of files and total size of a download before
  downloading anything, broken down per bundle
- Added `--print-urls` to print the url of each file being collected to stdout, one per line
- Ctrl+C now stops a run reliably. The first press asks transfers to wind up, which they notice
  between chunks rather than at the end of the current file, cleans up partial downloads and exits
  `130`; the second press quits immediately whatever state things are in. A transfer wedged
  somewhere uninterruptible is given ten seconds before the run leaves without it. Interrupted runs
  previously exited `0`, and a Ctrl+C arriving while waiting on downloads escaped as a traceback
- `--progress` now gives each parallel transfer its own line, repainted in place, with a muncher
  animating along the bar, plus a totals line underneath. One reporter thread owns the block, so
  concurrent transfers cannot shred each other's output; slots are stable so finished transfers do
  not make the other rows jump; and nothing is drawn when output is not a terminal, or when the
  terminal is too small for the block, which falls back to a single summary line
- Added `--progress-style` to pick the muncher: `fish`, `snail`, `bars` or `blocks`. They live in
  the new `progress_styles` module, a plain dict with no imports, so adding your own is easy
- Interrupted transfers now resume with an http `Range` request instead of starting over. Support
  is detected rather than assumed: a `206` appends to what was already fetched, a `200` means the
  server ignored the request and the file restarts. Content is written to `<file>.part` and moved
  into place only once complete, so a partial transfer can never be mistaken for a finished file
  and an existing copy survives a failed re-download
- Connection errors and 5xx responses are now retried with exponential backoff (`--retries`,
  default 5). A `429` pauses every worker for the duration of the server's `Retry-After`, rather
  than backing off one worker while the rest keep going, and the run reports how often it was
  throttled
- Requests now identify as `humblebundle-downloader/<version>` instead of `python-requests`
- Downloads now run 4 at a time by default. `--jobs`/`-j` sets the number, `--no-parallel` goes
  back to one at a time and overrides `--jobs`. The cache write is locked, each worker gets its own
  session, and `--progress` draws the per-file bar only when sequential, reporting each file once
  on completion otherwise
- Added `--prefer-format`/`-f` to keep a single format per item, trying the given extensions in
  order and falling back to the largest available file when none of them are present
- Fixed `_process_download` always reporting success: a `return True` in its `finally` block
  swallowed the `return False` from the failure path, so a failed download looked like a completed
  one to its caller
- Fixed a crash (`KeyError: 'product'`) when the order api returns anything other than an order,
  for example an authorization error. Such an order is now reported with the reason and skipped,
  and the rest of the library still downloads
- Fixed the test suite calling `_should_download_file_type`, which was renamed to
  `_should_download_ext`


## 0.4.3

- Added support for asm games ([#75](https://github.com/xtream1101/humblebundle-downloader/pull/75))


## 0.4.2

- Added public docker image
- New version just to make sure the updated ci/cd pipeline is working, no code changes in this release


## 0.4.1

- Fixed crash when missing cli args ([#48](https://github.com/xtream1101/humblebundle-downloader/pull/48))
- Updated the Trove url ([#59](https://github.com/xtream1101/humblebundle-downloader/pull/59))
- Using [pre-commit](https://pre-commit.com/) hooks for formatting and linting
- Moved from setuptools to poetry for packaging


## 0.4.0

- Deprecate the `download` argument. It is no longer needed since that is the only action that can be taken


## 0.3.4

- Merged in [PR 35](https://github.com/xtream1101/humblebundle-downloader/pull/35) to fix some trove games not downloading


## 0.3.3

- Fixed crashing when file is missing on humblebundle
- Updated cookie info in readme
    - Supports passing in the cookie value of `_simpleauth_sess` by using `--session-auth`


## 0.3.1

- Added support for netscape cookies


## 0.3.0

- pip install now requires python version 3.4+
- `--trove` will only download trove products, nothing else
- Filtering flags now work when downloading trove content


## 0.2.2

- Confirm the download is complete by checking the expected size to what downloaded
- Fixed the platform filter


## 0.2.1

- Fixed include & exclude logic being switched in v0.2.0


## 0.2.0

- Added **Humble Trove** support _(`--trove` to also check/download trove content)_
- Now by default only new content is downloaded. Use `--update` to also check for updated content


## 0.1.3

- Fixed re-downloading for real this time
    - Only use the url last modified time as the check for new versions


## 0.1.2

- Stop using md5 & sha1 hashes to check if file is unique (was creating duplicate downloads of the same file)
- Strip periods from end of directory & file names
- Rename older versions of a file before download the new one


## 0.1.1

- Delete failed downloaded files


## 0.1.0

- Filename saved is now the original name of the file
- key used in cache is different due to changing the file name
    - _this may result in duplicate downloads if you have run the older version_
- Support for downloading a single Bundle/Purchase by using the
  flag `-k` or `--key` and getting the key from the url of a purchase


## 0.0.8

- gen-cookies now works with SSO feature and 2FA logins
- Added `--include` & `--exclude` cli args to filter file types


## 0.0.7

- Replace `:` with `-` in filenames
- Ignore items that do not have a web url


## 0.0.6

- Started change log
- Added more detail to readme
- Removed the use of f-strings to support more python versions
- Fixed bug where folders and files were only a single letter
