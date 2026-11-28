"""Tests for summon-skills. Every test runs against a stub ~/.claude tree, never the real one."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "summon-skills.py"
spec = importlib.util.spec_from_file_location("summon_skills", SCRIPT)
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


def write_skill(root, name, invocable=True):
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    flag = "" if invocable else "disable-model-invocation: true\n"
    (d / "SKILL.md").write_text("---\nname: %s\ndescription: x\n%s---\nbody\n" % (name, flag))


@pytest.fixture
def home(tmp_path, monkeypatch):
    ch = tmp_path / "claude"
    cache = ch / "plugins" / "cache" / "mkt"
    plugins = {}

    def plugin(name, skills, enabled=True, version="1.0.0"):
        root = cache / name / version
        for s in skills:
            write_skill(root, s if isinstance(s, str) else s[0],
                        True if isinstance(s, str) else s[1])
        plugins["%s@mkt" % name] = ([{"installPath": str(root), "version": version}], enabled)

    plugin("superpowers", ["systematic-debugging", "brainstorming", "test-driven-development",
                           "writing-plans", "verification-before-completion"])
    plugin("mattpocock-skills", ["diagnosing-bugs", "tdd"])
    plugin("audit-loose-ends", ["audit-loose-ends"])
    plugin("vercel", ["nextjs", "react-best-practices"])
    plugin("human-only", [("secret", False)])
    plugin("disabled-one", ["ghost"], enabled=False)
    write_skill(ch, "dataviz")
    write_skill(ch, "media-ops")

    def sync():
        (ch / "plugins").mkdir(parents=True, exist_ok=True)
        (ch / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {k: v[0] for k, v in plugins.items()}}))
        (ch / "settings.json").write_text(json.dumps(
            {"enabledPlugins": {k: v[1] for k, v in plugins.items()}}))
    sync()

    monkeypatch.setenv("SUMMON_SKILLS_CLAUDE_HOME", str(ch))
    monkeypatch.setenv("SUMMON_SKILLS_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(ss.tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.delenv("SUMMON_SKILLS_OFF", raising=False)
    monkeypatch.delenv("SUMMON_SKILLS_ROUTES", raising=False)
    home_obj = type("Home", (), {})()
    home_obj.ch, home_obj.plugin, home_obj.sync, home_obj.tmp = ch, plugin, sync, tmp_path
    return home_obj


def names(text, shown=()):
    routes, errors = ss.load_routes()
    assert errors == []
    return [s for s, _ in ss.suggest(text, routes, ss.load_index(), shown)[0]]


# ---------------------------------------------------------------- index

def test_index_filters_disabled_and_human_only(home):
    skills = ss.load_index()["skills"]
    assert "superpowers:systematic-debugging" in skills
    assert "dataviz" in skills
    assert "disabled-one:ghost" not in skills
    assert skills["human-only:secret"]["invocable"] is False


def test_index_is_cached_until_fingerprint_moves(home):
    first = ss.load_index()
    assert ss.load_index()["built_at"] == first["built_at"]
    home.plugin("newcomer", ["fresh"])
    time.sleep(0.01)
    home.sync()
    assert "newcomer:fresh" in ss.load_index()["skills"]


def test_index_uses_install_path_not_newest_dir(home):
    # a newer cache dir left on disk but NOT the registered installPath must be ignored
    write_skill(home.ch / "plugins" / "cache" / "mkt" / "superpowers" / "9.9.9", "only-in-stale")
    skills = ss.load_index(force=True)["skills"]
    assert "superpowers:only-in-stale" not in skills
    assert "superpowers:brainstorming" in skills
    home.plugin("superpowers", ["only-in-v2"], version="9.9.9")  # now it IS registered
    home.sync()
    assert "superpowers:only-in-v2" in ss.load_index()["skills"]


def test_index_rebuilds_when_plugin_code_changes(home, monkeypatch):
    ss.load_index()
    monkeypatch.setattr(ss, "VERSION", "9.9.9")
    monkeypatch.setattr(ss, "discover_skills", lambda ch: {"new-logic": {"invocable": True}})
    assert "new-logic" in ss.load_index()["skills"]


def test_manifest_custom_skill_paths(home):
    root = home.ch / "plugins" / "cache" / "mkt" / "custom" / "1.0.0"
    for rel, name in (("skills/engineering/tdd", "tdd"), ("extra/grilling", "grilling")):
        (root / rel).mkdir(parents=True)                     # a listed skill dir, and a
        (root / rel / "SKILL.md").write_text("---\nname: %s\n---\n" % name)  # dir OF skill dirs
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "custom", "skills": ["./skills/engineering/tdd", "./extra"]}))
    home.plugin("custom", [])
    home.sync()
    skills = ss.load_index()["skills"]
    assert "custom:tdd" in skills and "custom:grilling" in skills


def test_builtin_skills_count_as_installed(home, tmp_path, monkeypatch):
    routes_file = tmp_path / "r.json"
    routes_file.write_text(json.dumps({"schema": 1, "builtin_skills": ["simplify"], "routes": [
        {"id": "s", "kind": "process", "match": {"words": ["simplify"]}, "skills": ["simplify"]}]}))
    monkeypatch.setenv("SUMMON_SKILLS_ROUTES", str(routes_file))
    assert names("please simplify this") == ["simplify"]


# ---------------------------------------------------------------- already-loaded skills

def _debug_routes(tmp_path, monkeypatch):
    routes_file = tmp_path / "r-debug.json"
    routes_file.write_text(json.dumps({"schema": 1, "routes": [
        {"id": "debug", "kind": "process", "match": {"words": ["debug"]},
         "skills": ["superpowers:systematic-debugging"]}]}))
    monkeypatch.setenv("SUMMON_SKILLS_ROUTES", str(routes_file))


def _write_transcript(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _skill_use(name):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Skill", "input": {"skill": name}}]}}


def test_hook_does_not_suggest_a_skill_already_loaded_this_session(home, tmp_path, monkeypatch):
    """waypoint summon-skills-don-t-suggest-a: seen 2026-10-30 — systematic-debugging was
    suggested one turn after it was invoked, because dedup only knew the hook's OWN past
    suggestions. The transcript's Skill tool_uses must count as shown."""
    _debug_routes(tmp_path, monkeypatch)
    tp = tmp_path / "t.jsonl"
    _write_transcript(tp, [_skill_use("superpowers:systematic-debugging")])
    out = ss.hook_prompt({"prompt": "debug this failing test", "session_id": "s-load",
                          "transcript_path": str(tp)})
    assert "systematic-debugging" not in out


def test_loaded_skills_reset_at_a_compact_boundary(home, tmp_path, monkeypatch):
    """A compacted-away skill may need the reminder again: only tool_uses AFTER the last
    compact_boundary count."""
    _debug_routes(tmp_path, monkeypatch)
    tp = tmp_path / "t.jsonl"
    _write_transcript(tp, [_skill_use("superpowers:systematic-debugging"),
                           {"type": "compact_boundary"}])
    out = ss.hook_prompt({"prompt": "debug this failing test", "session_id": "s-compact",
                          "transcript_path": str(tp)})
    assert "systematic-debugging" in out


def test_unreadable_transcript_fails_open(home, tmp_path, monkeypatch):
    """Unreadable/missing transcript = current behaviour (suggest as if nothing was loaded)."""
    _debug_routes(tmp_path, monkeypatch)
    out = ss.hook_prompt({"prompt": "debug this failing test", "session_id": "s-open",
                          "transcript_path": str(tmp_path / "missing.jsonl")})
    assert "systematic-debugging" in out


# ---------------------------------------------------------------- dependency (inert plugin) checks

def _declare(home, plugin, mkt_entry=None, mcp=None, manifest=None):
    root = home.ch / "plugins" / "cache" / "mkt" / plugin / "1.0.0"
    root.mkdir(parents=True, exist_ok=True)
    if mcp is not None:
        (root / ".mcp.json").write_text(json.dumps(mcp))
    if manifest is not None:
        (root / ".claude-plugin").mkdir(exist_ok=True)
        (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    home.plugin(plugin, [])
    mdir = home.ch / "plugins" / "marketplaces" / "mkt" / ".claude-plugin"
    mdir.mkdir(parents=True, exist_ok=True)
    mfile = mdir / "marketplace.json"
    data = json.loads(mfile.read_text()) if mfile.exists() else {"plugins": []}
    if mkt_entry is not None:
        data["plugins"].append(dict(mkt_entry, name=plugin))
    mfile.write_text(json.dumps(data))
    home.sync()


def test_deps_from_marketplace_lsp_and_mcp_declarations(home, monkeypatch):
    _declare(home, "pyright-lsp", mkt_entry={"lspServers": {"pyright": {"command": "pyright-langserver"}}})
    _declare(home, "chat", mcp={"mcpServers": {"chat": {"command": "bun", "args": ["start"]}}})
    _declare(home, "web", mcp={"web": {"type": "http", "url": "https://x"}})          # no command: never inert
    _declare(home, "tok", manifest={"name": "tok", "mcpServers": {"t": {"type": "http", "url": "https://x",
             "headers": {"Authorization": "Bearer ${TOK_PAT}", "X": "${OPT:-default}"}}}})
    monkeypatch.setattr(ss, "find_binary", lambda name: None)
    monkeypatch.delenv("TOK_PAT", raising=False)
    monkeypatch.delenv("OPT", raising=False)
    problems = {p["plugin"]: p for p in ss.inert_plugins(ss.load_index())}
    assert problems["pyright-lsp"]["missing"] == ["pyright-langserver"]
    assert "pipx install pyright" in problems["pyright-lsp"]["hint"]
    assert problems["chat"]["missing"] == ["bun"]
    assert "web" not in problems
    assert problems["tok"]["missing"] == ["$TOK_PAT"]                    # ${OPT:-default} is optional


def test_deps_satisfied_and_plugin_root_placeholder(home, monkeypatch):
    _declare(home, "local", mcp={"s": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/server"}})
    monkeypatch.setattr(ss, "find_binary", lambda name: "/bin/" + name)
    assert ss.inert_plugins(ss.load_index()) == []                        # plugin-relative path: not a PATH dep


def test_session_start_reports_inert_plugins(home, monkeypatch):
    _declare(home, "pyright-lsp", mkt_entry={"lspServers": {"pyright": {"command": "pyright-langserver"}}})
    monkeypatch.setattr(ss, "find_binary", lambda name: None)
    out = run_hook("session", {})[1]
    assert "inert" in out and "pyright-lsp" in out and "pipx install pyright" in out
    assert "Python language server" in out and "no editor feedback" in out  # purpose + impact, not a side note
    monkeypatch.setattr(ss, "find_binary", lambda name: "/bin/" + name)
    assert "inert" not in run_hook("session", {})[1]                      # checked live, not from the cache


# ---------------------------------------------------------------- branding: credit at acceptance

def test_suggestion_line_stays_unbranded_and_carries_acceptance_credit(home):
    out = run_hook("prompt", {"session_id": "brand3", "prompt": "ok, wrap"})[1]
    line = out.splitlines()[0]
    # The listing itself must carry NO identity glyph: the user sees this line on every
    # matching prompt, and the mark belongs at acceptance — not at suggestion.
    assert "💡" not in line
    assert line.startswith("summon-skills: likely relevant -> ")
    # The instruction names the mark by description (lightbulb), never by emitting the glyph —
    # and stays TIGHT: this line rides every matching prompt, so its length is a standing
    # context cost. The 0.1.7 trim cut ~30 chars; the old verbose phrasing must stay gone.
    assert line.endswith("credit it: name summon-skills at first mention with its lightbulb.")
    assert "because of this suggestion" not in line


def test_inert_flag_leads_with_the_brand_mark(home, monkeypatch):
    _declare(home, "pyright-lsp", mkt_entry={"lspServers": {"pyright": {"command": "pyright-langserver"}}})
    monkeypatch.setattr(ss, "find_binary", lambda name: None)
    out = run_hook("session", {})[1]
    inert = next(x for x in out.splitlines() if "pyright-lsp" in x)
    assert inert.startswith("💡 summon-skills: pyright-lsp is installed but inert: ")


def test_language_route_note_uses_declared_binary(home, monkeypatch):
    _declare(home, "pyright-lsp", mkt_entry={"lspServers": {"pyright": {"command": "pyright-langserver"}}})
    monkeypatch.setattr(ss, "find_binary", lambda name: None)
    out = run_hook("prompt", {"session_id": "d", "prompt": "refactor utils.py"})[1]
    assert "pyright-lsp is installed but inert" in out and "pyright-langserver" in out


# ---------------------------------------------------------------- matcher

@pytest.mark.parametrize("prompt,expected", [
    ("this test fails with a KeyError", "superpowers:systematic-debugging"),
    ("the build is broken since yesterday", "superpowers:systematic-debugging"),
    ("got a traceback from the script", "superpowers:systematic-debugging"),
    ("exit code 1 from the installer", "superpowers:systematic-debugging"),
    ("let's build a CLI for exporting notes", "superpowers:brainstorming"),
    ("write a plan for the migration", "superpowers:writing-plans"),
    ("let us build a new export feature", "superpowers:brainstorming"),
    ("create a new command for exports", "superpowers:brainstorming"),
    ("we need a new sync plugin", "superpowers:brainstorming"),
    ("add tests for the parser", "superpowers:test-driven-development"),
    ("ok, wrap", "audit-loose-ends:audit-loose-ends"),
    ("let's wrap up the session", "audit-loose-ends:audit-loose-ends"),
    ("build a chart of daily spend", "dataviz"),
    ("transcode these .mov files", "media-ops"),
    ("the Next.js app router page is slow", "vercel:nextjs"),
    ("update Button.tsx to use the new prop", "vercel:react-best-practices"),
    ("double check it before we ship", "superpowers:verification-before-completion"),
])
def test_positive_matches(home, prompt, expected):
    assert expected in names(prompt)


@pytest.mark.parametrize("prompt,unexpected", [
    ("wrap this line in a div", "audit-loose-ends:audit-loose-ends"),
    ("wrap the text at 80 columns", "audit-loose-ends:audit-loose-ends"),
    ("fix the typo in the README", "superpowers:systematic-debugging"),
    ("add error handling to the uploader", "superpowers:systematic-debugging"),
    ("draw the dependency graph", "dataviz"),
    ("the debugger variable name", "superpowers:systematic-debugging"),
    ("rename bugfix-notes.md", "superpowers:systematic-debugging"),
    ("what's the weather like", None),
    ("the new build is green", "superpowers:brainstorming"),
])
def test_negative_matches(home, prompt, unexpected):
    got = names(prompt)
    if unexpected is None:
        assert got == []
    else:
        assert unexpected not in got


@pytest.mark.parametrize("prompt", [
    "write a handoff plan for the free session",
    "plan the work for an unattended overnight run",
    "draft the execution plan for the successor session",
    "write a handover plan so another agent can execute it",
])
def test_execution_plan_pairs_layer_with_base(home, prompt):
    # The layer is ADDITIVE: a route picks only its first installed skill, so the pairing needs the
    # base planning route AND the layer route to fire on the same handoff-shaped prompt.
    home.plugin("lasting-plans", ["plan-airtight"])
    home.sync()
    got = names(prompt)
    assert "superpowers:writing-plans" in got
    assert "lasting-plans:plan-airtight" in got


@pytest.mark.parametrize("prompt", [
    "write a plan for the migration",
    "plan the database schema",
    "hand off the keys to the new admin",
])
def test_execution_plan_layer_not_on_plain_planning(home, prompt):
    home.plugin("lasting-plans", ["plan-airtight"])
    home.sync()
    assert "lasting-plans:plan-airtight" not in names(prompt)


def test_process_before_domain_and_cap(home):
    got = names("let's build a chart page; it fails with a TypeError, add tests, write a plan")
    assert len(got) == 3
    assert "dataviz" not in got  # domain loses to three process routes under the cap


def test_fallback_skill_when_first_missing(home):
    routes, _ = ss.load_routes()
    index = ss.load_index()
    del index["skills"]["superpowers:systematic-debugging"]
    got = [s for s, _ in ss.suggest("it crashes", routes, index)[0]]
    assert got == ["mattpocock-skills:diagnosing-bugs"]


def test_never_suggests_uninstalled_or_human_only(home, tmp_path, monkeypatch):
    routes_file = tmp_path / "r.json"
    routes_file.write_text(json.dumps({"schema": 1, "routes": [
        {"id": "x", "kind": "process", "match": {"words": ["zebra"]},
         "skills": ["human-only:secret", "not:installed"]}]}))
    monkeypatch.setenv("SUMMON_SKILLS_ROUTES", str(routes_file))
    assert names("zebra") == []


def test_long_paste_is_fast_and_bounded(home):
    blob = ("lorem ipsum dolor sit amet " * 2000) + " KeyError"
    routes, _ = ss.load_routes()
    index = ss.load_index()
    t0 = time.perf_counter()
    ss.suggest(blob, routes, index)
    assert time.perf_counter() - t0 < 0.1


# ---------------------------------------------------------------- local override

def test_local_override_replaces_disables_and_reports_bad_regex(home):
    state = Path(os.environ["SUMMON_SKILLS_HOME"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "routes.local.json").write_text(json.dumps({"routes": [
        {"id": "dataviz", "disabled": True},
        {"id": "broken", "match": {"regex": ["(unclosed"]}, "skills": ["dataviz"]},
        {"id": "mine", "kind": "domain", "match": {"words": ["gizmo"]}, "skills": ["media-ops"]}]}))
    routes, errors = ss.load_routes()
    ids = {r["id"] for r in routes}
    assert "dataviz" not in ids and "mine" in ids and "broken" not in ids
    assert any("broken" in e for e in errors)
    assert [s for s, _ in ss.suggest("gizmo", routes, ss.load_index())[0]] == ["media-ops"]


# ---------------------------------------------------------------- hooks

def run_hook(kind, payload):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        code = ss.run_hook(kind, io.StringIO(json.dumps(payload)))
    finally:
        sys.stdout = old
    return code, out.getvalue()


def test_prompt_hook_produces_output_then_dedupes(home):
    code, out = run_hook("prompt", {"session_id": "s1", "prompt": "it crashes on start"})
    assert code == 0
    assert "superpowers:systematic-debugging" in out
    assert len(out) <= 300
    assert run_hook("prompt", {"session_id": "s1", "prompt": "still crashes"})[1] == ""
    assert "systematic-debugging" in run_hook("prompt", {"session_id": "s2", "prompt": "crash"})[1]


def test_prompt_hook_without_session_id(home):
    assert "systematic-debugging" in run_hook("prompt", {"prompt": "it crashes"})[1]


def test_prompt_hook_silent_on_no_match(home):
    assert run_hook("prompt", {"session_id": "s", "prompt": "hello there"}) == (0, "")


def test_lsp_inert_note_once(home, monkeypatch):
    _declare(home, "pyright-lsp", mkt_entry={"lspServers": {"pyright": {"command": "pyright-langserver"}}})
    monkeypatch.setattr(ss, "find_binary", lambda name: None)
    out = run_hook("prompt", {"session_id": "l", "prompt": "refactor utils.py"})[1]
    assert "pyright-lsp is installed but inert" in out
    assert "pyright-lsp" not in run_hook("prompt", {"session_id": "l", "prompt": "edit main.py"})[1]


def test_lsp_silent_when_binary_present(home, monkeypatch):
    _declare(home, "pyright-lsp", mkt_entry={"lspServers": {"pyright": {"command": "pyright-langserver"}}})
    monkeypatch.setattr(ss, "find_binary", lambda name: "/bin/" + name)
    assert "inert" not in run_hook("prompt", {"session_id": "p", "prompt": "edit main.py"})[1]


def test_session_start_line(home, monkeypatch):
    monkeypatch.setattr(ss, "stale_route_targets", lambda *a: [])  # stub plugins are partial
    code, out = run_hook("session", {})
    assert code == 0 and out.startswith("summon-skills:") and len(out) <= 200


def test_session_start_unrouted_note_stays_on_status_line(home, monkeypatch):
    _declare(home, "pyright-lsp", mkt_entry={"lspServers": {"pyright": {"command": "pyright-langserver"}}})
    monkeypatch.setattr(ss, "find_binary", lambda name: None)
    monkeypatch.setattr(ss, "stale_route_targets", lambda *a: [])  # stub plugins are partial
    run_hook("session", {})                                   # records the baseline unrouted count
    home.plugin("extra", ["brand-new"])
    home.sync()
    first, second = run_hook("session", {})[1].rstrip("\n").split("\n")
    assert "unrouted" in first and "inert" not in first
    assert "inert" in second and "unrouted" not in second


@pytest.mark.parametrize("breakage", ["routes", "index", "stdin"])
def test_hooks_fail_open(home, tmp_path, monkeypatch, breakage):
    if breakage == "routes":
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        monkeypatch.setenv("SUMMON_SKILLS_ROUTES", str(bad))
    if breakage == "index":
        monkeypatch.setattr(ss, "load_index", lambda *a, **k: 1 / 0)
    stdin = io.StringIO("{garbage" if breakage == "stdin" else json.dumps({"prompt": "crash"}))
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        assert ss.run_hook("prompt", stdin) == 0
        assert ss.run_hook("session", io.StringIO("")) == 0
    finally:
        sys.stdout = old


def test_off_switch(home, monkeypatch):
    monkeypatch.setenv("SUMMON_SKILLS_OFF", "1")
    assert run_hook("prompt", {"prompt": "it crashes"}) == (0, "")


# ---------------------------------------------------------------- CLI (real subprocess)

def cli(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env=env)


def test_help_prints_usage_without_side_effects(home, tmp_path):
    env = dict(os.environ)
    r = cli("--help", env=env)
    assert r.returncode == 0
    assert "match" in r.stdout and "SUMMON_SKILLS_OFF" in r.stdout
    assert not (Path(env["SUMMON_SKILLS_HOME"]) / "index.json").exists()


def test_unknown_flag_and_no_command_exit_2(home):
    assert cli("--bogus").returncode == 2
    assert cli().returncode == 2


def test_cli_match_map_unrouted_doctor(home, tmp_path):
    env = dict(os.environ)
    assert "systematic-debugging" in cli("match", "it", "crashes", env=env).stdout
    out = tmp_path / "map.md"
    assert cli("map", "--out", str(out), env=env).returncode == 0
    assert "**debugging**" in out.read_text()
    assert "mattpocock-skills:tdd" not in cli("unrouted", env=env).stdout
    r = cli("doctor", env=env)
    assert r.returncode == 0 and "route skills not installed" in r.stdout


def test_hook_via_subprocess(home):
    r = subprocess.run([sys.executable, str(SCRIPT), "hook", "prompt"], capture_output=True,
                       text=True, input=json.dumps({"session_id": "x", "prompt": "fails"}),
                       env=dict(os.environ))
    assert r.returncode == 0 and "systematic-debugging" in r.stdout


# ---- 0.1.9: the prompt hook must never be killed mid-rebuild, forever --------------------
# Incident 2026-11-02: an unrelated settings.json write moved the fingerprint; the rebuild took
# 15-26 s under load; the 5 s hook timeout killed it before index.json was written, so EVERY
# later prompt restarted the same rebuild and timed out again.

def test_fingerprint_ignores_unrelated_settings_writes(home):
    fp1 = ss.fingerprint(home.ch)
    s = json.loads((home.ch / "settings.json").read_text())
    s["env"] = {"SOME_ROUTE": "paid"}          # what a route toggle / permission grant does
    time.sleep(0.01)
    (home.ch / "settings.json").write_text(json.dumps(s))
    assert ss.fingerprint(home.ch) == fp1


def test_fingerprint_moves_when_a_plugin_is_toggled(home):
    fp1 = ss.fingerprint(home.ch)
    s = json.loads((home.ch / "settings.json").read_text())
    s["enabledPlugins"]["vercel@mkt"] = False
    (home.ch / "settings.json").write_text(json.dumps(s))
    assert ss.fingerprint(home.ch) != fp1


def test_hook_serves_a_stale_index_and_reindexes_in_the_background(home, monkeypatch):
    ss.load_index()                                    # a cached index exists
    home.plugin("newcomer", ["fresh-skill"])
    home.sync()                                        # fingerprint now stale
    spawned = []
    monkeypatch.setattr(ss.subprocess, "Popen", lambda argv, **k: spawned.append(argv))
    monkeypatch.setattr(ss, "build_index", lambda ch: (_ for _ in ()).throw(
        AssertionError("rebuilt synchronously inside the hook")))
    ss.hook_prompt({"prompt": "debug this failing test", "session_id": "s1"})
    assert len(spawned) == 1 and spawned[0][-1] == "reindex"


def test_background_reindex_is_spawned_once_per_burst(home, monkeypatch):
    ss.load_index()
    home.plugin("newcomer", ["fresh-skill"])
    home.sync()
    spawned = []
    monkeypatch.setattr(ss.subprocess, "Popen", lambda argv, **k: spawned.append(argv))
    for i in range(5):
        ss.load_index(stale_ok=True)
    assert len(spawned) == 1


def test_first_run_without_a_cache_still_builds(home):
    idx = ss.load_index(stale_ok=True)
    assert "superpowers:systematic-debugging" in idx["skills"]


def test_reindex_command_rebuilds_and_releases_the_lock(home):
    ss.load_index()
    home.plugin("newcomer", ["fresh-skill"])
    home.sync()
    (ss.state_home() / "reindex.lock").write_text("x")
    assert ss.main(["reindex"]) == 0
    assert "newcomer:fresh-skill" in ss._load_json(ss.state_home() / "index.json", {})["skills"]
    assert not (ss.state_home() / "reindex.lock").exists()


# ---------------------------------------------------------------- stale route targets (0.2.7)
def _routes_file(tmp_path, monkeypatch, routes):
    p = tmp_path / "routes.json"
    p.write_text(json.dumps({"schema": 1, "routes": routes}))
    monkeypatch.setenv("SUMMON_SKILLS_ROUTES", str(p))


def test_stale_route_targets_names_installed_plugin_missing_skill(home, tmp_path, monkeypatch):
    _routes_file(tmp_path, monkeypatch, [
        {"id": "renamed", "match": {"words": ["x"]}, "skills": ["superpowers:gone-skill"]},
        {"id": "absent", "match": {"words": ["y"]}, "skills": ["not-installed:anything"]},
        {"id": "live", "match": {"words": ["z"]}, "skills": ["superpowers:brainstorming"]}])
    routes, _ = ss.load_routes()
    assert ss.stale_route_targets(routes, ss.load_index(force=True)) == ["superpowers:gone-skill"]


def test_session_start_line_names_stale_route_targets(home, tmp_path, monkeypatch):
    _routes_file(tmp_path, monkeypatch, [
        {"id": "renamed", "match": {"words": ["x"]}, "skills": ["superpowers:gone-skill"]}])
    line = run_hook("session", {})[1]
    assert "stale route target" in line and "superpowers:gone-skill" in line


def test_renamed_routes_point_at_live_names(home):
    """The three renames found 2026-11-28 point at the CURRENT skill names."""
    routes = {r["id"]: r["skills"] for r in json.loads((ROOT / "rules" / "routes.json").read_text())["routes"]}
    assert routes["local-offload"] == ["free-agents:offload"]
    assert routes["execution-plan-layer"] == ["lasting-plans:plan-airtight"]


@pytest.mark.parametrize("prompt", [
    "write a plan for the multi-step refactor",
    "break this task down into steps before we start",
    "decompose the migration into phases"])
def test_planning_prompts_suggest_offload(home, prompt):
    home.plugin("free-agents", ["offload", "free-session-handover"])
    home.sync()
    out = run_hook("prompt", {"prompt": prompt, "session_id": "s-%d" % abs(hash(prompt))})[1]
    assert "free-agents:offload" in out


def test_handoff_prompt_suggests_free_session_handover(home):
    home.plugin("free-agents", ["offload", "free-session-handover"])
    home.sync()
    out = run_hook("prompt", {"prompt": "hand this over to a successor session overnight",
                              "session_id": "s-handover"})[1]
    assert "free-agents:free-session-handover" in out


@pytest.mark.parametrize("prompt", [
    "help me plan the office party",
    "can you plan for my vacation next month",
    "a multi-step morning routine",
    "book me a free session at the climbing gym",
    "handover of the keys is on friday",
    "hand off the keys to the new admin"])
def test_offload_and_handover_not_on_ordinary_prompts(home, prompt):
    home.plugin("free-agents", ["offload", "free-session-handover"])
    home.sync()
    out = run_hook("prompt", {"prompt": prompt, "session_id": "neg-%d" % abs(hash(prompt))})[1]
    assert "free-agents:offload" not in out and "free-agents:free-session-handover" not in out


def test_shipped_routes_load_without_errors_and_merge_conflict_is_gone():
    routes, errors = ss.load_routes()
    assert errors == []
    assert "merge-conflict" not in {r["id"] for r in routes}


# ---------------------------------------------------------------- Task 2 (0.3.0): knowledge in context

def _write_tp(tmp_path, records):
    d = tmp_path / "tp"
    d.mkdir(exist_ok=True)
    f = d / "s.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return f


def _bash(cmd):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}


def test_known_via_skill_load(home, tmp_path):
    f = _write_tp(tmp_path, [_skill_use("superpowers:systematic-debugging")])
    routes, _ = ss.load_routes()
    known = ss.known_skills({"transcript_path": str(f)}, routes, ss.load_index())
    assert "superpowers:systematic-debugging" in known


def test_known_via_plugin_script_marks_only_plugin_own_skill(home, tmp_path):
    home.plugin("waypoints", ["waypoints", "waypoints-gated"])
    b = home.ch / "plugins" / "cache" / "mkt" / "waypoints" / "1.0.0" / "bin"
    b.mkdir(parents=True, exist_ok=True)
    exe = b / "waypoints.py"
    exe.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    exe.chmod(0o755)
    home.sync()
    f = _write_tp(tmp_path, [_bash("waypoints.py add fix the runners")])
    routes, _ = ss.load_routes()
    known = ss.known_skills({"transcript_path": str(f)}, routes, ss.load_index())
    assert "waypoints:waypoints" in known
    assert "waypoints:waypoints-gated" not in known


def test_known_via_banner(home, tmp_path):
    home.plugin("waypoints", ["waypoints"])
    home.sync()
    rec = {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "SessionStart hook success: 🧭 waypoints: 394 open waypoint(s)"}]}}
    f = tmp_path / "tp" / "b.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    # real transcripts store emoji both raw and \\u-escaped (verified 2026-11-28) — cover both
    f.write_text(json.dumps(rec) + "\n" + json.dumps(rec, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    routes, _ = ss.load_routes()
    known = ss.known_skills({"transcript_path": str(f)}, routes, ss.load_index())
    assert "waypoints:waypoints" in known


def test_compaction_resets_known(home, tmp_path):
    f = _write_tp(tmp_path, [_skill_use("superpowers:brainstorming"), {"type": "compact_boundary"}])
    routes, _ = ss.load_routes()
    assert ss.known_skills({"transcript_path": str(f)}, routes, ss.load_index()) == set()


def test_known_skills_fail_open_and_bounded(home, tmp_path):
    routes, _ = ss.load_routes()
    index = ss.load_index()
    assert ss.known_skills({"transcript_path": "/nonexistent/x.jsonl"}, routes, index) == set()
    assert ss.known_skills({}, routes, index) == set()
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{broken\n" + json.dumps({"type": "assistant"}) + "\n", encoding="utf-8")
    assert ss.known_skills({"transcript_path": str(bad)}, routes, index) == set()
    big = tmp_path / "big.jsonl"
    line = json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "working on it " * 20}]}}) + "\n"
    big.write_text(line * 50000, encoding="utf-8")
    t0 = time.perf_counter()
    known = ss.known_skills({"transcript_path": str(big)}, routes, index)
    dt = time.perf_counter() - t0
    assert isinstance(known, set)
    assert dt < 1.5, dt   # Review Focus 1: a huge transcript must not blow the hook budget


def test_hook_prompt_skips_script_known_skill(home, tmp_path):
    home.plugin("waypoints", ["waypoints"])
    b = home.ch / "plugins" / "cache" / "mkt" / "waypoints" / "1.0.0" / "bin"
    b.mkdir(parents=True, exist_ok=True)
    exe = b / "waypoints.py"
    exe.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    exe.chmod(0o755)
    home.sync()
    f = _write_tp(tmp_path, [_bash("waypoints.py list")])
    payload = {"session_id": "known-1", "prompt": "add a waypoint to fix the runners",
               "transcript_path": str(f), "cwd": str(home.ch)}
    out = run_hook("prompt", payload)[1]
    assert "waypoints:waypoints" not in out, out


# ---------------------------------------------------------------- Task 3 (0.3.0): familiarity cache + habit weight

def test_habit_weight_curve():
    assert ss.habit_weight(0) == 1.0
    assert ss.habit_weight(5) == 0.5
    assert ss.habit_weight(15) == 0.25
    assert ss.habit_weight(40) == 0.15     # floor
    assert ss.habit_weight(1000) == 0.15   # floor, never a hard cutoff
    prev = 2.0
    for u in range(101):                    # monotone non-increasing
        w = ss.habit_weight(u)
        assert w <= prev + 1e-12
        prev = w


def test_refresh_bounded(home, tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMON_SKILLS_TRANSCRIPTS", str(tmp_path / "projects"))
    d = tmp_path / "projects" / "p"
    d.mkdir(parents=True)
    line = json.dumps(_skill_use("superpowers:systematic-debugging")) + "\n"
    for i in range(300):
        (d / ("s%03d.jsonl" % i)).write_text(line, encoding="utf-8")
    n = ss.refresh_familiarity(max_files=200, budget_s=30.0)
    assert n <= 200
    assert (ss.state_home() / "familiarity.json").is_file()
    assert ss.familiarity_uses().get("superpowers:systematic-debugging", 0) > 0


def test_refresh_atomic_and_corrupt_tolerant(home, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv("SUMMON_SKILLS_TRANSCRIPTS", str(tmp_path / "projects"))
    d = tmp_path / "projects" / "p"
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text(json.dumps(_skill_use("superpowers:brainstorming")) + "\n",
                                encoding="utf-8")
    cache = ss.state_home() / "familiarity.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("garbage{{{", encoding="utf-8")
    assert ss.familiarity_uses() == {}      # corrupt cache reads as empty
    with ThreadPoolExecutor(max_workers=2) as pool:   # Review Focus 3: concurrent refreshes
        list(pool.map(lambda _: ss.refresh_familiarity(budget_s=30.0), range(2)))
    json.loads(cache.read_text(encoding="utf-8"))      # must parse after the race


def test_old_entries_dropped(home, tmp_path, monkeypatch):
    monkeypatch.setenv("SUMMON_SKILLS_TRANSCRIPTS", str(tmp_path / "projects"))
    (tmp_path / "projects").mkdir(parents=True)
    cache = ss.state_home() / "familiarity.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    old = time.strftime("%Y-%m-%d", time.localtime(time.time() - 40 * 86400))
    cache.write_text(json.dumps({"superpowers:brainstorming": {"old-sess": [old, 1.0]},
                                 "_meta": {"last_scan_mtime": 0.0}}), encoding="utf-8")
    ss.refresh_familiarity(budget_s=30.0)
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert "superpowers:brainstorming" not in data      # > 30 days old -> dropped on write
