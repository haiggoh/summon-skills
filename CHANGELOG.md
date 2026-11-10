# Changelog

## 0.1.11 — 2026-11-11

### Changed — UserPromptSubmit hook timeout 5s -> 30s

- `hooks/hooks.json`: `bin/summon-skills.py hook prompt` had a 5s budget; measured
  2026-11-10 under load avg ~80 (14 concurrent sessions) it took 5.7s cold / 1.2s warm,
  so every prompt during a load burst timed out ("timed out after 5s" in several sessions
  at once). A longer timeout costs nothing when the hook is fast.

## 0.1.10 — 2026-11-03

### Fixed — a skill already loaded this session is not suggested again

- `hook_prompt` now unions the shown-set with the Skill tool_uses found in the session's
  transcript (`transcript_path`), counting only entries after the last `compact_boundary`
  (a compacted-away skill may need the reminder again). Unreadable/missing transcript fails
  open to the old behaviour. Measured 2026-10-30: systematic-debugging was suggested one turn
  after it was invoked (waypoint summon-skills-don-t-suggest-a).
- Tests: loaded skill not re-suggested; compact boundary resets the set; missing transcript
  fails open. Mutation-checked (transcript read disabled -> red). Full suite 74 passed.

## 0.1.9 — 2026-11-02

### Fixed
- The prompt hook no longer times out on every prompt after an unrelated settings write.
  Incident: a write to `~/.claude/settings.json` (an env/route toggle) moved the index
  fingerprint; the rebuild took 15-26 s under machine load; the 5 s hook timeout killed it
  BEFORE `index.json` was written, so every following prompt restarted the same rebuild and
  timed out again ("UserPromptSubmit hook ... timed out after 5s — output discarded"), until
  something rebuilt the index outside a hook. Two changes:
  - `settings.json` enters the fingerprint by the CONTENT of `enabledPlugins` only, never by
    mtime — unrelated settings writes no longer invalidate the index.
  - The prompt hook is stale-while-revalidate: a stale cached index is served at once and a
    single detached `summon-skills.py reindex` (lock-guarded, 300 s stale-lock TTL) rebuilds
    it. The first run with no index at all still builds inline; the session-start hook stays
    synchronous so newly installed skills are announced at once (if it is killed, the next
    prompt finishes the build in the background).
- New `reindex` command (also what the hook spawns).
- The script's own `VERSION` constant had drifted to 0.1.1 (plugin.json said 0.1.8); it is part
  of the fingerprint, so plugin upgrades never invalidated the index. Now 0.1.9.

## 0.1.8 — 2026-10-31

### Changed
- The plugin description (plugin.json, and the marketplace listing it feeds) is rewritten to describe
  the plugin's full current capabilities.

## 0.1.7 — 2026-10-30

- Trimmed: the per-prompt acceptance-credit instruction (the tail of the suggestion line) lost
  ~30 more characters of standing context cost — "If you load one because of this, credit it:
  name summon-skills at first mention, with its lightbulb emoji." The rule is unchanged
  (condition, action, mark-by-description); the test pins the new exact ending and the old
  verbose phrasing's absence.

## 0.1.6 — 2026-10-29

- Fixed: the 0.1.5 acceptance-credit instruction emitted the 💡 glyph *inside* the per-prompt
  suggestion listing — the one line the user sees on every matching prompt, which must stay
  unbranded. The instruction now names the mark by description ("lightbulb identity emoji"),
  so the glyph appears only when the assistant actually credits summon-skills at acceptance.
  Caught by the independent final review (DeepSeek V4.1 Flash dispatch); the test now also
  pins the glyph's ABSENCE from the listing — a mutation-pin, not just a presence check.

## 0.1.5 — 2026-10-28

- Branding: credit at acceptance, not at speaking. The per-prompt suggestion line now ends with an
  instruction to name summon-skills with 💡 the moment a suggested skill actually gets loaded
  (the user sees which plugin earned the load); the inert-plugin flag line — a fix-it moment —
  leads with 💡. The suggestion and SessionStart route-count lines themselves stay unbranded.
  Static surfaces carry 💡 (plugin.json description, README); marketplace catalog sync rides the
  get-haiggoh batch ship.

## 0.1.4 — 2026-10-25

- Handoff-shaped planning prompts ("handoff plan", "plan … for the successor / free session /
  unattended run / another agent") now suggest BOTH the base plan-writing skill and
  run-to-completion's additive `writing-execution-plans` layer. A route suggests only its first
  installed skill, so the pairing is two routes firing on the same phrases (new route
  `execution-plan-layer`; the planning route gains the same condition regex). Plain planning prompts
  do not pull the layer in. Regression pairs added for both directions.

## 0.1.3 — 2026-10-24

- The skill gains "Gotchas a routes table cannot hold": a mention is not a request (authoring routes need
  verb + noun intent, each misfire becomes a regression pair), `*-lsp` plugins are not skills, and the three
  ways a naive skill listing misses skills. Moved here from the author's notes so the plugin carries its own
  lessons. No behaviour change.

## 0.1.2 — 2026-10-14

- The delegation route matches dispatch intent, not vocabulary: "delegate this/that/the",
  "dispatch a/an", "spawn a subagent", "run this in a subagent", "brief the delegate", with
  decline-phrases excluded ("no delegation", "not delegating", "without delegating", …).
  A prompt that merely mentions or declines delegation no longer suggests brief-agents
  (observed: "no delegation is planned" fired the suggestion).

## 0.1.1 — 2026-09-26

- Brainstorming route also catches "let us build", "build/create/add a new …" and
  "new <thing> feature/plugin/tool/app/command/skill" (found by dogfooding 0.1.0).
- SessionStart: the unrouted-skills note stays on the status line instead of trailing the
  inert-plugins line.

## 0.1.0 — 2026-09-26

- First release. Local per-prompt matcher (UserPromptSubmit) and one-line SessionStart status.
- 61 curated routes: process (brainstorming, planning, debugging, TDD, verification, review,
  wrap-up, automation, delegation, …) and domain (Next.js, React, web platform, Expo, Liquid,
  DuckDB, Fastly, Dataverse, Unreal, media, ComfyUI, dataviz, …), plus LSP-aware language routes.
- Installed-skill index from enabled plugins' registered install paths, manifest `skills` paths,
  and `~/.claude/skills`. Respects `disable-model-invocation`. Rebuilt only on fingerprint change.
- Inert-plugin detection: reads every enabled plugin's own LSP/MCP declarations (manifest,
  `.mcp.json`, marketplace entry) and checks external commands and required `${ENV}` vars live;
  reported at session start, in `doctor`, and on matching language prompts.
- Per-session dedupe; fail-open hooks; `SUMMON_SKILLS_OFF` switch.
- CLI: `match`, `doctor`, `unrouted`, `map`, `hook`, with `--help`.
- User override file `~/.claude/summon-skills/routes.local.json`.
