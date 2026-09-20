# Instinct nudge taxonomy → Momentum high-bar mapping

Research date: 2026-09-20. Every Instinct claim names its source inline.
Repo contract pinned: `app/nudge_scan.py` (HIGH BAR: direct-address unanswered,
or waiting_on + fresh activity + nudges ≥ 1, or needs_my_action + fresh `?`;
5-min window; cap 7; rank direct_address > waiting_on > needs_you;
snooze hides 72h, closed hides permanently).

## What Instinct is

Instinct is an invite-only personal AI assistant from Spear Street Technology
(Noah Shinn, ex-Sierra), backed by Kleiner Perkins and Conviction
([Startup Fortune, 2026-08-26](https://startupfortune.com/instincts-ai-assistant-sent-an-email-without-asking-and-testers-are-furious/)).
There is no app to open: it lives inside iMessage/WhatsApp — you text it like a
human assistant ([NYPost, 2026-09-17](https://nypost.com/2026/09/17/opinion/ai-assistant-instincts-valuation-has-quadrupled-to-10b-in-a-month-but-is-it-worth-the-hype/);
[Business Brain podcast, Sep 2026](https://businessbrain.show/fridai-instinct-10k-ai-update-business-brain-789/)).
It connects to email, messaging, calendar, screen, audio and location, and acts
via text/WhatsApp and phone calls
([TechCrunch, 2026-08-24](https://techcrunch.com/2026/08/24/instincts-powerful-ai-assistant-is-raising-privacy-and-security-concerns/)).
Valuation quadrupled to ~$10B within a month of buzz
([NYPost, 2026-09-17](https://nypost.com/2026/09/17/opinion/ai-assistant-instincts-valuation-has-quadrupled-to-10b-in-a-month-but-is-it-worth-the-hype/)).

## Delivery surface

- Conversational texts in iMessage/WhatsApp, plus voice calls — no feed, no cards
  ([TechCrunch, 2026-08-24](https://techcrunch.com/2026/08/24/instincts-powerful-ai-assistant-is-raising-privacy-and-security-concerns/);
  [NYPost, 2026-09-17](https://nypost.com/2026/09/17/opinion/ai-assistant-instincts-valuation-has-quadrupled-to-10b-in-a-month-but-is-it-worth-the-hype/)).
- Its own email identity for outbound follow-ups (contacting restaurants about a
  request, asking businesses about availability, signing up for services)
  ([TechCrunch, 2026-09-09](https://techcrunch.com/2026/09/09/viral-ai-assistant-instinct-now-has-its-own-email-address/)).
- Cadence is event-driven and always-on (monitors inbox/calendar/location
  continuously); no public fixed digest schedule found — timing follows the
  triggering event, not a clock.

## Nudge taxonomy (observed behaviors → types)

| # | Type | Trigger | Timing / cadence | Copy pattern | Action offered | Source |
|---|------|---------|------------------|--------------|----------------|--------|
| 1 | Direct-ask relay | Someone asks the user for something over a connected channel | On arrival / event-driven | "X asked you … — no reply from you yet" | Draft + send the reply | Company description: "follow up on conversations … things that need attention" ([mlq.ai](https://mlq.ai/news/instinct-is-still-invite-only-as-its-ai-assistant-takes-broad-access-to-users-data/)) |
| 2 | Unanswered-question chase | A question to the user sits without a reply | After a silence gap, while thread is live | Question quoted back + suggested answer | One-tap send | Reviewer pattern: "surface every unanswered question" as the core assistant job ([Medium/AI Advisory, Jun 2026](https://medium.com/@davidmpeterson1999/stop-letting-your-inbox-control-you-build-an-ai-follow-up-system-instead-abf7bad7ccb2)) |
| 3 | Third-party chase | User waits on a vendor/business; thread moves | On fresh activity in the thread | "Waiting on X; fresh activity … (nudge #N)" | Assistant sends the chase itself (own email identity) | "contact a restaurant …, ask a business about availability … follow up" ([TechCrunch, 2026-09-09](https://techcrunch.com/2026/09/09/viral-ai-assistant-instinct-now-has-its-own-email-address/)) |
| 4 | Owned-thread flag | Something the user owns gets a new question | On the fresh question | "Fresh question from X … on something you own" | Answer now or send holding reply | "suggested rearranging his meetings" after new info arrived ([NYPost, 2026-09-17](https://nypost.com/2026/09/17/opinion/ai-assistant-instincts-valuation-has-quadrupled-to-10b-in-a-month-but-is-it-worth-the-hype/)) |
| 5 | Itinerary prep | Trip/itinerary shared; bookings need arranging | Unsolicited, right after the itinerary lands | "I arranged X …" (statement, not a question) | None — it already acted | "Unsolicited it arranged an airport car service … followed up with his hotel" ([NYPost, 2026-09-17](https://nypost.com/2026/09/17/opinion/ai-assistant-instincts-valuation-has-quadrupled-to-10b-in-a-month-but-is-it-worth-the-hype/)) |
| 6 | Risk flag | Detects a hazard in user data (e.g. secret in sent mail) | Immediately on detection | "I caught X in your …" | Review / revoke | "It caught a plain-text password in my sent email" ([YouTube review, week two](https://www.youtube.com/watch?v=KuDh37epQdI)) |
| 7 | Silent task tracking | Ongoing commitments inferred across channels | Continuous, background | Black-box ("trust me") status | Ask for status | "Black-box task tracking that made me lose trust" ([YouTube review, week two](https://www.youtube.com/watch?v=KuDh37epQdI)) |
| 8 | Autonomous act | Assistant sends/pays/books as the user | Immediately, no confirmation | None — discovered after the fact | Undo / complain | "Sent an email without a tester's approval" ([Startup Fortune, 2026-08-26](https://startupfortune.com/instincts-ai-assistant-sent-an-email-without-asking-and-testers-are-furious/)); payments on behalf ([NYPost, 2026-09-17](https://nypost.com/2026/09/17/opinion/ai-assistant-instincts-valuation-has-quadrupled-to-10b-in-a-month-but-is-it-worth-the-hype/)) |

## Mapping onto our high bar (no lowering it)

- **Survive as-is (direct-address rule): types 1, 2** → `direct_address`
  branch: explicit address (owner name word-boundary, or you + `?`) with no
  owner reply after it. `why_now` copy mirrors the relay pattern verbatim.
- **Survive as-is (fresh-activity rule): types 3, 4** → `waiting_on` branch
  (waiting_on + fresh `last_ts` + nudges ≥ 1) and `needs_you` branch
  (needs_my_action + fresh `?`). Type 3's "assistant sends the chase itself"
  is adapted: we draft via `/api/replies` and send via `/api/send`, but never
  silently — dry-run default (`MOMENTUM_LIVE=0`), one-tap approval required.
- **Presentation only: the digest shape.** Instinct has no public digest
  cadence, but "3 things I'd send tonight" + Tonight/Tomorrow/Watching groups
  is our rendering of types 1–4 ranked direct > waiting > needs-you. Timing
  grouping is by thread age (`nudgeCluster`: <1d Tonight, <2d Tomorrow, else
  Watching), not by trigger — triggers stay inside the 5-min window.
- **Rejected — stays quiet: types 5, 6, 7.** Itinerary inference, content-risk
  flags and silent tracking have no direct address and no fresh waiting-on
  activity, so the high bar suppresses them. Type 6 is a legitimate future
  extension (new `risk_flag` reason), explicitly out of scope for this build.
- **Anti-pattern guardrail: type 8.** The "sent email without asking" backlash
  is why destructive action is a single primary pill, everything else ghost;
  sends stay dry-run until `MOMENTUM_LIVE=1`, with a 60s duplicate guard
  (`do_send`) and toast + undo on snooze/close.

## Why-now copy contract (Atlas tab)

Every card carries both lines from `GET /api/nudges`:
`why_now` ("who asked you {age} ago in {group} — no reply from you yet") and
`suggested_move` ("Send a short reply, or fire the nudge draft below.").
No new copy invented in the frontend; the tab renders API strings verbatim.
