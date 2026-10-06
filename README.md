# Tameion Payment Agent

An autonomous agent that receives payment requests (contractor milestones, supply
purchases, operational expenses) and decides to **approve, approve with a reduced
limit, or escalate to a human**, based on three calibrated scores: **confidence**,
**urgency**, and **importance**.

See [`docs/spec-tameion-project.md`](docs/spec-tameion-project.md) for the full
technical spec (background, architecture, priorities, and demo requirements).

## Quick start

```bash
cp .env.example .env              # then add your CIRCLE_API_KEY
pip install -e ".[dev]"           # installs deps + pytest
python -m scripts.setup_arc_wallet --wait   # one-time: entity secret + Arc testnet wallet
bash demo/run_demo.sh             # runs the 3 demo scenarios end to end
python -m pytest                  # unit tests (no credentials needed)
```

### One-time Arc testnet setup

`scripts/setup_arc_wallet.py` is idempotent (safe to re-run) and does:

1. Registers a new Circle entity secret and saves the recovery file to `./recovery/`.
2. Creates a wallet set and one wallet on `ARC-TESTNET`, writing the IDs to `.env`.
3. Opens the public faucet (https://faucet.circle.com/) so you can fund the wallet
   with testnet USDC. Funding is manual on purpose: the programmatic faucet API
   needs an upgraded Circle account.

Back up `./recovery/` somewhere safe. Both it and `.env` are git-ignored; never
commit them.

### Choosing the decision engine

Set `DECISION_ENGINE` in `.env`:

| Value | Backend | Needs |
|---|---|---|
| `kev` (default) | Self-hosted System-One-compatible server | Kev running locally |
| `jev` | The real Jev API | `JEV_API_KEY` |
| `mock` | Offline keyword heuristics (`src/decision_engine/mock_engine.py`) | nothing |

`mock` exists so the whole pipeline and dashboard can run with no external
service. It is not a model, and every audit entry records which engine
produced its scores (`engine_name`), so mock decisions are never mistaken for
real ones.

### Payment outcomes and idempotency

Every audit entry carries a `payment_status`: `executed`, `failed` (attempted
and errored), `skipped` (approved but not attempted, e.g. no
`counterparty_wallet` or no `CIRCLE_API_KEY`), or `not_applicable` (nothing
was due). `payment_detail` says why. The dashboard flags failed payments.

A `request_id` is processed at most once: resubmitting it returns HTTP 409
(CLI: a short error) and never pays twice. The same guard stops two reviewers
from both approving one escalation. Use a fresh `request_id` per payment;
`demo.seed_scenarios` stamps a new suffix on each run for this reason.

### Debugging the decision engine

`python -m demo.seed_scenarios && python -m demo.inspect_scores demo/scenarios/demo-approve.json`
prints the raw score, confidence, and probability distribution for each
question (requires a running Kev server).

## Configuring business rules without touching code

Category spending limits, policy thresholds (when to auto-approve / reduce /
escalate), and the Jev/Kev scoring rubrics all live in `config/rules.yaml`,
not in Python. It's created with sensible defaults the first time anything
reads it. Three ways to change it:

- **By hand:** edit `config/rules.yaml` directly (it's plain YAML), then
  `python -m scripts.manage_rules validate` to check it.
- **CLI:**
  ```bash
  python -m scripts.manage_rules show
  python -m scripts.manage_rules set-limit operational_expense 800
  python -m scripts.manage_rules set-threshold trust_min_auto_approve 0.8
  python -m scripts.manage_rules set-criteria-level importance 2 "Critical: ..."
  python -m scripts.manage_rules edit   # opens $EDITOR
  ```
- **Dashboard:** open `/settings` (linked from the main page) to edit limits,
  thresholds, and rubric text from a form.

Changes take effect immediately -- no restart needed, since the config is
read fresh on every request.

**Not covered by any of this:** adding a brand-new category (e.g.
`travel_reimbursement`) that the system will actually accept on incoming
requests. That's validated by the `RequestCategory` enum in
`src/requests/schema.py`, which is a data-model change, not a tunable rule --
`set-limit`/the settings page will *store* a limit for an unknown category
name, but requests using it will still fail schema validation until the enum
is updated in code.

## Dashboard (UI)

```bash
uvicorn src.api:app --reload --port 8000
```

Then open http://127.0.0.1:8000/. The dashboard:

- Shows escalated requests waiting on a human decision, with the scores and the
  reason they were escalated, and lets you **approve** (pays the full original
  amount, overriding the policy's automatic limit -- that override authority is
  the point of escalating) or **deny** them.
- Lets you submit a new payment request from a form, which runs it through the
  real pipeline (decision engine -> policy -> Arc, same as the CLI).
- Shows the full ledger (every request, newest first), with a link to the Arc
  testnet explorer for any executed payment's transaction hash.
- Links to `/settings` to edit category limits, thresholds, and scoring
  rubrics (see "Configuring business rules" above).

It's a single static HTML file (`ui/dashboard.html`, no build step) talking to
the FastAPI backend in `src/api.py`, which wraps the same `src/pipeline.py`
used by the CLI -- there's exactly one implementation of the decision logic.

## Project layout

- `src/config.py` — business rules (category limits, policy thresholds, scoring
  rubrics), backed by `config/rules.yaml`. See "Configuring business rules" above.
- `scripts/manage_rules.py` — CLI for the above.
- `src/requests/` — payment request schema and validation.
- `src/decision_engine/` — pluggable decision engine. `engine.py` defines the
  interface; `jev_client.py`, `fallback_classifier.py` (Kev) and `mock_engine.py`
  are interchangeable implementations behind it, sharing the request/question
  builders in `criteria.py`.
- `src/policy/` — deterministic rules and hard limits. This is the "contract" that
  the decision engine's scores are checked against; the engine cannot bypass it.
- `src/payments/` — Circle/Arc integration and escrow-style conditional release.
- `src/audit/` — append-only decision log (also the source for reports and the
  dashboard).
- `src/pipeline.py` — the request -> decision -> payment flow, shared by the CLI
  (`src/main.py`) and the API (`src/api.py`) so there's one implementation.
- `src/api.py` — FastAPI backend for the dashboard.
- `ui/dashboard.html` / `ui/settings.html` — the dashboard and rules-editing
  page (both static, no build step).
- `src/reports/` — generates a human-readable report from the audit log.
- `tests/` — unit tests.
- `demo/` — reproducible scenarios for the 3-minute demo video.

## Design note

The decision engine is intentionally isolated behind one interface
(`decision_engine/engine.py`). Which model actually powers it (Jev, a trained
classifier, or an LLM constrained to a JSON schema) is an implementation detail,
not something the rest of the system depends on.
