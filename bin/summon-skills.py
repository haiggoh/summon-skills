#!/usr/bin/env python3
"""summon-skills: suggest already-installed skills that fit the prompt at hand.

Local and deterministic: no model calls, no network, and skill bodies are never
read. The installed-skill index is rebuilt only when its fingerprint changes.
Python 3.9 compatible (hooks may run under macOS /usr/bin/python3).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

VERSION = "0.1.1"  # also in .claude-plugin/plugin.json and CHANGELOG.md
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
    """A stat-only digest; changes when plugins/skills are added, removed or toggled."""
    h = hashlib.sha1(("%s;%s;" % (VERSION, SCHEMA)).encode())
    files = [ch / "plugins" / "installed_plugins.json", ch / "settings.json", ch / "skills"]
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


def describe_inert(problem):
    text = "%s is installed but inert: %s not found" % (problem["plugin"], ", ".join(problem["missing"]))
    return text + (" (%s)" % problem["hint"] if problem["hint"] else "")


def build_index(ch):
    return {"schema": SCHEMA, "fingerprint": fingerprint(ch), "built_at": int(time.time()),
            "skills": discover_skills(ch),
            "plugins": sorted(name for name, _ in enabled_plugins(ch)),
            "deps": declared_deps(ch)}


def load_index(ch=None, force=False):
    """Return the cached index, rebuilding it only when the fingerprint moved."""
    ch = ch or claude_home()
    path = state_home() / "index.json"
    cached = _load_json(path, None)
    if not force and cached and cached.get("schema") == SCHEMA \
            and cached.get("fingerprint") == fingerprint(ch):
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


# ---------------------------------------------------------------- session state

def _state_file(session_id):
    safe = re.sub(r"[^\w.-]", "_", session_id or "nosession")[:80]
    return session_dir() / (safe + ".json")


def load_shown(session_id):
    return set(_load_json(_state_file(session_id), []))


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


def hook_prompt(payload):
    """Return the one-line nudge for this prompt, or '' when nothing new matches."""
    session_id = payload.get("session_id") or "nosession"
    routes, _ = load_routes()
    index = load_index()
    shown = load_shown(session_id)
    skills, notes = suggest(payload.get("prompt") or "", routes, index, shown, _cap())
    if not skills and not notes:
        return ""
    shown.update(s for s, _ in skills)
    shown.update("lsp:" + n.split(" ", 1)[0] for n in notes)
    save_shown(session_id, shown)
    parts = []
    if skills:
        parts.append("summon-skills: likely relevant -> " +
                     ", ".join("%s (%s)" % (s, r) for s, r in skills) +
                     " -- invoke before the first tool call, or say why not.")
    parts += ["summon-skills: " + n for n in notes]
    return "\n".join(parts)


def hook_session_start(payload):
    prune_sessions()
    routes, errors = load_routes()
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
        line += "\nsummon-skills: " + "; ".join(describe_inert(p) for p in inert) + \
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
        out = hook_prompt(payload) if kind == "prompt" else hook_session_start(payload)
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="summon-skills.py",
        description="Suggest already-installed skills that fit a prompt (no model calls).",
        epilog=HELP_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")
    p = sub.add_parser("hook", help="hook entry point (reads hook JSON on stdin)")
    p.add_argument("kind", choices=["session-start", "prompt"])
    p = sub.add_parser("match", help="show what would be suggested for TEXT")
    p.add_argument("text", nargs="+")
    p.add_argument("--max", type=int, default=DEFAULT_MAX)
    p = sub.add_parser("map", help="write the purpose -> skill map (markdown)")
    p.add_argument("--out", help="output path (default $SUMMON_SKILLS_HOME/map.md)")
    sub.add_parser("unrouted", help="list installed invocable skills no route mentions")
    p = sub.add_parser("doctor", help="validate routes, index and LSP binaries")
    p.add_argument("--rebuild", action="store_true", help="force an index rebuild")
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help(sys.stderr)
        return 2
    if args.cmd == "hook":
        return run_hook("prompt" if args.kind == "prompt" else "session", sys.stdin)
    return {"match": cmd_match, "map": cmd_map, "unrouted": cmd_unrouted,
            "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
