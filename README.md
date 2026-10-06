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

### Debugging the decision engine

`python -m demo.inspect_scores demo/scenarios/demo-approve.json` prints the raw
score, confidence, and probability distribution for each question.

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

It's a single static HTML file (`ui/dashboard.html`, no build step) talking to
the FastAPI backend in `src/api.py`, which wraps the same `src/pipeline.py`
used by the CLI -- there's exactly one implementation of the decision logic.

## Project layout

- `src/requests/` — payment request schema and validation.
- `src/decision_engine/` — pluggable decision engine. `engine.py` defines the
  interface; `jev_client.py` and `fallback_classifier.py` are two interchangeable
  implementations behind it.
- `src/policy/` — deterministic rules and hard limits. This is the "contract" that
  the decision engine's scores are checked against; the engine cannot bypass it.
- `src/payments/` — Circle/Arc integration and escrow-style conditional release.
- `src/audit/` — append-only decision log (also the source for reports and the
  dashboard).
- `src/pipeline.py` — the request -> decision -> payment flow, shared by the CLI
  (`src/main.py`) and the API (`src/api.py`) so there's one implementation.
- `src/api.py` — FastAPI backend for the dashboard.
- `ui/dashboard.html` — the dashboard itself (static, no build step).
- `src/reports/` — generates a human-readable report from the audit log.
- `tests/` — unit tests.
- `demo/` — reproducible scenarios for the 3-minute demo video.

## Design note

The decision engine is intentionally isolated behind one interface
(`decision_engine/engine.py`). Which model actually powers it (Jev, a trained
classifier, or an LLM constrained to a JSON schema) is an implementation detail,
not something the rest of the system depends on.
