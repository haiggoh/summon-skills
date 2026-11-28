# Changelog

## [0.3.0] — 2026-11-28

### Changed — suggestions rank by familiarity and stay quiet when the knowledge is already in context

- **Rarer skills surface first**: ranking score = route strength × a graded habit weight `max(0.15, 1/(1+uses/5))` summed over the last 30 days (`familiarity.json`, refreshed at SessionStart — bounded ≤200 files / ≤2 s, atomic write, stale cache kept on overrun). Habitual skills fade gradually, never a hard cutoff.
- **Known skills don't re-suggest**: a skill counts as known when loaded via the Skill tool, its plugin's executable ran in Bash, or its SessionStart banner is in the transcript — all after the last compact boundary. Seeded for `waypoints:waypoints`.
- **Mid-task dampers**: a literal skill name in the prompt takes no slot ("run waypoints.py" no longer re-suggests waypoints); pasted documents match on first line + first 600 chars only; short replies (<15 words after the first assistant turn) get at most one strength-1.0 suggestion; authoring routes (hook/plugin/skill dev) need an authoring verb within 6 words of the domain noun, so discussing a hook no longer suggests writing one.
- **Declines are learned**: a suggestion answered in visible text without loading counts 0.5 toward habit; a matched-but-known skill gets one quiet deferred reminder after 5 tool calls (never blocks, shares the 2-per-batch reminder budget, expires after reminding, at 20 calls, or on compaction).
- **Routes**: 14 new routes for previously unrouted skills (drafted by a free-lane dispatch, each replay-verified), 63 → 77 total.
- Measured on the labelled regression set (`replay tests/fixtures/regression.jsonl`): BEFORE 30 rows, 9 false-positive violations, mean 1.27 suggestions/prompt → AFTER 58 rows, 0 violations, mean 0.88, all labelled positives held; suite 175 passed.

## [0.2.7] — 2026-11-28

### Fixed — three routes were silently dead because the skill they named had been renamed or removed: `local-offload` now points at `free-agents:offload` (was `offload-to-local`), `execution-plan-layer` at `lasting-plans:plan-airtight` (was `run-to-completion:writing-execution-plans`), and the `merge-conflict` route is removed (mattpocock-skills 1.3.x no longer ships `resolving-merge-conflicts` as a skill).

- New guard: the SessionStart line names any **stale route target** — a route skill whose plugin is enabled but no longer ships it — so a future rename shows up the next session instead of going quiet.
- New `plan-offload` route: planning and decomposition prompts ("write a plan", "break this down", "decompose") also suggest `free-agents:offload`; new `free-handover` route: successor-session / handover-doc / overnight-run prompts suggest `free-agents:free-session-handover`. Both carry negative tests against everyday wording ("plan the office party", "a free session at the gym").
- Design for the next two releases (familiarity ranking, mid-task dampers, tool-output triggers): `docs/superpowers/specs/2026-11-28-contextual-triggering-design.md`.

## [0.2.6] — 2026-11-24

### Changed — the PreToolUse barrier no longer denies any tool call; an unanswered suggestion now gets at most two non-blocking reminders per batch (user decision 2026-11-23: zero denials). The reminders are emitted as `hookSpecificOutput.additionalContext` on both Claude Code and Codex, so the call always proceeds; after the second reminder the hook goes silent. `audit --json` gains a `reminders` field; `denies` is kept and now counts only legacy "denied once" lines.

- Keep reminder claims separate for each suggestion batch, including concurrent and late hook calls (numbered O_EXCL claim files, at most two per batch).
- Match visible skill acknowledgements by complete names; ambiguous short names no longer clear another skill.

## [0.2.5] — 2026-11-19

### Fixed — inert-plugin notes now say what the missing tool DOES and why it matters (user 2026-11-19: the bare 'installed but inert' line read as an unimportant side note). Each note names the language server's purpose (e.g. jdtls = Java diagnostics/navigation for .java) and the impact ('no editor feedback for its language until installed'), then the one-time install hint. Test pinned.

## [0.2.4] — 2026-11-17

- Recognize visible assistant acknowledgements in native Codex transcripts, so skill suggestions do not falsely block the next tool call. User text and reasoning remain excluded, and compaction resets old acknowledgements.

## [0.2.3] — 2026-11-16

- fix(§6b): role-blind visible-answer detection — only assistant text clears the barrier / counts as visible

## [0.2.2] — 2026-11-16

- feat(§6b step 3): PreToolUse deny-once barrier for unanswered skill suggestions (audit-measured; fails open after one deny)

## [0.2.1] — 2026-11-16

### Fixed — §6b staged triggering, steps 1-2 (measured)

- Baseline measured with `audit` on this machine's real transcripts (14 days): 929 fired —
  loaded 20% · visible 62% · thinking-only 2% · silent 16%. Noisy routes: open-items (112),
  worktree (96, 0 loaded), parallel-work.
- Step 1 precision: the `worktree` route is skipped when the hook payload's cwd is already a
  linked worktree (`.git` is a file) — the advice is already satisfied there.
- Step 2 visibility: the hook text now demands a VISIBLE reply ("or say in your VISIBLE
  reply why not (a thinking-only decline does not count)").
- `audit --json` now reports a `denies` field so step 3's deny-once barrier has its gate
  (silence must drop without new friction; denies counted per session).
- Step 3 (PreToolUse deny-once barrier) stays staged behind a re-measured audit window.

## [0.2.0] — 2026-11-15

### Added — §6b step 1 (measure before tuning) + Phase 4 Wave B portable package

- New `summon-skills.py audit` (read-only): scans transcripts for suggestion events and
  classifies each outcome per route — loaded (a later Skill tool_use) / visible (a later
  assistant TEXT block names it) / thinking-only / silent — as a table or `--json`, with
  `--since-days`/`--max-files` bounds. New `summon-skills.py replay CORPUS.jsonl` re-runs
  `suggest()` over a labelled corpus (`{"prompt":…, "expect":…}`) and reports fired-vs-
  expected per line. Both pinned by tests with synthetic transcripts/corpus.
- Phase 4 Wave B portable package (COORDINATION C1/C2): `scripts/build-portable.py` generates
  the committed `portable/` — same-name manifest, skill, hooks (bin/ -> scripts/ rewritten),
  the CLI at `scripts/summon-skills.py`, `rules/routes.json` at the portable root — with
  `PORTABLE-BUILD.json` and a deterministic dist zip. Freshness/closure/schema pinned.

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
