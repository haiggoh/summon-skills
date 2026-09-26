# summon-skills Implementation Plan

> Execution: Native (inline, user said "build it"), one whole-branch review at the end.

**Goal:** a plugin that suggests already-installed, relevant skills per prompt at near-zero token cost.
**Architecture:** one stdlib Python module `bin/summon-skills.py` (index + matcher + CLI + hook
entry points), curated `rules/routes.json`, two command hooks, one skill.
**Tech Stack:** Python 3 stdlib, pytest (pipx), bash hooks.json.
**Spec:** `docs/specs/2026-09-26-summon-skills-design.md`

## Global Constraints
- No model/network calls; hook reads only frontmatter at index build, never at prompt time.
- Hook output: SessionStart ≤ 200 chars, prompt ≤ 300 chars, ≤ 3 skills, once per skill per session.
- Any failure → exit 0, no stdout. `--help` everywhere; unknown flag → exit 2.
- Never suggest uninstalled, disabled, or `disable-model-invocation: true` skills.
- Portable: no hardcoded checkout or cache-version paths; plugin paths from `installPath`.

## Review Focus
1. Prompts containing code/log pastes (long text) — must stay fast and not over-match. → test: 50KB prompt < 100 ms.
2. Idiomatic false friends ("wrap this line", "fix the typo", "plan B") → negative matcher tests.
3. Stale index after plugin install/disable → fingerprint test flips on registry mtime change.
4. Missing `session_id` in hook JSON → still emits, dedupe keyed on "nosession".
5. Local override with bad regex → route skipped, others still work; `doctor` reports it.

## Offload
Zero local steps: the only qualifying slice (matcher case expansion, ~40 cases) is smaller than
a dispatch briefing and every expected value needs hand ground-truthing anyway —
`cloud:not worth offloading`.

## Tasks
1. Index: discovery (user skills + enabled plugins via installPath), frontmatter parse, fingerprint, cache. Tests with stub tree.
2. Routes: load defaults + local override merge, compile, match, pick first installed invocable skill, order, cap. Tests: positive/negative table.
3. Hooks: session-start line, prompt line with per-session dedupe, fail-open, LSP-inert warning. Tests incl. success branch + fail-open.
4. CLI: match/map/unrouted/doctor/--help. Tests: help runs without side effects, unknown flag exit 2.
5. Seed `rules/routes.json` from the memory map; `unrouted`/`doctor` against the real machine to sanity check.
6. Packaging: plugin.json, hooks.json, skill, README, CHANGELOG, LICENSE; plugin-validator.
7. Ship (confirm with user): GitHub repo, marketplace entry, tag, install, dogfood; reconcile CLAUDE.md + memory.
