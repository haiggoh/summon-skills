---
name: summon-skills
description: "Use when a summon-skills line appears in context (\"summon-skills: likely relevant -> ...\"), when decomposing any non-trivial task into phases, when unsure which installed skill fits the work, or when the user asks to add, tune or check skill suggestions (routes, false positives, unrouted skills, inert *-lsp plugins). Covers the rule for matching installed skills to each phase and the summon-skills.py CLI."
---

# summon-skills

Installed skills go unused when their one-line description scrolls past. This plugin puts
the matching ones in front of you, and this skill is the rule that goes with it.

## The rule

When breaking down a non-trivial task, **name the skill for each phase before the first tool
call**:

1. **Process skill first**: brainstorming or writing a plan before building, systematic
   debugging before a fix, TDD before implementation code, verification before claiming done.
2. **Then the domain skill** for the language or framework actually in the files.
3. **When no installed skill fits, say so and proceed.** Finding NEW skills is out of scope
   here; that is `skills find`.
4. **Skipping a suggested skill needs a stated reason** in one clause ("not a bug, a typo").
   Silent skipping is the failure this exists to prevent.

## Reading a suggestion

`summon-skills: likely relevant -> superpowers:systematic-debugging (debugging) -- ...`
comes from a keyword match on the prompt, not from judgement. Treat it as a strong hint:
invoke the named skill via the Skill tool, or state why it does not apply. Each skill is
suggested at most once per session, so its absence later does not mean it stopped applying.

`summon-skills: <plugin> is installed but inert: <command or $VAR> not found (...)` means an
enabled plugin declares an LSP or MCP server whose external command (or required environment
variable) is missing, so the plugin shows as installed but does nothing. Tell the user once,
with the install hint. Installing is their call unless they have already asked for it.

## Tuning (CLI)

The script lives at `${CLAUDE_PLUGIN_ROOT}/bin/summon-skills.py`; run it with `--help` first.

| Command | Use |
|---|---|
| `match "<prompt>"` | preview what a prompt would trigger (ignores the per-session dedupe) |
| `doctor` | route errors, route skills not installed, LSP binary status, timing |
| `unrouted` | installed skills that no route mentions: candidates for new routes |
| `map` | write a readable purpose -> skill map to `~/.claude/summon-skills/map.md` |

To add or fix routes, edit `~/.claude/summon-skills/routes.local.json` (survives plugin
updates). It uses the same schema as the plugin's `rules/routes.json`: a route with an
existing `id` replaces it, `{"id": "x", "disabled": true}` removes one, new ids are added.
Match fields are `words` (whole-phrase, case-insensitive), `regex`, and `ext` (file
extensions); `exclude` phrases veto a route. `skills` is an ordered fallback list: the
first installed one is suggested. Run `match` against a positive and a negative prompt
after every change.

Never edit the installed plugin cache copy of `routes.json`; the next update reverts it.

## Gotchas a routes table cannot hold

- **A mention is not a request.** A prompt that only *discusses* a hook's runtime behaviour once
  triggered an *authoring* skill (hook-development), because the route matched the bare noun
  "hook". Authoring-class routes (hook, command, agent, skill development) need an intent check:
  an authoring verb (write, add, change, fix, build) AND the artifact noun, via `regex` or an
  `exclude` list. Every misfire should become a regression PAIR checked with `match`: the
  discussing prompt (must not fire) next to the authoring prompt (must fire).
- **`*-lsp` plugins are not skills.** They wire a language server into the LSP tool, never appear in
  the skill list, and do nothing until the server binary is on PATH. `doctor` names the missing
  binary; that is a missing install, not a missing route.
- **Listing installed skills naively misses some.** A plugin may declare its skill directories in
  `plugin.json` instead of `skills/` (a `skills/*/SKILL.md` glob finds none of them), and built-in
  skills (`simplify`, `code-review`, `claude-api`, `update-config`, `security-review`) have no folder
  at all. Read each plugin at its registered `installPath`, never the newest cache directory. Any
  tool that enumerates skills must handle all three cases.
- **No skill for a language** (no Python, shell, Go or Rust skill installed) is normal: the process
  skills cover the work, and `skills find <language>` checks for one before substantial work.
