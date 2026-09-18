#!/usr/bin/env python3
"""Tests for ttft_report.py — stdlib only, no pytest."""

import os
import tempfile
import unittest

from ttft_report import models_used, parse_log, report, service_of, stats

# A log only supports a verdict if it says which model served it - the
# historical baseline ran a local Qwen, anything else is not comparable.
BASELINE_MODEL = "INFO:asha.server:[LLM] using llama-server (qwen3.5-4b-q4_k_m)\n"

# ---------------------------------------------------------------------------
# Fixture: a synthetic pipecat log with the five required scenarios
# ---------------------------------------------------------------------------

FIXTURE_LOG = """\
INFO:asha.server:[LLM] using llama-server (qwen3.5-4b-q4_k_m)
Generating chat from context [{'role': 'user', 'content': 'Introduce yourself as Jarvis. Your name is Jarvis. Say it in one small friendly spoken sentence.'}]
2026-09-17 17:42:50.900 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - BoundedContextLLM#0 TTFB: 1.871s
2026-09-17 17:42:53.166 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfat_metrics:269 - BoundedContextLLM#0 TTFAT: 2.268s (0.397s thinking)
2026-09-17 17:42:53.349 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:start_llm_usage_metrics:335 - BoundedContextLLM#0 prompt tokens: 5864, completion tokens: 20
2026-09-17 17:42:53.350 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_processing_metrics:308 - BoundedContextLLM#0 processing time: 2.452s
2026-09-17 17:42:55.469 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - KokoroTTSService#0 TTFB: 2.118s
2026-09-17 17:42:55.473 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:process_ttfa_metrics:228 - KokoroTTSService#0 TTFA: 2.165s
2026-09-17 17:42:55.474 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_processing_metrics:308 - KokoroTTSService#0 processing time: 2.122s
2026-09-17 17:43:00.077 | DEBUG    | pipecat.transports.base_output:_bot_stopped_speaking:758 - Bot stopped speaking
INFO:asha.server:[STATUS] ready emitted (greeting finished)
2026-09-17 17:45:00.100 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - BoundedContextLLM#0 TTFB: 0.480s
2026-09-17 17:45:00.500 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfat_metrics:269 - BoundedContextLLM#0 TTFAT: 0.620s (0.140s thinking)
2026-09-17 17:45:00.600 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:start_llm_usage_metrics:335 - BoundedContextLLM#0 prompt tokens: 120, completion tokens: 35, cache read input tokens: 80, reasoning tokens: 12
2026-09-17 17:45:02.200 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - KokoroTTSService#0 TTFB: 0.310s
2026-09-17 17:45:02.300 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:process_ttfa_metrics:228 - KokoroTTSService#0 TTFA: 0.350s
2026-09-17 17:46:00.100 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - BoundedContextLLM#0 TTFB: 0.520s
2026-09-17 17:46:00.600 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfat_metrics:269 - BoundedContextLLM#0 TTFAT: 0.830s (0.310s thinking)
2026-09-17 17:46:00.700 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:start_llm_usage_metrics:335 - BoundedContextLLM#0 prompt tokens: 150, completion tokens: 42
2026-09-17 17:46:02.400 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - KokoroTTSService#0 TTFB: 0.280s
2026-09-17 17:46:02.500 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:process_ttfa_metrics:228 - KokoroTTSService#0 TTFA: 0.310s
2026-09-17 17:47:00.100 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - BoundedContextLLM#0 TTFB: 1.200s
2026-09-17 17:47:00.800 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfat_metrics:269 - BoundedContextLLM#0 TTFAT: 1.600s (0.400s thinking)
2026-09-17 17:47:00.900 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:start_llm_usage_metrics:335 - BoundedContextLLM#0 prompt tokens: 200, completion tokens: 50
2026-09-17 17:47:02.100 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - KokoroTTSService#0 TTFB: 0.290s
2026-09-17 17:47:02.200 | DEBUG    | pipecat.processors.metrics.frame_processor_metrics:process_ttfa_metrics:228 - KokoroTTSService#0 TTFA: 0.330s
"""


def _write_fixture(content: str = FIXTURE_LOG) -> str:
    """Write fixture to a temp file, return path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False)
    f.write(content)
    f.close()
    return f.name


class TestParseLog(unittest.TestCase):
    """Test parse_log extracts records from fixture log."""

    def setUp(self):
        self.path = _write_fixture()
        self.records = parse_log(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_finds_all_llm_turns(self):
        llm_recs = [r for r in self.records if "LLM" in r["processor"]]
        # 1 greeting + 3 normal turns = 4 LLM records
        self.assertEqual(len(llm_recs), 4)

    def test_finds_all_tts_turns(self):
        tts_recs = [r for r in self.records if "Kokoro" in r["processor"]]
        # 1 greeting + 3 normal turns = 4 TTS records
        self.assertEqual(len(tts_recs), 4)

    def test_total_records(self):
        self.assertEqual(len(self.records), 8)

    def test_greeting_detected(self):
        greetings = [r for r in self.records if r["is_greeting"]]
        # 1 LLM + 1 TTS greeting
        self.assertEqual(len(greetings), 2)

    def test_normal_turn_detected(self):
        normals = [r for r in self.records if not r["is_greeting"]]
        llm_normals = [r for r in normals if "LLM" in r["processor"]]
        self.assertEqual(len(llm_normals), 3)

    def test_thinking_component_parsed(self):
        turn_with_thinking = [r for r in self.records if r["thinking_s"] is not None]
        self.assertGreaterEqual(len(turn_with_thinking), 1)

    def test_tokens_parsed(self):
        llm_recs = [r for r in self.records if "LLM" in r["processor"]]
        for rec in llm_recs:
            self.assertGreater(rec["tokens_in"], 0)

    def test_cache_read_parsed(self):
        normals = [r for r in self.records if not r["is_greeting"]]
        cache_reads = [r["cache_read"] for r in normals if r["cache_read"] > 0]
        self.assertGreater(len(cache_reads), 0)

    def test_turn_window_groups_metrics(self):
        """TTFB, TTFAT, usage for same processor within 5s should merge."""
        llm_greeting = [r for r in self.records if r["is_greeting"] and "LLM" in r["processor"]][0]
        self.assertIsNotNone(llm_greeting["ttfb_s"])
        self.assertIsNotNone(llm_greeting["ttft_s"])
        self.assertGreater(llm_greeting["tokens_in"], 0)


class TestStats(unittest.TestCase):
    """Test stats computes correct aggregates."""

    def test_stats_nonempty(self):
        path = _write_fixture()
        records = parse_log(path)
        os.unlink(path)
        s = stats(records)
        self.assertIn("ttfb_s", s)
        self.assertEqual(s["ttfb_s"]["count"], 8)

    def test_stats_empty(self):
        s = stats([])
        for key in ["ttfb_s", "ttft_s", "ttfa_s"]:
            self.assertEqual(s[key]["count"], 0)
            self.assertIsNone(s[key]["median"])


class TestReport(unittest.TestCase):
    """Test report output format and baseline comparison."""

    def setUp(self):
        self.path = _write_fixture()

    def tearDown(self):
        os.unlink(self.path)

    def test_report_contains_baseline(self):
        r = report(self.path)
        self.assertIn("559", r)

    def test_report_contains_verdict(self):
        r = report(self.path)
        self.assertIn("Verdict:", r)

    def test_report_turns_only_excludes_greeting(self):
        r = report(self.path, turns_only=True)
        self.assertIn("Excluded (greeting): 2", r)

    def test_turns_only_report_table(self):
        r = report(self.path, turns_only=True)
        self.assertIn("Turns: 6", r)
        self.assertIn("Note: 2 greeting record(s) excluded", r)


class TestRegressionVerdict(unittest.TestCase):
    """Test the verdict logic with minimal fixtures."""

    def test_exactly_0_5s_regression(self):
        log = (
            "2026-09-17 18:00:00.000 | DEBUG    | "
            "pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - "
            "BoundedContextLLM#0 TTFB: 1.059s\n"
            + BASELINE_MODEL
        )
        path = _write_fixture(log)
        r = report(path)
        os.unlink(path)
        self.assertIn("REGRESSION", r)

    def test_under_threshold_not_regression(self):
        log = (
            "2026-09-17 18:00:00.000 | DEBUG    | "
            "pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - "
            "BoundedContextLLM#0 TTFB: 1.049s\n"
            + BASELINE_MODEL
        )
        path = _write_fixture(log)
        r = report(path)
        os.unlink(path)
        self.assertIn("within budget", r)

    def test_below_baseline(self):
        log = (
            "2026-09-17 18:00:00.000 | DEBUG    | "
            "pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - "
            "BoundedContextLLM#0 TTFB: 0.400s\n"
            + BASELINE_MODEL
        )
        path = _write_fixture(log)
        r = report(path)
        os.unlink(path)
        self.assertIn("faster than baseline", r)


class TestRegressionFromFixture(unittest.TestCase):
    """Test regression detection using the full fixture with a regression turn."""

    def test_regression_turn_in_fixture(self):
        """The fixture has a turn at 1.2s. With --turns-only, the LLM median
        of [480, 520, 1200] = 520ms, within budget. The full fixture median of
        [480, 520, 1200, 1871] = 860ms, also within budget. So we test the
        regression turn in isolation."""
        log = (
            BASELINE_MODEL
            + "Generating chat from context [{'role': 'user', 'content': 'Hi'}]\n"
            "2026-09-17 18:00:00.000 | DEBUG    | "
            "pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - "
            "BoundedContextLLM#0 TTFB: 1.871s\n"
            "2026-09-17 18:00:05.000 | DEBUG    | "
            "pipecat.transports.base_output:_bot_stopped_speaking:758 - Bot stopped speaking\n"
            "INFO:asha.server:[STATUS] ready emitted (greeting finished)\n"
            "2026-09-17 18:01:00.000 | DEBUG    | "
            "pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - "
            "BoundedContextLLM#0 TTFB: 1.200s\n"
            "2026-09-17 18:01:00.500 | DEBUG    | "
            "pipecat.processors.metrics.frame_processor_metrics:stop_ttfat_metrics:269 - "
            "BoundedContextLLM#0 TTFAT: 1.600s (0.400s thinking)\n"
        )
        path = _write_fixture(log)
        r = report(path, turns_only=True)
        os.unlink(path)
        # Single non-greeting turn at 1.2s → median 1200ms → REGRESSION
        self.assertIn("REGRESSION", r)


class TestTurnsOnlyGreetingOnly(unittest.TestCase):
    """Test --turns-only with only greeting turns."""

    def test_no_data_when_all_greetings(self):
        log = (
            "Generating chat from context [{'role': 'user', 'content': 'Introduce yourself as Jarvis'}]\n"
            "2026-09-17 18:00:00.000 | DEBUG    | "
            "pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - "
            "BoundedContextLLM#0 TTFB: 1.000s\n"
            "2026-09-17 18:00:05.000 | DEBUG    | "
            "pipecat.transports.base_output:_bot_stopped_speaking:758 - Bot stopped speaking\n"
            "INFO:asha.server:[STATUS] ready emitted (greeting finished)\n"
        )
        path = _write_fixture(log)
        r = report(path, turns_only=True)
        os.unlink(path)
        self.assertIn("NO DATA", r)
        self.assertIn("Excluded (greeting): 1", r)


class TestModelComparability(unittest.TestCase):
    """A baseline measured on another model cannot judge this model."""

    def test_different_model_withholds_the_verdict(self):
        log = (
            "INFO:asha.server:[LLM] using OpenCode-Go-deepseek-v4.1-flash (deepseek-v4.1-flash)\n"
            "2026-09-17 19:13:30.157 | DEBUG    | "
            "pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - "
            "BoundedContextLLM#0 TTFB: 1.560s\n"
        )
        path = _write_fixture(log)
        r = report(path)
        os.unlink(path)
        self.assertIn("NOT COMPARABLE", r)
        self.assertNotIn("REGRESSION", r)
        self.assertIn("1560 ms", r)

    def test_force_flag_restores_the_comparison(self):
        log = (
            "INFO:asha.server:[LLM] using OpenCode-Go-deepseek-v4.1-flash (deepseek-v4.1-flash)\n"
            "2026-09-17 19:13:30.157 | DEBUG    | "
            "pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - "
            "BoundedContextLLM#0 TTFB: 1.560s\n"
        )
        path = _write_fixture(log)
        r = report(path, assume_comparable=True)
        os.unlink(path)
        self.assertIn("REGRESSION", r)


class TestAttributionOnRealLogShape(unittest.TestCase):
    """The two bugs found against the real log on 2026-09-17.

    1. Every later turn's logged context still contains the greeting prompt, so
       matching that prompt marked real turns as greeting and dropped their LLM
       metrics (the report then said 'NO DATA').
    2. STT first-byte was counted as the LLM's, which hid a 1.5s LLM latency
       behind 0.6s transcripts.
    """

    def _llm(self, ts, ttfb, ttft=None, thinking=None):
        out = (f"2026-09-17 {ts} | DEBUG    | "
               f"pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - "
               f"BoundedContextLLM#0 TTFB: {ttfb}s\n")
        if ttft:
            out += (f"2026-09-17 {ts} | DEBUG    | "
                    f"pipecat.processors.metrics.frame_processor_metrics:stop_ttfat_metrics:269 - "
                    f"BoundedContextLLM#0 TTFAT: {ttft}s ({thinking}s thinking)\n")
        return out

    def _stt(self, ts, ttfb):
        return (f"2026-09-17 {ts} | DEBUG    | "
                f"pipecat.processors.metrics.frame_processor_metrics:stop_ttfb_metrics:175 - "
                f"MoonshineSTTService#0 TTFB: {ttfb}s\n")

    def test_later_turn_whose_context_contains_the_greeting_is_a_turn(self):
        log = (
            "Generating chat from context [{'role': 'user', 'content': 'Introduce yourself as Jarvis. "
            "Your name is Jarvis.'}]\n"
            + self._llm("19:13:30.157", "2.047")
            + "INFO:asha.server:[STATUS] ready emitted (greeting finished)\n"
            + "Generating chat from context [{'role': 'user', 'content': 'Introduce yourself as Jarvis. "
            "Your name is Jarvis.'}, {'role': 'assistant', 'content': 'Hey, I am Jarvis.'}, "
            "{'role': 'user', 'content': 'Hello Jarvis'}]\n"
            + self._llm("19:13:54.538", "1.350", "1.758", "0.408")
        )
        path = _write_fixture(log)
        records = parse_log(path)
        os.unlink(path)
        turns = [r for r in records if not r["is_greeting"]]
        self.assertEqual(len(turns), 1, "the real turn must not be swallowed as greeting")
        self.assertEqual(turns[0]["ttfb_s"], 1.350)

    def test_stt_first_byte_never_decides_the_verdict(self):
        log = (
            "INFO:asha.server:[STATUS] ready emitted (greeting finished)\n"
            + BASELINE_MODEL
            + self._stt("19:13:42.719", "0.669")
            + self._llm("19:13:44.424", "1.684", "2.051", "0.367")
        )
        path = _write_fixture(log)
        r = report(path, turns_only=True)
        os.unlink(path)
        self.assertIn("REGRESSION", r)
        self.assertIn("Measured median LLM TTFB: 1684 ms", r)
        self.assertIn("STT first-byte 669 ms", r)
        self.assertIn("STT", r)

    def test_cache_read_and_reasoning_parsed_from_real_format(self):
        log = (
            "INFO:asha.server:[STATUS] ready emitted (greeting finished)\n"
            "2026-09-17 19:13:44.878 | DEBUG    | "
            "pipecat.processors.metrics.frame_processor_metrics:start_llm_usage_metrics:335 - "
            "BoundedContextLLM#0 prompt tokens: 5867, completion tokens: 43, "
            "cache read input tokens: 5632, reasoning tokens: 23\n"
        )
        path = _write_fixture(log)
        records = parse_log(path)
        os.unlink(path)
        rec = [r for r in records if r["service"] == "LLM"][0]
        self.assertEqual(rec["cache_read"], 5632)
        self.assertEqual(rec["reasoning_tokens"], 23)

    def test_service_classification(self):
        self.assertEqual(service_of("BoundedContextLLM#0"), "LLM")
        self.assertEqual(service_of("MoonshineSTTService#0"), "STT")
        self.assertEqual(service_of("KokoroTTSService#0"), "TTS")
        self.assertEqual(service_of("SomeOther#0"), "other")


if __name__ == "__main__":
    unittest.main()
