"""Guards for LLM lifecycle safety (incident 2026-09-17 22:31).

A typed middle-of-conversation turn that used a tool failed with a 400 from the
go gateway. Pipecat classified the HTTP 400 as ``INVALID_REQUEST`` (permanent),
marked the LLM service unusable, and the worker's ``END`` policy tore the whole
24/7 pipeline down — the app stayed open but could not reconnect.

These tests lock in the two fixes:

* ``BoundedContextLLM._classify_error`` never lets a provider error mark the
  service unusable (defect 2), and the worker policy is ``CONTINUE`` (source
  guard);
* the outgoing message list can never contain a dangling ``tool`` message —
  the text-tool path writes a matching assistant/tool pair and the sanitizer
  drops any orphan before dispatch (defect 1).

Run with:

    ../../.venv/bin/python -m unittest test_llm_lifecycle -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UI_DIR = Path(__file__).resolve().parent

import server  # noqa: E402


def _seed_model_cache():
    """Keep LLM construction off the network/shell: a fixed one-item model
    list so ``_oc_models_cached`` never shells out to the opencode CLI."""
    saved = dict(server._OC_MODELS_CACHE)
    server._OC_MODELS_CACHE.update({
        "ts": time.monotonic(),
        "items": [{"id": server.BRAIN_MODEL_ID, "tier": "go",
                   "name": "DeepSeek V4.1 Flash", "free": False}],
    })
    return saved


def _make_llm(**kwargs):
    saved = _seed_model_cache()
    try:
        llm = server.BoundedContextLLM(
            api_key="test-key", base_url="http://127.0.0.1:1/v1", **kwargs
        )
    finally:
        server._OC_MODELS_CACHE.update(saved)
    llm._fallback_enabled = False
    return llm


def _call(call_id: str, name: str = "web_search"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": '{"query": "x"}'},
    }


class SanitizeToolMessagesTests(unittest.TestCase):
    def test_dangling_tool_message_is_dropped(self):
        out, dropped = server.BoundedContextLLM._sanitize_tool_messages([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [_call("id1")]},
            {"role": "tool", "content": "listing", "tool_call_id": "id1"},
            {"role": "tool", "content": "orphan", "tool_call_id": "id2"},
        ])
        self.assertEqual(dropped, 1)
        roles = [m["role"] for m in out]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        self.assertEqual(out[2]["tool_call_id"], "id1")

    def _assert_pairs_complete(self, msgs):
        pending = set()
        for m in msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    pending.add(tc["id"])
            elif m.get("role") == "tool":
                self.assertIn(m["tool_call_id"], pending,
                              "tool result without a preceding call")
                pending.discard(m["tool_call_id"])
        self.assertEqual(pending, set(), "tool call without a result")

    def test_missing_result_is_synthesised_and_pair_completes(self):
        # The shape that 400'd: assistant tool_calls whose result never came.
        out, dropped = server.BoundedContextLLM._sanitize_tool_messages([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [_call("id1")]},
            {"role": "user", "content": "next"},
        ])
        self.assertEqual(dropped, 0)
        self.assertEqual([m["role"] for m in out],
                         ["user", "assistant", "tool", "user"])
        placeholder = out[2]
        self.assertEqual(placeholder["tool_call_id"], "id1")
        self.assertTrue(placeholder["content"].strip())
        self.assertLessEqual(len(placeholder["content"]), 200)
        self._assert_pairs_complete(out)

    def test_missing_result_at_end_is_synthesised_per_id(self):
        out, dropped = server.BoundedContextLLM._sanitize_tool_messages([
            {"role": "assistant", "content": "",
             "tool_calls": [_call("id1"), _call("id2")]},
        ])
        self.assertEqual(dropped, 0)
        self.assertEqual([m["role"] for m in out],
                         ["assistant", "tool", "tool"])
        self.assertEqual([m["tool_call_id"] for m in out[1:]], ["id1", "id2"])
        self._assert_pairs_complete(out)

    def test_orphan_only_is_still_dropped_without_placeholder(self):
        out, dropped = server.BoundedContextLLM._sanitize_tool_messages([
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "orphan", "tool_call_id": "id9"},
        ])
        self.assertEqual(dropped, 1)
        self.assertEqual([m["role"] for m in out], ["user"])

    def test_text_tool_placeholder_pair(self):
        out, dropped = server.BoundedContextLLM._sanitize_tool_messages([
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "no id at all"},
        ])
        self.assertEqual(dropped, 1)
        self.assertEqual([m["role"] for m in out], ["user"])

    def test_assistant_with_empty_tool_calls_list_is_cleaned(self):
        out, dropped = server.BoundedContextLLM._sanitize_tool_messages(
            [{"role": "assistant", "content": "hi", "tool_calls": []}]
        )
        self.assertEqual(dropped, 0)
        self.assertNotIn("tool_calls", out[0])

    def test_empty_tool_content_is_dropped(self):
        out, dropped = server.BoundedContextLLM._sanitize_tool_messages([
            {"role": "assistant", "content": "", "tool_calls": [_call("a")]},
            {"role": "tool", "content": "", "tool_call_id": "a"},
        ])
        self.assertEqual(dropped, 1)
        self.assertEqual([m["role"] for m in out], ["assistant"])

    def test_valid_sequence_untouched(self):
        def _seq():
            return [
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "", "tool_calls": [_call("a")]},
                {"role": "tool", "content": "ra", "tool_call_id": "a"},
                {"role": "assistant", "content": "", "tool_calls": [_call("b")]},
                {"role": "tool", "content": "rb", "tool_call_id": "b"},
                {"role": "assistant", "content": "done"},
            ]

        out, dropped = server.BoundedContextLLM._sanitize_tool_messages(_seq())
        self.assertEqual(dropped, 0)
        self.assertEqual(out, _seq())


class TextToolPairTests(unittest.TestCase):
    _TAG = (
        "<tool_call><function=web_search>"
        "<parameter=query>weather in Mumbai</parameter>"
        "</function></tool_call>"
    )

    def test_text_tool_path_writes_a_matching_pair(self):
        llm = _make_llm()
        calls: list = []

        async def _executor(name, args):
            calls.append((name, args))
            return "RESULT"

        async def _empty_gen():
            if False:  # pragma: no cover - empty async iterator
                yield None

        async def _fake_base(_self, _context):
            return _empty_gen()

        async def _one_chunk(_text):
            yield server.BoundedContextLLM._text_chunk(_text)

        llm._text_tool_executor = _executor
        llm._base_completions = _fake_base

        ctx = server.LLMContext([{"role": "user", "content": "weather?"}])

        async def _drain():
            async for _ in llm._intercept_text_tool_calls(ctx, _one_chunk(self._TAG)):
                pass

        asyncio.run(_drain())

        self.assertEqual(calls and calls[0][0], "web_search")
        roles = [m.get("role") for m in ctx.messages]
        ai = roles.index("assistant")
        assistant = ctx.messages[ai]
        self.assertTrue(assistant.get("tool_calls"))
        tool = ctx.messages[ai + 1]
        self.assertEqual(tool["role"], "tool")
        self.assertEqual(tool["tool_call_id"], assistant["tool_calls"][0]["id"])
        self.assertTrue(tool["tool_call_id"])


class ProviderErrorLifecycleTests(unittest.TestCase):
    def test_provider_error_keeps_llm_usable(self):
        llm = _make_llm()
        self.assertTrue(llm.is_usable)

        class _BadRequest(Exception):
            pass

        e = _BadRequest("400 Bad Request")
        e.status_code = 400
        asyncio.run(llm.push_error("boom", exception=e))

        self.assertTrue(llm.is_usable)
        self.assertTrue(llm._is_usable)

    def test_unusable_policy_is_continue(self):
        src = (UI_DIR / "server.py").read_text(encoding="utf-8")
        self.assertIn(
            "processor_unusable_policy=ProcessorUnusablePolicy.CONTINUE", src
        )
        self.assertNotIn("ProcessorUnusablePolicy.END", src)


class ReasoningContentTests(unittest.TestCase):
    def test_captured_reasoning_is_attached_to_previous_assistant(self):
        llm = _make_llm()
        ctx = server.LLMContext([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        llm._reasoning_full = "The user said hi; I should greet them."
        llm._attach_pending_reasoning(ctx)
        self.assertEqual(
            ctx.messages[-1]["reasoning_content"],
            "The user said hi; I should greet them.",
        )
        self.assertEqual(llm._reasoning_full, "")

    def test_note_reasoning_accumulates_full_text(self):
        llm = _make_llm()
        asyncio.run(llm._note_reasoning("alpha"))
        asyncio.run(llm._note_reasoning("beta"))
        self.assertEqual(llm._reasoning_full, "alphabeta")

    def test_ensure_reasoning_content_backfills_missing(self):
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [_call("a")]},
            {"role": "tool", "content": "r", "tool_call_id": "a"},
            {"role": "assistant", "content": "done", "reasoning_content": "kept"},
        ]
        filled = server.BoundedContextLLM._ensure_reasoning_content(msgs)
        self.assertEqual(filled, 1)
        for m in msgs:
            if m["role"] == "assistant":
                self.assertIn("reasoning_content", m)
        self.assertEqual(msgs[3]["reasoning_content"], "kept")

    def test_resumed_session_history_is_valid_for_thinking_mode(self):
        hist = [
            {"role": "user", "content": "what's 2+2"},
            {"role": "assistant", "content": "4"},
            {"role": "user", "content": "and 3+3?"},
        ]
        filled = server.BoundedContextLLM._ensure_reasoning_content(hist)
        self.assertEqual(filled, 1)
        self.assertEqual(hist[1]["reasoning_content"], "")


class OldToolResultCompactionTests(unittest.TestCase):
    """Old tool output must not be replayed verbatim in the voice context.

    A stale investigation's ``read_file`` / ``file_search`` / ``run_bash`` output
    used to ride along in full, so the model kept volunteering a status report
    the user never asked for. The discriminator is the USER TURN BOUNDARY: once a
    new user message arrives, every earlier tool result is superseded and its
    content is compacted, while the current turn's results stay verbatim.
    """

    @staticmethod
    def _count_chars(messages):
        total = 0
        for m in messages:
            c = m.get("content")
            if isinstance(c, str):
                total += len(c)
        return total

    def _fixture(self):
        """Two real turns: an old investigation, then a new user request."""
        big_old = "A" * 40000
        big_mid = "B" * 30000
        return [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "Help me with the Cal Matters project."},
            {"role": "assistant", "content": "", "reasoning_content": "think-1",
             "tool_calls": [_call("t1", "file_search")]},
            {"role": "tool", "content": big_old, "tool_call_id": "t1"},
            {"role": "assistant", "content": "", "reasoning_content": "think-2",
             "tool_calls": [_call("t2", "read_file")]},
            {"role": "tool", "content": big_mid, "tool_call_id": "t2"},
            {"role": "assistant",
             "content": "I found it: calorie_tracker on your Desktop."},
            {"role": "user", "content": "now check the tests"},
            {"role": "assistant", "content": "", "reasoning_content": "think-3",
             "tool_calls": [_call("t3", "run_bash")]},
            {"role": "tool", "content": "RECENT-RESULT-VERBATIM",
             "tool_call_id": "t3"},
        ]

    def _calorie_fixture(self):
        """The exact bug shape: a whole investigation, then a social reply."""
        # Keep system + first turn + the assistant's finding, then the user's
        # social turn ("I'm good") directly after — no second tool round.
        return self._fixture()[:7] + [{"role": "user", "content": "I'm good"}]

    def _trim(self, messages):
        llm = server.BoundedContextLLM.__new__(server.BoundedContextLLM)
        llm.MAX_CHARS = 220000
        llm.TOOL_RESULT_KEEP = 1
        ctx = server.LLMContext(list(messages))
        llm._trim_context(ctx)
        return list(ctx.messages)

    def _trimmed(self):
        return self._fixture(), self._trim(self._fixture())

    def test_old_tool_results_compacted_recent_stays_verbatim(self):
        _before, out = self._trimmed()
        by_id = {m.get("tool_call_id"): m for m in out
                 if m.get("role") == "tool"}
        # Both pre-boundary results replaced by short notes.
        for tid, name in (("t1", "file_search"), ("t2", "read_file")):
            self.assertIn("omitted", by_id[tid]["content"])
            self.assertIn(name, by_id[tid]["content"])
        self.assertNotIn("A" * 100, by_id["t1"]["content"])
        self.assertNotIn("B" * 100, by_id["t2"]["content"])
        # Current-turn result untouched, verbatim.
        self.assertEqual(by_id["t3"]["content"], "RECENT-RESULT-VERBATIM")
        # Only contents changed, never the pairing fields.
        self.assertTrue(by_id["t1"]["tool_call_id"])
        self.assertEqual(by_id["t1"]["role"], "tool")

    def test_calorie_scenario_new_user_turn_supersedes_investigation(self):
        out = self._trim(self._calorie_fixture())
        by_id = {m.get("tool_call_id"): m for m in out
                 if m.get("role") == "tool"}
        # The user moved on ("I'm good"); both stale results compact, including
        # the biggest one (the read_file of HANDOFF.md = t2).
        self.assertIn("omitted", by_id["t1"]["content"])
        self.assertIn("omitted", by_id["t2"]["content"])
        self.assertNotIn("A" * 100, by_id["t1"]["content"])
        self.assertNotIn("B" * 100, by_id["t2"]["content"])

    def test_tool_call_pairing_survives_and_sanitizer_drops_nothing(self):
        _before, out = self._trimmed()
        # Every assistant tool_calls id has a following tool message with it.
        pending = set()
        for m in out:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    pending.add(tc["id"])
            elif m.get("role") == "tool":
                self.assertIn(m["tool_call_id"], pending)
                pending.discard(m["tool_call_id"])
        self.assertEqual(pending, set())
        _out2, dropped = server.BoundedContextLLM._sanitize_tool_messages(out)
        self.assertEqual(dropped, 0)

    def test_reasoning_content_survives_on_every_assistant(self):
        before, out = self._trimmed()
        want = [m.get("reasoning_content") for m in before
                if m.get("role") == "assistant" and m.get("reasoning_content")]
        got = [m.get("reasoning_content") for m in out
               if m.get("role") == "assistant" and m.get("reasoning_content")]
        self.assertEqual(got, want)
        self.assertEqual(set(want), {"think-1", "think-2", "think-3"})

    def test_total_chars_shrink_by_at_least_half(self):
        before, out = self._trimmed()
        self.assertLessEqual(self._count_chars(out),
                             self._count_chars(before) * 0.5)


def _chunk(text="", tool_calls=None):
    return {"choices": [{"delta": {"content": text, "tool_calls": tool_calls}}]}


def _tool_delta():
    return _chunk(tool_calls=[{
        "index": 0, "id": "call_1",
        "function": {"name": "web_search", "arguments": "{}"},
    }])


class _FakeCreate:
    """Records request params and returns an async stream (fake model)."""

    def __init__(self, chunks):
        self.calls: list = []
        self._chunks = chunks

    async def __call__(self, **params):
        self.calls.append(params)
        chunks = self._chunks

        async def _gen():
            for c in chunks:
                yield c

        return _gen()


class _FakeProvider:
    def __init__(self, created):
        from types import SimpleNamespace as _NS
        self.name = "fake-brain"
        self.model = server.BRAIN_MODEL_ID
        self.client = _NS(chat=_NS(completions=_NS(create=created)))
        self.cooldown_until = 0.0
        self.probing = False


def _wire_fake_model(llm, chunks):
    created = _FakeCreate(chunks)
    llm._providers = {server.BRAIN_MODEL_ID: _FakeProvider(created)}
    llm._selected_model = server.BRAIN_MODEL_ID
    llm._settings.system_instruction = "sys"
    return created


class ConversationalClassificationTests(unittest.TestCase):
    def test_conversational_intents(self):
        for text in ("hey, how are you?", "thanks!", "ok", "good morning",
                     "what's up", "yes", "no", "i'm good", "goodbye",
                     "sounds good", "no thanks"):
            with self.subTest(text=text):
                cls, signal = server.classify_turn_text(text)
                self.assertEqual(cls, server.TURN_CLASS_CONVERSATIONAL, signal)

    def test_work_utterances(self):
        for text in ("can you refactor the auth module", "fix the bug",
                     "set a timer for 5 minutes", "what time is it",
                     "why is the sky blue", "do it",
                     "write a report on the Cal Matters project"):
            with self.subTest(text=text):
                cls, signal = server.classify_turn_text(text)
                self.assertEqual(cls, server.TURN_CLASS_WORK, signal)

    def test_long_utterance_is_work(self):
        text = " ".join(["hey"] * 12)
        self.assertEqual(server.classify_turn_text(text)[0],
                         server.TURN_CLASS_WORK)

    def test_empty_is_work(self):
        self.assertEqual(server.classify_turn_text("   ")[0],
                         server.TURN_CLASS_WORK)

    def test_turn_level_guards(self):
        llm = _make_llm()
        ctx = server.LLMContext([{"role": "user", "content": "hey"}])
        self.assertEqual(llm.classify_turn(ctx)[0],
                         server.TURN_CLASS_CONVERSATIONAL)
        # An image turn, or one that already used a tool, is never shortcut.
        img = server.LLMContext([{
            "role": "user",
            "content": [{"type": "text", "text": "hey"},
                        {"type": "image_url", "image_url": {"url": "data:x"}}],
        }])
        self.assertEqual(llm.classify_turn(img), (server.TURN_CLASS_WORK,
                                                  "image-attached"))
        tools = server.LLMContext([
            {"role": "user", "content": "hey"},
            {"role": "assistant", "content": "", "tool_calls": [_call("t1")]},
            {"role": "tool", "content": "r", "tool_call_id": "t1"},
        ])
        self.assertEqual(llm.classify_turn(tools),
                         (server.TURN_CLASS_WORK, "tool-used-this-turn"))


class ConversationalShortcutRequestTests(unittest.TestCase):
    def _drain(self, llm, ctx):
        async def _run():
            out = []
            stream = await llm.get_chat_completions(ctx)
            async for c in stream:
                out.append(c)
            return out

        return asyncio.run(_run())

    def test_conversational_turn_sends_thinking_off(self):
        llm = _make_llm()
        created = _wire_fake_model(llm, [_chunk("hi!")])
        ctx = server.LLMContext([{"role": "user", "content": "hey, how are you?"}])
        self._drain(llm, ctx)
        self.assertEqual(len(created.calls), 1)
        self.assertEqual(created.calls[0].get("extra_body"),
                         {"reasoning_effort": "none"})
        # The off-switch is for the single request only.
        self.assertEqual(llm._settings.extra, {})

    def test_work_turn_request_is_untouched(self):
        llm = _make_llm()
        created = _wire_fake_model(llm, [_chunk("done")])
        ctx = server.LLMContext([{"role": "user",
                                  "content": "refactor the auth module"}])
        self._drain(llm, ctx)
        self.assertEqual(len(created.calls), 1)
        self.assertNotIn("extra_body", created.calls[0])

    def test_shortcut_disabled_sends_nothing(self):
        llm = _make_llm()
        llm._conversational_shortcut = False
        created = _wire_fake_model(llm, [_chunk("hi!")])
        ctx = server.LLMContext([{"role": "user", "content": "hey"}])
        self._drain(llm, ctx)
        self.assertNotIn("extra_body", created.calls[0])


class ConversationalReaskTests(unittest.TestCase):
    def _drain(self, llm, ctx, first_chunks):
        async def _gen():
            for c in first_chunks:
                yield c

        async def _run():
            out = []
            async for c in llm._conversational_reask(ctx, _gen()):
                out.append(server.BoundedContextLLM._delta_text(c)
                           or server.BoundedContextLLM._delta_toolcall(c))
            return out

        return asyncio.run(_run())

    def test_tool_call_triggers_thinking_reask(self):
        llm = _make_llm()
        asked = {"n": 0}

        async def _retry(_self, _context):
            asked["n"] += 1

            async def _g():
                yield _chunk("Good to see you!")
            return _g()

        llm._base_completions = _retry
        ctx = server.LLMContext([{"role": "user", "content": "hey"}])
        out = self._drain(llm, ctx, [_tool_delta()])
        self.assertEqual(asked["n"], 1)
        self.assertEqual("".join(out), "Good to see you!")

    def test_clarifying_question_triggers_reask(self):
        llm = _make_llm()
        asked = {"n": 0}

        async def _retry(_self, _context):
            asked["n"] += 1

            async def _g():
                yield _chunk("Sure \u2014 which project?")
            return _g()

        llm._base_completions = _retry
        ctx = server.LLMContext([{"role": "user", "content": "do it"}])
        out = self._drain(llm, ctx, [_chunk("I need more detail about that.")])
        self.assertEqual(asked["n"], 1)
        self.assertEqual("".join(out), "Sure \u2014 which project?")

    def test_normal_reply_streams_without_reask(self):
        llm = _make_llm()
        asked = {"n": 0}

        async def _retry(_self, _context):  # pragma: no cover - must not run
            asked["n"] += 1

            async def _g():
                yield _chunk("nope")
            return _g()

        llm._base_completions = _retry
        ctx = server.LLMContext([{"role": "user", "content": "hey"}])
        out = self._drain(llm, ctx, [_chunk("I'm doing well, thanks!")])
        self.assertEqual(asked["n"], 0)
        self.assertEqual("".join(out), "I'm doing well, thanks!")

    def test_stream_error_falls_back_to_thinking(self):
        llm = _make_llm()

        async def _boom():
            raise RuntimeError("no-thinking rejected")
            yield  # pragma: no cover

        async def _retry(_self, _context):
            async def _g():
                yield _chunk("Hello there!")
            return _g()

        llm._base_completions = _retry
        ctx = server.LLMContext([{"role": "user", "content": "hey"}])

        async def _run():
            out = []
            stream = llm._conversational_reask(ctx, _boom())
            async for c in stream:
                out.append(server.BoundedContextLLM._delta_text(c))
            return out

        self.assertEqual("".join(asyncio.run(_run())), "Hello there!")

    def test_no_thinking_request_error_falls_back_without_cooldown(self):
        llm = _make_llm()
        seen = []

        async def create(**params):
            seen.append(params.get("extra_body"))
            if params.get("extra_body"):
                raise RuntimeError("reasoning_effort rejected")
            async def _g():
                yield _chunk("Hi there!")
            return _g()

        prov = _FakeProvider(create)
        llm._providers = {server.BRAIN_MODEL_ID: prov}
        llm._selected_model = server.BRAIN_MODEL_ID
        llm._settings.system_instruction = "sys"
        ctx = server.LLMContext([{"role": "user", "content": "hey"}])

        async def _run():
            out = []
            stream = await llm.get_chat_completions(ctx)
            async for c in stream:
                out.append(server.BoundedContextLLM._delta_text(c))
            return out

        self.assertEqual("".join(asyncio.run(_run())), "Hi there!")
        self.assertEqual(seen, [{"reasoning_effort": "none"}, None])
        self.assertEqual(prov.cooldown_until, 0.0)
        self.assertEqual(llm._settings.extra, {})

    def test_shortcut_is_wired_in_get_chat_completions(self):
        src = (UI_DIR / "server.py").read_text(encoding="utf-8")
        self.assertIn("stream = self._conversational_reask(context, stream)", src)
        self.assertIn('"extra_body": {"reasoning_effort": "none"}', src)


class BrainContractVolunteeringRuleTests(unittest.TestCase):
    def test_rule_present_and_existing_rules_kept(self):
        contract = server._BRAIN_CONTRACT
        self.assertIn(
            "Do not volunteer status updates, summaries or next steps about "
            "work the user has not asked about in this turn.",
            contract,
        )
        self.assertIn("do not turn it into a work report", contract)
        for kept in (
            "DECIDE, DO NOT ASK",
            "Agents are your team",
            "Any of them can do any task",
        ):
            self.assertIn(kept, contract)


if __name__ == "__main__":
    unittest.main(verbosity=2)
