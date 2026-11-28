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

import test_summon_skills as _tss

home = _tss.home  # shared stub ~/.claude-tree fixture (pytest registers fixtures by name)

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


# ---------------------------------------------------------------- Task 1 (0.3.0): ctx + violations

FIXTURE = ROOT / "tests" / "fixtures" / "regression.jsonl"


def _fixture_rows():
    return [json.loads(ln) for ln in FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_suggest_ctx_none_is_identity(home):
    """ctx=None (the default) MUST reproduce 0.2.7 output exactly for every fixture prompt;
    a non-None ctx must at least be accepted without error (dampers land in later tasks)."""
    routes, errors = ss.load_routes()
    assert errors == []
    index = ss.load_index()
    for row in _fixture_rows():
        p = row.get("prompt") or ""
        base = ss.suggest(p, routes, index, (), ss.DEFAULT_MAX)
        assert ss.suggest(p, routes, index, (), ss.DEFAULT_MAX, ctx=None) == base
        out = ss.suggest(p, routes, index, (), ss.DEFAULT_MAX,
                         ctx={"turns": 3, "known": ["x:y"], "uses": {"x:y": 2.0}})
        assert isinstance(out, tuple) and isinstance(out[0], list)


def test_replay_reports_violations(tmp_path, home, capsys):
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps({"prompt": "my test fails with a KeyError traceback",
                                  "expect_any": ["superpowers:systematic-debugging"]}) + "\n"
                      + json.dumps({"prompt": "gardening question about tomato plants",
                                    "expect_any": ["superpowers:systematic-debugging"],
                                    "expect_none": ["superpowers:brainstorming"]}) + "\n",
                      encoding="utf-8")
    rc = ss.main(["replay", str(corpus), "--json"])
    out = capsys.readouterr().out
    assert rc == 1, out
    data = json.loads(out)
    assert data["lines"] == 2
    assert len(data["violations"]) == 1, data
    v = data["violations"][0]
    assert v["missing"] == ["superpowers:systematic-debugging"]
    assert v["unwanted"] == []
    assert "tomato" in v["prompt"]


def test_regression_positives_hold(home):
    """Every non-pending expect_any row must be satisfied with the shipped routes and a home
    stub that installs every skill the fixture names."""
    rows = _fixture_rows()
    named = set()
    for row in rows:
        named.update(row.get("expect_any") or [])
        named.update(row.get("expect_none") or [])
    routes0, _ = ss.load_routes()
    builtins = set()
    for r in routes0:
        builtins.update(r.get("_builtin", ()))
    for skill in sorted(named):
        if ":" in skill and skill not in builtins:
            plugin, name = skill.split(":", 1)
            home.plugin(plugin, [name])
    home.sync()
    routes, errors = ss.load_routes()
    assert errors == []
    index = ss.load_index()
    checked = 0
    for row in rows:
        if row.get("pending_task"):
            continue  # activates when that task lands; the test reads the key
        any_expect = row.get("expect_any") or []
        if not any_expect:
            continue
        fired = [s for s, _ in ss.suggest(row.get("prompt") or "", routes, index, (),
                                          ss.DEFAULT_MAX, ctx=row.get("context"))[0]]
        assert any(s in fired for s in any_expect), (row.get("prompt"), fired, any_expect)
        checked += 1
    assert checked >= 8, checked
