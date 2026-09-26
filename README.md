# summon-skills

**Make Claude actually use the skills you already have.**

A Claude Code session can hold hundreds of installed skills, but each is only a one-line
description in a long list, so the right one often goes unused: systematic debugging for a
bug, brainstorming before a build, TDD before code, the framework skill for the files in play.
summon-skills reads each prompt locally and names the 1-3 installed skills that fit it:

```
summon-skills: likely relevant -> superpowers:systematic-debugging (debugging) -- invoke before the first tool call, or say why not.
```

It is about using what is **installed**. It never recommends skills you do not have.

## How it stays cheap

- **No model calls, no network.** Matching is local regex over curated routes, typically under 1 ms.
- **No skill bodies read.** Only SKILL.md frontmatter, and only when the index is rebuilt.
- **Index rebuilt only on change**, detected by a stat-only fingerprint of the plugin registry,
  settings and user skills. Otherwise one small JSON read.
- **Bounded output:** one line at session start; nothing on prompts that match nothing; one line
  (up to 3 skills, process skills first) on prompts that do; **each skill at most once per session**.
- **Fail-open:** any error means no output and exit 0. It can never block a prompt.

## What it respects

- Only enabled plugins at their registered install path, plus `~/.claude/skills`, plus the
  manifest's custom `skills` paths.
- Skills marked `disable-model-invocation: true` are never suggested.
- Each route lists fallbacks in order; the first installed one wins.

## LSP awareness

The official `*-lsp` plugins (pyright, typescript, rust-analyzer, swift, clangd, php) are not
skills. They connect a language server to Claude Code's LSP tool, and they do nothing unless that
server binary is installed. When a prompt touches one of those languages and the plugin is enabled
but its binary is missing, summon-skills says so once per session, with the install command.

## Install

Through the haiggoh marketplace (get-haiggoh), or:

```
/plugin marketplace add haiggoh/get-haiggoh
/plugin install summon-skills@haiggoh
```

Requires `python3` (3.9+) on PATH. There are no other dependencies.

## CLI

```
python3 "${CLAUDE_PLUGIN_ROOT}/bin/summon-skills.py" --help
  match TEXT    preview suggestions for a prompt
  doctor        route errors, missing route skills, LSP status, timing
  unrouted      installed skills no route mentions
  map           write a readable purpose -> skill map
  hook KIND     hook entry point (session-start | prompt)
```

| Env var | Effect |
|---|---|
| `SUMMON_SKILLS_OFF=1` | silence both hooks |
| `SUMMON_SKILLS_MAX` | max skills per prompt (default 3) |
| `SUMMON_SKILLS_HOME` | state dir (default `~/.claude/summon-skills`) |
| `SUMMON_SKILLS_CLAUDE_HOME` | Claude config dir to scan (default `~/.claude`) |
| `SUMMON_SKILLS_ROUTES` | alternative default routes file |
| `SUMMON_SKILLS_DEBUG=1` | log hook exceptions to `$SUMMON_SKILLS_HOME/debug.log` |

## Your own routes

Put them in `~/.claude/summon-skills/routes.local.json` (survives updates). The schema is the same as
[`rules/routes.json`](rules/routes.json): a route with the same `id` replaces the default, and
`{"id": "dataviz", "disabled": true}` turns one off. Check the result with `summon-skills.py match "…"`.

## Files it writes

| Path | What |
|---|---|
| `~/.claude/summon-skills/index.json` | cached installed-skill index |
| `~/.claude/summon-skills/unrouted.count` | last unrouted count, so SessionStart only mentions growth |
| `$TMPDIR/summon-skills/<session>.json` | per-session "already suggested" list, pruned after 2 days |

## Tests

```
pytest tests
```

Tests run against a stub `~/.claude` tree, never your real one.

Sibling plugins: measure-twice (survey before building), brief-agents (brief subagents),
audit-loose-ends (reconcile at wrap-up), no-hidden-changes.
