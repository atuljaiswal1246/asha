"""Tests for the MoneyPrinterTurbo client (mpt_video_tool.py).

Zero network, zero real renders: every request goes through
``httpx.MockTransport``. ``_GENERATED_DIR``/``_JOBS_PATH`` are redirected to a
temp dir so no video or state is written into the repo.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

import mpt_video_tool as mpt  # noqa: E402


def ok(task_id="T1"):
    return httpx.Response(200, json={"status": 200, "data": {"task_id": task_id}})


def task(state=4, progress=0, **extra):
    data = {"state": state, "progress": progress}
    data.update(extra)
    return httpx.Response(200, json={"status": 200, "data": data})


class Recorder:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"status": 404})
        return handler(request)

    @property
    def last(self):
        return self.requests[-1]


_ENV_KEYS = (
    "MONEYPRINTER_BASE_URL",
    "MONEYPRINTER_API_KEY",
    "MONEYPRINTER_HOME",
    "MONEYPRINTER_CONFIG",
)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._gen = mpt._GENERATED_DIR
        self._jobs = mpt._JOBS_PATH
        mpt._GENERATED_DIR = self.tmp
        mpt._JOBS_PATH = os.path.join(self.tmp, ".mpt_jobs.json")
        self._env = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["MONEYPRINTER_BASE_URL"] = "http://127.0.0.1:8080"
        os.environ["MONEYPRINTER_API_KEY"] = "test-key"
        # Hermetic: never read the real machine's MPT config in a test. An empty
        # temp home means "no config" unless a test writes one.
        self.home = os.path.join(self.tmp, "mpt-home")
        os.makedirs(self.home, exist_ok=True)
        os.environ["MONEYPRINTER_HOME"] = self.home
        os.environ.pop("MONEYPRINTER_CONFIG", None)

    def tearDown(self):
        mpt._GENERATED_DIR = self._gen
        mpt._JOBS_PATH = self._jobs
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, body, name="config.toml"):
        path = os.path.join(self.home, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def add_footage(self, *names, data=b"x"):
        """Create files in MPT's local footage folder under the temp home."""
        footage = os.path.join(self.home, "storage", "local_videos")
        os.makedirs(footage, exist_ok=True)
        for name in names:
            with open(os.path.join(footage, name), "wb") as f:
                f.write(data)

    def run_(self, coro):
        return asyncio.run(coro)


class HealthTests(Base):
    def test_healthy_true(self):
        rec = Recorder({"/ping": lambda r: httpx.Response(200, text="pong")})
        self.assertTrue(self.run_(mpt.healthy(transport=httpx.MockTransport(rec))))

    def test_healthy_false_when_down(self):
        def boom(request):
            raise httpx.ConnectError("refused")

        self.assertFalse(self.run_(mpt.healthy(transport=httpx.MockTransport(boom))))


class KeyResolutionTests(Base):
    """Auto-discovery: env wins, else MPT's own config.toml, else no auth."""

    def test_env_key_wins_over_config(self):
        self.write_config('[app]\napi_key = "config-secret"\n')
        os.environ["MONEYPRINTER_API_KEY"] = "env-secret"
        self.assertEqual(mpt.api_key(), "env-secret")
        self.assertEqual(mpt._headers()["x-api-key"], "env-secret")

    def test_env_unset_discovers_key_from_config(self):
        self.write_config('[app]\napi_key = "config-secret"\n')
        os.environ.pop("MONEYPRINTER_API_KEY", None)
        self.assertEqual(mpt.api_key(), "config-secret")
        self.assertEqual(mpt._headers()["x-api-key"], "config-secret")

    def test_both_absent_means_no_auth_and_no_header(self):
        os.environ.pop("MONEYPRINTER_API_KEY", None)
        self.assertEqual(mpt.api_key(), "")
        self.assertNotIn("x-api-key", mpt._headers())

    def test_blank_env_falls_back_to_config(self):
        self.write_config('[app]\napi_key = "config-secret"\n')
        os.environ["MONEYPRINTER_API_KEY"] = "   "
        self.assertEqual(mpt.api_key(), "config-secret")

    def test_missing_config_is_safe(self):
        os.environ.pop("MONEYPRINTER_API_KEY", None)
        self.assertEqual(mpt.api_key(), "")

    def test_malformed_config_is_safe(self):
        self.write_config("this is not = = toml\n[app\n")
        os.environ.pop("MONEYPRINTER_API_KEY", None)
        self.assertEqual(mpt.api_key(), "")

    def test_config_without_app_section_is_safe(self):
        self.write_config('[other]\nvalue = 1\n')
        os.environ.pop("MONEYPRINTER_API_KEY", None)
        self.assertEqual(mpt.api_key(), "")

    def test_config_with_empty_or_wrong_typed_key_is_safe(self):
        self.write_config('[app]\napi_key = ""\n')
        os.environ.pop("MONEYPRINTER_API_KEY", None)
        self.assertEqual(mpt.api_key(), "")
        self.write_config('[app]\napi_key = 12345\n')
        self.assertEqual(mpt.api_key(), "")
        self.write_config('[app]\napi_key = ["a", "b"]\n')
        self.assertEqual(mpt.api_key(), "")

    def test_explicit_config_path_override(self):
        path = self.write_config('[app]\napi_key = "explicit-secret"\n',
                                 name="elsewhere.toml")
        os.environ.pop("MONEYPRINTER_API_KEY", None)
        os.environ["MONEYPRINTER_CONFIG"] = path
        self.assertEqual(mpt.api_key(), "explicit-secret")

    def test_discovered_key_is_sent_on_requests(self):
        self.write_config('[app]\napi_key = "config-secret"\n')
        os.environ.pop("MONEYPRINTER_API_KEY", None)
        rec = Recorder({"/api/v1/videos": lambda r: ok("T1")})
        self.run_(mpt.spawn("ocean", terms="ocean",
                            transport=httpx.MockTransport(rec)))
        self.assertEqual(rec.last.headers["x-api-key"], "config-secret")


class SpawnTests(Base):
    def test_happy_path_sends_script_and_terms(self):
        rec = Recorder({"/api/v1/videos": lambda r: ok("T1")})
        t = self.run_(mpt.spawn(
            "volcanoes", script="Volcanoes shaped the islands.",
            terms="volcano,lava", transport=httpx.MockTransport(rec)))
        self.assertEqual(t, "T1")
        import json

        body = json.loads(rec.last.content.decode())
        self.assertEqual(body["video_subject"], "volcanoes")
        self.assertEqual(body["video_script"], "Volcanoes shaped the islands.")
        self.assertEqual(body["video_terms"], "volcano,lava")
        self.assertEqual(body["video_source"], "pexels")
        self.assertEqual(rec.last.headers["x-api-key"], "test-key")

    def test_terms_derived_when_missing(self):
        rec = Recorder({"/api/v1/videos": lambda r: ok("T1")})
        self.run_(mpt.spawn("space exploration", terms="",
                            transport=httpx.MockTransport(rec)))
        self.assertIn("space", rec.last.content.decode())

    def test_empty_topic_rejected(self):
        with self.assertRaises(mpt.MptError):
            self.run_(mpt.spawn("", transport=httpx.MockTransport(ok)))

    def test_401_bad_key(self):
        rec = Recorder({"/api/v1/videos": lambda r: httpx.Response(401, json={})})
        with self.assertRaises(mpt.MptError) as cm:
            self.run_(mpt.spawn("x", transport=httpx.MockTransport(rec)))
        self.assertEqual(str(cm.exception), mpt.KEY_REJECTED_MSG)
        self.assertIn("rejected the key", str(cm.exception))

    def test_403_forbidden(self):
        rec = Recorder({"/api/v1/videos": lambda r: httpx.Response(403, json={})})
        with self.assertRaises(mpt.MptError) as cm:
            self.run_(mpt.spawn("x", transport=httpx.MockTransport(rec)))
        self.assertEqual(str(cm.exception), mpt.KEY_REJECTED_MSG)

    def test_429_rate_limited(self):
        rec = Recorder({"/api/v1/videos": lambda r: httpx.Response(429, json={})})
        with self.assertRaises(mpt.MptError) as cm:
            self.run_(mpt.spawn("x", transport=httpx.MockTransport(rec)))
        self.assertEqual(str(cm.exception), mpt.BUSY_MSG)
        self.assertIn("queue is full", str(cm.exception))


class StatusDownloadTests(Base):
    def test_status_parses_envelope(self):
        rec = Recorder({"/api/v1/tasks/T1": lambda r: task(4, 50)})
        st = self.run_(mpt.status("T1", transport=httpx.MockTransport(rec)))
        self.assertEqual(st["state"], 4)
        self.assertEqual(st["progress"], 50)

    def test_download_saves_file(self):
        rec = Recorder({"/api/v1/download/T1/final-1.mp4":
                        lambda r: httpx.Response(200, content=b"MP4DATA")})
        path = self.run_(mpt.download("T1", transport=httpx.MockTransport(rec)))
        self.assertEqual(path, "/generated/mpt-T1.mp4")
        with open(os.path.join(self.tmp, "mpt-T1.mp4"), "rb") as f:
            self.assertEqual(f.read(), b"MP4DATA")

    def test_wait_happy_path_polls_then_downloads(self):
        calls = {"n": 0}

        def st(request):
            calls["n"] += 1
            return task(4, 10) if calls["n"] == 1 else task(1, 100)

        rec = Recorder({
            "/api/v1/tasks/T1": st,
            "/api/v1/download/T1/final-1.mp4":
                lambda r: httpx.Response(200, content=b"MP4DATA"),
        })
        path = self.run_(mpt.wait_for_video(
            "T1", topic="x", timeout=5, interval=0.01,
            transport=httpx.MockTransport(rec)))
        self.assertEqual(path, "/generated/mpt-T1.mp4")
        self.assertGreaterEqual(calls["n"], 2)

    def test_render_failed_is_a_speakable_sentence(self):
        rec = Recorder({"/api/v1/tasks/T1":
                        lambda r: task(-1, failed_stage="video",
                                       error="Traceback (most recent call last):")})
        with self.assertRaises(mpt.MptError) as cm:
            self.run_(mpt.wait_for_video(
                "T1", timeout=5, interval=0.01,
                transport=httpx.MockTransport(rec)))
        msg = str(cm.exception)
        self.assertEqual(msg, mpt.RENDER_FAILED_MSG.format(stage="video"))
        self.assertNotIn("Traceback", msg)
        self.assertNotIn("\n", msg)

    def test_render_failed_with_odd_stage_is_sanitized(self):
        rec = Recorder({"/api/v1/tasks/T1":
                        lambda r: task(-1, failed_stage="bad stage!!" * 20)})
        with self.assertRaises(mpt.MptError) as cm:
            self.run_(mpt.wait_for_video(
                "T1", timeout=5, interval=0.01,
                transport=httpx.MockTransport(rec)))
        self.assertEqual(str(cm.exception),
                         mpt.RENDER_FAILED_MSG.format(stage="render"))

    def test_timeout_raises_and_says_so(self):
        rec = Recorder({"/api/v1/tasks/T1": lambda r: task(4, 30)})
        with self.assertRaises(mpt.MptError) as cm:
            self.run_(mpt.wait_for_video(
                "T1", timeout=0.05, interval=0.01,
                transport=httpx.MockTransport(rec)))
        self.assertIn("taking longer than", str(cm.exception))
        self.assertIn("stopped waiting", str(cm.exception))


class MakeVideoTests(Base):
    def test_not_installed_is_graceful(self):
        def boom(request):
            raise httpx.ConnectError("refused")

        out = self.run_(mpt.make_video(
            "space", transport=httpx.MockTransport(boom)))
        self.assertEqual(out, mpt.NOT_INSTALLED_MSG)
        self.assertNotIn("[video error]", out)

    def test_empty_topic_is_actionable(self):
        out = self.run_(mpt.make_video("", transport=httpx.MockTransport(ok)))
        self.assertIn("about", out)

    def test_happy_path_returns_ack_then_watcher_lands(self):
        rec = Recorder({
            "/ping": lambda r: httpx.Response(200, text="pong"),
            "/api/v1/videos": lambda r: ok("T2"),
            "/api/v1/tasks/T2": lambda r: task(1, 100),
            "/api/v1/download/T2/final-1.mp4":
                lambda r: httpx.Response(200, content=b"MP4DATA"),
        })
        transport = httpx.MockTransport(rec)

        async def scenario():
            seen = []

            async def on_done(okay, detail, topic):
                seen.append((okay, detail, topic))

            ack = await mpt.make_video("ocean", transport=transport,
                                       on_done=on_done, timeout=5, interval=0.01)
            self.assertEqual(ack, mpt.ACK_MSG)
            for _ in range(200):
                if seen:
                    break
                await asyncio.sleep(0.01)
            return seen, mpt.pending_jobs()

        seen, pending = self.run_(scenario())
        self.assertTrue(seen, "watcher never called on_done")
        self.assertTrue(seen[0][0])
        self.assertEqual(seen[0][1], "/generated/mpt-T2.mp4")
        self.assertEqual(pending, [])

    def test_spawn_401_surfaces_short_speakable_error(self):
        rec = Recorder({
            "/ping": lambda r: httpx.Response(200, text="pong"),
            "/api/v1/videos": lambda r: httpx.Response(401, json={}),
        })
        out = self.run_(mpt.make_video("x", transport=httpx.MockTransport(rec)))
        self.assertTrue(out.startswith("[video error]"))
        self.assertIn(mpt.KEY_REJECTED_MSG, out)
        self.assertNotIn("Traceback", out)

    def test_not_installed_message_has_no_error_prefix(self):
        self.assertNotIn("[video error]", mpt.NOT_INSTALLED_MSG)
        self.assertIn("optional add-on", mpt.NOT_INSTALLED_MSG)


class PersistenceTests(Base):
    def test_register_pending_clear(self):
        mpt.register_job("T9", "topic nine")
        rows = mpt.pending_jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "T9")
        self.assertTrue(os.path.exists(mpt._JOBS_PATH))
        mpt.clear_job("T9")
        self.assertEqual(mpt.pending_jobs(), [])


class ToolExposureTests(Base):
    def test_voice_tier_exposes_make_video(self):
        import server

        names = [s.name for s in server._voice_tool_schemas()]
        self.assertIn("make_video", names)

    def test_agent_tier_exposes_make_video(self):
        import agent_loop

        names = [t["function"]["name"] for t in agent_loop.TOOLS]
        self.assertIn("make_video", names)
        tool = next(t for t in agent_loop.TOOLS
                    if t["function"]["name"] == "make_video")
        self.assertEqual(tool["function"]["parameters"]["required"], ["topic"])


class FootageConfigTests(Base):
    """Detecting a configured footage source from MPT's own config."""

    def test_pexels_key_present_is_available(self):
        self.write_config('[app]\npexels_api_keys = ["pk-1"]\n')
        self.assertTrue(mpt.footage_available("pexels"))

    def test_single_string_key_is_accepted(self):
        self.write_config('[app]\npexels_api_keys = "pk-1"\n')
        self.assertTrue(mpt.footage_available("pexels"))

    def test_empty_key_list_is_not_available(self):
        self.write_config('[app]\npexels_api_keys = []\n')
        self.assertFalse(mpt.footage_available("pexels"))

    def test_blank_string_key_is_not_available(self):
        self.write_config('[app]\npexels_api_keys = "  "\n')
        self.assertFalse(mpt.footage_available("pexels"))

    def test_missing_footage_field_is_not_available(self):
        self.write_config('[app]\napi_key = "k"\n')
        self.assertFalse(mpt.footage_available("pexels"))

    def test_no_config_fails_open(self):
        self.assertTrue(mpt.footage_available("pexels"))

    def test_malformed_config_fails_open(self):
        self.write_config("this is not = = toml\n[app\n")
        self.assertTrue(mpt.footage_available("pexels"))

    def test_unknown_source_fails_open(self):
        self.write_config('[app]\npexels_api_keys = []\n')
        self.assertTrue(mpt.footage_available("somewhere_else"))

    def test_local_available_only_when_clips_exist(self):
        self.assertFalse(mpt.footage_available("local"))
        self.add_footage("clip.mp4")
        self.assertTrue(mpt.footage_available("local"))


class LocalFootageTests(Base):
    """The opt-in local-footage folder is read safely and non-recursively."""

    def test_lists_supported_clips_only_sorted(self):
        self.add_footage("b.mov", "a.mp4", "notes.txt", ".hidden.mp4")
        self.assertEqual(mpt.local_footage_files(), ["a.mp4", "b.mov"])

    def test_missing_folder_is_empty(self):
        self.assertEqual(mpt.local_footage_files(), [])

    def test_resolve_source_defaults_to_pexels(self):
        self.assertEqual(mpt.resolve_source(), "pexels")

    def test_resolve_source_honours_config_local(self):
        self.write_config('[app]\nvideo_source = "local"\n')
        self.assertEqual(mpt.resolve_source(), "local")

    def test_resolve_source_ignores_other_configured_sources(self):
        self.write_config('[app]\nvideo_source = "pixabay"\n')
        self.assertEqual(mpt.resolve_source(), "pexels")

    def test_explicit_source_wins_and_is_lowercased(self):
        self.write_config('[app]\nvideo_source = "local"\n')
        self.assertEqual(mpt.resolve_source("pexels"), "pexels")
        self.assertEqual(mpt.resolve_source("LOCAL"), "local")


class FootageDetectionTests(Base):
    """MPT reachable but no footage source: honest speech, no silent spawn."""

    def test_missing_pexels_key_returns_honest_sentence(self):
        self.write_config('[app]\npexels_api_keys = []\n')
        rec = Recorder({
            "/ping": lambda r: httpx.Response(200, text="pong"),
            "/api/v1/videos": lambda r: ok("T1"),
        })
        out = self.run_(mpt.make_video(
            "ocean", transport=httpx.MockTransport(rec)))
        self.assertEqual(out, mpt.NO_FOOTAGE_MSG)
        self.assertFalse(out.startswith("[video error]"))
        self.assertNotIn("/api/v1/videos", [r.url.path for r in rec.requests])

    def test_missing_pexels_key_does_not_leak_raw_error(self):
        self.write_config('[app]\npexels_api_keys = []\n')
        rec = Recorder({"/ping": lambda r: httpx.Response(200, text="pong")})
        out = self.run_(mpt.make_video(
            "ocean", transport=httpx.MockTransport(rec)))
        self.assertNotIn("Traceback", out)
        self.assertNotIn("\n", out)
        self.assertIn("Pexels", out)

    def test_local_opt_in_spawns_with_materials(self):
        self.write_config('[app]\nvideo_source = "local"\n')
        self.add_footage("clip.mp4")
        rec = Recorder({
            "/ping": lambda r: httpx.Response(200, text="pong"),
            "/api/v1/videos": lambda r: ok("T3"),
        })
        out = self.run_(mpt.make_video(
            "ocean", transport=httpx.MockTransport(rec)))
        self.assertEqual(out, mpt.ACK_MSG)
        body = json.loads(rec.last.content.decode())
        self.assertEqual(body["video_source"], "local")
        self.assertEqual(
            body["video_materials"],
            [{"provider": "local", "url": "clip.mp4", "duration": 0}],
        )

    def test_local_chosen_but_folder_empty_is_honest(self):
        rec = Recorder({"/ping": lambda r: httpx.Response(200, text="pong")})
        out = self.run_(mpt.make_video(
            "ocean", source="local", transport=httpx.MockTransport(rec)))
        self.assertEqual(out, mpt.NO_LOCAL_FOOTAGE_MSG)
        self.assertFalse(out.startswith("[video error]"))
        self.assertNotIn("/api/v1/videos", [r.url.path for r in rec.requests])

    def test_default_path_is_unchanged(self):
        # No config at all: fail open, use the stock default, send no materials.
        rec = Recorder({
            "/ping": lambda r: httpx.Response(200, text="pong"),
            "/api/v1/videos": lambda r: ok("T4"),
        })
        out = self.run_(mpt.make_video(
            "ocean", transport=httpx.MockTransport(rec)))
        self.assertEqual(out, mpt.ACK_MSG)
        body = json.loads(rec.last.content.decode())
        self.assertEqual(body["video_source"], "pexels")
        self.assertNotIn("video_materials", body)

    def test_spawn_local_without_clips_raises(self):
        with self.assertRaises(mpt.MptError) as cm:
            self.run_(mpt.spawn(
                "ocean", source="local",
                transport=httpx.MockTransport(lambda r: ok("T1"))))
        self.assertEqual(str(cm.exception), mpt.NO_LOCAL_FOOTAGE_MSG)

    def test_handle_make_video_forwards_local_source(self):
        seen = {}

        async def fake(topic, **kwargs):
            seen["topic"] = topic
            seen["source"] = kwargs.get("source")
            return mpt.ACK_MSG

        original = mpt.make_video
        mpt.make_video = fake
        try:
            out = self.run_(mpt.handle_make_video(
                {"topic": "ocean", "source": "local"}))
        finally:
            mpt.make_video = original
        self.assertEqual(out, mpt.ACK_MSG)
        self.assertEqual(seen["topic"], "ocean")
        self.assertEqual(seen["source"], "local")


if __name__ == "__main__":
    unittest.main()
