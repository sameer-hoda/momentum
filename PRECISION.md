# Task-extraction precision note

Battery: 5 FP fixtures (F1–F5) + 5 true-task threads (T1–T5) through
`heuristic_extract` / `worth_llm`. OLD = `git HEAD` code, NEW = worktree.
Probe: `/tmp/extract_counts.py` (throwaway, not committed).
Pins: `tests/test_extraction_fixtures.py` (5 tests) plus existing
`test_item_gates.py`, `test_pipeline_fp.py`, `test_status_owner.py`,
`test_derive_state.py`.

Counts: OLD FP emitted 3/5, TRUE emitted 5/5 → NEW FP emitted 0/5,
TRUE emitted 5/5 (all 5 true-task outputs byte-identical OLD vs NEW).

Note: `tests/fixtures/g3/` holds bridge QR/pairing logs (no chat
content), so it cannot serve as an extraction battery; the battery above
uses real-shape thread fixtures instead.

## F1 — banter thread (3 msgs, no signal)

Fixture: "Morning all, coffee run at eleven" / "Haha count me in, see you
there" / "Great weather today, lovely morning".

- OLD: `worth_llm=True`, `got={"items": [{"kind": "update", "what":
  "Great weather today, lovely morning", "status": "open", ...}]}` — a
  task from pure banter, plus a wasted LLM call (size ≥3 auto-qualified).
- NEW: `worth_llm=False`, `got=None`.
- Gates: `worth_llm` signal-only (size never qualifies);
  `BAD_FIRST_WORDS` += greetings/reactions (`haha`, `great`, …).

## F2 — FYI-link share

Fixture: "FYI team, Q3 numbers are out" /
"https://docs.google.com/q3-report".

- OLD: `worth_llm=False`, `got=None`.
- NEW: `worth_llm=False`, `got=None` (unchanged).
- Gate: `JUNK_URL_RE` (predates HEAD); newly pinned at thread level.

## F3 — past-tense recap

Fixture: "Shared the deck yesterday" / "Got the numbers too".

- OLD: `worth_llm=False`, `got={"items": [{"kind": "update", "what":
  "Got the numbers too", "status": "open", ...}]}` — narration as task
  (`-ed` check misses irregular past tense).
- NEW: `worth_llm=False`, `got=None`.
- Gate: new `IRREG_PAST` set (`sent/went/got/said/…`); present-tense
  twins (`Send/Make/Take/Go`) stay valid, pinned in
  `TestImperativeRecallKept`.

## F4 — quoted question

Fixture: "Rohan is coming tomorrow?".

- OLD: `worth_llm=True`, `got=None`.
- NEW: `worth_llm=True`, `got=None` (unchanged).
- Gate: `QUOTED_Q_RE` + bare-`?`-is-not-ask-signal (predates HEAD);
  newly pinned at thread level. Known spend: bare `?` still earns an LLM
  call (recall tradeoff — the LLM prompt returns `[]` for these).

## F5 — bot digest (multi-message, all bot)

Fixture: "⚡ *Pending Follow-Ups* open items: merchant offers any
update?" / "📋 *Last 24 Hrs* 5 follow-ups pending, please review".

- OLD: `worth_llm=True`, `got={"items": [{"kind": "ask", "what":
  "📋 *Last 24 Hrs* 5 follow-ups pending, please review", "status":
  "open", ...}]}` — an automated digest emitted as an **ask**.
- NEW: `worth_llm=True`, `got=None`.
- Gate: `heuristic_extract` all-messages-match-`BOT_RE` early `None`
  (single-bot case also covered by pre-existing pin).

## Recall battery (all byte-identical OLD vs NEW)

T1 "Please share the numbers by EOD" → ask, `what="share the numbers
by EOD"`. T2 "any update on the merchant offers?" → ask. T3 "Send me
the invoice today" → ask. T4 "Unblock the checkout dip, stuck on API
key" → update, `status="blocked"`. T5 "Share the deck with Rohan" →
ask. (T4 `worth_llm=False` on both — pre-existing signal-gate
characteristic, unchanged; the heuristic fallback still extracts it.)
