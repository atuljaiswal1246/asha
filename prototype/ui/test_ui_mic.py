"""Guard: the mic survives device changes and is not secretly auto-gained.

The user plugs/unplugs headphones constantly, then reports the mic "sometimes
stops working". A `MediaStreamTrack` is bound to the device that existed when it
was acquired: unplugging a headset ends/mutes it (or macOS moves the default
input) while nothing re-acquires it, so the UI keeps saying "listening" over a
dead input. Separately, the browser's `autoGainControl` defaults to ON and
boosts the mic in quiet conditions ("the mic suddenly got sensitive").

This asserts the served static asset:
  * disables auto gain while keeping echo cancel / noise suppress / mono,
  * re-acquires the mic on `devicechange`, `track.onended` and `track.onmute`,
  * debounces bursts and guards against overlapping attempts,
  * reports a dead mic as OFF (never keeps saying "listening"),
  * leaves the watchdog/threshold logic and sample rate untouched.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STATIC = Path(__file__).resolve().parent / "static"


class MicDeviceChangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.compact = re.sub(r"\s+", "", cls.js)

    def _constraints(self) -> str:
        m = re.search(r"getUserMedia\(\{audio:\{([^}]*)\}\}\)", self.compact)
        self.assertIsNotNone(m, "no getUserMedia({audio:{...}}) constraints found")
        return m.group(1)

    def test_auto_gain_is_disabled(self):
        self.assertIn("autoGainControl:false", self._constraints())
        self.assertNotIn("autoGainControl:true", self.compact)

    def test_other_constraints_are_preserved(self):
        c = self._constraints()
        self.assertIn("echoCancellation:true", c)
        self.assertIn("noiseSuppression:true", c)
        self.assertIn("channelCount:1", c)

    def test_devicechange_is_handled(self):
        self.assertRegex(self.js, r"addEventListener\(\s*['\"]devicechange['\"]")
        self.assertIn("micDeviceChanged", self.js)

    def test_track_end_and_mute_are_handled(self):
        self.assertRegex(self.js, r"micTrack\.onended\s*=\s*\(\)\s*=>\s*micDeviceChanged\(")
        self.assertRegex(self.js, r"micTrack\.onmute\s*=\s*\(\)\s*=>\s*micDeviceChanged\(")

    def test_reacquisition_stops_old_and_rewires_new(self):
        # old graph torn down: source + worklet/node disconnected, tracks stopped
        self.assertIn("micSrc.disconnect()", self.js)
        self.assertIn("micProc.disconnect()", self.js)
        self.assertIn("micStream.getTracks().forEach(t => t.stop())", self.js)
        # fresh stream / graph and audio_toggle state resumed
        self.assertIn("createMediaStreamSource", self.js)
        self.assertIn("createScriptProcessor", self.js)
        self.assertIn("type: 'audio_toggle', enabled: true", self.js)

    def test_debounce_and_overlap_guard(self):
        self.assertRegex(self.js, r"MIC_DEVICE_DEBOUNCE_MS\s*=\s*\d+")
        self.assertIn("clearTimeout(micReacquireTimer)", self.js)
        self.assertIn("manualMicOff || micReacquiring", self.js)
        # the guard is released only when the async attempt settles
        self.assertIn("Promise.resolve(startMic()).finally", self.js)

    def test_dead_mic_is_reported_off_not_listening(self):
        self.assertIn("function _markMicDown", self.js)
        self.assertIn("microphone is OFF", self.js)
        # definition + the denied and transient call sites
        self.assertGreaterEqual(self.js.count("_markMicDown("), 3)
        self.assertIn("'mic blocked'", self.js)
        self.assertIn("mic off", self.js)

    def test_sample_rate_and_threshold_logic_untouched(self):
        self.assertIn("sampleRate: 16000", self.js)
        for const in ("MIC_SILENCE_MS = 2500", "MIC_QUIET_THRESHOLD = 80",
                      "MIC_DIGITAL_SILENCE_MS = 12000", "MIC_MAX_REACQUIRE = 3",
                      "MIC_REACQUIRE_SLOW_MS = 15000"):
            self.assertIn(const, self.js, "mic watchdog constant changed: " + const)
        for fn in ("function micRecovery", "function micWatchdog",
                   "function trackMicEnergy", "function hardResetMic"):
            self.assertIn(fn, self.js)

    def test_mic_button_semantics_unchanged(self):
        self.assertIn("function _toggleMic", self.js)
        self.assertIn("micActive ? stopMic() : startMic()", self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
