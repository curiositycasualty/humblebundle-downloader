"""Where a run has got to, kept up from its events for a display to read.

Downloads only ever update counters here, under one short lock, and the
display takes a snapshot when it redraws. That keeps drawing off the
download path entirely: however slow the terminal, a transfer never
waits on it.
"""
import threading
import time
from dataclasses import dataclass, field

from .events import Listener


@dataclass(frozen=True)
class Transfer:
    """One download in flight"""

    name: str
    done: int
    total: object                   # bytes, or None when not known
    rate: object                    # bytes a second, or None

    @property
    def fraction(self):
        return (self.done / self.total) if self.total else None

    @property
    def eta(self):
        if self.total and self.rate:
            return max(0.0, (self.total - self.done) / self.rate)
        return None


@dataclass(frozen=True)
class Snapshot:
    """Everything a redraw needs, copied out at one moment"""

    slots: tuple                    # a Transfer, or None for an idle slot
    done: int
    failed: int
    expected: int
    walk_done: bool
    seen: int                       # bytes, finished files and in flight
    rate: object                    # bytes a second overall, or None
    elapsed: float
    jobs: object                    # downloads at once, when known
    bundle: object                  # the bundle being checked
    bundles: int                    # how many have been checked
    phase: str
    stopping: bool = field(default=False)
    paused: bool = field(default=False)
    skipped: int = field(default=0)

    @property
    def fraction(self):
        if self.walk_done and self.expected:
            return min(1.0, self.done / self.expected)
        return None


class RunState(Listener):
    """A listener that remembers. Safe to call from every thread"""

    shows_transfers = True

    def __init__(self, window=3.0, clock=time.monotonic):
        super().__init__()
        self.window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._files = {}             # name -> {"done", "total", "samples"}
        self._slot_names = []
        self._done = 0
        self._failed = 0
        self._queued = 0
        self._skipped = 0
        self._bytes = 0              # from finished files
        self._walk_done = False
        self._overall = []           # (when, bytes seen)
        self._started = clock()
        self._jobs = None
        self._bundle = None
        self._bundles = 0
        self._phase = "Reading your library"
        self._stopping = False
        self._paused = False
        self._skipped_by_hand = 0

    # -- the run ------------------------------------------------------

    def downloads_started(self, slots):
        with self._lock:
            self._jobs = slots
            # Shown as empty rows until something fills them, so the
            # block does not jump about as the first downloads start
            while len(self._slot_names) < slots:
                self._slot_names.append(None)

    def jobs_changed(self, limit):
        with self._lock:
            self._jobs = limit

    def downloads_finished(self):
        with self._lock:
            if not self._stopping:
                self._phase = "Finishing up"

    def paused(self, paused):
        self._paused = paused

    def stopping(self):
        # Called from a signal handler as well: no lock, just a flag
        self._stopping = True
        self._phase = "Stopping"

    @property
    def stopping_asked(self):
        return self._stopping

    def run_ending(self):
        with self._lock:
            self._phase = "Done"

    def set_phase(self, phase):
        with self._lock:
            self._phase = phase

    # -- the walk -----------------------------------------------------

    def bundle_started(self, title):
        with self._lock:
            self._bundle = title
            self._bundles += 1
            if not self._stopping:
                self._phase = "Checking bundles"

    def file_queued(self):
        with self._lock:
            self._queued += 1

    def file_skipped(self):
        with self._lock:
            self._skipped += 1

    def traversal_finished(self):
        with self._lock:
            self._walk_done = True
            self._bundle = None
            if not self._stopping:
                self._phase = "Downloading"

    # -- each download ------------------------------------------------

    def file_started(self, name, total):
        with self._lock:
            entry = self._files.get(name)
            if entry is None:
                self._files[name] = {"done": 0, "total": total, "samples": []}
            else:
                # Begun again after a resume, at a new size
                entry["total"] = total
                entry["samples"] = []
            if name not in self._slot_names:
                if None in self._slot_names:
                    self._slot_names[self._slot_names.index(None)] = name
                else:
                    self._slot_names.append(name)

    def file_progress(self, name, downloaded):
        # Once per chunk: one dict write. Rates are worked out when a
        # snapshot is taken, not here
        with self._lock:
            entry = self._files.get(name)
            if entry is not None:
                entry["done"] = downloaded

    def file_finished(self, name, ok=True, skipped=False):
        with self._lock:
            entry = self._files.pop(name, None)
            if name in self._slot_names:
                self._slot_names[self._slot_names.index(name)] = None
            if ok:
                self._done += 1
                if entry is not None:
                    self._bytes += entry["done"]
            elif skipped:
                self._skipped_by_hand += 1
            else:
                self._failed += 1

    # -- reading it ---------------------------------------------------

    def snapshot(self):
        now = self._clock()
        with self._lock:
            seen = self._bytes
            transfers = {}
            for name, entry in self._files.items():
                seen += entry["done"]
                samples = entry["samples"]
                samples.append((now, entry["done"]))
                _trim(samples, now, self.window)
                transfers[name] = Transfer(
                    name, entry["done"], entry["total"], _rate(samples)
                )
            self._overall.append((now, seen))
            _trim(self._overall, now, self.window)
            # Slots no longer needed (-j auto went down) are dropped
            # once idle, from the end
            while (self._jobs is not None and len(self._slot_names) > self._jobs
                   and self._slot_names[-1] is None):
                self._slot_names.pop()
            return Snapshot(
                slots=tuple(transfers.get(n) if n else None for n in self._slot_names),
                done=self._done,
                failed=self._failed,
                expected=max(0, self._queued - self._skipped - self._skipped_by_hand),
                walk_done=self._walk_done,
                seen=seen,
                rate=_rate(self._overall),
                elapsed=now - self._started,
                jobs=self._jobs,
                bundle=self._bundle,
                bundles=self._bundles,
                phase="Paused" if self._paused and not self._stopping else self._phase,
                stopping=self._stopping,
                paused=self._paused,
                skipped=self._skipped_by_hand,
            )


def _trim(samples, now, window):
    while len(samples) > 2 and now - samples[0][0] > window:
        samples.pop(0)


def _rate(samples):
    if len(samples) < 2:
        return None
    elapsed = samples[-1][0] - samples[0][0]
    if elapsed <= 0:
        return None
    moved = samples[-1][1] - samples[0][1]
    return moved / elapsed if moved > 0 else 0.0
