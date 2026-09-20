# Journey v2 — gated onboarding: no step proceeds without its inputs.
# Design: every stage is OWNED by an agent, gates are machine-checked, and the
# UI shows Queue/Task-style background progress (AI Elements: Queue + Task).
# Refs: https://elements.ai-sdk.dev/components/queue
#       https://elements.ai-sdk.dev/components/task

## The guarantee
For ANY user, from scratch: sign-in -> key -> pair(QR) -> sync -> analyze -> board.
It is IMPOSSIBLE to be on step N unless steps 1..N-1 gates all pass. Every stall
gets a named cause + exact fix + elapsed time. No silent hangs, ever.

## Gate contracts (each gate is a pure function of observable state)
- G0 SERVICES: bridge process alive AND api listening. Else: "starting services",
  auto-retry with backoff, show which one is down.
- G1 AUTH: valid session cookie. Else: sign-in screen (sole screen, nothing else).
- G2 KEY: valid Gemini key (fresh-process ping, never the buggy in-process one)
  OR explicit heuristic-opt-out receipt (.skip-key). No silent fallback: the mode
  (AI vs heuristic) is SHOWN on every later screen.
- G3 PAIRED: messages.db count > 0 OR bridge-log "connected/logged in" hint.
  QR is ALWAYS visible while G3 fails: backend caches latest QR + its age, pushes
  refresh every 20s; frontend shows QR + countdown + "code refreshes automatically".
  QR NEVER goes stale-silently: each code shows age + refresh tick.
- G4 SYNCED: 3 consecutive stable 15s polls AND count > 0 AND bridge still alive.
  Progress: count + rate + elapsed + per-1000 checkpoint ticks. Stall>60s: cause
  callout ("phone may be asleep — keep WhatsApp open") not "idle".
- G5 ANALYZED: frontend_data.json exists AND parses AND tasks>0 AND fresh
  (exported_at newer than last sync change). Build runs supervised: heartbeat log
  every 30s, watchdog restarts once on 5-min silence, then surfaces the REAL
  traceback (never "check deploy logs" locally).
- G6 BOARD: data loads into UI; empty-tasks state explains why (0 groups mapped?
  all filtered? key missing?) with one-click fixes, never a blank board.

## Journey progress UI (Queue + Task language, bespoke CSS, no new deps)
- A persistent journey rail (Queue): sections Queued (upcoming) / Todo (active) /
  Done, counts per section, collapsible. Active section expands to Task items:
  each step = TaskTrigger (title + status icon: pending/spinner/check/error) +
  TaskContent (live detail: QR / count+rate / heartbeat / error+fix).
- Statuses: pending (grey), in_progress (accent pulse), completed (green check),
  error (red, with cause + fix + retry button). Progress counter "3/6 steps".
- Rich + fancy within current language: aurora accent gradient, tabular-nums,
  motion-safe pulse, reduced-motion honored, mono for all counts/ages.
- The rail lives INSIDE the boot overlay AND persists as a dismissible activity
  drawer post-ready (background rebuilds show there, never silently).

## Agents (muse-spark-1.3 · Ayulo mode)
- ORCHESTRATOR: owns journey state machine (G0..G6), gate evaluation order,
  watchdog timers, and cross-step invariants (e.g. never analyzing without G4).
  Sole writer of /tmp/provision.json + provision.json schema {stage, detail,
  gates:{g0..g6:pass/fail}, messages, tasks, updated, error_cause, error_fix}.
- G0 SERVICES agent: process supervision (bridge/api/provision alive, ports bound,
  auto-restart once, then truthful error). Owns start-all.sh/stop-all.sh/restart.
- G1 AUTH agent: session gate, login/setup flows, 401->login redirect everywhere.
- G2 KEY agent: key validation in FRESH processes, volume-file persistence,
  heuristic-opt-out receipts, mode badge (AI/heuristic) on all screens.
- G3 PAIR agent: QR lifecycle — always-fresh QR cache (code+issued_at), 20s push,
  frontend countdown, paired detection (count>0 OR log hint), logout recovery.
- G4 SYNC agent: stable-poll logic, rate/elapsed/checkpoints, stall callouts with
  named causes (phone asleep? bridge down? Wi-Fi?), never "idle" without reason.
- G5 ANALYZE agent: supervised build_data runs (heartbeat, watchdog, real
  tracebacks surfaced), empty-OWNER_NAME crash class eliminated + regression test,
  incremental resume, output validation (parse + tasks>0 + freshness).
- G6 BOARD agent: data loading, empty-state explanations with one-click fixes,
  Queue/Task journey rail UI + activity drawer, showcase-first (taste-skill).
- QA agent: gate-contract tests (each G0..G6 pass/fail as unit tests with
  fixtures), fresh-start drill script, 49+ suite stays green.

## Non-goals (this pass)
- No Railway changes. No new pip/npm deps. No framework migration.
- Heuristic-vs-AI quality tuning stays in the categorization track.
