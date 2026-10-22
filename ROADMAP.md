# El Verificador — Long-Term Build Plan

**Status:** planning document
**Version:** 0.1
**Date:** 2026-10-22
**Owner:** Michael Hamanaka
**Supersedes:** nothing. **Amends:** `SPEC.md` v1.0 (see §2.5 — the SPEC describes capabilities the code does not have).

---

## 0. Executive summary

El Verificador is the trust boundary of the local software factory. It sits between
"an agent says the code is done" and "the code enters the durable record." That single
position defines everything about how it must be built: it must be *independent*,
*evidence-backed*, *replayable*, and *incapable of granting itself authority*.

The current build is a competent skeleton with three working parts — running commands
in a container, a regex stub scan, and a SQLite insert — and four decorative parts
that are advertised in the README and SPEC but produce no decision effect: the AST
"semantic diff", the intent-coverage score, the LLM-composed Correction Prompt, and
the EdgeRecord audit trail. The headline feature ("did the agent actually build the
API route, or just write a TODO stub?") is **not implemented**; the code that claims
to answer it is never consulted.

The long-term plan is therefore not "add features." It is, in order:

1. **Tell the truth** (Phase 0) — remove the gap between what the docs claim and what
   the code does, and build the test assets that make the gap impossible to reopen.
2. **Real isolation, real acceptance** (Phase 1) — never execute untrusted code on the
   host; execute the acceptance contract that actually came with the work, not a
   hardcoded three-command list.
3. **Real intent alignment** (Phase 2) — replace a fuzzy word-overlap score with
   *obligations*: individually checkable claims, each with a status and a piece of
   evidence. The decision becomes a pure function of those obligations.
4. **Real integration** (Phase 3) — become the thing Taskrivet asks when it wants to
   know if it is done, and the thing that flags a ticket when it is not.
5. **Real proof** (Phase 4) — emit receipts that conform to the *actual* EdgeRecord
   contract (canonical hashing, chained receipts), and retire the invented SQLite ledger.
6. **Learn and scale** (Phase 5) — measure false-positive rate, tune policy from
   evidence, support more than one language.

The decision to pass or fail must never depend on a model's opinion. The LLM's place
in this system is *proposal*, never *adjudication*.

---

## 1. What El Verificador is for

### 1.1 The one-sentence contract

> El Verificador converts *"Taskrivet asserts this work package is complete"* into
> *"there is sealed, replayable evidence that the stated obligations were fulfilled"* —
> or into a bounded, well-evidenced failure that names exactly what is missing.

### 1.2 Why it must exist

Taskrivet writes the code. Whoever wrote the code cannot be the only judge of the
code. Every other component in the factory is either an *author* (Panther, Destilador,
Taskrivet) or a *scribe* (EdgeRecord, The Chronicler). El Verificador is the only
component whose job is to be *right about someone else's work*. That makes three
properties non-negotiable:

- **Independence** — it must not consume Taskrivet's self-reported status. It
  re-derives every fact from the repository and the sandbox.
- **Evidence over assertion** — every decision cites at least one content hash. A
  decision with no evidence is not a decision; it is a guess with a nice schema.
- **Replayability** — given a run id and the sealed evidence, the same decision must
  be derivable forever, by anyone, without re-running the agent.

### 1.3 Non-goals

These are explicitly *not* El Verificador's job, and building them here would be a
category error:

| Not a job | Why | Who owns it |
|---|---|---|
| Deciding *what* to build | El Destilador distills intent; the board triages it | Destilador / Board |
| Writing the code | Taskrivet's entire purpose | Taskrivet |
| Provisioning the execution environment | El Hidratador hydrates sandboxes and holds leases | Hidratador |
| Storing long-term memory | The Chronicler is the memory core | Chronicler |
| Holding the durable record | EdgeRecord is the record layer | EdgeRecord |
| Grading the grader | El Verificador must never score itself | Nobody — see §1.4 |

### 1.4 The "cannot grade its own homework" rule, stated precisely

The phrase means two different things and only one of them is currently true:

- **True today:** El Verificador does not read Taskrivet's self-reported status; it
  re-runs the checks itself. Good — preserve this.
- **Not true yet:** El Verificador runs *in the same process, on the same host, with
  the same credentials and the same context* as the code it judges. A verifier that
  shares a filesystem with the agent can be trivially influenced — by a
  `conftest.py` the agent wrote, a `pytest.ini` it added, a `sitecustomize.py` it
  dropped, a `.pylintrc` it tuned, a test file it deleted, a `Dockerfile` it changed.

The taskrivet `AGENTS.md` already names this failure mode precisely: *"Do not
conflate … ledger checks with process isolation."* A green ledger is not a sandbox.
Phase 1 and Phase 3 exist to make the second half of the rule true.

---

## 2. Verified assessment of the current build

Assessed on 2026-10-22 against `a1100cf`. Every claim below was checked by running
the code, not by reading it. Reproduction commands are in Appendix A.

**Assessment basis, and what survived later repairs.** The assessment was made
against `a1100cf`. Repairs committed afterwards (Appendix B) fixed five
*execution-path* defects and took the suite from 22 to 27 tests. Every finding in
this section was re-verified against the code as of `56edd17` and **all of them
still hold**: the host-execution fallback is present, `MODIFIED` is still never
emitted, the coverage threshold is still never consulted, the LLM's Correction
Prompt is still discarded, `git add -A` is still in the commit path, the escalation
path still drops its evidence, and the audit trail is still written to an invented
SQLite table rather than to EdgeRecord. The repairs made the tool *run*; they did
not make it *judge*.

Read the two halves together: Appendix B is the other agent's honest, verifiable
account of why the tool could not complete a verification at all. §2 is the
equivalent account of why, when it does complete one, the verdict it produces
carries very little information.

**Repairs since that assessment.** The findings in §2.2 about the semantic
diff — `MODIFIED` never emitted, the coverage score decoupled from the code,
`fail_on_todo_stubs` unread, and the model's Correction Prompt discarded —
have been closed and are recorded in **Appendix D**. The findings that are not
listed there still hold as written, including the host-execution fallback,
the missing sandbox image, `git add -A` in the commit path, the escalation
path dropping its evidence, and the audit trail not reaching the real
EdgeRecord.

### 2.1 What actually works

| Capability | Status |
|---|---|
| Package structure, packaging, entry point, CI workflow | Working. `pytest` → 27 passed. `unittest discover` → 27 passed. |
| Pydantic v2 contracts with `extra="forbid"` | Working and well-formed. |
| Container lifecycle (`start` / `teardown`) | Working, but see §2.3.1. |
| `CheckResult` capture (exit code, stdout, stderr, duration) | Working. |
| Deterministic `CorrectionPrompt.render()` | Working — this is the *template* path, and it is good. |
| Retry-count escalation to `ESCALATE` | Structurally correct, but loses evidence (§2.3.4). |

### 2.2 What is decorative (advertised, but has no effect)

| Advertised capability | Reality | Impact |
|---|---|---|
| **"Semantic diff" / AST change detection** | `ast_changes` only ever contains `ADDED` (and `REMOVED`). `MODIFIED` is **never emitted**: `_node_tag()` stores `"FunctionDef:name"` as the symbol's "source", so base and head are always equal and the comparison `base_sym.source != sym.source` is never true. | The diff engine cannot see a function body being rewritten — which is the single most important change in the whole exercise. **Repaired — Appendix D.1.** |
| **Intent coverage score** | `_intent_coverage()` unions goal words with `all_symbols`, but `all_symbols` is populated from `_scan_stubs()`, which returns only *stub* symbols. So the score is effectively "how many `related_files` did you touch", capped at 0.4. Measured: **0.1 for both a correct implementation and a stub.** | The score is decoupled from the code it claims to measure. **Repaired — Appendix D.2.** |
| **Intent coverage threshold (0.85)** | Referenced **0 times** in `engine.py`. `SemanticDiff.aligned` compares `coverage >= 0.0`. | The headline guardrail is dead code. A run with 0.0 coverage and no stubs is judged aligned. **Still open — the score is repaired (D.2) but the threshold is still reported, not enforced, until the obligation model lands (Phase 2).** |
| **`fail_on_todo_stubs` config** | Referenced **0 times** in `engine.py`. Stubs always fail, regardless of config. | Config lies. **Repaired — Appendix D.3.** |
| **LLM-composed Correction Prompt** | `correction.py` calls Ollama, then stores the text via `object.__setattr__(correction, "_llm_text", rendered)`. `CorrectionPrompt.render()` **never reads `_llm_text`**. The text is generated and discarded. | The Ollama dependency, the model config, and the whole "smart debugger" story produce no output. The default model `qwen2.5:14b` is not even installed locally — and nobody noticed, which is itself the proof. **Repaired — Appendix D.4.** The note is now advisory and labelled as such. |
| **"Fires a cryptographically secure payload to EdgeRecord"** | Writes to `~/.edgerecord/ledger.sqlite`, a SQLite table invented by this repo. **The real EdgeRecord is file-based**: git-tracked JSON receipts in `receipts/`, fingerprinted per `RUN_ERA.md` "Canonical hashing" (recursively key-sorted, compact-separator, `ensure_ascii=False`, SHA-256), chained by `previous_fingerprint`, verified by `playbooks/verify-chain.py`, and enforced by a pre-commit hook. | The audit trail is **not in the system of record**. `el-destilador` has the same defect — it will need a parallel fix, but El Verificador should not compound it. |
| **"Flags the ticket on the Ticket Board"** | No ticket-board integration exists in the codebase. | The escalation path terminates in a local variable. |

> Two of the findings above are closed: the semantic diff (§2.2, Appendix D)
> and the commit path (§2.3.3, Appendix E). The `intent_coverage_threshold`
> remains reported rather than enforced, and is the one part of the semantic
> diff story that is still incomplete.

### 2.3 The five defects that matter

#### 2.3.1 Dry-run executes untrusted code on the host

`docker_runner.run_check()` falls back to `subprocess.run(...)` in the repository
working directory when Docker is unavailable or `dry_run` is set. `dry_run` is the
documented "offline development" mode and the mode the README's Quick Start uses.

This is the most serious defect in the project. The one component whose entire job
is to run *agent-generated, potentially hostile* code has a supported path that runs
it directly on the operator's machine, with the operator's credentials, in the
operator's real working tree. A generated `conftest.py` or `sitecustomize.py` is
code execution. `dry_run` must mean *"no writes, no execution"* — it should replay
recorded fixtures, not improvise.

#### 2.3.2 The sandbox image does not exist

`docker_image` defaults to `taskrivet/sandbox:latest`. That string appears in
**exactly two files in the entire factory** — this repo's `schemas.py` and
`config.example.yaml`. There is no such image, no Dockerfile that builds it, and no
component that publishes it. `taskrivet/Dockerfile` exists but builds the Taskrivet
CLI, not a verification sandbox.

Meanwhile `el-hidratador` — the component the README says provisions the sandbox —
is in fact a **coordination ledger** (tickets, sessions, leases, checkpoints,
failure fingerprints, token budgets). It does not provision Docker. The two sides of
this contract describe different products.

#### 2.3.3 `git add -A` on a shared checkout

`GitOps.commit()` runs `git checkout <branch>` then `git add -A` then `git commit`.
`git add -A` sweeps *every* file in the tree into the commit, including changes this
process did not make and does not own. The factory's operating rules are explicit and
repeated on this: *"NEVER `git add -A` or `git add .`"*, *"Stage only the files you
personally changed"*, *"Treat all pre-existing tracked and untracked changes as other
sessions' work."* A verifier that commits to `main` with a blanket add is a
data-loss vector, and it is the one component whose failure is least likely to be
noticed, because the commit is supposed to look like a success.

#### 2.3.4 The escalation path discards the evidence it exists to deliver

On `RetryExhausted`, `engine.verify()` constructs a fresh `VerificationResult` with
**no `check_results` and no `semantic_diff`**. The SPEC promises escalation *"with
full context"*; the implementation escalates with a count. The stack traces, the
failing test output, and the semantic diff — the exact material a human needs to fix
the ticket — are dropped on the floor at the moment they become most valuable.

Related: the retry loop calls `taskrivet_retry(prompt)` and then re-runs *the same
checks* with no check that the code actually changed. An agent that ignores the
correction burns the full retry budget in seconds. There is no no-progress detector.

#### 2.3.5 Two contracts, one config, and a hash that does not cover its own data

- `VerificationAudit.compute_hash()` hashes `work_package_id + decision + rationale
  + passed + committed + timestamp`. It does **not** cover `key_evidence`, `attempts`,
  `repo_path`, the check results, or the diff. The "content hash" attests to a
  sentence, not to the evidence.
- `EdgeRecordLogger.log_decision()` recomputes the same string and *overwrites*
  `audit.content_hash` — so the value the model's validator computed is discarded.
  Two independent definitions of the same hash, kept in sync by hand.
- `verify_chain()` orders by `timestamp` and compares each row's `previous_audit_hash`
  to the previous row's `content_hash`. With concurrent writers, two processes can
  select the same predecessor and fork the chain; with timestamp ties, the ordering
  is arbitrary. The chain has no lock, no monotonic sequence, and no per-record
  predecessor guarantee.
- `VerificationConfig.__post_init__` is **never called** — pydantic v2 requires
  `model_post_init`. The `~` in `edgerecord_db` is never expanded by the config
  (it happens to be expanded later by `EdgeRecordLogger`, so the bug is masked).
- `config --init` writes `~/.el-verificador/config.yaml`; **nothing ever reads it**.
  The CLI only loads config when `--config` is passed explicitly. The documented
  configuration path is a dead end.
- `datetime.utcnow()` is used in 4 places and emits `DeprecationWarning` on the
  Python 3.14 interpreter this project is actually running under.

### 2.4 Test coverage of the real risk surface

| Module | Test file | Note |
|---|---|---|
| `schemas.py` | `tests/test_schemas.py` | Present |
| `engine.py` | `tests/test_engine.py` | Present, but **mocks `SemanticDiffEngine` entirely** |
| `correction.py` | `tests/test_correction.py` | Present |
| `docker_runner.py` | `tests/test_docker_runner.py` | Present |
| **`semantic_diff.py`** | **none** | **The headline feature is untested.** |
| `git_ops.py` | none | Contains the `git add -A` defect, untested |
| `ollama_client.py` | none | |

The engine test suite replaces the one component that answers the central question
with a mock. So the semantic diff is untested *and* mocked out of the one test that
exercises it — which is precisely how the dead `MODIFIED` branch and the 0.1-coverage
bug survived four commits.

### 2.5 The documentation is the largest single risk

`README.md` says **Status: Implemented**. `SPEC.md` v1.0 specifies an EdgeRecord
integration, a semantic intent check, a ticket-board escalation, and a guardrailed
feedback loop as though all are done. `AGENTS.md` states these as contributor rules
("Keep `max_retries=3` and the fail-safe escalation path intact", "Every verification
decision must produce an EdgeRecord audit record"). An agent reading this repo's own
instructions will build on capabilities that do not exist, and will preserve bugs
because they look like load-bearing design.

**The first phase of work is therefore editorial and corrective, and it is not
optional.** The repo's instructions must be true before any agent extends them.

---

## 3. Target architecture

### 3.1 Layer map

```
                    ┌───────────────────────────────────────────┐
   Taskrivet ──────▶│  integrate/   adapters & protocol edges   │
   Ticket Board ───▶│  (Taskrivet, Board, EdgeRecord, MCP)      │
   CLI / MCP ──────▶└───────────────┬───────────────────────────┘
                                    │
                    ┌───────────────▼───────────────────────────┐
                    │  orchestrate/  run state machine            │
                    │  PROVISION→CHECK→SEAL→ALIGN→DECIDE→EMIT    │
                    └───┬───────────────┬───────────────┬─────────┘
                        │               │               │
          ┌─────────────▼──┐   ┌────────▼────────┐   ┌─▼──────────────┐
          │   sandbox/     │   │    align/       │   │   evidence/    │
          │ real isolation │   │ obligation model│   │ content-addressed
          │ no host exec   │   │ propose → verify│   │ store + manifest
          └─────────────┬──┘   └────────┬────────┘   └─┬──────────────┘
                        │               │               │
          ┌─────────────▼───────────────▼───────────────▼─────────────┐
          │  contracts/   pydantic v2 models, factory-contracts-aligned │
          └────────────────────────────────────────────────────────────┘
```

### 3.2 The decision function

The single most important structural change: **the decision is a pure function of
sealed evidence.** No I/O, no clock, no model, no randomness.

```python
def decide(obligations: list[Obligation]) -> Decision:
    """Pure. Same obligations in → same decision out, forever."""
    if any(o.status is ObligationStatus.VIOLATED for o in obligations):
        return Decision.FAIL
    if any(o.status is ObligationStatus.UNPROVEN for o in obligations):
        return Decision.FAIL
    return Decision.PASS
```

Everything else in the system exists to produce obligations with honest statuses and
to attach evidence to them. This is what makes the audit trail mean something: a
verifier that can be replayed is trusted; a verifier that must be re-run is not.

### 3.3 The obligation model (replacing the coverage score)

An **obligation** is one checkable claim extracted from the intent, the acceptance
contract, and the repository's own conventions.

```python
class ObligationKind(str, Enum):
    ACCEPTANCE_COMMAND = "acceptance_command"   # the ticket's own command must exit 0
    BEHAVIOR_DEMONSTRATED = "behavior"           # a named behavior is covered by a passing test
    SYMBOL_PRESENT = "symbol"                   # a named symbol exists in the changed code
    FILE_TOUCHED = "file"                       # a named file was modified
    SCOPE_RESPECTED = "scope"                   # nothing outside the allowed paths changed
    NO_STUB = "no_stub"                         # no unimplemented placeholder in changed code
    TESTS_NOT_WEAKENED = "tests_intact"         # no test was deleted, skipped, or asserted away

class Obligation(BaseModel):
    id: str
    kind: ObligationKind
    statement: str                    # "the /api/users route returns 200 for a valid body"
    source: str                       # which input produced this obligation
    check: ObligationCheck            # how it is proven
    status: ObligationStatus          # SATISFIED | VIOLATED | UNPROVEN | NOT_APPLICABLE
    evidence_refs: list[str]          # content hashes into the evidence store
    detail: str = ""
```

**Where the LLM fits — proposal, never adjudication.**

| Component | Role | Failure mode |
|---|---|---|
| LLM (local Qwen) | *Proposes* a candidate obligation set from `true_goal` + acceptance + context | Advisory only. Its proposal is recorded in the receipt so a human can audit what was asked vs. what was enforced. |
| Deterministic evaluator | *Enforces* mechanically checkable obligations, and decides | Never consults the LLM. Never takes the LLM's word. |
| Operator | Approves the obligation set for high-stakes work packages | Session cards are records, not authorizations — so approval is explicit and logged. |

This is the same shape the rest of the factory already uses: Destilador *proposes*
routing with a confidence score; Hidratador's `route()` returns **unranked** options
with `execution_authorized: False`. El Verificador should not be the one component in
the chain that lets a model decide.

**Degraded mode is mandatory.** If Ollama is unreachable, the deterministic
obligations (acceptance commands, file/scope, stub, test-integrity) still run, and
the run is stamped `degraded_alignment: true` with the reason. It must never
silently skip alignment, and it must never fail-open.

### 3.4 The acceptance contract (currently dropped on the floor)

`IntentPackage.from_dict()` keeps `id`, `true_goal`, `true_goal_summary`,
`related_files`, `context_packet`, `estimated_tokens`. It throws away everything
else in the `DistilledWorkPackage`, including the scoping, risk factors, evidence
trail, and — critically — it never picks up the **acceptance criteria**, which live
on the ticket (`El Hidratador`'s `Ticket.acceptance` and `Ticket.acceptance_criteria`
fields, and `workboard`'s ticket rows). Both fields exist. Neither is used.

The verifier runs a hardcoded `["pytest -q", "ruff check .", "bandit -r . -q"]`
regardless of what the work package actually promised. A ticket that asked for
`npm test && npm run lint` is "verified" by running pytest.

```python
class AcceptanceContract(BaseModel):
    commands: list[AcceptanceCommand]     # from the ticket's acceptance block
    expected_files: list[str]             # files the goal implies
    forbidden_paths: list[str]            # scope fences
    coverage_floor: float | None
    timeout_s: int
    source: str                           # "ticket" | "work_package" | "default"
```

Parsing rule: a fenced block in the acceptance text is a command list; anything else
falls back to the configured default, and the receipt records which was used.

### 3.5 Evidence store

Evidence is content-addressed, immutable, and referenced by hash. Not summaries —
the raw bytes.

```
~/.el-verificador/evidence/
  └── ab/
      └── ab12cd34.../
          ├── manifest.json          # run id, kind, sha256, bytes, produced_at, produced_by
          ├── check-pytest.stdout
          ├── check-pytest.stderr
          ├── junit.xml
          ├── diff.patch
          ├── ast-summary.json
          └── sandbox-meta.json
```

Rules:
- One artifact = one file = one hash. The hash is in the manifest and in the receipt.
- Evidence is **sealed** at a defined point in the run (after checks complete, before
  alignment is evaluated). After sealing, no artifact is mutable.
- The decision cites evidence hashes. The receipt references artifact paths, types,
  byte counts, and hashes.

### 3.6 Run state machine

```
                 ┌──────────┐
                 │ PENDING  │
                 └────┬─────┘
                      ▼
              ┌───────────────┐
              │ SANDBOX_READY │──── error ──▶ ┌──────────┐
              └────┬──────────┘              │  FAILED  │ (sealed evidence, no pass)
                   ▼                         └──────────┘
             ┌──────────────┐                       ▲
             │ CHECKS_RUN   │───────────────────────┘
             └────┬─────────┘
                  ▼
          ┌──────────────────┐
          │ EVIDENCE_SEALED  │◀── the boundary that makes replay possible
          └────┬─────────────┘
               ▼
          ┌──────────────────┐
          │ ALIGNMENT_EVAL   │
          └────┬─────────────┘
               ▼
          ┌──────────────────┐
          │    DECIDED       │
          └──┬───┬───┬──────┘
             │   │   │
     PASS ───┘   │   └─── ESCALATED ──▶ ticket flagged + receipt
             │   │
             │   └─── FAIL ──▶ CORRECTION_ISSUED ──▶ (retry, no-progress guard)
             │                                     │
             ▼                                     ▼
       ┌──────────┐                        ┌──────────────┐
       │ COMMITTED │                        │ EXHAUSTED    │
       └──────────┘                        └──────────────┘
```

Every transition appends an event. The event log plus the sealed evidence is
sufficient to re-derive the decision without re-running anything.

**Diagram.** The target run lifecycle is specified in
`docs/archify/verification-run.lifecycle.json` and rendered as an explorable
standalone HTML artifact (Archify artifact id
`a2d23dcd2e5f4f29a62ac9017b25c1eb`, local HTML at
`~/.ai-dashboard/archify/artifacts/a2d23dcd2e5f4f29a62ac9017b25c1eb/diagram.html`,
viewable in the dashboard at `http://127.0.0.1:7878/#archify`). It shows the three
bands — verification run, guardrailed correction loop, terminal outcomes — with the
evidence seal as the boundary before the decision. The diagram is a *target*; the
current code's behaviour is in §2.

### 3.7 Invariants (enforced by tests, not by convention)

1. **No untrusted code executes outside the sandbox.** There is no host-execution
   fallback, in any mode, including "dry run". Dry run replays fixtures.
2. **The decision is a pure function of sealed evidence.** Re-deriving it from the
   evidence store yields an identical result, indefinitely.
3. **Every decision cites at least one evidence hash.** No evidence → no decision.
4. **The LLM never decides.** It proposes; the deterministic evaluator decides.
5. **Failure is evidence, not an exception.** Any crash produces a sealed evidence
   bundle and a `FAIL`/`ESCALATE` decision. A crash is never a silent pass.
6. **The verifier writes only to paths it owns.** No blanket `git add`. No writes to
   a shared working tree outside the evidence store, the receipt directory, and the
   ticket note it is explicitly authorized to write.
7. **The receipt is the durable artifact.** The SQLite ledger, if kept at all, is a
   query cache over receipts — never a competing source of truth.
8. **Local inference only.** No silent substitution of a paid cloud model. If the
   configured model is missing, say so and degrade explicitly.

### 3.8 Sandbox isolation requirements

Not "runs in Docker" — a specific floor:

| Control | Requirement |
|---|---|
| Execution | Non-root uid, no `docker.sock`, no host namespace sharing, read-only root fs with a tmpfs scratch layer |
| Network | Disabled by default; an explicit, per-work-package egress allowlist if a test genuinely needs it — logged in the receipt |
| Resources | CPU, memory, PID, and wall-clock caps; wall-clock kill is a hard timeout, not a soft one |
| Scope | Only the worktree under test is mounted; the operator's home is not |
| Hygiene | A fresh, digest-pinned image per run; no mutation of the image between checks |
| Provenance | Image digest, container id, and the mount list recorded in `sandbox-meta.json` |

### 3.9 Contract alignment

| Concern | Target | Current |
|---|---|---|
| Work item | `factory-contracts/schemas/work_item.v1.json` | Not integrated |
| Receipt | `factory-contracts/schemas/receipt.v1.json` + EdgeRecord canonical hashing | Invented SQLite table |
| Handoff | `factory-contracts/schemas/handoff.v1.json` | Not integrated |
| Distilled work package | `el-destilador`'s `DistilledWorkPackage` | Subset, via `IntentPackage.from_dict` |
| Ticket / acceptance | `el-hidratador` `Ticket.acceptance` / `acceptance_criteria`; `workboard` ticket rows | Never read |

`factory-contracts` exists precisely to stop every component inventing its own
version of the same object. El Verificador is currently inventing its own version of
the receipt. Phase 4 closes this.

---

## 4. The build plan

Six phases. Each has a goal, a scope, and **exit criteria that are objectively
checkable** — a phase is done when a command or a check passes, not when a feature
"exists".

---

### Phase 0 — Tell the truth
**Window:** weeks 1–2. **Depends on:** nothing. **Blocks:** everything else.

This phase changes no architecture. It makes the repository honest and builds the
assets that make the rest of the plan verifiable.

**Scope**

*Correctness*
- Fix the AST diff so `MODIFIED` is emitted: capture real source text (`ast.get_source_segment`), compare function/method bodies, and report added/removed/modified symbols with before/after.
- Scope stub detection to the *diff*, not the whole file, and detect stubs AST-aware (a function whose body is only `pass`/`...`/`raise NotImplementedError`, or whose body is only a TODO comment) rather than by regex over raw lines. Keep a configurable allowlist for legitimate `pass` (e.g. abstract protocol methods).
- Rename `intent_coverage` to what it actually measures, or delete it. Recommendation: delete it in favour of the obligation model (Phase 2); until then, rename it `related_files_touched_ratio` and stop presenting it as intent coverage.
- Enforce `intent_coverage_threshold` in the engine, or delete the setting. Recommendation: delete in Phase 2; enforce in Phase 0 so the setting stops lying.
- Honour `fail_on_todo_stubs` in the engine.
- Fix `VerificationConfig.__post_init__` → `model_post_init`.
- Replace all `datetime.utcnow()` with `datetime.now(UTC)`.
- Make the LLM correction text actually reach Taskrivet, or remove the Ollama dependency from the correction path. Recommendation: remove it in Phase 0; Phase 2 reintroduces a *useful* LLM role (obligation proposal) with a real output.
- Give `VerificationAudit.compute_hash()` a single definition, cover the full decision body, and have `log_decision()` stop overwriting it.
- Make the audit chain monotonic and concurrency-safe: a sequence column, a transaction-scoped predecessor read, and a unique index on `previous_audit_hash`.
- Load `~/.el-verificador/config.yaml` by default; make `--config` an override. Validate the YAML through the pydantic model (`model_validate`) instead of `setattr`, and fail loudly on unknown keys.

*Safety*
- Remove the host-execution fallback from `docker_runner`. `dry_run` becomes fixture replay. Add a module-level guard that raises if a check is scheduled for host execution.

*Git safety*
- Replace `git add -A` with an explicit path list derived from the diff.
- Never `git checkout` in a shared working tree; work on a dedicated branch.
- Add a test that asserts the commit contains only the changed-file set.

*Documentation*
- Rewrite `README.md` status to the truth: **alpha, partially implemented**.
- Rewrite `SPEC.md` to a *target* specification, with each section marked
  `IMPLEMENTED` / `PARTIAL` / `NOT IMPLEMENTED`, and a `## Status` line per mechanism.
- Rewrite `AGENTS.md` so every contributor rule is a rule about code that exists.
  Remove the EdgeRecord rule until Phase 4 makes it true.
- Add a `docs/CONTRACTS.md` recording the contract with each sibling component and
  which side owns it.

*Tests*
- Add `tests/test_semantic_diff.py` and `tests/test_git_ops.py`.
- Add a **golden fixture suite** (§4.0.1) — this is the single highest-value asset in
  the phase and the foundation of Phases 1–3.

**Exit criteria**
- [ ] `pytest` passes; `ruff` and `mypy --strict` clean.
- [ ] Every module in `el_verificador/` has a test file.
- [ ] `grep -c intent_coverage_threshold el_verificador/engine.py` → non-zero, or the setting is deleted.
- [ ] A golden fixture with a rewritten function body reports `MODIFIED` with a before/after snippet.
- [ ] A golden fixture with a host-execution attempt in dry-run mode raises rather than executing.
- [ ] A commit in the golden repo contains only the changed-file set (asserted by a test).
- [ ] `README.md` status matches reality; `SPEC.md` has a per-mechanism status marker.
- [ ] No `datetime.utcnow()` remains.

#### 4.0.1 The golden fixture suite (build this first)

A directory of small synthetic repositories, each with a *known correct verdict*.
These fixtures are the regression harness for every later phase.

```
tests/fixtures/
  correct_impl/        → PASS    (implements the goal; tests pass; scoped)
  todo_stub/           → FAIL    (test passes, implementation is a stub)
  wrong_impl/          → FAIL    (tests pass, behaviour is wrong)
  test_gaming/         → FAIL    (agent deleted or weakened the failing test)
  scope_creep/         → FAIL    (agent modified an unrelated file)
  no_tests/            → FAIL    (no test demonstrates the new behaviour)
  empty_diff/          → FAIL    (nothing was changed at all)
  broken_build/        → FAIL    (import error; every check fails)
  py_only/             → PASS    (baseline sanity)
```

Each fixture is a repo with a base commit and a head commit, plus the expected
decision and the expected violated obligations. These nine fixtures encode the
entire "did the agent actually build the thing" question as executable assertions.

---

### Phase 1 — Real isolation and the real acceptance contract
**Window:** weeks 3–8. **Depends on:** Phase 0.

**Goal:** the verifier runs untrusted code nowhere but in a sandbox, and it runs
*the acceptance contract the work actually specified*.

**Scope**
- **Sandbox image.** Build and publish a digest-pinned verification image in this
  repo (`docker/verification/Dockerfile`): pinned base by digest, pinned toolchain
  (pytest, ruff, bandit) as *pre-installed* dependencies so the network can stay
  off, non-root uid, no package manager at runtime.
- **Decide the hydration contract with El Hidratador.** El Hidratador already owns
  leases, token budgets, and per-task state. The recommendation: *El Hidratador owns
  the environment description; El Verificador owns the verification container.*
  Define a `SandboxSpec` contract (image digest, mount list, env allowlist, resource
  caps) that El Hidratador can produce and El Verificador can consume. Until that
  contract exists, El Verificador provisions from its own image — but the README
  must stop claiming El Hidratador provisions it.
- **Resource limits and hard timeouts**, per §3.8.
- **Acceptance contract ingestion.** Read the ticket's `acceptance` /
  `acceptance_criteria` (via `workboard`/`el-hidratador`), parse a fenced command
  block into `AcceptanceContract`, fall back to the configured default, and record
  which was used.
- **Structured test results.** Capture JUnit XML (`pytest --junit-xml`) and parse it
  into per-test results. Exit code alone is not evidence: it cannot distinguish
  "3 tests failed" from "collection error", and it cannot support the
  `TESTS_NOT_WEAKENED` obligation.
- **Evidence store.** Implement §3.5: content-addressed store, manifest, sealing.
- **Per-check isolation.** Run each acceptance command in its own container exec with
  its own resource accounting, so one hung test does not poison the rest.

**Exit criteria**
- [ ] A fixture repo whose test executes `os.system("touch /tmp/pwned")` leaves no trace on the host, and the sandbox is confirmed to have run it (sandbox-meta records the container id and digest).
- [ ] The sandbox refuses to start with a host-network or host-pid configuration (rejected at config validation, not at runtime).
- [ ] A ticket with three custom acceptance commands runs exactly those three, in order, and nothing else.
- [ ] JUnit parsing reports per-test pass/fail counts for the `correct_impl` fixture.
- [ ] Every check in a run has a corresponding content-addressed artifact in the evidence store, and the manifest hashes verify.
- [ ] Resource caps are enforced and observable (a test that allocates 2 GB is killed and recorded as `KILLED_RESOURCE`).

---

### Phase 2 — Real intent alignment
**Window:** weeks 9–16. **Depends on:** Phase 1.

**Goal:** replace the word-overlap score with obligations that a human can audit and
a machine can enforce.

**Scope**
- Implement the `Obligation` model and the pure `decide()` function (§3.2, §3.3).
- **Deterministic obligation extractors:**
  - `AcceptanceCommand` — from the acceptance contract.
  - `ScopeRespected` — from the changed-file set vs. the allowed scope; unexpected paths are violations.
  - `NoStub` — AST-aware, diff-scoped, allowlist-aware.
  - `TestsIntact` — compare the test files at base vs. head: deletions, `@pytest.mark.skip` additions, weakened assertions, and `conftest.py` changes are violations. *This is the obligation that catches test-gaming, and it is the one no current implementation has.*
  - `BehaviorDemonstrated` — a new behavior must be covered by a test that exists at head and did not exist at base.
- **LLM obligation proposer (advisory).** Local Qwen proposes obligations from the
  true goal and the context packet. Proposal is recorded in the receipt; only
  mechanically checkable proposals are enforced; everything else is reported as
  `UNPROVEN` with a reason, or parked for operator review.
- **Degraded mode** when Ollama is unavailable, stamped in the receipt.
- **Multi-language parsing.** Replace Python's `ast` with **tree-sitter** so the
  diff engine works for JavaScript/TypeScript, Go, and Rust. Keep `ast` as a
  fast path for Python if measurement says it is worth it. The factory's repos are
  predominantly Python, but the *verified* repos are the operator's customers' code,
  which is not.
- **Obligation-trace output.** Every decision prints "5 obligations, 3 satisfied,
  1 violated, 1 unproven" with the violated statement in plain language. This is
  what goes into the Correction Prompt and into the ticket note.

**Exit criteria**
- [ ] `todo_stub` fixture → `FAIL`, violated obligation `NO_STUB`, message names the stub symbol.
- [ ] `wrong_impl` fixture → `FAIL` (a passing test that does not demonstrate the goal must not pass).
- [ ] `test_gaming` fixture → `FAIL`, violated obligation `TESTS_NOT_WEAKENED`, message names the deleted/weakened test.
- [ ] `scope_creep` fixture → `FAIL`, violated obligation `SCOPE_RESPECTED`, message names the out-of-scope file.
- [ ] `correct_impl` fixture → `PASS` with every obligation satisfied.
- [ ] `decide()` is pure: property-based test (Hypothesis) asserts identical output for shuffled obligation order and repeated evaluation.
- [ ] With Ollama stopped, the suite still passes and the run is stamped `degraded_alignment: true`.
- [ ] A JavaScript fixture repo produces a non-empty symbol diff.

---

### Phase 3 — Pipeline integration
**Window:** weeks 17–24. **Depends on:** Phase 2.

**Goal:** El Verificador stops being a tool someone runs and becomes the component
the pipeline asks.

**Scope**
- **Executor protocol.** Define `Executor` — `execute(correction) -> ExecutionOutcome`
  — and implement `TaskrivetAdapter` against Taskrivet's real interface. Replace
  the ad-hoc `Callable[[CorrectionPrompt], None]` callback.
- **No-progress guard.** Fingerprint the worktree between attempts (tree hash of
  tracked files). If attempt *n+1* has the same fingerprint as attempt *n*, that is a
  no-progress retry: escalate immediately, do not burn the remaining budget. Also cap
  the loop by wall-clock (`max_retry_duration_ms` — currently dead — and a
  per-attempt cap) and by an explicit cost budget.
- **Ticket-board adapter.** On `ESCALATE`, write a `needs-human` note and tag to the
  ticket, using the existing `workboard.update_ticket(ticket, fields)` surface
  (allowed fields: `status`, `priority`, `title`, `project_label`, `tags`, `notes`).
  Constraints: the write is logged verbatim in the receipt; a missing or unauthorized
  ticket board degrades to "escalation recorded locally" rather than failing the run;
  and the write is a *record*, never an authorization (no status change that implies
  work may proceed).
- **MCP tool surface.** Register read-mostly tools on the dashboard's MCP surface so
  agents can *ask the verifier* instead of reimplementing checks:
  - `verify_work_package(work_package, repo, base, head) -> VerificationOutcome`
  - `verification_status(work_package_id) -> VerificationOutcome`
  - `verification_evidence(run_id) -> list[EvidenceRef]`
  Following the existing `hsf_*` naming and the "connections do not grant permission"
  rule from the dashboard's `.factory/AGENTS.md`.
- **Run in a separate process.** The final gate should not share a process, a
  filesystem view, or a credential set with the agent whose work it judges. The MCP
  tool surface is the natural home for this.
- **Concurrency.** Multiple work packages must verify in parallel without corrupting
  each other's evidence, receipts, or git state. Each run gets its own worktree.

**Exit criteria**
- [ ] An end-to-end run drives Taskrivet: a failing implementation is corrected, re-verified, and passes — with the correction trace showing the failing obligation that drove the prompt.
- [ ] An agent that ignores the correction escalates **early** via the no-progress guard, not after burning three full attempts.
- [ ] An escalation updates the ticket with a `needs-human` note, and the receipt records the exact fields written.
- [ ] `verify_work_package` over MCP returns a decision with evidence hashes, and the caller cannot obtain a PASS without them.
- [ ] Two concurrent runs on different worktrees produce independent, non-interleaved evidence and receipts.

---

### Phase 4 — Proof-of-work conformance
**Window:** weeks 25–32. **Depends on:** Phase 3.

**Goal:** the durable record is a real EdgeRecord receipt, on the real chain, with the
real hashing rule.

**Scope**
- **Emit EdgeRecord receipts.** Use the canonical hashing from `RUN_ERA.md`:
  recursively key-sorted, compact separators, `ensure_ascii=False`, UTF-8, SHA-256;
  `proof_fingerprint` = first 16 hex, `full_hash` = all 64. **Never hand-type a
  hash** — reuse `bin/era-fingerprint.py`'s logic (import it, or reimplement with a
  test that proves byte-equality against it).
- **Chain receipts.** Each verification receipt carries `previous_receipt_id` and
  `previous_fingerprint`, following the existing receipt pattern.
- **Conform to `factory-contracts/schemas/receipt.v1.json`** (`receipt_id`, `kind`,
  `created_at`, `ticket`, `actor`, `outcome`, `artifacts`, `notes`) and validate
  against it in CI.
- **Retire the invented SQLite ledger.** Keep a SQLite table only as a *query cache*
  over receipts, explicitly marked as derived and rebuildable. If the SQLite path
  stays, rename it away from "EdgeRecord" immediately in Phase 0.
- **Artifact linkage.** Receipt `artifacts` entries carry `path`, `type`, `bytes`, and
  `sha256`; the artifacts live in the evidence store and are referenced by relative
  path.
- **Bidirectional:** receipts are queryable to answer "has this repo/branch ever
  been verified?", and the evidence store is reachable from a receipt by id alone.

**Exit criteria**
- [ ] `python3 playbooks/verify-chain.py` in the EdgeRecord repo passes over receipts emitted by El Verificador.
- [ ] `python3 bin/era-fingerprint.py --check` reports `OK` for every emitted receipt.
- [ ] A receipt emitted for a non-ASCII summary hashes identically under Python and JavaScript `JSON.stringify` (the ERA-000314 regression).
- [ ] A CI test validates every emitted receipt against `factory-contracts/schemas/receipt.v1.json`.
- [ ] Deleting the SQLite database and rebuilding from receipts reproduces the full verification history.

---

### Phase 5 — Learn, and scale
**Window:** weeks 33+. **Depends on:** Phase 4.

**Goal:** the verifier gets measurably better, and stops being Python-only.

**Scope**
- **Metrics.** From the receipt chain: first-attempt pass rate, escalation rate by
  failure category, mean attempts to pass, false-positive rate (spot-checked by a
  human), time-to-verify, and the fraction of failures attributable to each
  obligation kind. Publish these; do not optimize against them silently.
- **False-positive control.** The most dangerous failure mode of a gatekeeper is
  crying wolf: a verifier that blocks good work gets switched off. Measure and
  protect against it. Every escalation should be sampled.
- **Policy tuning from evidence,** with operator approval for any change to
  thresholds. Never auto-tune a threshold that gates a commit.
- **Multi-language check packs.** Python, TypeScript/JavaScript, Go, Rust — each
  with its own test/lint/security command set and AST language, following the
  `AcceptanceCommand` + tree-sitter design.
- **Remote sandbox option.** Run the container on a remote worker when the local
  machine cannot isolate a run (this is *not* `horizon-broker`, which queues
  inference, not execution).
- **Regression corpus.** Every real escalation becomes a golden fixture, so the
  verifier's future accuracy is measurable against its own history.

**Exit criteria**
- [ ] A published metrics report with at least 100 real verification runs.
- [ ] Sampled false-positive rate below an agreed threshold.
- [ ] Every escalation in the corpus has a corresponding golden fixture.
- [ ] A TypeScript and a Go repo verify end-to-end.

---

## 5. Guardrails — the things that must never happen

These are constraints on the design, not aspirations. Each maps to a test.

1. **Never execute agent-generated code on the host.** No fallback, no "just for
   local dev", no dry-run exception.
2. **Never let a model decide a pass or a fail.** Models propose; deterministic code
   adjudicates.
3. **Never let a decision exist without evidence.** No evidence hash → no decision.
4. **Never commit with a blanket add.** Stage only the paths the diff names.
5. **Never touch a shared working tree** outside paths this run owns. Use a dedicated
   worktree and branch.
6. **Never swallow an exception into a pass.** Failures are evidence; crashes are
   failures; a crash is never a silent success.
7. **Never substitute a paid cloud model for local inference.** If the local model is
   missing, degrade explicitly and record it.
8. **Never trust Taskrivet's self-reported status, or Taskrivet's own test results.**
   Re-derive everything.
9. **Never let a receipt claim more than the evidence shows.** A receipt that says
   "verified" must be backed by the artifact hashes it lists.
10. **Never let El Verificador authorize anything.** It verifies and it records. It
    does not approve, merge, publish, or unblock.

---

## 6. Engineering standards

Match the sibling repos (`el-destilador`, `el-hidratador`, `taskrivet`) and the
factory-wide rules:

| Standard | Value | Note |
|---|---|---|
| Python | 3.11+ (CI: 3.11, 3.13) | Runtime here is 3.14 — keep both working |
| Package | `el_verificador`, entry point `el_verificador.__main__:main` | Unchanged |
| Contracts | pydantic v2, `extra="forbid"` | Unchanged, and extend to `factory-contracts` |
| Tests | pytest, `unittest` style classes (both runners must work) | The CI uses `unittest discover`; keep it green |
| Lint | ruff, line-length 100 | Unchanged |
| Types | mypy strict | Unchanged |
| Time | `datetime.now(UTC)` everywhere | **Changed** — `utcnow()` is deprecated |
| Tests | Every module has a test file; golden fixtures cover every verdict class | **New** |
| Local inference | Ollama at `127.0.0.1:11434` | Default model must be one that is actually installed; resolve dynamically and fail loudly otherwise |
| Secrets | Never in fixtures, logs, receipts, or commits | Fixtures are synthetic |
| Commits | Stage by explicit path | **Changed** — no blanket add |

**Repo hygiene:** `.pytest_cache/` and `__pycache__/` are currently present in the
working tree. They are not tracked (good) but should be ignored and removed.

---

## 7. Definition of success

| Horizon | Measure | Target |
|---|---|---|
| Phase 0 | Docs match code; every module tested; no dead settings | 100% of §Phase 0 exit criteria |
| Phase 1 | Untrusted code runs only in the sandbox | 0 host executions, verified by test |
| Phase 2 | Golden fixtures produce the right verdict | 9/9 correct |
| Phase 3 | A real pipeline run self-corrects and escalates correctly | End-to-end demo passes |
| Phase 4 | Receipts verify on the real EdgeRecord chain | `verify-chain.py` passes |
| Phase 5 | The gate is trusted enough to leave enabled | False-positive rate below threshold; 100+ runs measured |

**The north star:** *a work package that passes El Verificador is safe to merge,
because the proof is reproducible by someone who was not there.*

---

## 8. Open decisions for the operator

Each of these changes the design and should be settled before the phase that depends
on it starts. My recommendation is given, but these are not mine to decide.

| # | Decision | Recommendation | Needed by |
|---|---|---|---|
| 1 | Does the verifier auto-commit to the target branch, or produce a verified branch + receipt and let a separate authorized act merge? | **Branch + receipt.** Auto-merging to `main` is a footgun in a shared checkout, and the factory's rules already push against it. | Phase 1 |
| 2 | Who owns the sandbox image — El Verificador or El Hidratador? | **El Hidratador owns the environment description (`SandboxSpec`); El Verificador owns the verification container.** Until the contract exists, El Verificador ships its own image and the README stops claiming otherwise. | Phase 1 |
| 3 | Should the LLM be allowed to propose obligations at all, or are obligations strictly deterministic? | **Advisory only, recorded in the receipt.** Deterministic-only would be simpler but would miss intent that cannot be mechanically derived. | Phase 2 |
| 4 | Must the final gate run in a separate process/host for "cannot grade its own homework" to hold? | **Yes.** The MCP tool surface is the natural home. | Phase 3 |
| 5 | Is 3 the right retry cap, or should it vary by work-package cost? | **Keep 3 as a hard floor; allow a policy to raise it for expensive tickets.** Add the no-progress guard first — most wasted budget comes from no-progress retries, not from too low a cap. | Phase 3 |
| 6 | Multi-language scope: Python-only for how long? | **Python through Phase 2; tree-sitter in Phase 2; JS/TS by Phase 5.** | Phase 5 |
| 7 | Should the SQLite audit table survive at all? | **As a derived query cache only, clearly labelled.** The receipt chain is the source of truth. | Phase 4 |
| 8 | Should `el-destilador` be fixed in parallel — it has the same invented-EdgeRecord defect? | **Yes, but not in this repo.** El Destilador needs the same receipt-conformance work. It is a separate change in a separate repo, and it should not block El Verificador. | Phase 4+ |

---

## Appendix A — Evidence log

Every claim in §2 was produced by running the code on 2026-10-22.

**A.1 — `MODIFIED` is never emitted; a rewritten function body is invisible**

```bash
cd /tmp && rm -rf evtest && mkdir evtest && cd evtest && git init -q .
# base: helper() -> 1, create_user_route() -> TODO stub
# head: helper() -> 2 (REWRITTEN), create_user_route() -> real implementation,
#        new_thing() added
cd ~/Projects/el-verificador && python3 -c "
from el_verificador.semantic_diff import SemanticDiffEngine
from el_verificador.schemas import IntentPackage
d = SemanticDiffEngine('/tmp/evtest','HEAD~1','HEAD').compute(
    IntentPackage.from_dict({'id':'dwp-1','true_goal':'Implement the create user route'}))
print([(c.change_type.value, c.name) for c in d.ast_changes])"
```

Observed: `[('added', 'new_thing')]`. The rewrite of `helper` and the de-stubbing of
`create_user_route` — the two changes that matter — are absent.

**A.2 — Intent coverage is decoupled from the code**

Same fixture, varying only whether the goal is implemented:

| Case | Coverage | Stubs |
|---|---|---|
| Goal implemented | 0.1 | False |
| Goal is a TODO stub | 0.1 | True |

Identical coverage. The number measures "how many `related_files` were touched"
(capped at 0.4), because `_scan_stubs()` returns only stub symbols and populates
`all_symbols` — the variable the coverage calculation treats as "all symbols".

**A.3 — The coverage threshold never gates**

```
$ grep -c intent_coverage_threshold el_verificador/engine.py
0
$ python3 -c "from el_verificador.engine import ElVerificador; ..."
D: _intent_aligned(coverage=0.0, no stubs) = True
```

**A.4 — The LLM's Correction Prompt is discarded**

```
$ grep -rn "_llm_text" el_verificador/
el_verificador/correction.py:190: object.__setattr__(correction, "_llm_text", rendered)
```

Set once, read never. `CorrectionPrompt.render()` builds a deterministic template
that ignores it. The configured default model `qwen2.5:14b` is not installed on this
machine (installed: `qwen3.8:latest`, `qwen2.5:7b`, `qwen3.8-hermes:latest`, …) —
and the failure is invisible because the output is thrown away.

**A.5 — The configured sandbox image does not exist**

```
$ grep -rl "taskrivet/sandbox" ~/Projects --include='*.py' --include='*.yaml' --include='*.yml' --include='*.md'
  ~/Projects/el-verificador/el_verificador/schemas.py
  ~/Projects/el-verificador/config.example.yaml
```

No Dockerfile builds it; no component publishes it. `el-hidratador` is a
coordination ledger and provisions no Docker.

**A.6 — The documented config file is never read**

`config --init` writes `~/.el-verificador/config.yaml`; `__main__.py` only loads a
config when `--config` is passed explicitly. There is no code path that reads the
default location.

**A.7 — The audit hash does not cover the evidence**

`VerificationAudit.compute_hash()` covers `work_package_id`, `decision`, `rationale`,
`passed`, `committed`, `timestamp`. It does not cover `key_evidence`, `attempts`,
`repo_path`, check results, or the diff. `EdgeRecordLogger.log_decision()` then
recomputes and overwrites it, so the model's own computed value is discarded.

**A.8 — The real EdgeRecord contract**

From `~/Projects/edgerecord-main/RUN_ERA.md` ("Canonical hashing") and
`bin/era-fingerprint.py`: receipts are git-tracked JSON in `receipts/`, fingerprinted
by recursively key-sorting, compact-separator JSON with `ensure_ascii=False`, UTF-8
encoding, and SHA-256; `proof_fingerprint` is the first 16 hex characters and
`full_hash` the full 64. `playbooks/verify-chain.py` verifies the chain and a
pre-commit hook blocks broken chains. The SQLite table in this repo appears in
neither.

**A.9 — Test coverage of the risk surface**

| Module | Test file |
|---|---|
| `schemas.py` | `tests/test_schemas.py` |
| `engine.py` | `tests/test_engine.py` (mocks `SemanticDiffEngine`) |
| `correction.py` | `tests/test_correction.py` |
| `docker_runner.py` | `tests/test_docker_runner.py` |
| `semantic_diff.py` | **none** |
| `git_ops.py` | **none** |
| `ollama_client.py` | **none** |

**A.10 — Baseline**

`pytest` → 27 passed. `python -m unittest discover -s tests` → 27 passed. CI
(`ubuntu-latest`/`macos-latest` × Python 3.11/3.13) is configured and green.
`datetime.utcnow()` emits `DeprecationWarning` under the Python 3.14 interpreter in
use.

**A.11 — Findings re-verified after the concurrent repairs**

Every finding in §2 was re-run against `56edd17` (after the Appendix B repairs).
Results, unchanged: `MODIFIED` still never emitted; `intent_coverage_threshold`
and `fail_on_todo_stubs` still 0 references in `engine.py`; `_llm_text` still set
and never read; `git add -A` still in `GitOps.commit()`; the `ESCALATE` branch of
`engine.verify()` still constructs a `VerificationResult` with no
`check_results` and no `semantic_diff`; the dry-run path in
`DockerRunner.run_check()` still shells out to the host; `VerificationConfig`
still defines `__post_init__` that pydantic v2 never calls; `~/.el-verificador/config.yaml`
still written by `config --init` and still never read; the audit hash still
computed from a concatenated string that excludes the evidence.

---

## Appendix B — Defects confirmed and fixed in the same change set

The assessment in §2 was made against `a1100cf`. While building the
screenshots in `docs/screenshots/`, four additional defects were confirmed by
running the code. All four are fixed in this change set, and each one shipped
green in CI because no test exercised the path it lived on.

**B.1 — Every non-dry-run verification crashed (`DockerRunner`)**

`DockerRunner.__init__` assigned `self._docker_available = None` while the
class also defined a method named `_docker_available()`. The instance attribute
shadowed the method, so `self._docker_available()` raised
`TypeError: 'NoneType' object is not callable` on every call outside dry-run
mode — including `start()`, `run_check()` and `teardown()`.

It was invisible to the suite because `dry_run or not self._docker_available()`
short-circuits before the shadowed method is reached, and every test in
`tests/test_docker_runner.py` set `dry_run=True`. The tool had therefore never
completed a production-mode verification.

*Fix:* the cache attribute is now `_docker_ok`. `TestDockerRunnerLiveMode`
exercises non-dry-run mode, so the short-circuit can no longer hide a defect
here.

**B.2 — The primary CLI command crashed (`__main__.py`)**

`json.dumps(result.model_dump(mode="json", indent=2), indent=2)` passed
`indent` to both calls, raising `TypeError` on every `verify` invocation. The
README's Quick Start could not have worked as written.

**B.3 — Dry-run acceptance checks ran in the wrong directory (`DockerRunner`)**

`start()` returned a `ContainerHandle` without storing it, so `run_check()`
fell back to `cwd=None` and executed the acceptance commands in the
operator's current directory rather than in the repository under verification.

**B.4 — The semantic diff crashed on binary files (`SemanticDiffEngine`)**

`compute()` read *every* changed file through `git show` with strict UTF-8
decoding, before checking whether the file was Python at all. A compiled
bytecode file, an image, or a binary lockfile in the diff raised
`UnicodeDecodeError` — turning a routine `FAIL` into an unhandled crash. Only
Python sources are read now, and decoding is lenient. A missing working
directory is also reported as such, instead of being misreported as a missing
command.

### B.5 — A missing sandbox image surfaced as a raw Docker 404

`start()` called `client.containers.run()` unguarded. When the configured
`docker_image` cannot be pulled, the SDK raises `docker.errors.ImageNotFound`
and the whole verification dies with a raw HTTP traceback that says nothing
about which image failed or what to do about it.

This was found by CI rather than by a test: GitHub's runners have a Docker
daemon, so the non-dry-run path ran for the first time, tried to pull
`taskrivet/sandbox:latest` — the image §2.3.2 says does not exist — and
failed the build.

*Fix:* `SandboxUnavailableError` now carries an actionable message naming the
image. The test suite no longer depends on whether the host running it has a
Docker daemon: the daemon probe is mocked, so the result is the same on a
laptop, in CI, and on a build host.

### B.6 — Two further defects recorded but not fixed here

These were found during the same pass. Both are left alone deliberately: each
one changes what passes and what fails, which is an operator decision (§8), not
a side effect of a documentation commit.

- **Check kinds are assigned positionally.** `run_all()` maps command *n* to
  `CheckKind` by `index % 3`. Adding, removing, or reordering a check silently
  relabels the others — a security scan can start being reported as a lint
  pass. The kind belongs in the config, next to the command.
- **`model_calls` is hard-coded to `0`** in every audit record, so model usage
  and cost are unrecoverable from the trail. The `max_retry_duration_ms`
  budget is likewise never enforced: the loop is bounded by attempt *count*
  but not by *time*.

## Appendix C — Screenshots

`docs/screenshots/` holds six **real captured CLI runs**, not mock-ups. The
capture script builds the two-commit demo repository in `docs/examples/`, runs
the actual `el-verificador` CLI against it, and replays the bytes it received
into a terminal window via `scripts/render_terminal.py`.

```sh
python scripts/capture_screenshots.py
```

| Image | What it shows |
|---|---|
| `01-verify-pass.png` | A passing verification: three acceptance checks green, the intent diff computed, the code committed. |
| `02-correction-prompt.png` | The bounded Correction Prompt handed back to Taskrivet — the real failing assertion, the real lint error, the real semantic diff. |
| `03-escalate.png` | The guardrail working: three attempts, all failed, `decision: escalate`. |
| `04-audit-chain.png` | `audit --verify-chain` over a populated ledger. |
| `05-audit-trail.png` | The hash chain itself: each record's `previous_audit_hash` pointing at its predecessor's `content_hash`, genesis `null`. |
| `06-config-init.png` | `config --init` writing the configuration file. |

Two things the captures make visible that prose does not:

1. **The intent-coverage score reads `1.00`, and the run passes.** Before the
   Appendix D repairs this number was `0.30` — it was computed from stub
   symbols rather than from the code, and it could not tell a correct
   implementation from a stub. It is now computed from the identifiers the
   goal names, and it is reported with its inputs. The configured threshold of
   `0.85` is still not enforced, so a green `verify` still means *"the
   acceptance commands exited zero, no stub was found, and the change provides
   something the goal named"* — not *"the intent was fulfilled"*. Until Phase 2
   replaces the score with obligations, that is the honest reading.
2. **The three-attempt escalation is real.** The CLI's retry hook prints the
   Correction Prompt rather than dispatching it to an agent, so the same
   failure is re-measured three times and the loop breaks. That is the
   containment guarantee working, and it is also why Phase 3 exists.

## Appendix D — Defects confirmed and fixed in the semantic-diff layer

The §2 assessment was made against `a1100cf` and re-verified against `56edd17`.
This appendix records the findings in §2.2 that have since been closed, with
the measurement that shows each one is closed. Everything in §2.2 that is not
listed here still holds as written.

**D.1 — `MODIFIED` was never emitted (`semantic_diff.py`)**

`_node_tag()` stored the string `"FunctionDef:name"` as a symbol's *source*, so
the base and head copies of the same symbol were always equal and the
comparison `base_sym.source != sym.source` could never be true. The diff engine
could not see a function body being rewritten — the single most important
change in the whole exercise.

*Fix:* symbols now carry their real source segment, and they are matched by
kind and qualified name, so `A.render` and `B.render` are not confused for
each other. A rewritten body is reported as `MODIFIED` with the old and new
source, bounded to 2,000 characters each.

**D.2 — The coverage score measured the wrong thing (`semantic_diff.py`)**

`_intent_coverage()` unioned the goal words with `all_symbols`, but
`all_symbols` was populated from `_scan_stubs()`, which returns only *stub*
symbols. For clean code the set was empty, so the score was "how many
`related_files` did you touch", capped at 0.4 — measured at 0.1 for a correct
implementation and 0.1 for a stub.

*Fix:* the score is now the weighted combination of the declared scope that was
touched and the goal's **checkable claims** — the identifiers the goal names
(`register_user`, `validate_payload`, `app/routes/users.py`), compared token by
token against the names and bodies of the changed symbols. Prose is excluded
from the denominator, because "implement", "add", and "new" cannot be checked
against code and would only make the score depend on how the goal was worded.
A goal that names no identifiers falls back to its significant words, and the
basis says which was used.

Measured on the demo fixture: **1.00 for the implementation, 0.36 for the
stub** that omits three of the five named functions. The score is reported
with its inputs — goal words, covered words, uncovered words, the identifiers
the goal named, and the related files that were touched — and the audit record
carries the same evidence.

**D.2b — A change that ignored the goal entirely could still pass (`engine.py`)**

The acceptance commands can all exit zero while the agent edits something
unrelated. With the score unable to see intent, nothing caught that.

*Fix:* a third rule, independent of the score: if the goal names identifiers
and the change provides **none** of them, verification fails. The rule has no
false positives — a change that contains none of the things the goal asked for
is not an attempt at the goal.

**D.3 — `fail_on_todo_stubs` was never read (`engine.py`)**

The config option existed and was documented, and was referenced zero times in
`engine.py`. Stubs always failed, regardless of the setting.

*Fix:* the option is honoured. The default remains `true`, so the decision
behaviour is unchanged.

**D.4 — The model's Correction Prompt was generated and thrown away
(`correction.py`, `schemas.py`)**

`correction.py` called Ollama, then stored the text with
`object.__setattr__(correction, "_llm_text", rendered)` — smuggling a field
past `extra="forbid"`. `CorrectionPrompt.render()` never read it, so the
generated text never reached Taskrivet.

*Fix:* `llm_text` is a real field on the contract, and `render()` includes it
in a clearly labelled *Advisor's Note* section, after the deterministic
evidence and before the required action. It is advisory: the decision was made
from the evidence, and the note cannot change it.

**What did not change.** The `intent_coverage_threshold` of 0.85 is still
reported rather than enforced. The score now measures what it claims to
measure, and it separates a faithful implementation from a stub by a wide
margin, but it has not been calibrated against a population of real work
packages, and a correct implementation that spells a name differently would
score lower than the threshold. Phase 2 replaces the score with obligations;
until that lands, the honest reading of a green `verify` is still *"the
acceptance commands exited zero, no stub was found, and the change provides
something the goal named"* — not *"the intent was fulfilled"*.

**Still open, unchanged:** the host-execution fallback in dry-run (§2.3.1), the
missing sandbox image (§2.3.2), the escalation path dropping its evidence
(§2.3.4), and the audit trail not being written to the real EdgeRecord (§2.2).

---

## Appendix E — Defects confirmed and fixed in the commit and escalation paths

Two more findings from §2.3, confirmed by running the code against a
repository that contained the operator's untracked files.

**E.1 — `git add -A` on a shared checkout (`git_ops.py`, `engine.py`)**

`GitOps.commit()` ran `git checkout <branch>` then `git add -A` then
`git commit`. `git add -A` stages *every* file in the tree.

Reproduced on the demo repository with the operator's own files present, a
verifier that passed the verification produced this commit:

```
feat(el-verificador): verify dwp-4c81f0a7b2d9 - Add register_user ...
.env                                    ← a secrets file
.venv                                   ← an entire virtualenv
app/routes/users.py
operator-notes.md                       ← the operator's unrelated work
verificador.yaml
work_package.json
```

The factory's operating rules are explicit and repeated: *"NEVER `git add -A`
or `git add .`"*, *"Stage only the files you personally changed"*, *"Treat all
pre-existing tracked and untracked changes as other sessions' work."* A
verifier that commits to `main` with a blanket add is a data-loss vector, and
it is the one component whose failure is least likely to be noticed, because
the commit is supposed to look like a success.

*Fix:* the set of paths to stage is an explicit argument, and there is no
blanket add anywhere in the commit path. The commit is refused when the index
already holds staged work the verification did not review, and when the
working tree's copy of a reviewed file differs from the commit that was
actually inspected — committing that would put unverified bytes into a commit
carrying a verification receipt. When the reviewed paths are already
committed, the outcome is recorded as `already-committed` rather than as a
commit the verifier made. `VerificationResult.commit_note` always says what
happened, and the audit record carries it, so "nothing was committed" is never
unexplained.

After the fix, the same run commits only `app/routes/users.py` and leaves
`.env`, `.venv`, `operator-notes.md`, and the verifier's own config in the
working tree, where they belong.

**E.2 — The escalation path discarded its evidence (`engine.py`)**

On `RetryExhausted`, `engine.verify()` built a fresh `VerificationResult`
with no `check_results` and no `semantic_diff`. The SPEC promises escalation
*"with full context"*; the implementation escalated with a count. The failing
test output, the stack trace, and the semantic diff — the exact material a
human needs to fix the ticket — were dropped at the moment they became most
valuable, and the audit record for an escalation said `intent_coverage=no_diff`.

*Fix:* the last attempt's check results and semantic diff are carried into
the escalating result, and the audit record lists the failed checks
explicitly. The escalation now arrives with the evidence it exists to
deliver.

**Still open, unchanged:** the host-execution fallback in dry-run (§2.3.1), the
missing sandbox image (§2.3.2), the retry loop re-running the same checks
without a no-progress detector, the audit trail not being written to the real
EdgeRecord (§2.2).

---

## Appendix F — Defects confirmed and fixed in the audit trail

**F.1 — The content hash did not cover the evidence (§2.3.5)**

`VerificationAudit.compute_hash()` hashed
`work_package_id + decision + rationale + passed + committed + timestamp`. It
did not cover `key_evidence`, `attempts`, `repo_path`, the check results, or
the diff. The "content hash" attested to a sentence, not to the decision the
sentence described. Editing the evidence in a stored record left the hash
intact.

*Fix:* the hash covers the whole decision, serialised as canonical JSON with
recursively sorted keys and compact separators, hashed with SHA-256. The
covered fields are the work package, the repository, the decision, the
rationale, the key evidence, the attempt count, the timing, and the
timestamp.

**F.2 — Two definitions of the same hash (§2.3.5)**

`EdgeRecordLogger.log_decision()` recomputed the hash with a second copy of
the formula and **overwrote** the value the model's validator had computed.
Two definitions, kept in sync by hand.

*Fix:* one definition, in `el_verificador/canonical.py`. The model computes
the digest, the writer stores it, and the verifier recomputes it from the
same canonical form. A test asserts that the model and the writer agree.

**F.3 — The chain verified links but not contents (§2.3.5)**

`verify_chain()` compared `previous_audit_hash` against the previous
`content_hash` and stopped there. A removed record broke the chain; an
*edited* record did not.

*Fix:* `verify_chain()` now re-hashes every record from its stored columns
and compares it with the stored digest, so a record edited after the fact is
detected and named. `chain_status()` returns the reason, and the CLI prints
it.

**F.4 — The chain was ordered by timestamp (§2.3.5)**

Records were ordered by `timestamp`, so two decisions written in the same
millisecond could be verified in an order that was not the order they were
written in.

*Fix:* the chain follows insertion order, and a test covers two records with
the same timestamp.

**Still open, unchanged:** the missing sandbox image (§2.3.2), and the audit
trail still being written to a SQLite table this repository invented rather
than to the real EdgeRecord (§2.2). The local ledger is the tool's own; it is
not yet the system of record.

---

## Appendix H — The host is no longer a silent fallback for the sandbox

**H.1 — Dry-run executed untrusted code on the host (§2.3.1)**

`DockerRunner.run_check()` fell back to `subprocess.run(command, cwd=repo)`
whenever Docker was unavailable or `dry_run` was set. The repository under
test holds code the agent wrote, and running its test suite there executes
that code with the operator's credentials, in the operator's working tree,
with the operator's network. A `conftest.py` is arbitrary code, and pytest
imports it before the tests run. `dry_run` was documented as the mode the
README's own Quick Start used.

*Fix:* the host fallback is **opt-in**. `allow_host_execution` in the
configuration, or `--allow-host-execution` on the command line, is what
authorises it. Without it the check is refused with exit code `125` — the
conventional "could not be executed", distinct from a test failure — and the
message says whether Docker is unavailable or this is dry-run mode, and what
to do about it. The verification fails closed.

This is a guard, not the Phase 1 answer. Phase 1 replaces the fallback with a
real, digest-pinned sandbox and a real acceptance contract; this change makes
the hole impossible to fall into by accident in the meantime.

The refusal and the opt-in are covered by tests: a repository whose
`conftest.py` writes to the host leaves no trace when the check is refused,
and does leave one when the operator explicitly allows it.

**Still open, unchanged:** the missing sandbox image (§2.3.2) — the sandbox
still has to be built and published, and until it is, the checks run nowhere
rather than on the host — and the audit trail not being written to the real
EdgeRecord (§2.2).

---

## Appendix I — The verifier runs the contract the work actually specified

**I.1 — The acceptance contract was hardcoded (`acceptance.py`, `engine.py`)**

`VerificationConfig.test_commands` was the only contract. It is three Python
commands, which is the right default for a Python repository and the wrong
one for everything else: a Go project verified with `pytest -q` is not
verified at all, and the verdict is about the tool rather than about the
work. Nothing recorded which contract produced a decision either.

*Fix:* the work package may carry its own contract, and the verifier runs it:

- an explicit list, under `acceptance_commands`;
- a fenced code block inside the acceptance criteria, which is how a ticket
  written for a human usually states the commands to run.

When the work package declares no contract, the configured default is used.
`VerificationResult.acceptance_contract_source` records which one was used —
`work-package` or `config` — the commands are recorded with the result, and
the audit record carries both. The contract is bounded: at most 16 commands,
at most 500 characters each, and a criterion with no code block declares
nothing.

This is the "acceptance contract ingestion" half of Phase 1. The other half —
structured test results, per-check isolation, resource caps, and the evidence
store — is not in this change.

**I.2 — The Taskrivet adapter read a field that did not exist
(`taskrivet_adapter.py`)**

The adapter already tried to read `intent.acceptance_criteria` and
`intent.acceptance_commands`, defensively, because those fields were not on
the contract yet. With the contract now carrying them, the adapter joins the
criteria list rather than passing the list through as a string.

**Still open, unchanged:** the missing sandbox image (§2.3.2), structured
test results, per-check isolation, resource caps, and the evidence store
(Phase 1); the obligation model that replaces the coverage score (Phase 2);
and the audit trail not being written to the real EdgeRecord (§2.2).

---

## Appendix G — The retry loop no longer spends its budget on an unchanged tree

**G.1 — No progress detector (`correction.py`, `git_ops.py`, `engine.py`)**

The retry loop called `taskrivet_retry(prompt)` and then re-ran the *same*
checks, with nothing checking whether the agent had changed anything. An
agent that ignored the correction burned the whole retry budget in seconds,
and every one of those runs produced the same failure.

*Fix:* the repository is fingerprinted before each attempt. The fingerprint
covers the head commit, the set of changed paths, and how much each one
changed — so an edit that leaves the same file names but different contents
produces a different fingerprint. When two consecutive attempts see the same
fingerprint, the correction prompt was not acted on and another identical run
would fail identically, so the loop escalates immediately with that reason
instead of spending the rest of the budget.

The check is disabled when no probe is configured, and a probe that cannot
answer is not treated as evidence of no progress.

`VerificationResult.escalation_reason` now carries the reason, so it is
visible in the result and not only in the audit's rationale. The refreshed
capture shows the difference:

```json
"escalation_reason": "the repository did not change between attempt 1 and
attempt 2; the correction prompt was not acted on, so another identical run
would fail identically",
"attempts": [ { "attempt": 1, "decision": "fail", ... } ]
```

**Still open, unchanged:** the host-execution fallback in dry-run (§2.3.1), the
missing sandbox image (§2.3.2), and the audit trail not being written to the
real EdgeRecord (§2.2).
