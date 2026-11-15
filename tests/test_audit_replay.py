"""summon-skills.py audit / replay (user scope §6b step 1: measure before tuning).

audit scans transcripts for suggestion events ("summon-skills: likely relevant -> SKILL
(route)") and classifies each outcome per route: loaded (a later Skill tool_use with that
skill), visible (an assistant TEXT block mentioning it — thinking blocks don't count),
thinking-only (only thinking blocks mention it), silent (neither).
replay re-runs suggest() over a labelled corpus and reports which routes fired vs expected.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "summon-skills.py"
spec = importlib.util.spec_from_file_location("summon_skills", SCRIPT)
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


def _hook_line(skill, route):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "UserPromptSubmit hook success: summon-skills: likely relevant -> "
                                 f"{skill} ({route}) -- invoke before the first tool call, or say why not."}]}}


def _skill_use(skill):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": skill}}]}}


def _text_block(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": text}]}}


def _thinking_block(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": text}]}}


def _write_transcript(tmp_path, name, records):
    d = tmp_path / "projects" / "sess"
    d.mkdir(parents=True)
    f = d / name
    f.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return f


def test_audit_classifies_all_four_outcomes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SUMMON_SKILLS_TRANSCRIPTS", str(tmp_path / "projects"))
    monkeypatch.setenv("SUMMON_SKILLS_STATE", str(tmp_path / "state"))
    recs = [
        _hook_line("superpowers:systematic-debugging", "debugging"),
        _skill_use("superpowers:systematic-debugging"),                       # loaded
        _hook_line("superpowers:brainstorming", "brainstorming"),
        _text_block("I will not use brainstorming here because the task is fixed."),  # visible
        _hook_line("run-to-completion:run-to-completion", "autonomous-run"),
        _thinking_block("maybe run-to-completion:run-to-completion later"),   # thinking-only
        _hook_line("check-code", "automation"),
        # nothing after this one                                                   # silent
    ]
    _write_transcript(tmp_path, "s1.jsonl", recs)
    rc = ss.main(["audit", "--json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    data = json.loads(out)
    per_route = {r["route"]: r for r in data["routes"]}
    assert per_route["debugging"]["loaded"] == 1
    assert per_route["brainstorming"]["visible"] == 1
    assert per_route["autonomous-run"]["thinking"] == 1
    assert per_route["automation"]["silent"] == 1
    assert data["total_fired"] == 4


def test_replay_reports_fired_vs_expected(tmp_path, capsys):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps({"prompt": "fix this bug", "expect": "debugging"}) + "\n"
                      + json.dumps({"prompt": "ship the branch", "expect": "debugging"}) + "\n",
                      encoding="utf-8")
    rc = ss.main(["replay", str(corpus), "--json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    data = json.loads(out)
    assert data["lines"] == 2
    assert data["hits"] == 1, data   # "fix this bug" matches the debugging route; the ship prompt does not
