# SPDX-License-Identifier: MIT
"""Background frame polling over capture.screenshot (no UI dependency).

    poller = FramePoller(rmm, rate=10, step=1)
    poller.start()
    f = poller.latest            # newest Frame or None; poller.fps is the measured rate
    poller.enabled = False       # stop asking (the last frame stays); True to resume
    poller.rate = 2.5            # frames per second asked for, 0.2..30
    poller.grab()                # one frame now, polling on or off
    poller.stop()

capture.screenshot never pauses the guest; each poll is one request on the shared connection.
"""

import threading
import time

from .client import Frame
from .rpc import RpcError

MIN_RATE, MAX_RATE = 0.2, 30.0


class FramePoller:
    def __init__(self, rmm, rate=10.0, step=1, enabled=True, on_frame=None):
        self.rmm = rmm
        self._rate = float(rate)
        self.step = step
        self.enabled = enabled
        self.on_frame = on_frame          # callback(Frame), on the poller thread
        self.latest = None
        self.fps = 0.0
        self.error = None
        self.frames = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._times = []

    @property
    def rate(self):
        return self._rate

    @rate.setter
    def rate(self, v):
        self._rate = max(MIN_RATE, min(MAX_RATE, float(v)))
        self._wake.set()

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="rmm-frames", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()

    def grab(self):
        """One frame now, whatever `enabled` says."""
        f = Frame(self.rmm.call("capture.screenshot", step=self.step, wait_ms=100))
        self._took(f)
        return f

    def _took(self, f):
        self.latest = f
        self.frames += 1
        self.error = None
        now = time.time()
        self._times = [t for t in self._times if now - t < 2.0] + [now]
        self.fps = (len(self._times) - 1) / (self._times[-1] - self._times[0]) if len(self._times) > 1 else 0.0
        if self.on_frame:
            self.on_frame(f)

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            if self.enabled and self.rmm.connected:
                try:
                    self.grab()
                except (RpcError, OSError) as e:
                    self.error = str(e)
            else:
                self._times = []
                self.fps = 0.0
            left = 1.0 / self._rate - (time.time() - t0)
            self._wake.clear()
            if left > 0:
                self._wake.wait(left)
