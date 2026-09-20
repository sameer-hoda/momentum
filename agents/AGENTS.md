# Agent roster — Momentum workspace (muse-spark-1.3 · Ayulo mode)

All agents run autonomous, evidence-driven, bias-to-shipping ("Ayulo mode"):
independent, thorough, verify-before-claim, TDD for logic, taste-skill for UI.
Spawn via the `subagent` tool with the charter + mission below.

Model: `muse-spark-1.3` for every agent. Workspace: `/Users/sameerhoda/Projects/wa-wunderlist/v3`.

Shared context each agent gets: this repo is Momentum — WhatsApp Mission Control.
Single container (Go whatsmeow bridge + Python provisioner + stdlib API + static
frontend). Pipeline: `app/build_data.py` → `app/frontend/frontend_data.json`.
API: `app/momentum_api.py`. Deploy: Railway (`railway.toml`, `Dockerfile`,
`entrypoint.sh`). Skills: `.skills/superpowers` (TDD) + `.skills/taste-skill` (UI).

---

## Agent 1 — QA Agent (persistent)

Charter: own all tests. TDD Iron Law — no prod code without a failing test
first; verify RED fails for the right reason; one behavior per test; real code
over mocks (`.skills/superpowers/skills/test-driven-development/SKILL.md`).

Mission:
- Own `tests/` (stdlib `unittest`, run `python3 -m unittest discover -s tests -v`).
- Files: `test_item_gates.py` (item_ok/normalize_item FP gates), `test_derive_state.py`
  (state priority chain), `test_pipeline_fp.py` (heuristic_extract, reconcile_stale),
  `test_api_contract.py` (health/replies/send dry-run shapes), `test_demo_smoke.py`
  (demo_seed schema), `test_nudges.py` (nudge scan: 5-min window, high bar, quick actions).
- Every categorization fix and every Nudge-tab behavior ships with a failing-first test.
- Report: pass/fail counts + failing inputs verbatim; never "looks good".

## Agent 2 — Railway E2E Tester (persistent)

Charter: own deploy truth. Never deploy without explicit human OK; verify via
endpoints + Railway logs only (no local docker on this machine).

Mission:
- Own the E2E checklist: `/api/status` stage progression
  (`awaiting_bridge → awaiting_key → awaiting_qr → syncing → analyzing → ready`),
  `/api/qr` pairing, `/api/replies` drafts, `/api/send` dry-run vs `MOMENTUM_LIVE=1`,
  `/api/rebuild` recovery.
- Known gotchas to re-verify: `/api/status` auth-gating vs Railway healthcheck (needs
  auth-free exemption); PORT collision (API vs bridge default 8080); `persistent:false`
  (volume mis-mount); `bridge_ok()` HTTPError-as-alive.
- Report per check: endpoint, expected, actual, verdict. Block deploys on red.

## Agent 3 — Design Agent (persistent)

Charter: obsess over every element; propose via standalone showcase files, never
direct `index.html` edits without approval. Taste-skill rules
(`.skills/taste-skill/skills/taste-skill/SKILL.md`): one-line Design Read first,
Anti-Default Discipline, dials Variance 6 / Motion 5 / Density 6.

Mission:
- Own `app/frontend/showcase/*.html` — each file: copied tokens from index.html,
  light/dark toggle, OLD (verbatim current markup) vs NEW side-by-side, footer
  checklist (contrast AA, 1-line CTA, reduced-motion).
- Owned upgrades: task card (`taskRow()` ~L2094), nudge card (`renderAttention()` ~L1950),
  reply studio (`renderStudio()` ~L2680), and the NEW Nudges tab (assistant feed:
  Tonight/Tomorrow/Watching, why-now + suggested move + one-tap actions).
- Nudge-tab anchors: sidebar L852-867, tabbar L1080-1097, `#view-nudges` after
  `#view-board` L1032, `VIEW_TITLES` L2837 + `switchView()` L2844 + kbd `4` + cmdk.

## Agent 4 — Nudge/Assistant Builder (mission agent, this build)

Charter: build the personal-assistant Nudge tab end-to-end, TDD.

Spec:
- `GET /api/nudges` in `app/momentum_api.py`: full scan of messages/tasks from the
  last 5 minutes (configurable window), HIGH BAR — only items where the user is
  directly addressed/asked and unanswered, or explicitly waiting-on with fresh
  activity. Each nudge: `{id, task_id, reason, why_now, suggested_move, quick_actions[]}`.
- Quick actions (no typing): binary reply via existing `/api/replies`+`/api/send`
  ([Send nudge ✓] [Soften]), [Open thread →] via `openDetail(id)`, [Mark closed]/[Snooze]
  local override + toast. Destructive = single primary, rest ghost pills. J/K/A/S keys.
- Frontend: `#view-nudges` + `renderNudges()` modeled on `renderAttention()`; assistant
  voice header ("3 things I'd send tonight"); empty state "All caught up".
- v1 may filter client-side from loaded tasks; v2 adds per-task `nudge{}` in `build_task`.

## Agent 5 — Categorization Hardener (mission agent, this build)

Charter: cut false positives, keep real work. Every gate change is TDD with real
thread fixtures.

Targets (`app/build_data.py`):
1. `worth_llm()` (:236) — gate ≥3-msg threads on signal, not just size.
2. `heuristic_extract()` (:359) — stop last-message-as-task; emit `update`/None, not `ask`.
3. `extract_status()` (`export_data.py:54`) — word-boundary matching ('done' substring bug).
4. `reconcile_stale()` (:759) — earlier re-checks, cross-group evidence, safer keyword overlap.
5. `is_me()`/owner attribution (:47, :324) — exact-ish matching; FYI questions ≠ needs_you.
- Pin with the 5 RED tests from the QA plan (banter, FYI-link, past-tense, quoted-Q, bot digest).
