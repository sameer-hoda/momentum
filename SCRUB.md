# SCRUB — backend significance cut (784 → 110 default)

Board before: 784 tasks in `app/frontend/frontend_data.json`
(partial snapshot 200/245 threads, exported 2026-09-20T11:06Z).

## Measurement (before)

kind: ask 513, update 74, commitment 149, decision 31, blocker 17.
status: running 698, blocked 34, completed 52.
state: needs_you 22, blocked 34, active 386, stale 290, done 52.
msg_count: 1-msg 150, 2–3 210, 4+ 424.
stale flag 317, needs_my_action 22, waiting_on 0, nudges>0 136.

## Rules and dispositions (first match wins in `assess_significance`)

| # | Rule | Keep/Drop | Count | Recall impact | Verdict |
|---|---|---|---|---|---|
| 1 | addressed-to-user (needs_my_action / waiting_on) | keep | 22 | 22/22 kept | PASS |
| 2 | stuck-needs-attention (blocked) | keep | 34 | none demoted | PASS |
| 3 | done-history (completed, unaddressed) | demote | 52 | 0 addressed among them | PASS |
| 4 | fyi-forward (`Fwd:/FYI` + update, no nudge) | demote | 0 live | — | PASS (pinned, no live match) |
| 5 | bot-context (digest text in msgs + update, no nudge) | demote | 0 live | — | PASS (pinned, no live match) |
| 6 | tiny-no-signal (update, ≤2 msgs, no nudge) | demote | 33 | 0 addressed | PASS |
| 7 | stale-quiet (stale, no nudge) | demote | 224 | 0 addressed | PASS |
| 8 | being-chased (nudge>0, age≤6d) | keep | 34 | — | PASS |
| 9 | new-and-unjudged (age≤1d, kind≠update) | keep | 20 | — | PASS |
| 10 | quiet-old / low-signal (everything else) | demote | 238 / 127 | 0 addressed | PASS |

Default board: **110 tasks** (ask 79, blocker 10, commitment 12,
decision 1, update 8). Demoted shelf: **674** under `demoted_tasks`
(flagged `significant:false` + `significance_reason`, recoverable —
the client Signal toggle composes on top of the default list).

## Extraction hard-drops (prospective — stop future junk, no live rows)

- Reaction one-liner: ["Weekend trek photos soon", "Looks great"].
  OLD `{"kind": "update", "what": "Looks great", "status": "open"}` →
  NEW `None` (`REACTION_RE`: signal-free + ≤3 words + reaction opener).
- Emoji-only: ["Nice photos from the offsite", "👍 🙏 👏"].
  OLD `{"kind": "update", "what": "👍 🙏 👏", "status": "open"}` →
  NEW `None` (`item_ok` requires a letter).
- Bot-signal laundering: digest "any update?/please review" no longer
  mints `nudges`/`has_ask` (`enrich_thread_signals` scores human
  messages only). Mixed digest thread went from kind `ask` to `update`
  (then demote rule 5 where applicable).

## Demote exemplars (old row → disposition)

- done-history <- ('Coin rush campaign', ask, age 1d, 2 msgs)
- low-signal <- ('100 was pre claim and 175 post. Why is the message
  saying re…', update, age 0d, 3 msgs — fresh but heuristic-only)
- quiet-old <- ('Take the current item live', ask, age 7d, 1 msg)
- stale-quiet <- ("Record Papa's felicitation segment during the Zoom
  meeting", commitment, age 14d, 26 msgs)
- tiny-no-signal <- ("from the team's coach", update, age 0d, 2 msgs)

## Recall proof

- needs_my_action: 22/22 on default board (100%).
- waiting_on: 0/0 (none on board before or after).
- True-ask battery (`TestTrueAskBatteryEndToEnd`: explicit ask, nudge
  follow-up, send-me ask, blocker, share-the ask — fresh threads through
  heuristic → build_task → gate): 5/5 kept.
- Full suite: 185/185 green, incl. new `tests/test_significance.py`
  (21 tests). New-code RED was verified against HEAD sandbox first
  (4 failures + 16 missing-function errors, then green).

Restore point: pre-cut export at `/tmp/frontend_data.pre-scrub.json`
(not committed). Next pipeline run regenerates the same shape via
`main()`/`publish_partial` (`split_significant` wired into both).
