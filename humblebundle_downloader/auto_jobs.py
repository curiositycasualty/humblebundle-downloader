"""-j auto: find how many downloads at once suit this connection.

The best number depends on the Wi-Fi, the drive and how Humble's servers
feel that day far more than on the machine, so it is found by trying.
Downloads start four at a time. Every so often the total speed is
measured; while adding two more lifts it by at least a tenth, two more
are added. As soon as a step does not pay, it is taken back and left
alone for a while before trying again. Being throttled halves it.

JobTuner only decides. JobGate is what the workers wait at. The download
code measures, and hands JobTuner the numbers.
"""
import threading

START = 4
MINIMUM = 1
MAXIMUM = 16
STEP = 2
# How much faster in total a step up has to make things to be kept
GAIN = 1.10
# Seconds between measurements, and how many to leave it alone for
# after backing off, before trying a step up again
INTERVAL = 15.0
HOLD = 4


class JobGate:
    """Lets no more than `limit` workers download at once. The limit can
    move while they wait; a lower one takes effect as downloads finish,
    since nothing in flight is stopped to make room
    """

    def __init__(self, limit):
        self._limit = max(MINIMUM, int(limit))
        self._active = 0
        self._cond = threading.Condition()

    @property
    def limit(self):
        return self._limit

    @property
    def active(self):
        return self._active

    def set_limit(self, limit):
        with self._cond:
            self._limit = max(MINIMUM, int(limit))
            self._cond.notify_all()

    def acquire(self, stop=None, poll=0.25):
        """Wait for a free slot. False if stop is set while waiting"""
        with self._cond:
            while self._active >= self._limit:
                if stop is not None and stop.is_set():
                    return False
                self._cond.wait(poll)
            self._active += 1
            return True

    def release(self):
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()


class JobTuner:
    """Hill climbing on total speed, with a quick retreat.

    observe() is given one measurement and returns (limit, reason), with
    reason None when nothing changed
    """

    def __init__(self, start=START, minimum=MINIMUM, maximum=MAXIMUM,
                 step=STEP, gain=GAIN, hold=HOLD):
        self.limit = max(minimum, min(maximum, start))
        self.minimum = minimum
        self.maximum = maximum
        self.step = step
        self.gain = gain
        self.hold_for = hold
        self._hold = 0
        self._probing = False
        self._before = None
        self.best = None                      # (rate, limit)

    def observe(self, rate, busy=True, throttled=False):
        if throttled:
            old = self.limit
            self.limit = max(self.minimum, self.limit // 2)
            self._probing = False
            self._hold = self.hold_for
            if self.limit != old:
                return self.limit, "Humble asked us to slow down"
            return self.limit, None

        if not busy:
            # Not every slot had something to download, so this says
            # nothing about what more at once would do
            return self.limit, None

        if self.best is None or rate > self.best[0]:
            self.best = (rate, self.limit)

        if self._hold:
            self._hold -= 1
            return self.limit, None

        if self._probing:
            if rate >= self._before * self.gain and self.limit < self.maximum:
                self._before = rate
                return self._up("more at once is still faster")
            if rate >= self._before * self.gain:
                self._probing = False
                self._hold = self.hold_for
                return self.limit, None
            self._probing = False
            self._hold = self.hold_for
            self.limit = max(self.minimum, self.limit - self.step)
            return self.limit, "more at once was no faster"

        if self.limit >= self.maximum:
            return self.limit, None
        self._before = rate
        return self._up("trying more at once")

    def _up(self, reason):
        self._probing = True
        self.limit = min(self.maximum, self.limit + self.step)
        return self.limit, reason
