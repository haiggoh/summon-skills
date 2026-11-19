#!/usr/bin/env python3
"""summon-skills: suggest already-installed skills that fit the prompt at hand.

Local and deterministic: no model calls, no network, and skill bodies are never
read. The installed-skill index is rebuilt only when its fingerprint changes.
Python 3.9 compatible (hooks may run under macOS /usr/bin/python3).
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

def _manifest_version():
    """Derived, never hardcoded: the manifest next to the package root is the single source
    of truth (host: bin/../.claude-plugin/plugin.json; portable copy: scripts/../…). A
    hardcoded VERSION constant drifted on every bump (shipped 0.2.2 while --version
    printed 0.2.1 — caught by test_portable_cli_runs_from_portable_root)."""
    here = Path(__file__).resolve().parent
    return json.loads((here / ".." / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]


VERSION = _manifest_version()
SCHEMA = 1
DEFAULT_MAX = 3
SCAN_CHARS = 20000          # long pastes: only the head is scanned
STATE_TTL = 2 * 86400       # per-session dedupe files older than this are pruned
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.cargo/bin",
              "~/.npm-global/bin", "/opt/homebrew/opt/llvm/bin"]

HELP_EPILOG = """\
environment:
  SUMMON_SKILLS_HOME        state dir (index, local routes, map); default ~/.claude/summon-skills
  SUMMON_SKILLS_CLAUDE_HOME Claude config dir to scan; default ~/.claude
  SUMMON_SKILLS_ROUTES      default routes file; default <plugin>/rules/routes.json
  SUMMON_SKILLS_MAX         max skills per prompt (default 3)
  SUMMON_SKILLS_OFF=1       silence both hooks
  SUMMON_SKILLS_DEBUG=1     log hook exceptions to $SUMMON_SKILLS_HOME/debug.log
"""


# ---------------------------------------------------------------- paths

def claude_home():
    return Path(os.environ.get("SUMMON_SKILLS_CLAUDE_HOME", "~/.claude")).expanduser()


def state_home():
    env = os.environ.get("SUMMON_SKILLS_HOME")
    return Path(env).expanduser() if env else claude_home() / "summon-skills"


def plugin_root():
    return Path(__file__).resolve().parent.parent


def default_routes_path():
    env = os.environ.get("SUMMON_SKILLS_ROUTES")
    return Path(env).expanduser() if env else plugin_root() / "rules" / "routes.json"


def session_dir():
    return Path(tempfile.gettempdir()) / "summon-skills"


# ---------------------------------------------------------------- index

def parse_frontmatter(path):
    """Return the YAML frontmatter's top-level scalar keys (name, flags) only."""
    out = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            if fh.readline().strip() != "---":
                return out
            for _ in range(60):
                line = fh.readline()
                if not line or line.strip() == "---":
                    break
                m = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
                if m:
                    out[m.group(1)] = m.group(2).strip().strip("'\"")
    except OSError:
        pass
    return out


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def enabled_plugins(ch):
    """[(plugin_name, install_path)] for installed plugins not disabled in settings."""
    registry = _load_json(ch / "plugins" / "installed_plugins.json", {}).get("plugins", {})
    flags = _load_json(ch / "settings.json", {}).get("enabledPlugins", {})
    out = []
    for key, entries in sorted(registry.items()):
        if flags.get(key) is False or not entries:
            continue
        paths = [e.get("installPath") for e in entries if e.get("installPath")]
        if paths:
            out.append((key.split("@", 1)[0], Path(paths[-1])))
    return out


def plugin_skill_files(root):
    """SKILL.md files from skills/ plus any manifest `skills` paths (which supplement it)."""
    found = set(root.glob("skills/*/SKILL.md"))
    extra = _load_json(root / ".claude-plugin" / "plugin.json", {}).get("skills", [])
    for rel in [extra] if isinstance(extra, str) else extra:
        path = (root / rel).resolve()
        if (path / "SKILL.md").is_file():
            found.add(path / "SKILL.md")
        else:
            found.update(path.glob("*/SKILL.md"))
    return sorted(found)


def _is_true(value):
    return str(value).lower() in ("true", "yes", "1")


def discover_skills(ch):
    """{skill_name: {"invocable": bool}} over user skills and enabled plugins."""
    skills = {}

    def add(name, fm):
        skills[name] = {"invocable": not _is_true(fm.get("disable-model-invocation", ""))}

    for md in sorted((ch / "skills").glob("*/SKILL.md")):
        fm = parse_frontmatter(md)
        add(fm.get("name") or md.parent.name, fm)
    for plugin, root in enabled_plugins(ch):
        for md in plugin_skill_files(root):
            fm = parse_frontmatter(md)
            add("%s:%s" % (plugin, fm.get("name") or md.parent.name), fm)
        for md in sorted(root.glob("commands/*.md")):
            add("%s:%s" % (plugin, md.stem), parse_frontmatter(md))
    return skills


def fingerprint(ch):
    """A stat-only digest; changes when plugins/skills are added, removed or toggled.

    settings.json enters by the CONTENT of its enabledPlugins section only — never by mtime:
    the file is rewritten by unrelated writers (env/route toggles, permission grants, other
    sessions' hooks), and each such write used to force a full rebuild (incident 2026-11-02)."""
    h = hashlib.sha1(("%s;%s;" % (VERSION, SCHEMA)).encode())
    flags = _load_json(ch / "settings.json", {}).get("enabledPlugins", {})
    h.update(("enabledPlugins:%s;" % json.dumps(flags, sort_keys=True)).encode())
    files = [ch / "plugins" / "installed_plugins.json", ch / "skills"]
    files += sorted((ch / "plugins" / "marketplaces").glob("*/.claude-plugin/marketplace.json"))
    files += sorted((ch / "skills").glob("*"))
    for path in files:
        try:
            h.update(("%s:%d;" % (path, path.stat().st_mtime_ns)).encode())
        except OSError:
            h.update(("%s:-;" % path).encode())
    return h.hexdigest()


def find_binary(name):
    extra = os.pathsep.join(str(Path(p).expanduser()) for p in EXTRA_PATH)
    return shutil.which(name, path=os.environ.get("PATH", "") + os.pathsep + extra)


INSTALL_HINTS = {
    "pyright-langserver": "pipx install pyright",
    "typescript-language-server": "npm install -g typescript-language-server typescript",
    "intelephense": "npm install -g intelephense",
    "rust-analyzer": "rustup component add rust-analyzer (or brew install rust-analyzer)",
    "gopls": "go install golang.org/x/tools/gopls@latest",
    "clangd": "brew install llvm", "sourcekit-lsp": "install Xcode or brew install swift",
    "lua-language-server": "brew install lua-language-server", "ruby-lsp": "gem install ruby-lsp",
    "csharp-ls": "dotnet tool install --global csharp-ls", "jdtls": "brew install jdtls",
    "kotlin-lsp": "brew install JetBrains/utils/kotlin-lsp",
    "bun": "brew install oven-sh/bun/bun", "npx": "brew install node", "node": "brew install node",
    "uvx": "brew install uv", "uv": "brew install uv", "docker": "install Docker Desktop",
    "deno": "brew install deno", "python3": "brew install python",
}
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-[^}]*)?\}")


def _marketplace_entries(ch):
    """{plugin_name: marketplace entry} -- some plugins (the *-lsp ones) declare servers only there."""
    out = {}
    for mfile in (ch / "plugins" / "marketplaces").glob("*/.claude-plugin/marketplace.json"):
        for entry in _load_json(mfile, {}).get("plugins", []):
            if isinstance(entry, dict) and entry.get("name"):
                out.setdefault(entry["name"], entry)
    return out


def _servers(block):
    """Normalise {"mcpServers": {...}} or a bare {name: spec} to a list of spec dicts."""
    if not isinstance(block, dict):
        return []
    inner = block.get("mcpServers", block)
    return [v for v in inner.values() if isinstance(v, dict)] if isinstance(inner, dict) else []


def declared_deps(ch):
    """{plugin: {"commands": [...], "env": [...]}} from each enabled plugin's own lsp/mcp declarations.

    Only external requirements are kept: a command under ${CLAUDE_PLUGIN_ROOT} ships with the
    plugin, and ${VAR:-default} references are optional, so neither can make a plugin inert.
    """
    market = _marketplace_entries(ch)
    deps = {}
    for plugin, root in enabled_plugins(ch):
        manifest = _load_json(root / ".claude-plugin" / "plugin.json", {})
        specs = _servers(manifest.get("lspServers")) + _servers(market.get(plugin, {}).get("lspServers"))
        specs += _servers(manifest.get("mcpServers")) + _servers(_load_json(root / ".mcp.json", {}))
        commands, env = [], []
        for spec in specs:
            cmd = spec.get("command")
            if isinstance(cmd, str) and cmd and "${CLAUDE_PLUGIN_ROOT}" not in cmd \
                    and not cmd.startswith((".", "/")) and cmd not in commands:
                commands.append(cmd)
            text = json.dumps({k: spec.get(k) for k in ("url", "headers", "env", "args")})
            for name, default in _ENV_REF.findall(text):
                if not default and name != "CLAUDE_PLUGIN_ROOT" and name not in env:
                    env.append(name)
        if commands or env:
            deps[plugin] = {"commands": commands, "env": env}
    return deps


def inert_plugins(index):
    """Enabled plugins whose declared external command or required env var is missing right now.

    Checked live on every call (cheap: a few PATH lookups), so installing the missing tool
    clears the warning immediately, without waiting for an index rebuild.
    """
    out = []
    for plugin, dep in sorted(index.get("deps", {}).items()):
        missing = [c for c in dep.get("commands", []) if not find_binary(c)]
        missing += ["$" + v for v in dep.get("env", []) if not os.environ.get(v)]
        if missing:
            hints = [INSTALL_HINTS[m] for m in missing if m in INSTALL_HINTS]
            out.append({"plugin": plugin, "missing": missing, "hint": "; ".join(hints)})
    return out


# What each missing command DOES and why it matters, so the note reads as an offer,
# not a side note (user 2026-11-19: "jdtls-lsp installed but inert" read as unimportant).
TOOL_PURPOSE = {
    "jdtls": "Java language server (Eclipse JDT.LS): real diagnostics, go-to-definition and refactoring for .java files — without it, Java edits get no editor intelligence",
    "pyright-langserver": "Python language server: type-checking and navigation for .py files",
    "typescript-language-server": "TypeScript language server: type errors and navigation for .ts/.tsx/.js files",
    "intelephense": "PHP language server: diagnostics and navigation for .php files",
    "rust-analyzer": "Rust language server: type errors and navigation for .rs files",
    "gopls": "Go language server: type errors and navigation for .go files",
    "clangd": "C/C++ language server: diagnostics and navigation for C/C++/ObjC files",
    "sourcekit-lsp": "Swift language server: diagnostics and navigation for .swift files",
    "csharp-ls": "C# language server: diagnostics and navigation for .cs files",
    "kotlin-lsp": "Kotlin language server: diagnostics and navigation for .kt files",
    "lua-language-server": "Lua language server: diagnostics and navigation for .lua files",
    "ruby-lsp": "Ruby language server: diagnostics and navigation for .rb files",
}


def describe_inert(problem):
    missing = ", ".join(problem["missing"])
    text = "%s is installed but inert: %s not found on PATH" % (problem["plugin"], missing)
    for m in problem["missing"]:
        if m in TOOL_PURPOSE:
            text += ". %s does %s" % (m, TOOL_PURPOSE[m])
            break
    text += " — until installed, that plugin gives no editor feedback for its language."
    if problem["hint"]:
        text += " One-time install: %s" % problem["hint"]
    return text


def build_index(ch):
    return {"schema": SCHEMA, "fingerprint": fingerprint(ch), "built_at": int(time.time()),
            "skills": discover_skills(ch),
            "plugins": sorted(name for name, _ in enabled_plugins(ch)),
            "deps": declared_deps(ch)}


REINDEX_LOCK_TTL = 300      # a lock older than this is a crashed reindex, not a running one


def _spawn_reindex():
    """Start ONE detached `reindex` (lock-guarded), so a hook never rebuilds inline."""
    lock = state_home() / "reindex.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            if time.time() - lock.stat().st_mtime < REINDEX_LOCK_TTL:
                return False                     # one is already running
            lock.unlink()                        # stale: its owner died
        except FileNotFoundError:
            pass
        os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        return False                             # lost the race, or no writable state dir
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "reindex"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def load_index(ch=None, force=False, stale_ok=False):
    """Return the cached index, rebuilding it only when the fingerprint moved.

    stale_ok (the hooks): a stale cached index is returned AT ONCE and the rebuild runs in a
    detached process. A rebuild can take longer than the hook timeout under load; inline, it
    was killed before index.json was written, so every later prompt restarted it (2026-11-02).
    With no cached index at all there is nothing to serve, so the first build stays inline."""
    ch = ch or claude_home()
    path = state_home() / "index.json"
    cached = _load_json(path, None)
    if not force and cached and cached.get("schema") == SCHEMA:
        if cached.get("fingerprint") == fingerprint(ch):
            return cached
        if stale_ok:
            _spawn_reindex()
            return cached
    index = build_index(ch)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return index


# ---------------------------------------------------------------- routes

def _phrase_re(phrase):
    return r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])"


def compile_route(route):
    """Attach a compiled `_re` (match) and `_ex` (exclude); raises re.error on bad input."""
    m = route.get("match", {})
    parts = [_phrase_re(w) for w in m.get("words", [])]
    parts += ["(?:%s)" % r for r in m.get("regex", [])]
    parts += [r"\.%s\b" % re.escape(e.lstrip(".")) for e in m.get("ext", [])]
    route["_re"] = re.compile("|".join(parts), re.I) if parts else None
    # cheap pre-check: a words-only route cannot match unless one word occurs as a substring
    route["_needles"] = None if (m.get("regex") or m.get("ext")) else \
        [w.lower() for w in m.get("words", [])]
    ex = [_phrase_re(w) for w in route.get("exclude", [])]
    route["_ex"] = re.compile("|".join(ex), re.I) if ex else None
    return route


def load_routes(defaults=None, local=None):
    """Return (routes, errors). Local routes replace defaults by id; disabled drops."""
    defaults = defaults or default_routes_path()
    local = local or (state_home() / "routes.local.json")
    merged, order, errors, builtins = {}, [], [], set()
    for source in (defaults, local):
        data = _load_json(source, None)
        if data is None:
            if Path(source).exists():
                errors.append("%s: not valid JSON" % source)
            continue
        builtins.update(data.get("builtin_skills", []))
        for route in data.get("routes", []):
            rid = route.get("id")
            if not rid:
                errors.append("%s: route without id" % source)
                continue
            if rid not in merged:
                order.append(rid)
            merged[rid] = route
    routes = []
    for rid in order:
        route = merged[rid]
        if route.get("disabled"):
            continue
        try:
            compiled = compile_route(dict(route))
            compiled["_builtin"] = builtins
            routes.append(compiled)
        except re.error as exc:
            errors.append("route %s: bad regex (%s)" % (rid, exc))
    return routes, errors


def matching_routes(text, routes):
    text = text[:SCAN_CHARS]
    low = text.lower()
    hits = []
    for route in routes:
        needles = route.get("_needles")
        if needles is not None and not any(n in low for n in needles):
            continue
        if route["_re"] is None or not route["_re"].search(text):
            continue
        if route["_ex"] is not None and route["_ex"].search(text):
            continue
        hits.append(route)
    return hits


def suggest(text, routes, index, shown=(), cap=DEFAULT_MAX):
    """Return (skills, notes): skills=[(skill, route_id)] process-first, capped, deduped."""
    installed = index.get("skills", {})
    plugins = set(index.get("plugins", []))
    picked, notes, seen = [], [], set(shown)
    hits = matching_routes(text, routes)
    hits.sort(key=lambda r: 0 if r.get("kind") == "process" else 1)
    for route in hits:
        for skill in route.get("skills", []):
            info = installed.get(skill) or (
                {"invocable": True} if skill in route.get("_builtin", ()) else None)
            if info and info.get("invocable"):
                if skill not in seen:
                    picked.append((skill, route["id"]))
                    seen.add(skill)
                break
        lsp = route.get("lsp")
        key = "lsp:%s" % (lsp or {}).get("plugin")
        if lsp and lsp.get("plugin") in plugins and key not in seen:
            dep = index.get("deps", {}).get(lsp["plugin"])
            if dep:
                problems = inert_plugins({"deps": {lsp["plugin"]: dep}})
                if problems:
                    notes.append(describe_inert(problems[0]))
            seen.add(key)
    return picked[:cap], notes


def mentioned_skills(routes):
    return {s for r in routes for s in r.get("skills", [])}


# ---------------------------------------------------------------- audit / replay
# User scope 2026-11-13 (HANDOFF §6b step 1): measure BEFORE tuning routes. audit scans
# transcripts for suggestion events and classifies each outcome per route (loaded / visible /
# thinking-only / silent); replay re-runs suggest() over a labelled corpus. Read-only.

SUGGEST_RE = re.compile(r"summon-skills: likely relevant -> ([^\s(]+) \(([^)]+)\)")


def _transcript_files(since_days, max_files):
    root = os.environ.get("SUMMON_SKILLS_TRANSCRIPTS") or os.path.expanduser("~/.claude/projects")
    paths = []
    for pat in ("*/*.jsonl", "*.jsonl"):
        paths.extend(glob.glob(os.path.join(root, pat)))
    try:
        paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    except OSError:
        pass
    if since_days:
        cutoff = time.time() - since_days * 86400
        paths = [p for p in paths if os.path.getmtime(p) >= cutoff]
    return paths[:max_files]


def _classify_outcome(records_after, skill):
    """loaded (a later Skill tool_use with this skill) > visible (a later assistant TEXT
    block naming it — thinking blocks don't count) > thinking-only > silent."""
    short = skill.split(":")[-1]
    loaded = visible = thinking = False
    for rec in records_after:
        msg = rec.get("message") or {}
        # Only ASSISTANT text counts as a visible answer — the UserPromptSubmit hook's own
        # suggestion line is persisted as a USER text block naming the skill, and a role-blind
        # match would read it as an answer (review round 2: this made audit's visible count
        # wrong and would have made the step-3 barrier clear itself).
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Skill" \
                    and (block.get("input") or {}).get("skill") == skill:
                loaded = True
            elif block.get("type") == "text" and short in (block.get("text") or ""):
                visible = True
            elif block.get("type") == "thinking" and short in (block.get("thinking") or ""):
                thinking = True
    if loaded:
        return "loaded"
    if visible:
        return "visible"
    if thinking:
        return "thinking"
    return "silent"


def cmd_audit(args):
    """Scan transcripts for suggestion events; classify each outcome per route."""
    routes, _errors = load_routes()
    per_route = {r["id"]: {"route": r["id"], "fired": 0, "loaded": 0, "visible": 0,
                            "thinking": 0, "silent": 0} for r in routes}
    total = 0
    files = _transcript_files(args.since_days, args.max_files)
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            continue
        records = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                records.append(json.loads(ln))
            except ValueError:
                records.append({})
        for i, ln in enumerate(lines):
            for m in SUGGEST_RE.finditer(ln):
                skill, route_id = m.group(1), m.group(2)
                if route_id not in per_route:
                    continue
                outcome = _classify_outcome(records[i + 1:], skill)
                per_route[route_id]["fired"] += 1
                per_route[route_id][outcome] += 1
                total += 1
    denies = 0
    for path in _transcript_files(args.since_days, args.max_files):
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                denies += sum(1 for ln in f if "denied once" in ln and "summon-skills" in ln)
        except OSError:
            pass
    if args.json:
        print(json.dumps({"total_fired": total, "denies": denies,
                          "routes": [per_route[r] for r in sorted(per_route)
                                     if per_route[r]["fired"]]}))
        return 0
    print("route                    fired  loaded  visible  thinking  silent")
    for rid in sorted(per_route):
        r = per_route[rid]
        if not r["fired"]:
            continue
        print("%-24s %5d  %5d  %6d  %7d  %5d" % (rid, r["fired"], r["loaded"], r["visible"],
                                                 r["thinking"], r["silent"]))
    fired = sum(r["fired"] for r in per_route.values())
    if fired:
        loaded = sum(r["loaded"] for r in per_route.values())
        visible = sum(r["visible"] for r in per_route.values())
        thinking = sum(r["thinking"] for r in per_route.values())
        print("total %d fired (%d denies): loaded %.0f%% · visible %.0f%% · thinking-only %.0f%% · silent %.0f%%"
              % (fired, denies, 100.0 * loaded / fired, 100.0 * visible / fired,
                 100.0 * thinking / fired, 100.0 * (fired - loaded - visible - thinking) / fired))
    else:
        print("no suggestion events found (scanned %d file(s))" % len(files))
    return 0


def cmd_replay(args):
    """Re-run suggest() over a labelled corpus (jsonl lines: {"prompt":…, "expect":…})."""
    routes, _errors = load_routes()
    index = load_index(stale_ok=True)
    lines = hits = 0
    details = []
    try:
        with open(args.corpus, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                rec = json.loads(ln)
                skills, _notes = suggest(rec.get("prompt") or "", routes, index, shown=set(),
                                         cap=DEFAULT_MAX)
                fired = [rid for _s, rid in skills]
                expect = rec.get("expect")
                hit = bool(expect) and expect in fired
                lines += 1
                hits += 1 if hit else 0
                details.append({"prompt": rec.get("prompt", "")[:80], "fired": fired,
                                "expect": expect, "hit": hit})
    except OSError as e:
        print("replay: cannot read corpus: %s" % e, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"lines": lines, "hits": hits, "details": details}))
    else:
        for d in details:
            print("%-6s expect=%-18s fired=%s  %s" % ("HIT" if d["hit"] else "miss",
                                                    d["expect"] or "-", ",".join(d["fired"]) or "-",
                                                    d["prompt"]))
        print("replay: %d/%d corpus lines hit their expected route" % (hits, lines))
    return 0


# ---------------------------------------------------------------- session state

def _state_file(session_id):
    safe = re.sub(r"[^\w.-]", "_", session_id or "nosession")[:80]
    return session_dir() / (safe + ".json")


def load_shown(session_id):
    return set(_load_json(_state_file(session_id), []))


# §6b step 3 barrier state — a SEPARATE file so the shown-set schema stays pinned by its tests.
def _barrier_file(session_id):
    safe = re.sub(r"[^\w.-]", "_", session_id or "nosession")[:80]
    return session_dir() / (safe + ".barrier.json")


def _load_barrier(session_id):
    st = _load_json(_barrier_file(session_id), {})
    return st if isinstance(st, dict) else {}


def _save_barrier(session_id, st):
    p = _barrier_file(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st), encoding="utf-8")


def _is_compact_boundary(rec):
    """Both harnesses discard earlier context at compaction."""
    return (rec.get("type") in ("compact_boundary", "compacted")
            or (rec.get("type") == "event_msg"
                and isinstance(rec.get("payload"), dict)
                and rec["payload"].get("type") == "context_compacted"))


def _assistant_text_blocks(rec):
    """Visible assistant text only: Claude messages and native Codex response items."""
    msg = rec.get("message")
    if rec.get("type") == "response_item":
        msg = rec.get("payload")
        if not isinstance(msg, dict) or msg.get("type") != "message":
            return []
        if msg.get("phase") not in (None, "commentary", "final_answer"):
            return []
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return []
    return [b.get("text") or "" for b in msg.get("content") or []
            if isinstance(b, dict) and b.get("type") in ("text", "output_text")]


def _visible_mentions(payload, pending):
    """Which pending skills appear in a post-compact assistant TEXT block (thinking blocks
    don't count — same rule as the audit's visible classifier). Fail OPEN to empty: an
    unreadable transcript never proves an answer."""
    shorts = {}
    for s in pending:
        shorts.setdefault(s.split(":")[-1], s)
        shorts.setdefault(s, s)
    tp = (payload.get("transcript_path") or "").strip()
    if not tp or not os.path.isfile(tp):
        return set()
    found = set()
    try:
        with open(tp, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if _is_compact_boundary(rec):
                    found = set()
                    continue
                for text in _assistant_text_blocks(rec):
                    for short, full in shorts.items():
                        if short in text:
                            found.add(full)
    except OSError:
        pass
    return found


def _loaded_skills_from_transcript(payload):
    """Skill names already invoked via the Skill tool in this session's transcript, counting
    only tool_uses AFTER the last compact_boundary (a compacted-away skill may need the
    reminder again). Fail OPEN: a missing/unreadable transcript returns an empty set, i.e.
    the pre-fix behaviour (waypoint summon-skills-don-t-suggest-a)."""
    tp = (payload.get("transcript_path") or "").strip()
    if not tp or not os.path.isfile(tp):
        return set()
    loaded = set()
    try:
        with open(tp, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if _is_compact_boundary(rec):
                    loaded = set()
                    continue
                for block in (rec.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" \
                            and block.get("name") == "Skill":
                        sk = (block.get("input") or {}).get("skill")
                        if sk:
                            loaded.add(sk)
    except OSError:
        return set()
    return loaded


def save_shown(session_id, shown):
    path = _state_file(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(shown)), encoding="utf-8")


def prune_sessions(now=None):
    now = now or time.time()
    for path in session_dir().glob("*.json"):
        try:
            if now - path.stat().st_mtime > STATE_TTL:
                path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- hooks

def _cap():
    try:
        return max(1, int(os.environ.get("SUMMON_SKILLS_MAX", DEFAULT_MAX)))
    except ValueError:
        return DEFAULT_MAX


def _cwd_is_linked_worktree(payload):
    """True when the payload's cwd is already a linked worktree (.git is a FILE pointing at
    the real gitdir) — the worktree skill's advice is already satisfied there, so suggesting
    it again is pure noise (measured: 96 fires, 0 loads on this route)."""
    cwd = (payload.get("cwd") or "").strip()
    return bool(cwd) and os.path.isfile(os.path.join(cwd, ".git"))


def hook_prompt(payload):
    """Return the one-line nudge for this prompt, or '' when nothing new matches."""
    session_id = payload.get("session_id") or "nosession"
    routes, _ = load_routes()
    if _cwd_is_linked_worktree(payload):
        routes = [r for r in routes if r.get("id") != "worktree"]
    index = load_index(stale_ok=True)
    shown = load_shown(session_id) | _loaded_skills_from_transcript(payload)
    skills, notes = suggest(payload.get("prompt") or "", routes, index, shown, _cap())
    if not skills and not notes:
        return ""
    shown.update(s for s, _ in skills)
    shown.update("lsp:" + n.split(" ", 1)[0] for n in notes)
    save_shown(session_id, shown)
    if skills:
        # §6b step 3: record the pending suggestions for the PreToolUse deny-once barrier.
        # Each new suggestion set gets a fresh denied flag (one deny per suggestion batch).
        _save_barrier(session_id, {"pending": {s: r for s, r in skills}, "denied": False})
    parts = []
    if skills:
        parts.append("summon-skills: likely relevant -> " +
                     ", ".join("%s (%s)" % (s, r) for s, r in skills) +
                     " -- invoke before the first tool call, or say in your VISIBLE reply "
                     "why not (thinking-only doesn't count). If you load one"
                     ", credit it: name summon-skills at first mention with its lightbulb.")
    parts += ["summon-skills: " + n for n in notes]
    return "\n".join(parts)


def hook_pretool(payload):
    """§6b step 3 — deny ONCE the first non-Skill tool call after a suggestion that was
    neither loaded (Skill tool_use) nor answered in visible text (assistant text naming it;
    thinking doesn't count). Then fail OPEN. Skill calls themselves are exempt. The deny
    reason carries "summon-skills" + "denied once" so `audit` counts barrier denials.
    Never raises, never wrongly blocks: any error — or a missing/unreadable transcript, from
    which an answer cannot be proven either way — returns '' (allow)."""
    try:
        return _hook_pretool(payload)
    except Exception:
        return ""  # fail open: a hook must never wrongly block on a bug


def _hook_pretool(payload):
    if not isinstance(payload, dict):
        return ""
    session_id = payload.get("session_id") or "nosession"
    st = _load_barrier(session_id)
    pending = dict(st.get("pending") or {})
    if not pending or not isinstance(pending, dict):
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if (payload.get("tool_name") or "") == "Skill":
        sk = str(tool_input.get("skill") or "")
        if sk in pending:
            pending.pop(sk)
            st["pending"] = pending
            _save_barrier(session_id, st)
        return ""
    tp = (payload.get("transcript_path") or "").strip()
    if not tp or not os.path.isfile(tp):
        return ""  # cannot prove load/answer either way -> fail open WITHOUT denying
    loaded = _loaded_skills_from_transcript(payload)
    visible = _visible_mentions(payload, pending)
    for sk in list(pending):
        if sk in loaded or sk in visible:
            pending.pop(sk)
    if not pending:
        st["pending"] = {}
        _save_barrier(session_id, st)
        return ""
    # Deny at most once per suggestion batch — claimed ATOMICALLY: two parallel tool calls
    # in one block run two hook processes; a plain read-modify-write of a flag can let both
    # read denied=false and both deny. The O_EXCL claim file is the single-writer arbiter;
    # named *.json so prune_sessions' TTL glob covers it.
    claim = _barrier_file(session_id).with_name(
        _barrier_file(session_id).name.replace(".barrier.json", ".deny.json"))
    try:
        fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return ""  # already denied once for this batch -> fail open
    skill = next(iter(pending))
    st["denied"] = True
    try:
        _save_barrier(session_id, st)
    except OSError:
        pass  # the claim file already guarantees the once-only invariant
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "summon-skills: '%s' was suggested for this task and is neither loaded nor "
            "answered in your visible reply — this call is denied once. Invoke the skill "
            "(or say in your visible reply why not), then re-issue this call; the barrier "
            "fails open after one deny." % skill)}})


def hook_session_start(payload):
    prune_sessions()
    routes, errors = load_routes()
    # Synchronous on purpose: announcing newly installed skills is this hook's job. If it is
    # killed mid-build, the next prompt hook (stale_ok) finishes the build in the background.
    index = load_index()
    installed = index.get("skills", {})
    unrouted = unrouted_skills(routes, index)
    line = ("summon-skills: %d routes over %d installed skills -- matching ones are suggested "
            "per prompt; name a skill per phase before the first tool call."
            % (len(routes), len(installed)))
    if errors:
        line += " (%d route errors: summon-skills.py doctor)" % len(errors)
    marker = state_home() / "unrouted.count"
    try:
        previous = int(marker.read_text())
    except (OSError, ValueError):
        previous = None
    if unrouted and previous is not None and len(unrouted) > previous:
        line += " (%d unrouted skills: summon-skills.py unrouted)" % len(unrouted)
    try:
        marker.write_text(str(len(unrouted)))
    except OSError:
        pass
    inert = inert_plugins(index)
    if inert:
        line += "\n💡 summon-skills: " + "; ".join(describe_inert(p) for p in inert) + \
                " -- tell the user once; installing is their call."
    return line


def unrouted_skills(routes, index):
    covered = mentioned_skills(routes)
    return sorted(s for s, i in index.get("skills", {}).items()
                  if i.get("invocable") and s not in covered)


def run_hook(kind, stdin):
    """Entry point for hooks: never raises, never blocks, exit code always 0."""
    if os.environ.get("SUMMON_SKILLS_OFF") == "1":
        return 0
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if kind == "prompt":
            out = hook_prompt(payload)
        elif kind == "pretool":
            out = hook_pretool(payload)
        else:
            out = hook_session_start(payload)
        if out:
            sys.stdout.write(out + "\n")
    except Exception as exc:  # fail open by design
        if os.environ.get("SUMMON_SKILLS_DEBUG") == "1":
            try:
                log = state_home() / "debug.log"
                log.parent.mkdir(parents=True, exist_ok=True)
                with open(log, "a", encoding="utf-8") as fh:
                    fh.write("%s %s: %r\n" % (time.strftime("%F %T"), kind, exc))
            except OSError:
                pass
    return 0


# ---------------------------------------------------------------- CLI

def cmd_match(args):
    routes, _ = load_routes()
    skills, notes = suggest(" ".join(args.text), routes, load_index(), (), args.max)
    if not skills and not notes:
        print("(no match)")
    for skill, rid in skills:
        print("%-45s %s" % (skill, rid))
    for note in notes:
        print("note: " + note)
    return 0


def render_map(routes, index):
    installed = index.get("skills", {})
    lines = ["# summon-skills map (generated %s; do not edit -- edit routes)" % time.strftime("%F"),
             ""]
    for route in routes:
        live = [s for s in route.get("skills", [])
                if installed.get(s, {}).get("invocable") or s in route.get("_builtin", ())]
        missing = [s for s in route.get("skills", []) if s not in live]
        line = "- **%s** (%s): %s" % (route["id"], route.get("kind", "domain"),
                                     ", ".join("`%s`" % s for s in live) or "_no installed skill_")
        if missing:
            line += " -- not installed: " + ", ".join(missing)
        lines.append(line)
    return "\n".join(lines) + "\n"


def cmd_map(args):
    routes, _ = load_routes()
    out = Path(args.out).expanduser() if args.out else state_home() / "map.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_map(routes, load_index()), encoding="utf-8")
    print(out)
    return 0


def cmd_unrouted(args):
    routes, _ = load_routes()
    for skill in unrouted_skills(routes, load_index()):
        print(skill)
    return 0


def cmd_doctor(args):
    t0 = time.perf_counter()
    routes, errors = load_routes()
    index = load_index(force=args.rebuild)
    installed = index.get("skills", {})
    print("routes: %d (defaults %s)" % (len(routes), default_routes_path()))
    print("installed skills: %d (%d invocable), index built %s"
          % (len(installed), sum(1 for i in installed.values() if i.get("invocable")),
             time.strftime("%F %T", time.localtime(index.get("built_at", 0)))))
    for err in errors:
        print("ERROR " + err)
    builtin = set().union(*(r.get("_builtin", set()) for r in routes)) if routes else set()
    missing = sorted(mentioned_skills(routes) - set(installed) - builtin)
    print("route skills not installed: %d" % len(missing))
    for skill in missing:
        print("  - " + skill)
    deps = index.get("deps", {})
    inert = {p["plugin"]: p for p in inert_plugins(index)}
    print("plugins with external dependencies: %d, inert: %d" % (len(deps), len(inert)))
    for plugin in sorted(deps):
        if plugin in inert:
            print("  INERT %s" % describe_inert(inert[plugin]))
        else:
            print("  ok    %-24s %s" % (plugin, ", ".join(deps[plugin]["commands"] +
                                                       ["$" + v for v in deps[plugin]["env"]])))
    t1 = time.perf_counter()
    suggest("this test fails with a KeyError traceback", routes, index)
    print("unrouted skills: %d; warm match %.1f ms; doctor total %.0f ms"
          % (len(unrouted_skills(routes, index)), (time.perf_counter() - t1) * 1000,
             (time.perf_counter() - t0) * 1000))
    return 1 if errors or inert else 0


def cmd_reindex(args):
    """Rebuild index.json; always release the lock _spawn_reindex took."""
    try:
        index = load_index(force=True)
    finally:
        try:
            (state_home() / "reindex.lock").unlink()
        except OSError:
            pass
    print("indexed %d skills" % len(index.get("skills", {})))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="summon-skills.py",
        description="Suggest already-installed skills that fit a prompt (no model calls).",
        epilog=HELP_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")
    p = sub.add_parser("hook", help="hook entry point (reads hook JSON on stdin)")
    p.add_argument("kind", choices=["session-start", "prompt", "pretool"])
    p = sub.add_parser("match", help="show what would be suggested for TEXT")
    p.add_argument("text", nargs="+")
    p.add_argument("--max", type=int, default=DEFAULT_MAX)
    p = sub.add_parser("map", help="write the purpose -> skill map (markdown)")
    p.add_argument("--out", help="output path (default $SUMMON_SKILLS_HOME/map.md)")
    sub.add_parser("unrouted", help="list installed invocable skills no route mentions")
    sub.add_parser("reindex", help="rebuild the installed-skill index now (the hooks run this "
                                   "detached when the index is stale)")
    p = sub.add_parser("audit", help="scan transcripts; classify each suggestion's outcome per route "
                                     "(loaded/visible/thinking-only/silent) — read-only measurement")
    p.add_argument("--since-days", type=int, default=14, dest="since_days")
    p.add_argument("--max-files", type=int, default=200, dest="max_files")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("replay", help="re-run suggest() over a labelled corpus jsonl "
                                      "({prompt, expect}); reports fired vs expected per line")
    p.add_argument("corpus")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("doctor", help="validate routes, index and LSP binaries")
    p.add_argument("--rebuild", action="store_true", help="force an index rebuild")
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help(sys.stderr)
        return 2
    if args.cmd == "hook":
        kind = {"prompt": "prompt", "pretool": "pretool"}.get(args.kind, "session")
        return run_hook(kind, sys.stdin)
    return {"match": cmd_match, "map": cmd_map, "unrouted": cmd_unrouted, "audit": cmd_audit,
            "replay": cmd_replay, "doctor": cmd_doctor, "reindex": cmd_reindex}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
