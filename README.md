# Custodiante

An autonomous agent that receives payment requests (contractor milestones, supply
purchases, operational expenses) and decides to **approve, approve with a reduced
limit, or escalate to a human**, based on three calibrated scores: **trust**,
**urgency**, and **importance**. Each score also carries a `confidence`, which is
recorded and shown to managers but does not gate the decision -- see the note in
`src/config.py` on the auto-approve rule it used to gate.

See [`docs/spec-tameion-project.md`](docs/spec-tameion-project.md) for the full
technical spec (background, architecture, priorities, and demo requirements),
and [`docs/TODO.md`](docs/TODO.md) for the roadmap: the work deliberately
deferred past the hackathon and the gaps still open in the current behaviour.

## Quick start

```bash
cp .env.example .env              # then add your CIRCLE_API_KEY
uv sync --extra dev                # deps + pytest, exactly the locked versions
python -m scripts.setup_arc_wallet --wait   # one-time: entity secret + Arc testnet wallet
python -m demo.run_demo           # runs the 3 demo scenarios end to end
                                  # (it also registers the two demo payees in the vendor
                                  #  registry, as a manager would: the payee check is ON by default)
python -m pytest                  # unit tests (no credentials needed)
```

`uv.lock` pins every dependency. `pyproject.toml` also pins the direct ones
exactly, because this system moves money and reads model scores: two installs of
the same commit must decide the same way, or the audit log cannot be replayed.

**If `pytest` fails with a `ModuleNotFoundError` for a Circle model**, run
`python -m pytest tests/test_circle_sdk_available.py` first. It names the real
problem instead of leaving the cryptic lazy-import error.

**Before you reinstall anything, rule out a sandboxed file policy.** The SDK's
`models/` directory is 107 files, and a tool that denies `os.stat`/`open` on some
of them *by path* while `os.listdir` still lists them produces exactly the same
`ModuleNotFoundError`. If the guard reports unreadable files, confirm the install
is actually broken before touching it: compare two independent readers of the
same file (Python vs. your shell), and check that the wheel in the uv cache
contains the file and that the on-disk copy is the same size. A healthy install
whose files read fine from a shell but not from Python is an environment policy,
not a dependency problem — reinstalling will not change it.

Also note that `pytest` is in the `dev` extra: `uv sync` alone does **not**
install it, and the suite cannot run without `uv sync --extra dev`.

### One-time Arc testnet setup

`scripts/setup_arc_wallet.py` is idempotent (safe to re-run) and does:

1. Registers a new Circle entity secret and saves the recovery file to `./recovery/`.
2. Creates a wallet set and one wallet on `ARC-TESTNET`, writing the IDs to `.env`.
3. Opens the public faucet (https://faucet.circle.com/) so you can fund the wallet
   with testnet USDC. Funding is manual on purpose: the programmatic faucet API
   needs an upgraded Circle account.

Back up `./recovery/` somewhere safe. Both it and `.env` are git-ignored; never
commit them.

### What the engine is asked, and what it is shown

Each request goes to the engine as one call carrying every question, with a
`state` that holds the request **and the history the system has recorded**
(`src/history.py`, computed from the audit log):

- **Questions.** The three core scores the policy reads (`trust`, `urgency`,
  `importance`) plus the extra questions in `config/rules.yaml`
  (`assessment_questions`). The defaults are the antifraud set: a `choice` for
  the nature of the transaction (`natureza_transacao`, which offers
  `risco_fraude` as an explicit option), a `noul` for fraud indicators, and a
  `noul` for whether the description justifies the category. They are editable
  (types `noul`, `choice`, `score`; at most 7 extras; an empty section turns
  them off). One factor per question: split compound judgments.
- **When it arrived.** The state carries `submitted_at` (UTC), `local_hour` and
  `utc_offset`. The hour of day is one of the strongest fraud signals there is —
  a transfer requested at 03:00 is a different animal from the same amount at
  10:45 — and nothing else in the model recorded it. The pipeline stamps
  `submitted_at`; a caller may set it explicitly to stay reproducible (the demo
  seeds and the calibration scenarios do). A naive timestamp is read as UTC,
  because assuming it is local would be a guess, and a wrong guess silently
  mislabels the signal.
- **History.** For the destination (keyed by wallet) and the requester (keyed by
  the typed name): attempts and outcomes (succeeded / failed / refunded /
  denied), recent bad outcomes, first-seen age, average successful amount, the
  ratio of this amount to it, and a `reliability_index` = (succeeded + 1) /
  (attempts + 2), so one lucky payment cannot buy a high score and a party
  with no history sits at 0.5, not at "good".
- **Reading the answers.** A `noul` is the probability of "yes" and has no
  confidence of its own (0.5 is an even split, not "medium"). For `choice` and
  `score`, `confidence` reflects how concentrated the probabilities are, not
  whether the answer is right: a benign request spread over several plausible
  options gets a low confidence. Threshold on probabilities, from labelled data.
- **Everything is recorded.** Each audit entry stores the answers and an
  `evidence` block: the exact `state` the model saw, its raw answers with
  probabilities, the model name and latency. History changes over time; without
  this a past decision could not be explained. Managers see all of it (pending
  escalations and the expandable ledger row); requesters never do.

**Status: two answers decide, the rest inform a human.** The policy decides from
`trust` and, since the fraud veto, from `fraud_indicators`. `urgency`,
`importance`, `natureza_transacao` and `description_matches_category` are
recorded and shown to a manager. The policy, not the model, stays the authority.

**What decides, and exactly how.**

- **`fraud_indicators` (a `noul`) is a veto, and so is the nature question.**
  There are two independent paths, and either one is enough:

  1. `fraud_indicators` at or above `thresholds.fraud_veto_at` (default 0.7);
  2. `natureza_transacao` answering `risco_fraude` — the engine saying so
     outright rather than as a probability.

  At or above the bar, no automatic approval happens: the request escalates,
  whatever the trust score, the urgency and the amount say. It is a veto and
  *not* a refusal, deliberately — it can stop the machine approving on its own,
  but it can never stop a person, so a human reviewing the escalation can still
  approve it. A missing or mistyped answer on either question leaves that path
  off rather than stalling the queue: a safety control that fires because the
  engine failed to answer would be worse than none. `RulesConfig` refuses to
  save `fraud_indicators` as anything but a `noul`, because retyping it would
  silently disable that path.
- **`trust` is the hard stop and the auto-approve gate**: below
  `trust_min_to_consider` it escalates immediately, and at or above
  `trust_min_auto_approve` it is approved without a human.

**Why there is no confidence threshold.** The auto-approve rule used to also
require `trust.confidence >= 0.7`. Calibration on 39 real answers from the
engine showed that bar is unreachable: the confidence of `trust` never exceeded
0.60, so the clause never fired. Removing it doubled the legitimate
auto-approvals (0/12 good scenarios to 6/12) and blocked **no** bad case. The
reason is that confidence measures how concentrated the model's distribution
was, not whether the answer is right — and in that data the bad cases had the
*higher* confidence (mean 0.42 against 0.30 for good ones), because a clear
fraud story is a crisp narrative. Confidence is still recorded and shown to
managers; it just no longer decides.

## The payee registry, and why scores are not enough

There is one check that no score and no probability can make, because it is not
a judgement: **is this wallet one the payee is allowed to be paid at?**

The history in `src/history.py` is keyed by the wallet *in the request*. So it
answers "has this wallet been paid cleanly before?" — not "is this the wallet of
the vendor being invoiced?". Those come apart exactly where it costs money.
Measured on real engine answers, a request that redirected an established
contractor's payment to a different account scored **trust 0.81** (above the
0.75 auto-approve bar) and **P(fraud) 0.36** (nowhere near the 0.7 veto), and was
classified as an ordinary purchase. The new wallet had six clean payments of its
own, so it looked established. The system paid it with no human involved — and a
bigger model made it *worse*, raising the trust score rather than the fraud
signal.

`src/payees.py` closes that. A payee is the request's `vendor` when one is named,
otherwise the `requester`. Two sources of knowledge are merged:

- **registered** — `vendor_registry` in `config/rules.yaml`: a manager saying
  "this payee uses these wallets". Authoritative, and the only way to authorise a
  genuinely new wallet.
- **observed** — the audit log. A payee already paid successfully, and not
  refunded, at a wallet is observed to use it. Without this the registry would be
  empty on day one and every request would escalate until someone typed the whole
  vendor list in.

The outcomes, both of which **escalate rather than deny**:

| situation | decision |
|---|---|
| wallet is registered for the payee | continues to the normal rules |
| payee is registered, wallet is not one of theirs | **escalate** — this is the redirected invoice |
| payee is not in the registry at all | **escalate** — a first payment should be confirmed by a person |

Escalating rather than denying is what keeps a false positive cheap: a first-time
vendor or a vendor that really did change banks costs a human click, not a blocked
payment.

**The check is on by default, and an empty registry does not switch it off.** An
empty registry means every payee is unknown, so on a fresh install the first
payment to each payee waits for a person, and from then on the audit log
remembers the wallet. (An earlier version skipped the check while the registry was
empty. That exempted the very first payment, while the second payee already needed
a human, and because a failed or `unconfirmed` payment teaches the registry
nothing, it also left the check off for every request until one succeeded.) Only a
payment that settled counts as proof of use, so a first payment stuck in
`unconfirmed` does not open the gate.

To avoid a click per vendor, register the ones you already know:

```bash
python -m scripts.manage_rules register-vendor "Acme Ltda" 0xabc... --note "contract 2026-14"
```

That adds wallets and never replaces them: a vendor that really changed banks gets
the new wallet next to the old one, and removing the old one is a deliberate
`edit`. For a demo, or when rolling the check out on a system that already has
history, it can be switched off explicitly with
`python -m scripts.manage_rules payee-check off` (`show` prints which state it is
in, and `on` puts it back). The flag is `require_known_payee` in
`config/rules.yaml`; a config file written before it existed has no such key, and
a missing key means **on**.

## Part payments are not always an option

The reduced-approval branch pays a *fraction* of the category ceiling. Half a
supply purchase is still supply; **half an agreed contract price is not half a
delivery** — it pays for work that has never been accepted, or breaks the
contract outright. So the category decides:

```yaml
category_limits:
  contractor_milestone: { max_usdc: 2000.0, allow_reduced: false }
  supply_purchase:      { max_usdc: 1000.0, allow_reduced: true }
```

When `allow_reduced` is false and the reduced branch would have applied, the
policy **escalates** and says why, instead of quietly paying half. A bare
`contractor_milestone: 2000.0` in an existing config file is still read (the
pre-`allow_reduced` form), and takes the shipped default for its category — so
upgrading cannot silently re-enable part payments for a milestone.

**Known weaknesses of the history.**
- The requester's history is keyed by a name anyone can type, so it is weak
  evidence (it is labelled `self_declared`, and shown with a warning). A wallet
  address cannot be faked, so the destination's history is solid. Strong
  requester history needs authenticated requesters.
- It can be farmed: small clean payments, then a big one. History should lower
  friction inside the limits, never raise them.
- `failed` cannot tell a bad destination from an outage on our side.
- The log starts empty. For a demo, build a record by running real small payments
  first; do not fabricate entries in the hash-chained log.
- `jev-latest` moves when a new model is released and can change scores and
  confidence. Pin a version with `JEV_MODEL` for reproducible decisions.

### Collecting real answers for calibration

The extra questions are recorded but do not decide anything yet, because a
threshold picked without seeing real answers would be a guess. To get the data:

```bash
python -m scripts.collect_samples                 # 13 scenarios: good, doubtful, bad
python -m scripts.collect_samples --repeat 3      # also measure run-to-run noise
python -m scripts.collect_samples --only good_routine_supplies,bad_denied_before
```

It uses the engine selected by `DECISION_ENGINE`, through the same
`engine.score()` as the app, and writes `data/samples/samples-<time>.jsonl`:
a header with the questions, then one line per run with the exact `state` the
engine was shown and its raw answers (probabilities included). Each scenario
has a hand-built history, so the run is reproducible and never touches the real
audit log. At the end it prints a table and the mean P(yes) per group
(good / doubtful / bad), which is the first read on where thresholds could go.

**No timeout.** The script waits as long as the engine needs (a model on a CPU
can take many seconds), and every answer is written the moment it arrives, so
Ctrl-C or a crash loses nothing already collected. It stops by itself after 3
failures in a row. The app's own engine timeout is 15 s by default; for a slow
self-hosted engine set `ENGINE_TIMEOUT_SECONDS=none` (or a number) in `.env`.

### Choosing the decision engine

Set `DECISION_ENGINE` in `.env`:

| Value | Backend | Needs |
|---|---|---|
| `kev` (default) | Self-hosted System-One-compatible server (same protocol as Jev) | Kev running locally |
| `jev` | The real Jev API | `JEV_API_KEY` (and optionally `JEV_MODEL` to pin a version) |
| `mock` | Offline keyword heuristics (`src/decision_engine/mock_engine.py`) | nothing |

`mock` exists so the whole pipeline and the apps can run with no external
service. It is not a model, and every audit entry records which engine
produced its scores (`engine_name`), so mock decisions are never mistaken for
real ones.

### Payment outcomes and idempotency

Every audit entry carries a `payment_status`: `executed`, `failed` (attempted
and definitely did not happen: Circle refused it, or it ended FAILED, CANCELLED
or DENIED), `unconfirmed` (sent, and we could not find out how it ended: see
below), `skipped` (approved but not attempted, e.g. no `counterparty_wallet` or
no `CIRCLE_API_KEY`), or `not_applicable` (nothing was due). `payment_detail`
says why. The dashboard flags failed payments.

A `request_id` is processed at most once: resubmitting it returns HTTP 409
(CLI: a short error) and never pays twice. The same guard stops two reviewers
from both approving one escalation. Use a fresh `request_id` per payment;
`demo.seed_scenarios` stamps a new suffix on each run for this reason.

**Two rows per payment, because paying is not atomic with logging.** The
`request_id` check happens before anything moves, but it cannot survive a
crash: if the process died between sending the transfer and writing the result,
the log would show nothing and a retry would pay a second time. So an approved
payment appends **two** entries, whether it was approved by the policy or by a
human clearing an escalation (both go through the same `_settle_recorded`, so
neither can skip the first row):

1. a `pending` row *before* Circle is called, carrying `payment_attempt` (what
   we are about to do) and `payment_amount_usdc` (the figure authorized);
2. the outcome row, which clears `payment_attempt` and records the real status.

While a row still carries `payment_attempt`, that payment is unaccounted for.
A retry raises `PaymentInFlightError` (HTTP 409) instead of paying again, the
dashboard counts it as *Unconfirmed*, and the way out is **Reconcile** in the
dashboard (`POST /api/requests/{id}/reconcile`). The system will not guess: on
such a row the money is unknown, not zero, which is why it is reported apart from
failed payments.

### Getting an approved payment unstuck

An approved direct transfer can stop short of paid in three ways, and each has its
own way out. Both actions are manager-only and take a `reviewer` name for the log.

| Payment status | What happened | What the dashboard offers |
|---|---|---|
| `unconfirmed`, or a row left `Unconfirmed` by a crash | The transfer may have gone out and the outcome is unknown | **Reconcile**: asks Circle how it ended. COMPLETE becomes `executed`; FAILED, CANCELLED or DENIED becomes `failed`. Read only: it never sends money, and when Circle is still working or has no record it records nothing, so pressing it again does not fill the log. |
| `unconfirmed` and Circle has no record of it | Probably never sent (e.g. the process died first) | **Resend…**: resends with the *same* idempotency key, and only after a fresh lookup finds nothing. If the lookup missed an existing transfer, Circle answers with it instead of creating a second one. |
| `failed` | Circle refused it or it ended FAILED, CANCELLED or DENIED: the money did not move | **Try again**: a new attempt number, hence a new key (the old one may replay the failure). |
| `skipped` | Nothing was ever sent, e.g. `CIRCLE_API_KEY` was missing | **Try again**, once the cause is fixed. |

A retry is a payment like any other: it writes the in-flight row first, so a crash
during it is just as visible (`PaymentInFlightError`, HTTP 409, on the next click).
`attempt_number` on the log row says which try it was. Escrow payments are not
handled here: they are moved with the milestone actions, which check the chain.

### Amounts are exact where it matters

Amounts travel as plain JSON numbers (request bodies, the audit log, the
dashboard), which is fine for carrying the decimal a human typed. What a float
cannot do is arithmetic that ends up on the wire: `str(0.000001)` is `'1e-06'`,
`0.1 + 0.2` is `0.30000000000000004`, and `333.33 * 0.35` is `116.66549999999999`.
USDC has 6 decimal places, so each of those is a rejected transfer or an amount
nobody authorized. `src/money.py` is the one place that decides:

- **Entering:** an amount must be positive, finite, at most 6 decimal places and below
  a sanity ceiling of 10¹² USDC. `run_pipeline` checks it first, before the engine is
  called or anything is logged, and the API answers 422 with the reason (the
  requester route validates it in its own model).
- **Computed:** the reduced approval is `limit × fraction` in `Decimal`, rounded
  **down** to 6 places, so the system never authorizes more than the rule gave. If
  it rounds to zero the request escalates instead of approving nothing.
- **Leaving:** the amount sent to Circle and the integer sent to the escrow contract
  are rendered from a `Decimal` (`format_usdc`, `usdc_to_base_units`), never
  `str(float)`.
- **Shown:** dashboard totals are summed as decimals.

The model fields stay `float` on purpose. Making them `Decimal` would change the JSON
shape (strings), the audit log and the dashboard for no gain once the four rules
above hold. Order comparisons (`amount > limit`) stay on floats because two decimals
with at most 15 significant digits keep their order as doubles. Rows written before
this check, with more precision than USDC has, still load and still add up; they just
cannot be paid until corrected.

**`unconfirmed` is not `failed`.** If the poll runs out of time with the
transfer still in flight, a poll call errors, or the create call dies without an
answer, the transfer may have been sent. That outcome keeps the `payment_attempt`
marker (so the request stays blocked) and records the Circle `transaction_id` in
its own field. The request id is also sent to Circle as `refId`, so the
transaction can be found in Circle's console either way.

**The transfer carries an idempotency key** derived from the request id
(`payment_idempotency_key`, deterministic and shaped as the UUID v4 Circle
documents), so repeating the call cannot make Circle
move the money twice. A deliberate retry after a transfer is *known* to have
failed must use a new `attempt` number; how Circle answers a repeated key whose
transaction failed has not been verified against the sandbox. Escrow does not use
a key: it checks the job's on-chain status before every transaction.

**Still in-process.** The per-request lock is a `threading.Lock`, so it protects
one uvicorn worker (how the app is run) and one process lifetime. A second
worker, or a restart mid-payment, falls back to the `payment_attempt` guard: it
prevents the second payment, but a human has to reconcile.

### On-chain escrow (contractor milestones)

For `contractor_milestone` requests the policy's approval no longer sends a
transfer: it opens a job in the ERC-8183 contract that Arc already deploys on
testnet (no Solidity of our own), and the money stays locked until a human
confirms the work. Our treasury is the client and evaluator; the contractor
is the provider.

```
approved ─► createJob ─► (contractor) setBudget ─► fund ─► (contractor) submit
                                                      │                  │
                                              reject ─┴─► refunded       └─► release ─► paid
```

- The dashboard's **Milestones em escrow** panel shows each job with its live
  on-chain status and offers fund / release / reject. Release is only enabled
  once the chain says the contractor has submitted.
- Every step appends an entry to the audit log (`escrow_job_id`,
  `escrow_status`: `open` -> `funded` -> `submitted` -> `released` | `rejected`).
  Money counts as *paid* only on release; locked funds show separately.
- A human approving an escalated milestone also goes through escrow.
- `ESCROW_ENABLED=false` switches back to direct payment.

Safety property: the contractor sets the job's budget, so it is untrusted.
`fund` compares it to the amount the policy approved and sends nothing on a
mismatch; it also approves exactly that amount, never more. Only the request
id is written on-chain, never the description.

**Demo mode.** If `ESCROW_PROVIDER_WALLET_ADDRESS` equals the request's
`counterparty_wallet`, we control the contractor's key: the pipeline sets the
budget and funds the escrow immediately, and the dashboard offers "Simulate
delivery (demo)". With a real contractor they set the budget and submit
themselves.

**Verification status.** Validated on Arc testnet with
`python -m scripts.escrow_smoke`: the full happy path (create, budget, fund,
submit, complete) and the reject/refund path, from an EOA treasury wallet.
NOT yet verified: `claimRefund` (the `--scenario expire` run); the app does
not use it, since `reject` also refunds a funded job. Not yet exercised live
through the pipeline itself (only through fakes); do one real run first.

**Failed attempts are audited.** If fund / deliver / release / reject fails
(a transaction reverts, Circle errors, the contractor's budget does not match
what was approved), the error is re-raised to the caller AND appended to the
log as a copy of the current entry carrying `failed_attempt` (action, error,
and the failed transaction's id and hash when one was sent). The escrow
state in that entry is unchanged: funds did not move, and the log never says
otherwise. The dashboard shows the last failure on the milestone and counts
milestones needing attention; the next success clears it.

Two deliberate limits: an identical repeated failure is logged once (the API
has no authentication, so repeating a bad request must not grow the
hash-chained log without bound), and refusals made before reaching the escrow
(unknown request, action not valid for the current state) are not logged,
since nothing was attempted.

### Tamper-evident audit log

`data/audit_log.jsonl` is hash-chained: each line stores `prev_hash` and an
`entry_hash` (SHA-256 of its own content). Editing, deleting, or reordering
any past line breaks the chain from that point on.

```bash
python -m src.audit.verify           # exit 0 = intact, 1 = tampered
```

The dashboard shows the same check live, and `GET /api/audit/verify` exposes
it. Known limit: removing lines from the *end* of the file cannot be detected
by the chain alone; closing that needs the latest `entry_hash` anchored
somewhere the log's owner cannot rewrite (e.g. on-chain).

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
- **Dashboard:** open `/manager/settings` (linked from the manager page; needs the
  manager token) to edit limits, thresholds, and rubric text from a form.

Changes take effect immediately -- no restart needed, since the config is
read fresh on every request.

**The thresholds, and what each one does.** All six are in `config/rules.yaml`
under `thresholds`, and each is a number in [0, 1] except
`reduced_amount_fraction`, which is in (0, 1]:

| Threshold | Effect |
|---|---|
| `trust_min_auto_approve` | At or above this trust, the request is approved without a human. |
| `trust_min_to_consider` | Below this trust the request escalates immediately, before any other rule is tested. |
| `importance_high` | At or above this importance, a request with only medium trust qualifies for a reduced approval. |
| `urgency_high` | Same as `importance_high`, for urgency. Either one is enough. |
| `reduced_amount_fraction` | The fraction of the category ceiling released in a reduced approval (0.5 = half). |
| `fraud_veto_at` | At or above this probability of fraud, no automatic approval happens, whatever the trust score says. |

**What the config refuses to save.** `RulesConfig` validates coherence, not just
field ranges, because every one of these does something defensible per field and
destructive in combination:

- **Every request category needs a positive ceiling.** A missing one is not an
  error at read time: `policy/limits.py` falls back to a 200 USDC default, so a
  config that simply omits `contractor_milestone` would drop its ceiling from
  2000 to 200 silently. `{}` is refused too. Negative and non-finite limits are
  refused, since a non-positive ceiling escalates every request in its category
  and looks like a policy decision rather than a typo.
- **A ceiling must be money the wallet can actually back.** There is now an
  upper bound as well: `src/policy/ceiling.py` compares every category limit
  with the treasury's USDC balance (`get_balance()`), and `PUT /api/config` and
  `manage_rules.py set-limit` both refuse a limit above it. The bound is
  `min(max(balance, 1_000), 100_000)`:

  | Wallet balance | Largest allowed category limit |
  |---|---|
  | 0 – 1,000 USDC | 1,000 USDC (the "not funded yet" floor) |
  | 2,000 USDC | 2,000 USDC |
  | 100,000 USDC or more | 100,000 USDC (stops tracking the balance) |

  Without it, "auto-approve under the limit" stops being a control as soon as
  the limit is above the funds: a 900,000 USDC ceiling against a 1,000 USDC
  wallet used to be accepted. The practical consequence to expect: **a wallet
  holding 500 USDC refuses the shipped 2,000 USDC `contractor_milestone`
  ceiling** until you fund the wallet or lower the limit. That is the guard
  working, not a bug.

  **This check fails OPEN, and that is deliberate.** Reading the balance needs
  credentials and the network, so if Circle cannot be reached the ceilings are
  **not** checked and **any** limit is accepted — the CLI prints a warning and
  the API logs one, but nothing is refused. A rules screen that refuses to save
  because Circle is down would be uneditable exactly when someone needs it, and
  this is a sanity guard rather than a security boundary. Stated plainly: the
  control is only active while the wallet is reachable.
- **`trust_min_to_consider` must be <= `trust_min_auto_approve`.** Otherwise the
  "always escalate" bar sits above the "approve alone" bar, and the policy
  contradicts itself while loading without complaint.
- **`fraud_indicators` must stay a `noul` question**, since the veto reads it as
  a probability of fraud.

Deliberately *not* checked: `importance_high`/`urgency_high` against
`trust_min_to_consider`. The shipped defaults have 0.7 against 0.4, and that is
coherent — the consider bar is a hard stop applied first, so a request reaching
the reduced-approval branch has already cleared it.

**Not covered by any of this:** adding a brand-new category (e.g.
`travel_reimbursement`) that the system will actually accept on incoming
requests. That's validated by the `RequestCategory` enum in
`src/requests/schema.py`, which is a data-model change, not a tunable rule --
`set-limit`/the settings page will *store* a limit for an unknown category
name, but requests using it will still fail schema validation until the enum
is updated in code.

## Roles and access

The app serves two audiences, and the split is enforced by the **server**, not
just by which page is open:

| | Requester | Manager |
|---|---|---|
| Page | `/` | `/manager`, `/manager/settings` |
| Can | submit a payment request; follow its status | see every request with scores, certainty, reasons and engine; approve/deny escalations; fund/release/reject escrow; edit the rules |
| Sees | a coarse status: *Under review*, *Approved*, *Paid*, *Declined*; the amounts; the tx once paid | everything |
| Auth | none (optional shared `REQUESTER_ACCESS_CODE` to submit) | `MANAGER_TOKEN` as a bearer token |

Why the requester must not see how a decision was made: the policy `reason`
can quote exact limits ("exceeds 500 USDC"), and scores show which words move
the model. Either is a map for getting around the controls. So requester
responses are built from an allow-list (`src/public_view.py`): a field becomes
visible only if someone deliberately adds it there.

```bash
# .env
MANAGER_TOKEN=<at least 16 random characters>   # python -c "import secrets; print(secrets.token_urlsafe(24))"
REQUESTER_ACCESS_CODE=                          # optional: gate who may submit
```

- **Fail closed.** With no usable `MANAGER_TOKEN`, every manager route returns 503.
  The placeholder from `.env.example` and anything under 16 characters count as unset.
- Every `/api` route requires the token except `/api/requester/*`. This is enforced
  by one router dependency and a test that derives the route list from the app and
  calls each one anonymously, so a route added later and left open fails the suite.
- Request ids from the portal are generated by the server and unguessable
  (`req-…`); knowing one is what allows reading its status. There is no endpoint
  that lists requests for a requester.
- Requester errors are generic; internal messages stay in the server log. The
  interactive API docs (`/docs`) are disabled.

**What this alpha does not cover** (be upfront about it in the pitch):

- One shared manager token: every manager is the same identity. The `reviewer`
  name stored in the audit log is typed by the manager, not proven by the token.
- A requester's name and destination wallet are self-declared. The model's trust
  score reads the name, and the wallet decides where money goes. Fixing this
  needs per-requester identity and a vendor registry maintained by managers.
- A coarse status is still an oracle: someone can retry with different wording
  and watch the outcome. The defense is bounded impact (limits, human review,
  escrow), not secrecy. There is no per-requester aggregate limit yet, so many
  small requests under a limit are not caught.
- No rate limiting on sign-in or submission, and no TLS of its own: run it behind
  HTTPS before exposing it, since the token travels in a header.

## Running the app

```bash
uvicorn src.api:app --reload --port 8000
```

- http://127.0.0.1:8000/ : the requester portal. Submit a request; the list below
  follows each one sent from this browser (the ids are kept in its local storage).
- http://127.0.0.1:8000/manager : asks for the manager token, then shows the
  escalations waiting for a decision, the **Milestones em escrow** panel, and the
  ledger. Each ledger row expands to the request, the policy reason, the three scores
  with the engine's certainty, the engine used and the reviewer. Executed payments link
  to the Arc testnet explorer; failed ones say why.

The pages are static HTML (no build step) talking to the FastAPI backend in
`src/api.py`, which wraps the same `src/pipeline.py` used by the CLI, so there is
exactly one implementation of the decision logic.

## Project layout

- `src/config.py` — business rules (category limits, policy thresholds, scoring
  rubrics), backed by `config/rules.yaml`. See "Configuring business rules" above.
- `scripts/manage_rules.py` — CLI for the above.
- `src/requests/` — payment request schema and validation.
- `src/decision_engine/` — pluggable decision engine. `engine.py` defines the
  interface; `system_one.py` is the shared client for the System One protocol; `jev_client.py`
  and `fallback_classifier.py` (Kev) only say where to call. `mock_engine.py` answers
  the same schema offline. `answers.py` parses noul/choice/score; `criteria.py` builds
  the state and questions.
- `src/history.py` — requester and destination history computed from the audit log.
- `src/payees.py` — the payee registry: which wallets a payee may be paid at, from
  `config/rules.yaml` plus what the log proves. The one check the model cannot make.
- `src/policy/` — deterministic rules and hard limits. This is the "contract" that
  the decision engine's scores are checked against; the engine cannot bypass it.
- `src/payments/` — Circle/Arc integration: `arc_client.py` (plain USDC transfers)
  and `escrow.py` (on-chain milestone escrow, see below).
- `src/audit/` — append-only decision log (also the source for reports and the
  dashboard).
- `src/pipeline.py` — the request -> decision -> payment flow, shared by the CLI
  (`src/main.py`) and the API (`src/api.py`) so there's one implementation.
- `src/api.py` — FastAPI backend, routes split by audience.
- `src/auth.py` — manager token and requester access code.
- `src/public_view.py` — the allow-listed view a requester gets of their request.
- `ui/requester.html` — requester portal. `ui/manager.html` / `ui/settings.html` — manager
  dashboard and rules page, sharing the sign-in in `ui/manager-auth.js` (all static).
- `src/reports/` — generates a human-readable report from the audit log.
- `tests/` — unit tests.
- `demo/` — reproducible scenarios for the 3-minute demo video.

## Design note

The decision engine is intentionally isolated behind one interface
(`decision_engine/engine.py`). Which model actually powers it (Jev, a trained
classifier, or an LLM constrained to a JSON schema) is an implementation detail,
not something the rest of the system depends on.
