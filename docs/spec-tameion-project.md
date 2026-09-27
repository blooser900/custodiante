# Technical Specification — Payment Approval Agent with Confidence Scoring

**Event:** Tameion Agents Hackathon (Canteen × Circle)
**Primary RFB:** RFB04 — Autonomous Business Operator
**Secondary RFB:** RFB03 — Contractor & Vendor Network Manager
**Execution window:** ~2–3 afternoons of development

---

## 1. Overview

An autonomous agent that receives payment requests (contractor/milestone payments, supply purchases, operational expenses) and decides to **approve automatically, approve with a reduced limit, or escalate to a human**, based on three calibrated scores: **confidence**, **urgency**, and **importance**.

The central difference from the "run-of-the-mill" examples provided by the event: the decision does not come from a static rule (if/else), nor from a general-purpose LLM trying to "guess its own confidence" (which is poorly calibrated by nature). It comes from a **dedicated decision model**, in the "System One" style (e.g. Jev, by TypeSafe AI), which answers typed questions (Score/Choice/Noul) with calibrated probability — without producing free text, without hallucinating outside the defined schema.

The agent's final authority limit does not live only in the prompt: it is **enforced by a rule/contract** that the decision model cannot bypass, following the event's own guideline ("budgets and approval limits enforced in the contract rather than the prompt").

---

## 2. The Real Problem (Business Context)

Real business used as the use case: a small software business operated by the author and, secondarily, comparable day-to-day operational environments.

Concrete pain point: payments and purchases (contracts, supplies, expenses) currently depend on someone manually reviewing every request — which is slow and prone to human error or "rubber-stamp" approval without any real risk judgment. The goal is to automate routine judgment, keeping a human in the loop only for cases of genuine uncertainty or high impact.

---

## 3. System Architecture

```
[Payment request]
        │
        ▼
[1. Input normalization] → extracts: amount, category, requester, due date, textual context
        │
        ▼
[2. Decision Engine (Jev or equivalent)]
        → answers 3 typed questions:
           - "confidence" score: how trustworthy/legitimate is this request?
           - "urgency" score: how urgent is the payment (deadline, risk of delay)?
           - "importance" score: what is the impact if this payment is not made?
        → every answer carries a calibrated probability/confidence
        │
        ▼
[3. Rules Layer (contract/policy engine)]
        → combines the 3 scores against pre-defined thresholds
        → decides: APPROVE / APPROVE WITH REDUCED LIMIT / ESCALATE TO HUMAN
        → this limit is fixed and cannot be overridden by the decision model
        │
        ▼
[4. Payment execution] (if approved)
        → triggers a transaction via Circle/Arc (testnet or mainnet), in USDC
        → uses arc-escrow as the basis for conditional release logic (RFB03)
        │
        ▼
[5. Append-only log / Audit Trail]
        → records: input received, scores returned, decision made, reason,
          transaction executed (or not) and why
        → this log is reused directly as the source for the report (item 4 below)
```

---

## 4. Components to Build (by priority)

### Priority 1 — Core (required for the demo to work)
1. **Payment request endpoint/CLI** — receives a structured request (JSON: amount, category, requester, description, due date).
2. **Decision engine with calibrated scores** — integration with Jev (if access is available in time) OR a functionally equivalent substitute: a classifier (e.g. a small fine-tuned model, or an LLM + JSON formatter with output restricted to a schema) that returns the three scores along with an associated confidence/probability value. **Important:** the engine behind it is an implementation detail — the pitch and the external interface must behave like a "calibrated decision engine", regardless of the actual backend.
3. **Rules/policy layer** — simple, explicit thresholds (e.g. confidence < X → escalate; high importance + high urgency + medium confidence → approve with a reduced limit). Document these rules explicitly in code (not as a loose prompt).
4. **Simulated/real payment execution** — a call to the Arc/Circle stack (testnet USDC is acceptable; mainnet is valued in judging if feasible).
5. **Append-only log** — every decision produces a signable/reconstructible record (balance seen, scores, rule applied, outcome).

### Priority 2 — Reuse (low incremental cost)
6. **Report** — visualization/export of the existing log (item 5), with no new data-capture logic. It can be a simple dashboard or even a markdown/CSV generated from the log.

### Out of scope for this time window
- Multiple modules for unspecified "other pertinent services".
- Training a classifier from scratch (use a ready-made model, a third-party API, or an LLM + JSON schema as a fallback).
- A sophisticated visual interface — functionality and clarity of the flow matter more than design.

---

## 5. Event Technical Requirements (non-negotiable)

- Must run on the **Arc** network (Circle's L1, stablecoin-native), with payments in **USDC**.
- Public repository on **GitHub** (mandatory).
- Demo video of up to **3 minutes** (Loom/YouTube/Vimeo).
- A live link is optional, but recommended.
- The project will be judged on the **delta** of progress during the event window (Sep 27–Oct 10), not on prior history.
- Judging criteria: 30% agent sophistication (how much it actually *decides* vs. merely automates), 30% real traction, 20% use of the Circle stack, 20% innovation.

---

## 6. Suggested Stack

- **Decision/classification:** Jev (TypeSafe) via API, or a fallback with an in-house classifier + structured JSON output formatter.
- **Payment/settlement:** Circle Agent Stack (Wallets, Paymaster) + Arc CLI, with reference code in `circlefin/arc-escrow` (for conditional release logic).
- **Ledger/log:** a simple append-only structure (it can be based on `beancount` or a signed JSON log, depending on the time available).
- **Language:** at the author's discretion, according to the stack already mastered.

---

## 7. Definition of "Done" for the Demo

The 3-minute demo must show, end to end:
1. A payment request arriving at the system.
2. The three scores being generated and displayed (with their confidence levels).
3. The rules-layer decision being made visibly (approve / cap / escalate).
4. The transaction being executed on Arc (or explicitly held / escalated).
5. The corresponding log record, showing that the decision is auditable and reconstructible.

That is enough to demonstrate "agentic sophistication" and use of the Circle stack without requiring the construction of a complete financial management system.
