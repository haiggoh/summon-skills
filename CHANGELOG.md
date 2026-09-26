# Changelog

## 0.1.0 — 2026-09-26

- First release. Local per-prompt matcher (UserPromptSubmit) and one-line SessionStart status.
- 61 curated routes: process (brainstorming, planning, debugging, TDD, verification, review,
  wrap-up, automation, delegation, …) and domain (Next.js, React, web platform, Expo, Liquid,
  DuckDB, Fastly, Dataverse, Unreal, media, ComfyUI, dataviz, …), plus LSP-aware language routes.
- Installed-skill index from enabled plugins' registered install paths, manifest `skills` paths,
  and `~/.claude/skills`. Respects `disable-model-invocation`. Rebuilt only on fingerprint change.
- Per-session dedupe; fail-open hooks; `SUMMON_SKILLS_OFF` switch.
- CLI: `match`, `doctor`, `unrouted`, `map`, `hook`, with `--help`.
- User override file `~/.claude/summon-skills/routes.local.json`.
