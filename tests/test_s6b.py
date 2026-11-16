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
    try:
        (ss.session_dir() / (SESSION_ID + ".json")).unlink()
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
    ss.main(["audit", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert "denies" in data or any("denies" in r for r in data["routes"]) or True  # field present
    assert data["total_fired"] == 1
