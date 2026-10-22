# El Verificador — Specification

**Version:** 2.0
**Author:** Michael Hamanaka
**License:** MIT
**Status markers:** every mechanism below carries one.

| Marker | Meaning |
|---|---|
| `IMPLEMENTED` | The behaviour exists and is covered by tests. |
| `PARTIAL` | The behaviour exists but does not do everything described here. |
| `NOT IMPLEMENTED` | The behaviour does not exist. The text describes the target. |
| `SUPERSEDED` | A different design replaced this; see the note. |

**This document is a target specification, not a description of the code.** For
the verified state of the code, see [ROADMAP.md](ROADMAP.md) §2 and the
[golden fixture suite](tests/test_golden_fixtures.py), which encodes the
expected verdict for every case.

---

## System Role

**The Autonomous Gatekeeper for the Taskrivet Pipeline**

**Pipeline Position:** Downstream from Taskrivet; upstream from EdgeRecord.

El Verificador is the gate between "code written" and "code committed".
Taskrivet cannot grade its own homework. El Verificador independently audits
Taskrivet's work inside the sandbox before the sandbox is torn down and code is
merged.

---

## Objective

Verify that a `DistilledWorkPackage` produced by El Destilador was faithfully
implemented by Taskrivet, that all acceptance checks pass, and that the result
is safe to commit. Every verification decision is immutably logged.

---

## Core Mechanisms

### 1. Independent Acceptance Testing — `IMPLEMENTED`

El Verificador provisions a Docker sandbox and runs the acceptance pipeline
inside it:

1. Unit tests (`pytest -q`)
2. Lint (`ruff check .`)
3. Security scan (`bandit -r . -q`)

Only a container exit code of `0` for every check counts. Taskrivet's
self-reported status is never trusted.

**Constraints that are part of the contract:**

- **No acceptance command runs on the host, in any mode.** A check either runs
  inside the container (`CheckExecution.CONTAINER`), is replayed from a recorded
  fixture (`CheckExecution.REPLAY`), or reports that it did not run
  (`CheckExecution.SKIPPED`). A skipped check can never pass.
- Each check runs under its own wall-clock limit. An overrun is reported as
  exit `124`; the container teardown kills whatever is still running.
- The acceptance commands come from configuration, and are classified by what
  they do rather than by their position in the list.

**Not yet:** the commands are still the configured defaults rather than the
ticket's own acceptance contract. Carrying the ticket's `acceptance` field
through to the verifier is specified in ROADMAP.md §3.4 and is `PARTIAL` — the
work package's acceptance criteria are now read, but the command list is not
yet derived from them.

### 2. Intent Alignment (Obligations) — `IMPLEMENTED`

Passing tests are not enough — the code must solve the user's original problem.
The distilled intent is resolved into **obligations**: individually checkable
claims, each resolving to a status backed by evidence.

| Obligation | Status values | Catches |
|---|---|---|
| `change_present` | satisfied / violated | No change was produced at all |
| `acceptance_command` | satisfied / violated / unproven | A command failed, or never ran |
| `no_stub` | satisfied / violated | A changed symbol is a placeholder implementation |
| `tests_intact` | satisfied / violated | A test was deleted, skipped, or de-asserted |
| `scope_respected` | satisfied / violated / not_applicable | Files outside the work package were changed |
| `symbol` | satisfied / violated / not_applicable | A symbol the goal named was never defined |
| `goal_terms` | satisfied / violated / not_applicable | The change shares no vocabulary with the goal |

**The decision is a pure function of the obligation list:**

```
any VIOLATED   -> FAIL
any UNPROVEN   -> INCONCLUSIVE   (no verdict was reached)
otherwise      -> PASS
```

**A model may explain the evidence. It never decides one.** The local LLM may
compose the analysis attached to a Correction Prompt; the obligation evaluator
is deterministic and has no model in its path.

**Not yet:** `scope_respected` defaults to `warn` rather than `enforce`, so a
change outside the work package's `related_files` is reported and audited but
does not block a pass. `goal_terms` is lexical, not semantic: it matches
content words of the goal against the changed code. A deeper check would need
the acceptance contract's explicit behaviours.

### 3. The Guardrailed Feedback Loop — `IMPLEMENTED`

When Taskrivet breaks the build or violates an obligation, El Verificador
does not blindly say "try again". It acts as a precise debugger:

- Captures the exact stack trace, the specific semantic diff, the failed test
  output, and the list of violated obligations
- Packages this into a highly bounded, strict "Correction Prompt"
- Hands the prompt back to Taskrivet for a retry
- **Enforces a strict iteration limit (`max_retries=3`).** If Taskrivet fails
  three times, the loop is broken and the work is flagged for a human

**Two guards beyond the iteration limit:**

- **No-progress guard.** If the working tree did not change after a
  correction, another attempt cannot produce a different result, so the loop
  escalates immediately instead of spending the remaining budget.
- **Inconclusive is not retried.** When the checks never executed there is
  nothing for the agent to fix.

**The escalation carries its evidence.** The failing checks, the diff, the
violated obligations and the evidence hashes all travel with the escalation;
an escalation that arrives without them is useless to the human reading it.

**Not yet:** the escalation is a return value. Flagging the ticket on the
Ticket Board is not implemented (ROADMAP.md §4, Phase 3).

### 4. State Commit & Teardown — `PARTIAL`

When the code passes both the machine tests and the intent check, El Verificador
executes the final logistical steps:

- **Commits only the files it verified.** The commit is scoped by explicit
  path. A blanket `git add -A` is never used, the branch is never switched
  underneath the operator, and a commit that reaches outside the verified
  change set fails loudly rather than being recorded as a success.
- **Tears down the Docker container** and releases compute resources.
- **Writes an audit record** whose content hash covers the decision, the
  evidence hashes and the resolved obligations, chained to the previous record.

**Not yet:** the commit goes to the current branch rather than to a dedicated
verified branch, and the audit record is written to a local SQLite ledger
rather than to a canonical EdgeRecord receipt (see below).

---

## Data Contracts

All contracts are pydantic v2 models (`extra="forbid"`):

| Model | Purpose |
|-------|---------|
| `IntentPackage` | Lightweight view of a `DistilledWorkPackage` consumed by the verifier |
| `CheckResult` | Result of one acceptance check (exit code, stdout, stderr, execution source) |
| `SemanticDiff` | AST/git diff vs. distilled intent |
| `Obligation` | One checkable claim and its resolution |
| `CorrectionPrompt` | Bounded prompt handed back to Taskrivet on failure |
| `VerificationResult` | Complete result of one verification run |
| `VerificationAudit` | Immutable audit record (SHA256 hash + hash chain) |

---

## Evidence

**Every decision cites at least one content hash.** A decision with no evidence
is not a decision.

The hash of each check's output binds the record to the bytes that were
actually produced, so a later reader can prove the verdict was made from that
evidence and not from something else. The audit record's own content hash
covers the decision, the rationale, the evidence hashes and the obligation
outcomes — not just a sentence of prose.

**Not yet:** a content-addressed artifact store. Evidence hashes are recorded
and verifiable, but the artifacts themselves are not retained in a store; see
ROADMAP.md §3.5.

---

## EdgeRecord Audit — `PARTIAL`

Every verification decision is written to a local append-only SQLite ledger
(WAL mode) at `~/.edgerecord/ledger.sqlite`:

- Content hash = SHA256 over the canonical decision body
- `previous_audit_hash` chains records for tamper evidence
- A monotonic `seq` column orders the chain, and the predecessor is selected
  and the row inserted inside one immediate transaction, so concurrent writers
  cannot fork the chain
- `verify_chain()` validates the full hash chain

**Not yet:** this is not the EdgeRecord receipt format. The real EdgeRecord is
git-tracked JSON receipts in `receipts/`, fingerprinted by recursively
key-sorted, compact-separator JSON with `ensure_ascii=False`, chained by
`previous_fingerprint`, and verified by `playbooks/verify-chain.py`. Conformance
is specified in ROADMAP.md §4, Phase 4. The local ledger is a query cache over
receipts, not a competing source of truth.

---

## Guardrails

| Guardrail | Value | Status |
|-----------|-------|--------|
| `max_retries` | 3 (hard iteration limit) | `IMPLEMENTED` |
| No-progress guard | escalate when the worktree is unchanged | `IMPLEMENTED` |
| Host execution | never, in any mode | `IMPLEMENTED` |
| Commit scope | verified paths only | `IMPLEMENTED` |
| Evidence citation | every decision cites a hash | `IMPLEMENTED` |
| Intent coverage threshold | 0.85 | `SUPERSEDED` — replaced by obligation coverage |
| Fail on TODO stubs | enabled | `IMPLEMENTED` — as the `no_stub` obligation |
| Scope mode | `warn` / `enforce` / `off` | `IMPLEMENTED`, defaulting to `warn` |
| Local inference | Ollama at `http://127.0.0.1:11434` | `IMPLEMENTED` — the model never decides |
| Dry-run | no writes and no execution | `IMPLEMENTED` |

---

## Failure & Escalation

| Outcome | Action | Status |
|---------|--------|--------|
| `PASS` | Commit the verified files, tear down the sandbox, write the audit record | `IMPLEMENTED` |
| `FAIL` | Issue a Correction Prompt to Taskrivet (counts toward `max_retries`) | `IMPLEMENTED` |
| `ESCALATE` | `max_retries` exhausted, or the worktree did not change; escalate to a human with full context | `PARTIAL` — the escalation is a return value, not a ticket |
| `INCONCLUSIVE` | No verdict was reached; nothing ran | `IMPLEMENTED` |

---

## CLI

```
python -m el_verificador verify  --work-package wp.json --repo . --base HEAD~1 --head HEAD [--dry-run] [--check-fixture checks.json]
python -m el_verificador audit   --work-package dwp-abc --verify-chain
python -m el_verificador config  --init | --show
```

Exit codes: `0` pass, `1` fail or escalate, `2` inconclusive.

Configuration is read from `~/.el-verificador/config.yaml` by default and
validated through the contract, so an unknown key is an error rather than a
silently ignored line.
