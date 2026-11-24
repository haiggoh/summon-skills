"""Codex transcript regression: a visible acknowledgement must prevent a false reminder (never a denial)."""
import importlib.util
import json
from pathlib import Path
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("summon_codex", ROOT / "bin/summon-skills.py")
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)
SKILL = "superpowers:systematic-debugging"


@pytest.fixture(autouse=True)
def isolated_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "session_dir", lambda: tmp_path / "sessions")


def pretool(tmp_path, records):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
    session = uuid.uuid4().hex
    ss._save_barrier(session, {"pending": {SKILL: "debugging"}, "denied": False})
    return ss.hook_pretool({"session_id": session, "transcript_path": str(transcript),
                            "tool_name": "Bash", "tool_input": {"command": "true"}})


def message(role, text, phase="commentary"):
    return {"type": "response_item", "payload": {
        "type": "message", "role": role, "phase": phase,
        "content": [{"type": "output_text" if role == "assistant" else "input_text",
                     "text": text}]}}


@pytest.mark.parametrize("phase", ["commentary", "final_answer"])
def test_codex_visible_acknowledgement_allows_tool(tmp_path, phase):
    assert pretool(tmp_path, [message("assistant", "I am skipping systematic-debugging because this is a single read.", phase)]) == ""


def _assert_reminder(out):
    """The PreToolUse output must be the non-blocking additionalContext shape, never a deny."""
    d = json.loads(out)
    assert list(d.keys()) == ["hookSpecificOutput"], d
    hso = d["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse", hso
    assert "permissionDecision" not in hso, hso
    assert "summon-skills reminder" in hso["additionalContext"], hso


@pytest.mark.parametrize("record", [
    message("user", "Use systematic-debugging"),
    {"type": "response_item", "payload": {"type": "reasoning", "summary": [
        {"type": "summary_text", "text": "systematic-debugging is irrelevant"}]}},
])
def test_codex_user_and_reasoning_do_not_clear_guard(tmp_path, record):
    out = pretool(tmp_path, [record])
    _assert_reminder(out)


@pytest.mark.parametrize("boundary", [
    {"type": "compacted", "payload": {"message": "summary"}},
    {"type": "event_msg", "payload": {"type": "context_compacted"}},
])
def test_codex_compaction_drops_old_acknowledgement(tmp_path, boundary):
    out = pretool(tmp_path, [message("assistant", "Using systematic-debugging"), boundary,
                            message("assistant", "Now reading the data")])
    _assert_reminder(out)


def test_codex_payload_reminder_is_nonblocking(tmp_path):
    # Codex-shaped PreToolUse payload: two reminders, then silence — never a permissionDecision.
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(json.dumps(message("user", "Use systematic-debugging")) + "\n")
    session = uuid.uuid4().hex
    ss._save_barrier(session, {"pending": {SKILL: "debugging"}, "reminders": 0, "batch": "b1"},
                     new_batch=True)
    ptool = {"session_id": session, "transcript_path": str(transcript),
             "tool_name": "Bash", "tool_input": {"command": "true"}}
    outs = [ss.hook_pretool(ptool) for _ in range(3)]
    _assert_reminder(outs[0])
    _assert_reminder(outs[1])
    assert outs[2] == "", outs[2]
