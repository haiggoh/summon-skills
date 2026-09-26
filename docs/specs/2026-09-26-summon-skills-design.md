# summon-skills — design spec

Date: 2026-09-26 · Status: DRAFT for review · Target version: 0.1.0

## 1. Problem

Skills that are already installed go unused. Claude sees ~300 one-line descriptions in its
skill list and reliably misses the matching process skill (systematic-debugging, brainstorming,
TDD, verification) or the language/framework skill for the files in play. A hand-written memory
map (`memory/skill-routing-map.md`) plus a CLAUDE.md rule helps, but both depend on Claude
remembering to look, and the hand-written map goes stale as plugins change.

## 2. Goal and non-goals

**Goal:** put the right *already-installed* skill in front of Claude at the moment it decomposes
a task — cheaply, deterministically, and without nagging.

**Non-goals:**
- Discovering or recommending skills that are NOT installed (that is `find-skills` / `skills find`).
- Loading, unloading or rewriting skills, or changing what is in the skill list.
- Any model call, network call, or reading SKILL.md bodies at prompt time.
- Forcing invocation. The nudge is advisory; the rule asks for a stated reason when skipped.

## 3. Success criteria

1. A prompt like "this test fails with a KeyError" surfaces `superpowers:systematic-debugging`
   on that turn; "let's build X" surfaces brainstorming; a `.py`-heavy prompt surfaces the
   Python route.
2. Token cost: ≤ ~30 tokens at SessionStart; 0 tokens on non-matching prompts; ≤ ~40 tokens on a
   matching prompt; each skill suggested at most once per session.
3. Hook latency p95 < 100 ms on this machine with a warm cache.
4. A skill that is not installed, disabled, or marked `disable-model-invocation: true` is never
   suggested.
5. Any failure (bad JSON, missing cache, exception) → silent, exit 0. The hook can never block a
   prompt.

## 4. Components

```
summon-skills/
├── .claude-plugin/plugin.json
├── bin/summon-skills.py          # CLI + library; stdlib only; --help
├── bin/summon-skills-resolve.sh  # portable-repos self-discovery helper
├── hooks/hooks.json             # SessionStart + UserPromptSubmit → summon-skills.py
├── rules/routes.json            # curated default routes (versioned)
├── skills/summon-skills/SKILL.md # the always-on rule + how to extend routes
├── tests/                       # see §8
├── README.md · CHANGELOG.md · LICENSE
```

### 4.1 Routes file (`rules/routes.json`)

```json
{
  "schema": 1,
  "routes": [
    {
      "id": "debugging",
      "kind": "process",
      "match": {
        "words": ["bug", "broken", "fails", "failing", "traceback", "stack trace", "regression", "doesn't work", "flaky"],
        "regex": ["\\b[A-Z][a-zA-Z]+Error\\b", "exit code [1-9]"],
        "ext": []
      },
      "exclude": [],
      "skills": ["superpowers:systematic-debugging", "mattpocock-skills:diagnosing-bugs"]
    }
  ]
}
```

- `kind`: `process` | `domain`. Process suggestions are always listed before domain ones.
- `match.words`: case-insensitive, word-boundary matched phrases.
- `match.regex`: Python regexes, compiled once per process.
- `match.ext`: file extensions matched as `\.<ext>\b` or as a path token in the prompt.
- `exclude`: phrases that veto the route (e.g. `ship` route excludes "wrap this", "wrap the").
- `skills`: ordered preference; the FIRST installed, model-invocable skill is suggested (one per
  route), so alternates act as fallbacks rather than extra noise.
- Seed set: the purposes from today's memory map — brainstorming, planning, plan-execution,
  tdd, debugging, service-incident, review, verification, security, merge-conflict, ship/commit,
  automation (measure-twice), delegation, claude-api, plus domain routes for nextjs, react,
  web-platform, expo, liquid, duckdb/sql, fastly, dataverse, unreal, claude-plugin-dev,
  harness-config, media, comfyui, lora, dataviz.
- Language routes for python / rust / typescript / swift / c-cpp / php point at no skill today;
  they carry an `lsp` field (see 4.4) so the only thing they emit is the LSP-inert warning.

### 4.2 User override (`~/.claude/summon-skills/routes.local.json`)

Same schema. Merged by `id`: a local route with an existing id replaces it; `"disabled": true`
removes it; new ids are added. Survives plugin updates (lives outside the cache). Path
overridable by `SUMMON_SKILLS_HOME`.

### 4.3 Installed-skill index (cache)

`~/.claude/summon-skills/index.json`:
`{fingerprint, built_at, skills: {"plugin:skill": {"invocable": bool}}, lsp: {...}}`

- **Sources:** `~/.claude/skills/*/SKILL.md` (user skills, bare names) and, for each ENABLED
  plugin in `installed_plugins.json` + settings `enabledPlugins`, the newest cached version's
  `skills/*/SKILL.md` (numeric version sort, per portable-repos §4).
- Only the YAML **frontmatter** is read (`name`, `disable-model-invocation`), never the body.
- **Fingerprint:** sha1 of the sorted list of (skill dir path, SKILL.md mtime) plus the mtimes
  of `installed_plugins.json` and `settings.json`. Computing it is a stat walk, no reads.
- Rebuild only when the fingerprint differs. The hot path is: stat walk → compare → load JSON.
  If the stat walk itself proves slow (> 50 ms), fall back to fingerprinting only the two
  registry files + `~/.claude/skills` mtime.

### 4.4 LSP awareness

Per language route, `"lsp": {"plugin": "pyright-lsp", "binary": "pyright-langserver",
"install": "pipx install pyright"}`. The index records whether the plugin is enabled and the
binary is on PATH (resolved with an explicit PATH that includes Homebrew, `~/.local/bin`, and
npm global bin — memory: PATH differs between Bash tool and spawned process). If enabled but the
binary is missing, the language route emits once per session:
`summon-skills: pyright-lsp is installed but inert — pyright-langserver not on PATH (pipx install pyright)`.

## 5. Runtime behaviour

### 5.1 SessionStart

Refresh index if fingerprint changed; print one line:
`summon-skills: 41 routes over 312 installed skills — relevant skills are suggested per prompt; name one per phase before the first tool call.`
If `unrouted` count > 0 and changed since last session, append ` (7 new skills unrouted — summon-skills.py unrouted)`.

### 5.2 UserPromptSubmit

1. Read hook JSON from stdin (`prompt`, `session_id`). Parse args before anything else.
2. Load index (no rebuild on this path unless the index file is missing).
3. Match prompt against routes → candidate routes → first installed, invocable skill each.
4. Drop skills already suggested this session (state file
   `$TMPDIR/summon-skills/<session_id>.json`, pruned when older than 2 days).
5. Order process-first, cap at 3, emit ONE line as `additionalContext`, e.g.
   `summon-skills: likely relevant → superpowers:systematic-debugging (debugging), superpowers:test-driven-development (tdd) — invoke before the first tool call, or say why not.`
6. No match → no output at all.

Everything wrapped so any exception exits 0 with no output. `SUMMON_SKILLS_DEBUG=1` writes the
exception to `~/.claude/summon-skills/debug.log` instead of swallowing it silently.

### 5.3 CLI (`bin/summon-skills.py`)

| Subcommand | Does |
|---|---|
| `hook session-start` / `hook prompt` | the two hook entry points (stdin JSON) |
| `match "<text>" [--all]` | show what would be suggested (`--all` ignores the session dedupe) |
| `map [--out PATH]` | write a human-readable purpose → skill map (default `~/.claude/summon-skills/map.md`) |
| `unrouted` | installed invocable skills no route mentions |
| `doctor` | validate routes (parse, regex compile), list route skills that are not installed, LSP binary status, index age, hook timing |
| `--help` | usage, subcommands, env vars (`SUMMON_SKILLS_HOME`, `SUMMON_SKILLS_DEBUG`, `SUMMON_SKILLS_MAX`, `SUMMON_SKILLS_OFF`) |

Unknown flags → usage on stderr, exit 2. `SUMMON_SKILLS_OFF=1` silences both hooks.

## 6. Relationship to existing guidance

- The skill `summon-skills` holds the full rule ("match skills at decomposition, process before
  domain, `skills find` when nothing installed fits, state a reason when skipping").
- CLAUDE.md "Match skills to the task at decomposition" shrinks to a plugin-agnostic 2-line
  version (CLAUDE.md must not depend on a named plugin), keeping the behaviour if the plugin is
  disabled.
- `memory/skill-routing-map.md` is superseded in place: body becomes the curated knowledge that
  is NOT in routes.json (LSP explanation, gaps), description points at `summon-skills.py map`.
  No stub file.
- Sibling in spirit to brief-agents (surfacing what is otherwise invisible) and measure-twice
  (use what exists before building). No code dependency on either.

## 7. Risks

| Risk | Mitigation |
|---|---|
| False positives nag on unrelated prompts | word-boundary matching, `exclude` lists, once-per-session dedupe, negative test cases |
| Curated routes rot as plugins change | install filter hides missing skills; `doctor` + `unrouted` surface drift; SessionStart mentions new unrouted skills |
| Hook ordering with other UserPromptSubmit hooks | output is independent, one line; no reliance on order |
| Stat walk over plugin cache is slow | measured in tests; fallback fingerprint (§4.3) |
| Suggesting a human-only skill | `disable-model-invocation` respected (borrowed from gaia-skill-heaven) |

## 8. Testing

- **Matcher:** table-driven positive and negative prompts per route (≥ 3 each), including
  "wrap this line in a div" must not hit ship, "fix the typo" must not hit debugging's regex path.
  Case expansion is the local-offload step; every expected value ground-truthed by hand.
- **Install filter:** stub `~/.claude` tree in a temp dir (never the real machine state) with
  enabled, disabled, multi-version and `disable-model-invocation` skills.
- **Dedupe:** second identical prompt in the same session emits nothing; new session emits again.
- **Fail-open:** corrupt routes.json, corrupt index, unreadable state dir → exit 0, no stdout.
- **Budget:** emitted line ≤ 300 chars; SessionStart line ≤ 200 chars.
- **`--help`:** RUN it and assert usage text and exit 0 with no side effects; unknown flag → exit 2.
- **Success branch:** assert the hook actually PRODUCES output for a matching prompt (not just exit 0).
- **Timing:** warm-cache hook run < 100 ms on the fixture tree.

## 9. Delivery

New repo `~/ClaudeWorkspace/summon-skills` → GitHub `haiggoh/summon-skills` → entry in the
haiggoh marketplace (get-haiggoh) → v0.1.0 tag + release → install via get-haiggoh → dogfood on
real prompts in a fresh session → reconcile CLAUDE.md and memory (§6). Publishing steps are
confirmed with the user before they happen.
