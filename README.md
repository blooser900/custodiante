# Tameion Payment Agent

An autonomous agent that receives payment requests (contractor milestones, supply
purchases, operational expenses) and decides to **approve, approve with a reduced
limit, or escalate to a human**, based on three calibrated scores: **confidence**,
**urgency**, and **importance**.

See [`docs/spec-tameion-project.md`](docs/spec-tameion-project.md) for the full
technical spec (background, architecture, priorities, and demo requirements).

## Quick start

```bash
cp .env.example .env      # fill in real API keys
pip install -r requirements.txt   # or: poetry install
python -m demo.seed_scenarios     # generate example payment requests
python -m src.main --input demo/scenarios/example_1.json
```

## Project layout

- `src/requests/` — payment request schema and validation.
- `src/decision_engine/` — pluggable decision engine. `engine.py` defines the
  interface; `jev_client.py` and `fallback_classifier.py` are two interchangeable
  implementations behind it.
- `src/policy/` — deterministic rules and hard limits. This is the "contract" that
  the decision engine's scores are checked against; the engine cannot bypass it.
- `src/payments/` — Circle/Arc integration and escrow-style conditional release.
- `src/audit/` — append-only decision log (also the source for reports).
- `src/reports/` — generates a human-readable report from the audit log.
- `tests/` — unit tests, focused mainly on `policy/rules.py`.
- `demo/` — reproducible scenarios for the 3-minute demo video.

## Design note

The decision engine is intentionally isolated behind one interface
(`decision_engine/engine.py`). Which model actually powers it (Jev, a trained
classifier, or an LLM constrained to a JSON schema) is an implementation detail,
not something the rest of the system depends on.
