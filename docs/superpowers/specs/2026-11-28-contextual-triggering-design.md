# summon-skills — contextual triggering, familiarity ranking, live routes (design)

Date: 2026-11-28 · Status: DRAFT for user approval · Base: `main` @ 909b269 (0.2.6)
Waypoints covered: summon-skills-suggests, -suggest-free, -stop-suggesting, -favour-obscure,
-usage-aware-and-contextual, -smarter-triggering, -tune-routes. (Rename idea: deferred, out of scope.)

## 1. Intent — why

The user (2026-11-23/27): summon-skills "tends to always suggest the same skills I'm already
commonly using … I would love it to trigger on more obscure skills I've never used", and a
suggestion for a skill whose literal name is in the prompt adds little because Claude Code already
acts on named skills. Its niche is **contextual usefulness: surfacing a skill the user (or the
model) would not have thought of, at the moment it helps** — and staying silent when the skill's
knowledge is already in context. Measured baseline (2026-11-13, 316 suggestions): 18 % loaded,
10 % declined with a reason, 72 % ignored; 46 % fired on pasted documents.

Success: fewer ignored/declined suggestions per session AND relevant hits kept (both measured with
`audit` / `replay` on a labelled regression set), rarely-used installed skills appear among
suggestions, offload/handover are suggested on planning prompts.

## 2. Goals / non-goals / anti-goals

Goals — see §4 items 1–7.
Non-goals: the rename/voice brainstorm; LSP auto-install policy (waits on check-code); fleet-wide
CLI --version sweep; any change to other plugins' hooks.
Anti-goals (failure even if it "works"): (a) **overcorrecting** — a quieter tool that also loses the
relevant hits; (b) a hook that ever **blocks** a tool call (0.2.6 zero-denials stays); (c) a
**hard ban** on habitual skills (user: graded, never a cutoff); (d) non-deterministic output
(replay must be exact); (e) a slow hook — every hook stays fail-open and bounded.

## 3. Constraints

- Single file `bin/summon-skills.py` (tests import it); routes in `rules/routes.json` (+ local
  overrides). Python stdlib only; /bin/bash-3.2-safe shell if any.
- Hooks: SessionStart, UserPromptSubmit, PreToolUse today; this design adds PostToolUse (0.4.0).
  All hooks fail OPEN, never deny, bounded time (existing 30 s timeout).
- State under `state_home()`; per-session files pruned by `prune_sessions` (TTL 2 days).
- Ship via rtc-ship from a worktree; every release bumps every version surface + portable rebuild.
- Mid-turn user messages are NOT `type:user` records (measurement pitfall, 2026-11-13 analysis).

## 4. What gets built (user-decided parts marked **[user]**, my calls marked *[call]*)

1. **Live routes + guard.** Repoint renamed targets: `local-offload` → `free-agents:offload`;
   `execution-plan-layer` → `lasting-plans:plan-airtight`; `merge-conflict` → drop the vanished
   mattpocock target (keep route for other installs only if a live target exists; else remove
   the dead name) *[call]*. Guard: a test that fails when a route names a skill under a plugin
   from OUR marketplace (haiggoh) that the plugin's source does not ship *[call]*; `doctor` lists
   every dead route (installed plugin, missing skill) as a visible line.
2. **Offload at plan time.** New additive route `plan-offload` (pattern of `execution-plan-layer`)
   firing on planning/decomposition prompts → `free-agents:offload`, then
   `free-agents:free-session-handover` on handoff/successor/overnight wording **[user: waypoint]**.
3. **Familiarity.**
   - *Session "known"* **[user]**: skip a skill whose knowledge is in context after the last
     compact_boundary — (i) Skill tool_use (exists), (ii) a Bash command running one of the
     skill's plugin scripts, (iii) the plugin's SessionStart banner text present in the transcript.
     Script map *[call]*: derived per plugin from executables in its `bin/` and `scripts/` dirs
     (basename match on the Bash command's tokens), plus an optional `knows` block per route in
     routes.json for overrides. Banner map *[call]*: optional `banner` regex per skill in routes.json
     (seed: waypoints `🧭 waypoints:`); unknown → signal absent (fail toward suggesting).
   - *Long-run habit* **[user]**: graded, deterministic.
     `weight(uses) = max(0.15, 1 / (1 + uses/5))`, `uses` = number of distinct sessions in the
     last 30 days that used the skill (load or script) *[call: sessions, not calls]*.
     Ranking score = `route_strength × weight × session_decline_factor`; top `cap` (3) win.
     `route_strength` *[call]*: 1.0 regex/intent match, 0.7 bare-word match; process routes keep a
     +0.1 tie-break so process-first ordering survives equal scores.
   - Familiarity cache *[call]*: `state_home()/familiarity.json` `{skill: {session_id: last_date}}`,
     refreshed at SessionStart by scanning transcripts modified since the last scan (bounded: ≤ 200
     files, ≤ 2 s; on overrun keep the stale cache). Entries older than 30 days dropped on write.
4. **Mid-task dampers** **[user: all four]**.
   - *Learn from declines*: a suggestion answered in visible text (existing `_visible_mentions`)
     without a Skill load → `session_decline_factor` 0.3 for that skill for the rest of the
     session; counts as 0.5 of a use toward habit *[call: numbers]*.
   - *Short-reply mode*: prompt < 15 words AND the session already has ≥ 1 assistant turn → cap 1,
     and only route_strength 1.0 matches qualify.
   - *Mention ≠ request*: authoring-class routes (hook-dev, claude-plugin-dev, skill-authoring,
     command/agent-dev) gain an `intent` regex (write|add|create|change|fix|build … within N words
     of the domain noun); a bare domain word no longer fires them. *Document mode*: a prompt that
     looks pasted (starts with `#`, or `Read <path> first`, or > 1500 chars) is matched on its first
     line + first 600 chars only (2026-11-13 proposal 1).
   - *Literal names don't count*: if the prompt contains the skill's short name, its plugin name,
     or `/name`, that skill does not take a slot (the next contextual match does). Covers
     -stop-suggesting's "prompt names waypoints" case.
5. **Deferred reminder** *[call, flagged to user]*: a matched skill skipped only because it is
   "known" is recorded as `deferred`; if ≥ 5 tool calls pass with no Skill/script use of it, the
   PreToolUse hook adds ONE quiet additionalContext line (never blocks; shares the
   REMINDER_LIMIT budget). User idea 1 of the usage-aware waypoint.
6. **Tool-output triggers** **[user]** (0.4.0). New PostToolUse hook on Bash: route field
   `on_output` (regex over the tool result's first 4 000 chars) and optional `on_command` (regex
   over the command). Seed: failing tests (`\b\d+ failed\b|FAILED|Traceback`) → systematic-debugging;
   conflict markers / `CONFLICT (content)` → merge-conflict skill; `git commit`/`git push` about to
   run (PreToolUse command match) → verification-before-completion. Same familiarity + dampers;
   rate limit *[call]*: ≤ 1 output-trigger per 10 tool calls and once per route per session.
7. **Routes for unrouted skills.** `summon-skills.py unrouted` lists them; a free-lane dispatch
   drafts candidate routes from each SKILL.md `description:`; each accepted route needs ≥ 1
   positive and ≥ 1 negative labelled example in the regression set and must pass `replay` with
   no relevant-hit loss *[call]*.

## 5. Approaches (decided)

- **A — three staged releases from the same single file (PICKED by user).** 0.2.7 = items 1–2;
  0.3.0 = items 3, 4, 5, 7; 0.4.0 = item 6. Each independently measured, rollbackable, and a
  bounded free-lane handover.
- B — one 0.3.0 with everything: fewer ships, regressions mask each other, oversize handover.
- C — split the file into modules first: rejected; tests import the single file, risk without value.

## 6. Measurement (gate for 0.3.0 and 0.4.0)

`tests/fixtures/regression.jsonl` *[call]*: labelled `{prompt, context, expect_any, expect_none}`
rows — seeded from the 2026-11-13 classification, the 11-27 false-positive set (dispatch-lesson
-primes), session aeefc92b (waypoints / brainstorm literal), the 10-20 hook-dev misfire, and
planning prompts that must yield offload. `replay` reports per release: false positives removed,
relevant hits lost (must be 0 on labelled positives), mean suggestions per prompt. A post-ship
`audit --since-days 1` is the dogfood read (ignored % and declined % vs the 0.2.6 baseline).

## 6b. Builder-check resolutions (2026-11-28; check by Gemini 3.8 Flash, free dispatch)

User approved the design and delegated these calls to the run ("builder-check calls are yours").
All are *[call]* unless noted.

- **Shipped in 0.2.7 (supersedes §4.1/4.2 wording):** `local-offload` → `free-agents:offload`;
  `execution-plan-layer` → `lasting-plans:plan-airtight`; `merge-conflict` route **removed** (no live
  target). Two separate routes, `plan-offload` and `free-handover` (a route yields only its first
  installed skill). The guard is a **runtime** check, not a source-checkout test:
  `stale_route_targets()` (plugin enabled, skill gone) is named on the SessionStart line — CI-safe.
- **Declines vs `shown` (Q1):** `shown` keeps suppressing re-suggestion within a session, so a
  per-session decline factor would be dead code — **dropped**. A visible decline instead counts
  0.5 toward long-run habit, and suppresses the same skill's tool-output trigger (0.4.0) for the session.
- **Decline detection with no tool calls (Q2):** `_hook_prompt` scans the transcript once for
  visible mentions of the PREVIOUS batch's pending skills before scoring; decisions are recorded in
  the existing per-session state file under a new `declined` key (additive; `shown` key unchanged).
- **Deferred reminder lifecycle (Q3):** the counter spans prompts; a new suggestion batch does NOT
  reset deferred entries; an entry expires after it reminds once, after 20 tool calls, or at the next
  compact_boundary. Shares REMINDER_LIMIT with ordinary reminders.
- **Regression fixture `context` (Q5):** `{"turns": int, "known": [skill], "uses": {skill: float}}`,
  all optional; `suggest()` gains `ctx=None` (keyword-only, default reproduces today's behaviour so
  every existing caller and pinned test is unchanged); `cmd_replay` passes it through.
- **`knowledge` map (Q7):** a new top-level `"knowledge": {skill: {"scripts": [basename…],
  "banner": regex}}` in routes.json — `skills` lists stay strings. Auto-derivation: a Bash command
  running an executable from a plugin's `bin/`/`scripts/` marks known ONLY the skill whose short
  name equals the plugin name (e.g. `waypoints:waypoints`); other skills need an explicit entry.
- **Fractional uses (Q8):** `familiarity.json` = `{skill: {session_id: [date, weight]}}`, weight 1.0
  (load/script) or 0.5 (visible decline); `uses = sum(weights)` over the last 30 days.
- **0.4.0 hook surface (Q9):** PostToolUse (Bash) matches `on_output`; PreToolUse additionally
  matches `on_command`. Both reply with `hookSpecificOutput.additionalContext` (hookEventName
  set accordingly), never a decision field — builder verifies the PostToolUse payload/response shape
  against current Claude Code hook docs before writing the matcher. The conflict-marker seed ships
  only if an installed target skill exists; otherwise it is omitted.

## 7. Left to the builder

Exact regexes and word lists; where in `_hook_prompt` the scoring slots in (replace the
`picked[:cap]` slice in `suggest()` with a scored sort, preserving the `(skills, notes)` return
contract); transcript-scan performance details; how the PostToolUse payload's result field is
read (verify against the live payload shape before writing the matcher); test layout.
