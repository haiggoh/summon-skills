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
    monkeypatch.setattr(ss, "find_binary", lambda name: "/bin/" + name)
    assert "inert" not in run_hook("session", {})[1]                      # checked live, not from the cache


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


def test_session_start_line(home):
    code, out = run_hook("session", {})
    assert code == 0 and out.startswith("summon-skills:") and len(out) <= 200


def test_session_start_unrouted_note_stays_on_status_line(home, monkeypatch):
    _declare(home, "pyright-lsp", mkt_entry={"lspServers": {"pyright": {"command": "pyright-langserver"}}})
    monkeypatch.setattr(ss, "find_binary", lambda name: None)
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
