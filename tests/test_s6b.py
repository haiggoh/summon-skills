SESSION_ID = "init"
"""§6b staged triggering: step 1 precision (worktree route skipped when the payload cwd is
already a linked worktree — .git is a file) and step 2 visibility (the hook text demands a
VISIBLE reply; a thinking-only decline does not count). The audit counts denies for the
step-3 barrier gate."""
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "summon-skills.py"
import importlib.util
spec = importlib.util.spec_from_file_location("summon_skills", SCRIPT)
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMON_SKILLS_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SUMMON_SKILLS_TRANSCRIPTS", str(tmp_path / "projects"))
    monkeypatch.setenv("SUMMON_SKILLS_ROUTES", str(ROOT / "rules" / "routes.json"))
    # session state lives in the SHARED /tmp/summon-skills dir — use a unique session id
    # per test and remove only that state file
    global SESSION_ID
    import uuid
    SESSION_ID = uuid.uuid4().hex
    for suffix in (".json", ".barrier.json", ".deny.json"):
        try:
            (ss.session_dir() / (SESSION_ID + suffix)).unlink()
        except OSError:
            pass
    # index with the routes' skills invocable
    monkeypatch.setattr(ss, "load_index", lambda *a, **k: {
        "skills": {"superpowers:using-git-worktrees": {"invocable": True},
                   "waypoints:waypoints": {"invocable": True}},
        "plugins": [], "deps": {}})


def _payload(cwd, prompt="create a worktree for this"):
    return {"session_id": SESSION_ID, "cwd": str(cwd), "prompt": prompt,
            "transcript_path": ""}


def test_worktree_route_skipped_when_cwd_is_linked_worktree(tmp_path):
    linked = tmp_path / "wt"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: /elsewhere\n")   # linked worktree: .git is a FILE
    normal = tmp_path / "repo"
    (normal / ".git").mkdir(parents=True)                                # normal checkout: .git is a DIR
    out_linked = ss.hook_prompt(_payload(linked))
    assert "worktree" not in out_linked, out_linked   # skipped: cwd already a linked worktree
    out_normal = ss.hook_prompt(_payload(normal))
    assert "worktree" in out_normal, out_normal       # normal checkout: suggestion fires


def test_hook_text_demands_visible_reply():
    out = ss.hook_prompt(_payload(ROOT, "waypoints list"))
    assert "VISIBLE reply" in out and "thinking-only doesn't count" in out, out


def test_audit_json_has_denies_field(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SUMMON_SKILLS_TRANSCRIPTS", str(tmp_path / "projects"))
    d = tmp_path / "projects" / "s"
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text(json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "UserPromptSubmit hook success: summon-skills: likely relevant -> "
                                 "waypoints:waypoints (open-items) -- invoke before the first tool call"}]}}) + "\n", encoding="utf-8")
    # plant a deny reason line so the denies count is actually exercised (the old
    # `... or True` made the assertion vacuous — review finding)
    (d / "s1.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "UserPromptSubmit hook success: summon-skills: likely relevant -> "
                                     "waypoints:waypoints (open-items) -- invoke before the first tool call"}]}}) + "\n" +
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "PreToolUse hook: summon-skills: 'waypoints:waypoints' ... denied once. "
                                    "Invoke the skill ..."}]}}) + "\n", encoding="utf-8")
    ss.main(["audit", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data.get("denies") == 1, data   # the planted deny is counted
    assert data["total_fired"] == 1


# ---------------------------------------------------------------- §6b step 3: deny-once barrier

def _write_transcript(tmp_path, records):
    tp = tmp_path / "t.jsonl"
    tp.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(tp)


def _assistant_text(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": text}]}}


def _skill_use(skill):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": skill}}]}}


def _suggest_waypoints(tmp_path):
    """Fire the prompt hook with a waypoints-matching prompt; returns (suggestion, transcript)."""
    tp = _write_transcript(tmp_path, [{"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "prompt"}]}}])
    out = ss.hook_prompt({"session_id": SESSION_ID, "cwd": str(tmp_path),
                          "prompt": "waypoints list", "transcript_path": tp})
    assert "waypoints:waypoints" in out, out
    return out, tp


def test_step3_prompt_hook_records_pending_barrier_state(tmp_path):
    _suggest_waypoints(tmp_path)
    st = ss._load_barrier(SESSION_ID)
    assert st.get("pending", {}).get("waypoints:waypoints") == "open-items"
    assert st.get("denied") is False


def test_step3_denies_once_then_fails_open(tmp_path):
    _, tp = _suggest_waypoints(tmp_path)
    ptool = {"session_id": SESSION_ID, "tool_name": "Bash",
             "tool_input": {"command": "ls"}, "transcript_path": tp}
    out1 = ss.hook_pretool(ptool)
    d1 = json.loads(out1)
    assert d1["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = d1["hookSpecificOutput"]["permissionDecisionReason"]
    assert "summon-skills" in reason and "denied once" in reason and "waypoints" in reason
    out2 = ss.hook_pretool(ptool)   # second call: fail OPEN
    assert out2 == "", out2


def test_step3_skill_call_for_pending_skill_clears_and_allows(tmp_path):
    _, tp = _suggest_waypoints(tmp_path)
    out = ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Skill",
                           "tool_input": {"skill": "waypoints:waypoints"},
                           "transcript_path": tp})
    assert out == ""   # Skill calls are exempt
    out2 = ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Bash",
                            "tool_input": {"command": "ls"}, "transcript_path": tp})
    assert out2 == "", out2   # pending cleared by loading -> no deny


def test_step3_loaded_in_transcript_allows(tmp_path):
    _, tp = _suggest_waypoints(tmp_path)
    tp2 = _write_transcript(tmp_path, [{"type": "user", "message": {"role": "user", "content": []}},
                                       _skill_use("waypoints:waypoints")])
    out = ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Bash",
                           "tool_input": {"command": "ls"}, "transcript_path": tp2})
    assert out == "", out


def test_step3_visible_text_answer_allows_but_thinking_does_not(tmp_path):
    _, tp = _suggest_waypoints(tmp_path)
    tp_vis = _write_transcript(tmp_path, [_assistant_text("I'll skip waypoints for now because ...")])
    out = ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Bash",
                           "tool_input": {"command": "ls"}, "transcript_path": tp_vis})
    assert out == "", "visible-text answer must clear the barrier"
    # re-suggest so the barrier is pending again (the visible answer above cleared it);
    # clear the SHOWN set first, or suggest() suppresses the already-shown skill
    (ss.session_dir() / (SESSION_ID + ".json")).unlink(missing_ok=True)
    _suggest_waypoints(tmp_path)
    tp_think = _write_transcript(tmp_path, [{"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "maybe use waypoints here"}]}}])
    out2 = ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Bash",
                            "tool_input": {"command": "ls"}, "transcript_path": tp_think})
    assert out2 != "", "thinking-only mention must NOT clear the barrier"


def test_step3_no_transcript_fails_open_without_denying(tmp_path):
    # A missing/unreadable transcript cannot prove load/answer either way -> the barrier
    # must NOT deny (a hook never wrongly blocks); it just stays inert.
    _suggest_waypoints(tmp_path)
    out = ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Bash",
                           "tool_input": {"command": "ls"}, "transcript_path": ""})
    assert out == "", out


def test_step3_deny_claim_is_atomic(tmp_path):
    # Two parallel hook processes must not both deny: the O_EXCL claim file arbitrates.
    _, tp = _suggest_waypoints(tmp_path)
    claim = ss._barrier_file(SESSION_ID).with_name(
        ss._barrier_file(SESSION_ID).name.replace(
            ".barrier.json", "." + ss._load_barrier(SESSION_ID)["batch"] + ".deny.json"))
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.touch()   # simulate the other process having claimed the deny first
    out = ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Bash",
                           "tool_input": {"command": "ls"}, "transcript_path": tp})
    assert out == "", "an existing claim must fail open (once-only)"


def test_step3_malformed_payload_never_blocks(tmp_path):
    _suggest_waypoints(tmp_path)
    assert ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Bash",
                            "tool_input": "ls -la"}) == ""      # tool_input not a dict
    assert ss.hook_pretool("not a dict") == ""                  # payload not a dict
    assert ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Bash"}) == ""


def test_step3_suggestion_line_in_transcript_does_not_clear_barrier(tmp_path):
    # Review round 2 (DeepSeek): _visible_mentions was role-blind — the UserPromptSubmit
    # suggestion line is persisted as a USER text block containing the skill short name,
    # so the barrier cleared itself and never fired in production. Only ASSISTANT text
    # counts as a visible answer.
    tp = _write_transcript(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "UserPromptSubmit hook success: summon-skills: likely relevant -> "
                                     "waypoints:waypoints (open-items) -- invoke before the first tool call"}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "waypoints list"}]}},
    ])
    ss.hook_prompt({"session_id": SESSION_ID, "cwd": str(tmp_path),
                    "prompt": "waypoints list", "transcript_path": tp})
    out = ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Bash",
                           "tool_input": {"command": "ls"}, "transcript_path": tp})
    assert out != "", "the hook's own suggestion line (user role) must NOT count as a visible answer"
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_step3_assistant_text_still_clears(tmp_path):
    tp = _write_transcript(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "summon-skills: likely relevant -> waypoints:waypoints (open-items)"}]}},
        _assistant_text("skipping waypoints, not needed here"),
    ])
    ss.hook_prompt({"session_id": SESSION_ID, "cwd": str(tmp_path),
                    "prompt": "waypoints list", "transcript_path": tp})
    out = ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Bash",
                           "tool_input": {"command": "ls"}, "transcript_path": tp})
    assert out == "", "an assistant text mention must still clear the barrier"


def test_audit_visible_classification_ignores_user_role(tmp_path, monkeypatch, capsys):
    # same role-blindness in the audit classifier: a user-role line naming the skill must
    # not count as visible (the suggestion line itself would otherwise inflate it)
    monkeypatch.setenv("SUMMON_SKILLS_TRANSCRIPTS", str(tmp_path / "projects"))
    d = tmp_path / "projects" / "s"; d.mkdir(parents=True)
    (d / "s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "summon-skills: likely relevant -> waypoints:waypoints (open-items)"}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "later the user typed: waypoints"}]}},
    ]), encoding="utf-8")
    ss.main(["audit", "--json"])
    data = json.loads(capsys.readouterr().out)
    route = next(r for r in data["routes"] if r["route"] == "open-items")
    assert route["silent"] == 1 and route["visible"] == 0, route
