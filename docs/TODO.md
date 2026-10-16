# Roadmap and open work

Work that is deliberately deferred, plus gaps found in the current behaviour.
Everything here is a design decision taken knowingly, not an oversight.

This file describes the direction *after* the Tameion Agents Hackathon
submission. What is and is not covered by the alpha as it stands is in the
README ("What this alpha does not cover"); this document is the other half: what
would come next and what has to be settled first.

Two ground rules apply to everything below:

- **The policy, not the model, stays the authority.** No item here may be
  implemented by trusting the decision engine further.
- **A control that fails open must say so.** Where a check depends on the
  network or on credentials being reachable, the operator is told, and the
  README documents the consequence.

## 1. The reduced-approval path needs revisiting before the submission

**Where it stands.** `src/policy/rules.py` approves a request without a human
when `trust` is between `trust_min_to_consider` (0.4) and
`trust_min_auto_approve` (0.75), *and* `urgency` or `importance` is at or above
0.7, *and* the category allows a part payment. It pays
`reduced_amount_fraction` (0.5) of the category ceiling, and the reason string
records "medium trust, but high urgency/importance justifies a capped
approval".

**Why it is flagged.** This is the only path to automatic approval that is
entered on grounds other than a high trust score and clear fraud signals. It
opens on the strength of two scores that are otherwise advisory: `urgency` and
`importance` are recorded and shown to a manager, but here they are the *whole*
gate. Two consequences are worth deciding on deliberately:

1. The released amount is a fixed fraction of the category ceiling, so the
   branch cannot express "how urgent": a request just over 0.7 and one at the
   maximum release exactly the same. Combined with a short rubric, there is very
   little room for the signal to be graded.
2. If the engine's `trust` answers tend to sit below the auto-approve bar, this
   path stops being the exception and becomes the ordinary way money leaves.
   Whether that is intended is a product decision, not a bug, and it should be
   made explicitly rather than inherited from the defaults.

**Next step.** Decide whether the reduced branch should be reachable when the
fraud questions were never answered at all, and whether the strength of
`urgency`/`importance` should scale the released fraction instead of only gating
it. Anything more expressive needs the rubric work in section 3 first, because
the input is only as good as the rubric.

## 2. Config validation does not yet cover the question schema

**Where it stands.** `RulesConfig` validates field relationships: the trust
thresholds must be ordered (`trust_min_to_consider <= trust_min_auto_approve`),
and `fraud_indicators` is refused unless it is a `noul`, because the policy reads
it as a probability and retyping it would silently disable the fraud veto.

**What is still open.** The same argument applies to the transaction-nature
question and does not yet have a guard behind it:

- `natureza_transacao` is not type-checked. The policy treats it as a `choice`
  and vetoes on the `risco_fraude` option specifically. Retyping the question,
  or editing the option away, removes that veto path with no warning and no
  error.
- Rubric levels are checked for *count* (2 to 10, the range the engine's `score`
  question accepts) but not for content. Levels can be empty strings or
  duplicates, and nothing requires a level to describe a situation rather than a
  degree, which is what decides whether the engine can tell them apart.

**Next step.** Extend the validator so that every question the policy reads by
name is pinned to the type and the option it needs, and validate rubric content
(no empty or duplicate levels; each level describing a situation, not a degree).
A question the policy depends on should not be editable into a shape the policy
cannot read.

## 3. Let the end user author the scoring rubrics

**The goal.** Today the `criteria` texts are written by whoever develops the
system, and a manager can only edit them from the dashboard with no guidance.
The person who actually knows the risk of the business should be the one writing
them, with help on what a good level description looks like.

**Why it matters.** The rubric text determines whether the engine can use the
whole scale. Levels described as degrees ("low", "medium", "high") rather than as
situations give the model nothing to separate them with, and the answers
concentrate in a narrow band. If the auto-approve bar is never reached, the
policy quietly shifts weight onto the reduced-approval path — see section 1.

**Why it is deferred.** This is a new configuration surface, and it is exactly
the kind that section 2 shows is still under-guarded. Each newly editable field
is another way to disable a control without noticing. Opening the rubric to the
user requires, at minimum:

- the validators from section 2 (no empty or duplicate levels; each level
  describes a situation, not a degree; a workable number of levels);
- a preview of the effect of a rubric change before it is saved;
- a way to revert it.

## 4. Minor items

- **Anchor the tail of the audit hash chain on-chain.** The chain cannot detect
  removal of lines from the *end* of the log, because nothing commits to what the
  last line was. Publishing the latest `entry_hash` where the log's owner cannot
  rewrite it closes that gap, and it is the one audit property worth buying from
  the on-chain stack. (Already noted as a known limit in the README.)
- **Rate limiting on submission and manager sign-in.** Currently absent by
  design for the demo; see the README's "What this alpha does not cover". A
  per-requester aggregate limit matters more than the rate limit itself: many
  small requests, each under its category ceiling, are not caught by anything
  today.
- **Reconcile the remaining payment edge case.** Re-sending an idempotency key
  whose transaction already failed is not yet validated against the sandbox, and
  the reconciliation action for a payment that Circle has no record of should be
  exercised end to end.
- **Balance-aware ceiling.** The ceiling a category limit may be set to is
  derived from the wallet's USDC balance, and it deliberately fails open when
  Circle cannot be reached: the ceilings are not checked and any limit is
  accepted. The operator is warned. Revisit whether that trade is still right
  once the wallet is reachable on every deployment.
