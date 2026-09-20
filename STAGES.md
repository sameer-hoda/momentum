# Startup stages — QR to portal (observed, not assumed)

Pipeline: Go bridge (`bridge/main.go`, REST `:8080`) syncs WhatsApp into
`local-store/messages.db`; `app/provision.py` walks the stages below and
writes `/tmp/provision.json` + `<store>/provision.json`; `app/momentum_api.py`
serves the UI + `/api/status`; `app/build_data.py` writes
`app/frontend/frontend_data.json`; the boot loader in `app/frontend/index.html`
polls `/api/status` until `ready` (or renders early from a partial snapshot).

| Stage | Trigger (what moves it forward) | How verified |
|---|---|---|
| G0 services | bridge + API processes alive, ports bound | `ps` shows `wabridge` + `momentum_api.py`; `curl :8080/api/health` → 404 (bridge up, no such route); API banner prints `reachable` |
| G1 sign-in | session cookie or Basic auth; else sole login screen | unauth `GET /api/status` → 401, boot loader redirects to `/login` after 2 fails (read in `index.html`); authed → 200 |
| G2 key | env `GEMINI_API_KEY` (fresh-process check) or volume `gemini.key` or `.skip-key`; 10s poll | `tests/test_provision.py`: parent stays genai-clean; old in-process ping rejected valid keys (`RuntimeError` in `provision_run.log`) — fixed to `journey_gates.validate_key_fresh` |
| G3 paired | `messages.db` count > 0 OR bridge-log connected hint; QR always shown while failing, 20s refresh, 30-min cap | `tests/test_journey_gates.py` (11 G3 tests incl. mid-write + logout); `/api/qr` now serves newest **complete** block + `age_s`/`stale`/`trusted` (`tests/test_api_contract.py`); live log shows 19 bare `QR_BEGIN`s, no `QR_AT` yet — bridge now emits `[QR_BEGIN QR_AT=…]` (Go, built OK, live at next bridge restart) |
| G4 synced | 3 stable 15s polls, count > 0 (bridge-down polls pause) | gate logic in `tests/test_journey_gates.py` (stable/reset/zero/bridge-down/stall causes); live history in `provision_run.log`: 0 → 4097 → … → 51596 msgs then stable |
| G5 analyzed | `build_data.py` exit 0; output parses, tasks > 0, `exported_at` fresh; heartbeat every phase | `validate_analysis_output` on real partial: ok; `build_heartbeat.json` live (`extract 350/983`); partial snapshots (`partial:true` + progress) render the board mid-run |
| G6 board / ready | tasks load; empty states name cause + one-click fix | boot loader renders from partial (`enterLiveMode`); `describe_empty_board` pinned (5 tests); second reset: TRUE ZERO (no snapshot on disk, `awaiting_qr`, 0 msgs) — real rebuild still pending user decision; `demo_seed.write` interlock refuses the live path outside `DEMO_MODE=1`, demo verification uses scratch stores + restore-to-absent |

DEMO_MODE=1 shortcut: `analyzing` (seed) → `ready` (50 demo tasks) in seconds.
Ran 3× consecutively, zero manual intervention: provision log lines
`analyzing: seeding demo workspace` → `ready: demo workspace: 50 demo tasks`,
G5 gate ok, `GET /api/health` → 200, authed `/api/status` → `ready`,
`frontend_data.json` served with 50 tasks; stage walk
`awaiting_qr → syncing → analyzing → ready` returns each shape.

## Gotchas hardened (with tests)

- `/api/status` stays authed (boot 401→login depends on it); NEW public
  `GET /api/health` → `{ok, stage, build, updated}` for the Railway
  healthcheck + audit loop. `railway.toml` healthcheckPath → `/api/health`.
- `bridge_ok()`: only HTTP 404 counts as alive (Go mux, verified live);
  500/401 → False, no TCP-masking. Old code returned True for ANY error.
- Port collision: bridge port overridable via `WA_BRIDGE_PORT` (default 8080,
  unchanged); API warns loudly at startup when `PORT == bridge port`
  (`ports_collide`, tested).
- `persistent` flag extracted to tested `is_persistent_store()`; local dirs
  and missing mounts read False.

## Pending (needs a human / restart — not done here)

- Live API `:8099` was reset fresh after these fixes and now runs the fixed
  code (verified: `GET /api/health` → 200 `{ok, stage: awaiting_qr}`).
- Live bridge still runs the pre-fix binary (`local-run/wabridge` was not
  rebuilt): restart it from the edited `bridge/main.go` to emit `QR_AT`
  markers and honor `WA_BRIDGE_PORT` (old bare blocks keep working via the
  legacy fallback meanwhile — verified live: 35-line QR, untrusted, age 13s).
- `railway.toml` healthcheck change is edited but NOT deployed (no human OK).
- Full portal click-through needs a human session (login password unknown to
  agents); verified to the login gate + all API shapes.
- Provision sync loop has no bridge-alive pause (pure gate exists + tested,
  UI shows stall client-side); env-key liveness check stays at UI key-entry
  time (`validate_gemini_key`), not in the provisioner.
