SESSION_ID = "init"
"""§6b staged triggering: step 1 precision (worktree route skipped when the payload cwd is
already a linked worktree — .git is a file) and step 2 visibility (the hook text demands a
VISIBLE reply; a thinking-only decline does not count). The audit counts reminders (and
legacy denies) for the step-3 barrier gate; the barrier never denies a tool call."""
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
    monkeypatch.setattr(ss, "session_dir", lambda: tmp_path / "sessions")
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


def test_audit_counts_reminders_and_legacy_denies(tmp_path, monkeypatch, capsys):
    # 1 legacy "denied once" line + 2 "summon-skills reminder" lines -> denies==1, reminders==2
    monkeypatch.setenv("SUMMON_SKILLS_TRANSCRIPTS", str(tmp_path / "projects"))
    d = tmp_path / "projects" / "s"
    d.mkdir(parents=True)
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "UserPromptSubmit hook success: summon-skills: likely relevant -> "
                                     "waypoints:waypoints (open-items) -- invoke before the first tool call"}]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "PreToolUse hook: summon-skills: 'waypoints:waypoints' ... denied once. "
                                    "Invoke the skill ..."}]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "summon-skills reminder 1/2: 'waypoints:waypoints' ... NOT blocked."}]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "summon-skills reminder 2/2: 'waypoints:waypoints' ... NOT blocked."}]}}),
    ]
    (d / "s1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ss.main(["audit", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["denies"] == 1, data       # legacy "denied once" lines stay counted
    assert data["reminders"] == 2, data
    assert data["total_fired"] == 1, data


# ---------------------------------------------------------------- §6b step 3: reminder barrier (never denies)

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
    assert st.get("reminders") == 0


def test_step3_reminds_twice_then_silent(tmp_path):
    _, tp = _suggest_waypoints(tmp_path)
    ptool = {"session_id": SESSION_ID, "tool_name": "Bash",
             "tool_input": {"command": "ls"}, "transcript_path": tp}
    out1 = ss.hook_pretool(ptool)
    out2 = ss.hook_pretool(ptool)
    out3 = ss.hook_pretool(ptool)
    for out in (out1, out2):
        d = json.loads(out)
        assert "permissionDecision" not in d["hookSpecificOutput"], d
        ctx = d["hookSpecificOutput"]["additionalContext"]
        assert "summon-skills reminder" in ctx and "waypoints" in ctx, ctx
    assert out3 == "", out3   # third call on the same ignored batch goes silent


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


def test_step3_no_transcript_fails_open_without_reminding(tmp_path):
    # A missing/unreadable transcript cannot prove load/answer either way -> the barrier
    # must NOT block (a hook never wrongly blocks); it just stays inert.
    _suggest_waypoints(tmp_path)
    out = ss.hook_pretool({"session_id": SESSION_ID, "tool_name": "Bash",
                           "tool_input": {"command": "ls"}, "transcript_path": ""})
    assert out == "", out


def test_step3_reminder_claims_are_atomic(tmp_path):
    # Two parallel hook processes must not both claim the same reminder slot:
    # the O_EXCL claim files arbitrate.
    _, tp = _suggest_waypoints(tmp_path)
    batch = ss._load_barrier(SESSION_ID)["batch"]
    stem = ss._barrier_file(SESSION_ID).name.replace(".barrier.json", "")
    claim1 = ss._barrier_file(SESSION_ID).with_name("%s.%s.remind1.json" % (stem, batch))
    claim1.parent.mkdir(parents=True, exist_ok=True)
    claim1.touch()   # simulate the other process having claimed slot 1 first
    ptool = {"session_id": SESSION_ID, "tool_name": "Bash",
             "tool_input": {"command": "ls"}, "transcript_path": tp}
    out = ss.hook_pretool(ptool)
    d = json.loads(out)
    assert "summon-skills reminder 2/%d" % ss.REMINDER_LIMIT in \
        d["hookSpecificOutput"]["additionalContext"], d
    assert "permissionDecision" not in d["hookSpecificOutput"], d
    # plant slot 2 as well -> the next call goes silent
    claim2 = ss._barrier_file(SESSION_ID).with_name("%s.%s.remind2.json" % (stem, batch))
    claim2.touch()
    assert ss.hook_pretool(ptool) == "", "both slots claimed -> silent (never blocks)"


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
    d = json.loads(out)
    assert "permissionDecision" not in d["hookSpecificOutput"], d
    assert "summon-skills reminder" in d["hookSpecificOutput"]["additionalContext"], d


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
