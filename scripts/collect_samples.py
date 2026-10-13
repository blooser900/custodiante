"""Collect the decision engine's real answers for a fixed set of scenarios.

Why: the policy decides from `trust` and the fraud signals. Thresholds for the
fraud signals have to be chosen from real answers, and a threshold chosen
without seeing them would be a guess. This script sends a set of scenarios that
are plainly good, doubtful or bad -- plus a set that is deliberately
*discordant*, where trust and the fraud signal contradict each other -- to the
configured engine and saves everything it says.

The discordant scenarios matter more than they look. In the correlated ones the
trust score already separates the groups on its own, so the fraud veto never
gets to act and cannot be calibrated: measured on the first two collections, no
threshold between 0.40 and 0.70 changed a single decision. The discordant set
is where the veto has to earn its keep, and where a badly chosen threshold would
start vetoing legitimate payments.

Each scenario carries a hand-built history (a known vendor with a clean record,
a destination with several refunds, ...), so the run is reproducible and never
touches the real audit log. The request goes through the same engine.score()
the app uses, so what is sent is exactly what the app would send.

NO TIMEOUT: the script waits as long as the engine needs, because a model
running on a CPU can take many seconds per request. Stop it with Ctrl-C at any
time: every answer is written to the file the moment it arrives, so nothing
already collected is lost.

Usage:
    python -m scripts.collect_samples                    # all scenarios, once
    python -m scripts.collect_samples --repeat 3         # also measure run-to-run noise
    python -m scripts.collect_samples --only good_routine_supplies,bad_denied_before
    python -m scripts.collect_samples --out my_samples.jsonl

Uses the engine selected by DECISION_ENGINE in .env (default: kev). Send the
resulting .jsonl file back for calibration.
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.config import load_config  # noqa: E402
from src.decision_engine.criteria import build_questions  # noqa: E402
from src.history import PartyHistory, build_history, reliability_index  # noqa: E402
from src.pipeline import build_engine_from_env  # noqa: E402
from src.requests.schema import PaymentRequest  # noqa: E402

DEFAULT_OUT_DIR = Path("data/samples")
MAX_CONSECUTIVE_ERRORS = 3

# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def party(basis: str, *, succeeded=0, failed=0, refunded=0, denied=0, pending=0, avg=None,
          first_seen_days=None, recent_bad=0) -> PartyHistory:
    """A party's recorded history, with the derived fields computed by the
    same formulas the real system uses."""
    attempts = succeeded + failed + refunded + denied
    return PartyHistory(
        identity_basis=basis, known=(attempts + pending) > 0, first_seen_days=first_seen_days,
        attempts=attempts, pending=pending, succeeded=succeeded, failed=failed, refunded=refunded,
        denied=denied, recent_bad_outcomes_24h=recent_bad, avg_amount_usdc=avg,
        reliability_index=reliability_index(succeeded, attempts),
    )


def requester(**kw) -> PartyHistory:
    return party("self_declared", **kw)


def destination(**kw) -> PartyHistory:
    return party("wallet_address", **kw)


@dataclass(frozen=True)
class Scenario:
    id: str
    # The a-priori judgement this scenario was designed to express:
    #   "good"        plainly legitimate
    #   "doubtful"    one thing is off, nothing conclusive
    #   "bad"         several red flags, or a record of trouble
    #   "discordant"  built so the trust score and the fraud signal CONTRADICT
    #                 each other. Two are well-formed requests that carry a
    #                 fraud signal (the veto must fire); two are legitimate
    #                 requests that look flimsy (the veto must not). Without
    #                 these the veto cannot be calibrated at all, because in
    #                 the correlated scenarios the trust threshold already
    #                 decides everything and the veto never gets to act.
    expected: str
    category: str
    amount_usdc: float
    requester_name: str
    description: str
    requester_history: PartyHistory
    destination_history: PartyHistory | None  # None: the request names no wallet


# The designed groups, in reporting order.
SCENARIO_GROUPS = ("good", "doubtful", "bad", "discordant")


# A requester with a long, clean record, reused where the requester is not the point.
_TRUSTED_IT = requester(succeeded=38, failed=1, avg=165.0, first_seen_days=420)
_TRUSTED_ENG = requester(succeeded=15, avg=1450.0, first_seen_days=300)
_NOBODY = requester()

SCENARIOS: list[Scenario] = [
    # --- plainly good: normal amount, clean history on both sides ---------
    Scenario(
        "good_routine_supplies", "good", "supply_purchase", 180.0, "IT Department",
        "Monthly purchase of printer toner and network cables from our regular office supplier. "
        "Invoice attached and matches the purchase order.",
        _TRUSTED_IT, destination(succeeded=24, avg=170.0, first_seen_days=380),
    ),
    Scenario(
        "good_documented_milestone", "good", "contractor_milestone", 1500.0, "Engineering Team",
        "Milestone 2 of 4 of the API integration contract. Delivery accepted by the technical lead "
        "and the invoice matches the contract schedule.",
        _TRUSTED_ENG, destination(succeeded=6, avg=1500.0, first_seen_days=210),
    ),
    Scenario(
        "good_recurring_subscription", "good", "operational_expense", 90.0, "Finance",
        "Monthly renewal of the cloud hosting subscription. Same vendor and same amount as the "
        "previous eleven months.",
        requester(succeeded=60, avg=110.0, first_seen_days=800),
        destination(succeeded=11, avg=90.0, first_seen_days=340),
    ),
    Scenario(
        "good_small_known_vendor", "good", "operational_expense", 45.0, "Facilities",
        "Replacement cartridge for the office water filter, bought from the usual vendor.",
        requester(succeeded=22, avg=60.0, first_seen_days=500),
        destination(succeeded=8, avg=48.0, first_seen_days=260),
    ),
    # --- doubtful: one thing is off, nothing is conclusive -----------------
    Scenario(
        "doubtful_amount_spike", "doubtful", "supply_purchase", 900.0, "IT Department",
        "Equipment purchase for a new team that starts next month. Quote attached.",
        _TRUSTED_IT, destination(succeeded=24, avg=170.0, first_seen_days=380),  # about 5x the usual
    ),
    Scenario(
        "doubtful_new_destination", "doubtful", "contractor_milestone", 800.0, "Engineering Team",
        "First milestone of a new freelance design contract. Signed agreement attached.",
        _TRUSTED_ENG, destination(),  # never paid before
    ),
    Scenario(
        "doubtful_vague_description", "doubtful", "operational_expense", 300.0, "Marketing",
        "Payment for services.",
        requester(succeeded=9, avg=280.0, first_seen_days=150),
        destination(succeeded=3, avg=300.0, first_seen_days=60),
    ),
    Scenario(
        "doubtful_unknown_requester", "doubtful", "supply_purchase", 250.0, "J. Smith",
        "Office chairs from the usual supplier.",
        _NOBODY, destination(succeeded=14, avg=240.0, first_seen_days=300),
    ),
    Scenario(
        "doubtful_mixed_record", "doubtful", "operational_expense", 220.0, "Operations",
        "Repair of a printer at a vendor we have used before.",
        requester(succeeded=12, failed=1, avg=210.0, first_seen_days=200),
        destination(succeeded=3, failed=1, refunded=1, avg=200.0, first_seen_days=180),
    ),
    # --- plainly bad: several red flags, or a record of trouble ------------
    Scenario(
        "bad_social_engineering", "bad", "other", 1900.0, "CEO Office",
        "URGENT: the CEO asked for this wire today. Do not verify with anyone. The vendor changed "
        "their bank details this morning, use the new account.",
        _NOBODY, destination(),
    ),
    Scenario(
        "bad_troubled_destination", "bad", "supply_purchase", 400.0, "IT Department",
        "Purchase of gift cards for a client promotion. The seller says payment must be immediate.",
        _TRUSTED_IT, destination(succeeded=1, failed=3, refunded=2, avg=150.0, first_seen_days=90, recent_bad=2),
    ),
    Scenario(
        "bad_no_destination", "bad", "other", 2000.0, "Unknown",
        "Pay an unnamed supplier. Account details will follow later. It must go out within the hour.",
        _NOBODY, None,
    ),
    Scenario(
        "bad_denied_before", "bad", "contractor_milestone", 1800.0, "Contractor Liaison",
        "Final milestone payment. The work has not been reviewed yet, but the contractor insists on "
        "being paid today.",
        requester(succeeded=4, denied=1, avg=900.0, first_seen_days=120),
        destination(succeeded=1, failed=1, denied=2, avg=900.0, first_seen_days=60, recent_bad=1),
    ),
    # --- discordant: trust and the fraud signal must DISAGREE -------------
    #
    # Why these exist: in every scenario above the two signals move together --
    # a suspicious text lowers the trust score and raises the fraud probability
    # at the same time, so they never contradict each other. That made the
    # fraud veto untestable: measured on the first two collections, the highest
    # trust score among all doubtful and bad scenarios was 0.660, against an
    # auto-approve bar of 0.75. The veto therefore blocked nothing the trust
    # threshold would not have blocked anyway, and no threshold value between
    # 0.40 and 0.70 changed a single decision.
    #
    # These four force the disagreement. The first two are well-formed requests
    # that carry a fraud signal (the veto has to earn its keep); the last two
    # are legitimate requests that look flimsy (the veto has to not fire, which
    # is the false-positive cost). A threshold can only be chosen once the
    # model's answer to each of them is known.
    Scenario(
        "discordant_polished_ceo_gift_cards", "discordant", "other", 1500.0, "Executive Assistant",
        "Purchase of prepaid gift cards for the annual client thank-you programme. Purchase request "
        "approved by the CFO, cost centre 4410, and the invoice is attached. The vendor asked for "
        "payment to a newly opened account because their previous bank details are being migrated.",
        requester(succeeded=41, failed=1, avg=1400.0, first_seen_days=540),
        destination(),  # never paid before
    ),
    Scenario(
        "discordant_polished_wallet_swap", "discordant", "contractor_milestone", 1600.0, "Engineering Team",
        "Milestone 3 of 4 of the API integration contract, delivered and accepted by the technical "
        "lead, invoice matching the schedule. Note the payment should go to the new account below: "
        "the vendor emailed this morning to say their old one is being closed.",
        requester(succeeded=15, avg=1450.0, first_seen_days=300),
        destination(succeeded=5, avg=1500.0, first_seen_days=200, failed=1),
    ),
    Scenario(
        "discordant_minimal_but_ordinary", "discordant", "operational_expense", 60.0, "Reception",
        "Replacement coffee machine descaler, from the usual supplier.",
        requester(),  # no history at all
        destination(succeeded=40, avg=55.0, first_seen_days=600),  # long, clean, high volume
    ),
    Scenario(
        "discordant_first_time_vendor_trial", "discordant", "supply_purchase", 220.0, "Procurement",
        "Trial order from a new packaging supplier. Small quantity to test quality before committing "
        "to a larger contract. Purchase order raised and the samples were already delivered.",
        requester(succeeded=3, avg=200.0, first_seen_days=25),
        destination(),  # never paid before
    ),
]


def to_request(index: int, scenario: Scenario) -> PaymentRequest:
    """The request, with a fake but deterministic wallet when it names one."""
    wallet = f"0x{index + 1:040x}" if scenario.destination_history is not None else None
    return PaymentRequest(
        request_id=f"sample-{scenario.id}",
        category=scenario.category,
        amount_usdc=scenario.amount_usdc,
        requester=scenario.requester_name,
        description=scenario.description,
        counterparty_wallet=wallet,
    )


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _short(question_id: str) -> str:
    return "".join(word[0] for word in question_id.split("_")).upper()


def _cell(answer) -> str:
    if answer is None:
        return "-"
    if answer.type == "noul":
        return f"{answer.noul:.0%}"
    if answer.type == "choice":
        return answer.choice[:10]
    return f"{answer.score:.2f}"


def print_summary(rows: list, extra_ids: list[str]) -> None:
    print("\n" + "=" * 100)
    legend = "  ".join(f"{_short(i)}={i}" for i in extra_ids)
    print(f"Columns: TR/UR/IM = trust/urgency/importance (0-1).  {legend}")
    print("noul cells are P(yes); choice cells are the picked option; score cells are the raw position.\n")
    header = f"{'scenario':<40}{'exp':<12}{'TR':>5}{'UR':>5}{'IM':>5}  " + "".join(f"{_short(i):>11}" for i in extra_ids)
    print(header)
    print("-" * len(header))
    for scenario, scores in rows:
        cells = "".join(f"{_cell(scores.extra.get(i)):>11}" for i in extra_ids)
        print(f"{scenario.id:<40}{scenario.expected:<12}{scores.trust.value:>5.2f}{scores.urgency.value:>5.2f}"
              f"{scores.importance.value:>5.2f}  {cells}")

    noul_ids = [i for i in extra_ids if any(s.extra.get(i) is not None and s.extra[i].type == "noul" for _, s in rows)]
    if noul_ids:
        print("\nMean P(yes) by what the scenario was designed to be:")
        print(f"{'':<34}" + "".join(f"{_short(i):>8}" for i in noul_ids))
        for group in SCENARIO_GROUPS:
            members = [s for sc, s in rows if sc.expected == group]
            if not members:
                continue
            means = []
            for i in noul_ids:
                values = [s.extra[i].noul for s in members if i in s.extra]
                means.append(f"{sum(values) / len(values):>8.0%}" if values else f"{'-':>8}")
            print(f"{group:<34}" + "".join(means))

    # The discordant group is the one the veto is calibrated on: trust and the
    # fraud signal are supposed to disagree there, so a group mean hides the
    # whole point. Print it scenario by scenario, next to the trust score, so
    # the two signals can be read against each other.
    discordant = [(sc, s) for sc, s in rows if sc.expected == "discordant"]
    if discordant:
        print("\nDiscordant scenarios -- the veto is calibrated here, one by one:")
        print(f"  {'scenario':<38} {'trust':>6} {'FI':>7} {'natureza':>22}  {'o veto barraria?':>16}")
        for sc, s in discordant:
            fi = s.extra.get("fraud_indicators")
            nt = s.extra.get("natureza_transacao")
            fi_val = fi.noul if fi is not None and fi.type == "noul" else None
            # The threshold is read from the live config, so this column always
            # reflects what the policy would really do.
            veto_at = load_config().thresholds.fraud_veto_at
            barraria = "sim" if (
                (fi_val is not None and fi_val >= veto_at)
                or (nt is not None and getattr(nt, "choice", None) == "risco_fraude")
            ) else "nao"
            print(f"  {sc.id:<38} {s.trust.value:>6.2f} {('%.0f%%' % (fi_val * 100)) if fi_val is not None else '-':>7} "
                  f"{getattr(nt, 'choice', '-'):>22}  {barraria:>16}")

    if len(rows) > len({sc.id for sc, _ in rows}) and noul_ids:
        by_id: dict[str, list] = {}
        for sc, s in rows:
            by_id.setdefault(sc.id, []).append(s)
        print("\nRun-to-run noise (largest max-min spread of P(yes) across repeats of one scenario):")
        for i in noul_ids:
            spread = max((max(s.extra[i].noul for s in runs if i in s.extra) - min(s.extra[i].noul for s in runs if i in s.extra))
                         for runs in by_id.values() if sum(1 for s in runs if i in s.extra) > 1)
            print(f"  {_short(i):<6}{spread:.3f}")


def run_collection(engine, scenarios: list[Scenario], repeat: int, out_path: Path) -> int:
    """Run every scenario through `engine`, appending each answer to
    `out_path` the moment it arrives. Returns a process exit code."""
    config = load_config()
    extra_ids = list(config.assessment_questions)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header = {
        "kind": "header",
        "created": datetime.now(timezone.utc).isoformat(),
        "engine": getattr(engine, "name", type(engine).__name__),
        "model": getattr(engine, "model", None),
        "api_url": getattr(engine, "api_url", None),
        "timeout": None,
        "repeat": repeat,
        "questions": build_questions(config.criteria, config.assessment_questions),
    }
    rows, errors, consecutive, total = [], 0, 0, len(scenarios) * repeat
    done = 0

    print(f"Engine: {header['engine']}  model: {header['model']}  url: {header['api_url']}")
    if header["engine"] == "mock":
        print("NOTE: this is the offline mock engine, not a model: the answers are not real samples.")
    print(f"{len(scenarios)} scenarios x {repeat} run(s). No timeout: waiting as long as the engine needs.")
    print(f"Writing to {out_path}  (each answer is saved as it arrives)\n")

    try:
        with out_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
            f.flush()
            for index, scenario in enumerate(scenarios):
                request = to_request(index, scenario)
                history = build_history(request.amount_usdc, scenario.requester_history, scenario.destination_history)
                for run in range(1, repeat + 1):
                    done += 1
                    label = f"[{done}/{total}] {scenario.id}" + (f" (run {run})" if repeat > 1 else "")
                    started = time.monotonic()
                    try:
                        scores = engine.score(request, history=history)
                    except Exception as e:  # noqa: BLE001 -- one bad scenario must not lose the others
                        errors += 1
                        consecutive += 1
                        f.write(json.dumps({"kind": "error", "id": scenario.id, "expected": scenario.expected,
                                            "run": run, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n")
                        f.flush()
                        print(f"{label} ... FAILED: {e}")
                        if consecutive >= MAX_CONSECUTIVE_ERRORS:
                            print(f"\nStopping: {MAX_CONSECUTIVE_ERRORS} failures in a row. Is the engine running?")
                            return 1
                        continue
                    consecutive = 0
                    elapsed = time.monotonic() - started
                    evidence = scores.evidence
                    f.write(json.dumps({
                        "kind": "result", "id": scenario.id, "expected": scenario.expected, "run": run,
                        "seconds": round(elapsed, 2),
                        "model": evidence.model if evidence else None,
                        "latency_ms": evidence.latency_ms if evidence else None,
                        "state": evidence.state if evidence else None,   # exactly what the engine was shown
                        "answers": evidence.answers if evidence else None,  # exactly what it said
                        "problems": evidence.problems if evidence else [],
                    }, ensure_ascii=False) + "\n")
                    f.flush()
                    rows.append((scenario, scores))
                    note = f"  [{len(evidence.problems)} answer(s) missing or invalid]" if evidence and evidence.problems else ""
                    print(f"{label} ... {elapsed:.1f}s{note}")
    except KeyboardInterrupt:
        print(f"\nInterrupted. {len(rows)} answer(s) were already saved to {out_path}")
        return 130

    if rows:
        print_summary(rows, extra_ids)
    print(f"\n{len(rows)} answer(s), {errors} failure(s). Saved to: {out_path}")
    print("Send me that file.")
    return 0 if rows else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect the engine's answers for fixed scenarios.")
    parser.add_argument("--repeat", type=int, default=1, help="runs per scenario (>1 also measures noise)")
    parser.add_argument("--only", help="comma-separated scenario ids to run")
    parser.add_argument("--out", help="output .jsonl file (default: data/samples/samples-<time>.jsonl)")
    args = parser.parse_args(argv)

    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    scenarios = SCENARIOS
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = sorted(set(wanted) - {s.id for s in SCENARIOS})
        if unknown:
            parser.error(f"unknown scenario id(s): {', '.join(unknown)}. Known: {', '.join(s.id for s in SCENARIOS)}")
        scenarios = [s for s in SCENARIOS if s.id in wanted]

    out_path = Path(args.out) if args.out else DEFAULT_OUT_DIR / f"samples-{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    engine = build_engine_from_env()
    if hasattr(engine, "timeout"):
        engine.timeout = None  # wait as long as the engine needs
    return run_collection(engine, scenarios, args.repeat, out_path)


if __name__ == "__main__":
    sys.exit(main())
