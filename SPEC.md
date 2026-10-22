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

**What was judged must be what is committed.** The diff's upper bound is the
working tree by default: the agent leaves its work uncommitted, the verifier
judges exactly that, and the commit carries exactly that. When the upper bound
is a commit range instead, the working tree was not judged, so the commit step
is skipped rather than committing content nothing verified.


When the code passes both the machine tests and the intent check, El Verificador
executes the final logistical steps:

- **Commits only the files it verified.** The commit is scoped by explicit
  path. A blanket `git add -A` is never used, the branch is never switched
  underneath the operator, and a commit that reaches outside the verified
  change set fails loudly rather than being recorded as a success.
- **Tears down the Docker container** and releases compute resources.
- **Writes a receipt** to the durable record, chained to the previous one.

**Not yet:** the commit goes to the current branch rather than to a dedicated
verified branch.

---

## The Acceptance Contract — `IMPLEMENTED`

The commands that must succeed are resolved once per run, and the resolution
records where they came from:

| Source | When |
|---|---|
| `work_package` | The work package carries an explicit `acceptance_commands` list |
| `criteria` | The work package carries `acceptance_criteria` from which commands can be parsed |
| `config` | Neither: the configured default is used |

Only two shapes are parsed out of free-text criteria — a fenced code block,
and a bare line that starts with a known tool. Anything else yields nothing
and the configured default is used, because guessing would mean running
commands the ticket never asked for.

---

## Evidence — `IMPLEMENTED`

**Every decision cites at least one content hash, and the bytes are kept.**

Evidence is written to a content-addressed store
(`~/.el-verificador/evidence/`). The path *is* the hash, so identical bytes
land in the same place rather than accumulating copies, and nothing is ever
overwritten. Every artifact can be re-hashed to prove it is what the decision
was made from.

Sealed per run: the acceptance command output, the per-check result, the
semantic diff summary, the resolved obligations, and the sandbox metadata
(image, resource caps, network state, acceptance commands and their source).

---

## The Durable Record — `IMPLEMENTED`

Every decision is written as an **EdgeRecord receipt**: a JSON file, chained to
the previous one, with a canonical fingerprint.

The receipts conform to two contracts at once:

- `factory-contracts/schemas/receipt.v1.json` — `receipt_id`, `kind`,
  `created_at`, `ticket`, `actor`, `outcome`, `artifacts`, `notes`.
- The EdgeRecord receipt shape and its canonical hashing rule, so the receipts
  can be checked with the same tooling that checks the operator's own cycle
  journal. The canonical bytes are byte-for-byte identical to EdgeRecord's
  `bin/era-fingerprint.py`, including the non-ASCII case that broke
  ERA-000314.

The chain is ordered by a monotonic `seq`, not by `created_at`: the timestamp
has second resolution, and a verifier that writes several receipts in the
same second would otherwise be ordered by a random receipt id.

**The local SQLite ledger is a derived query cache over the receipts.** It is
convenient to query and rebuilt from them. It is not the source of truth, and
`verify_chain()` on it is a second opinion rather than the verdict.

---

## Ticket Board Flagging — `PARTIAL`

On escalation, the ticket is flagged for a human through the dashboard's HTTP
API.

**The write is additive.** The ticket's existing tags survive, and its notes
are read and preserved rather than replaced. The ticket's `status` and
`priority` are never touched, because a status change can imply that work
may proceed.

**The write is never attributed to the operator.** The dashboard's
`/api/tix/update` route hardcodes `actor="operator"`, and the dashboard's own
code carries a warning about an untagged coordination post landing on a ticket
as `sender="Mike", role="human"` — data nobody owned. This component uses the
workboard route and names itself in the note.

**It is off by default** (`ticket_board_enabled: false`). Writing to somebody
else's board is the operator's decision, and it is one config line. The
delivery outcome is recorded in the receipt, so an escalation that could not
be delivered is visibly different from one that was.

**Not yet:** the ticket is the work package's `ticket` field. The board's own
`acceptance` field is not yet the source of the acceptance contract.

---

## The MCP Surface — `IMPLEMENTED`

The verifier is also an MCP server over stdio, so an agent can ask it rather
than reimplementing checks.

| Tool | What it does |
|---|---|
| `verify_work_package` | Verify a work package and return the verdict, the obligations and the evidence hashes |
| `verification_status` | The last recorded verdict for a work package |
| `verification_evidence` | The evidence artifacts behind a receipt |
| `verify_receipt_chain` | Check the integrity of the verification record |

This is what keeps the checker from being the author: Taskrivet wrote the
code, and the tool that judges it runs in another process with another
context.

---

## The Sandbox — `IMPLEMENTED`

`docker/verification/Dockerfile` builds the image the acceptance checks run
inside:

- The acceptance toolchain is baked in, so the runtime container needs no
  network
- A non-root uid
- Resource caps — memory, CPU and PIDs — that are part of the isolation
  contract rather than a convenience
- The network is off by default; a test that genuinely needs one must say so
  explicitly, and the exception is recorded in the receipt

**Not yet:** the base image is tag-pinned. A release must resolve and record
the digest.

---

## Data Contracts

All contracts are pydantic v2 models (`extra="forbid"`):

| Model | Purpose |
|-------|---------|
| `IntentPackage` | Lightweight view of a `DistilledWorkPackage` consumed by the verifier |
| `CheckResult` | Result of one acceptance check (exit code, stdout, stderr, execution source) |
| `SemanticDiff` | AST/git diff vs. distilled intent |
| `Obligation` | One checkable claim and its resolution |
| `AcceptanceContract` | The commands that must succeed, and where they came from |
| `EvidenceArtifact` | One stored artifact, addressed by the hash of its bytes |
| `CorrectionPrompt` | Bounded prompt handed back to Taskrivet on failure |
| `VerificationResult` | Complete result of one verification run |
| `VerificationAudit` | Immutable audit record (SHA256 hash + hash chain) |

---

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
| Evidence retention | the bytes a decision rests on are kept and re-hashable | `IMPLEMENTED` |
| Receipt canonicality | byte-identical to EdgeRecord's own fingerprint tool | `IMPLEMENTED` |
| Ticket Board write | additive, never a status change, never attributed to the operator | `IMPLEMENTED`, off by default |

---

## Failure & Escalation

| Outcome | Action | Status |
|---------|--------|--------|
| `PASS` | Commit the verified files, tear down the sandbox, write the audit record | `IMPLEMENTED` |
| `FAIL` | Issue a Correction Prompt to Taskrivet (counts toward `max_retries`) | `IMPLEMENTED` |
| `ESCALATE` | `max_retries` exhausted, or the worktree did not change; escalate to a human with full context | `IMPLEMENTED` — the ticket is flagged, and the flag's delivery is recorded |
| `INCONCLUSIVE` | No verdict was reached; nothing ran | `IMPLEMENTED` |

---

## CLI

```
python -m el_verificador verify  --work-package wp.json --repo . --base HEAD~1 [--head WORKTREE|<commit>] [--dry-run] [--check-fixture checks.json]
python -m el_verificador audit   --work-package dwp-abc --verify-chain | --receipts
python -m el_verificador config  --init | --show
```

The MCP server is a separate entry point:

```
el-verificador-mcp            # stdio transport
```

Exit codes: `0` pass, `1` fail or escalate, `2` inconclusive.

Configuration is read from `~/.el-verificador/config.yaml` by default and
validated through the contract, so an unknown key is an error rather than a
silently ignored line.
