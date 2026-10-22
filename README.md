# El Verificador

```text
EEEEE  L        V   V  EEEEE  RRRR   IIIII  FFFFF  IIIII   CCCC   AAA   DDDD    OOO   RRRR
E      L        V   V  E      R   R    I    F        I    C      A   A  D   D  O   O  R   R
E      L        V   V  E      RRRR     I    F        I    C      AAAAA  D   D  O   O  RRRR
EEE    L        V   V  EEE    R  R     I    FFF      I    C      A   A  D   D  O   O  R  R
E      L         V V   E      R   R    I    F        I    C      A   A  D   D  O   O  R   R
E      L         V V   E      R   R    I    F        I    C      A   A  D   D  O   O  R   R
EEEEE  LLLLL      V    EEEEE  R   R  IIIII  F      IIIII   CCCC  A   A  DDDD    OOO   R   R

  >_ Code written is not code committed.
```

[![Tests](https://github.com/hamanakam1/el-verificador/actions/workflows/tests.yml/badge.svg)](https://github.com/hamanakam1/el-verificador/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Runtime dependencies: 4](https://img.shields.io/badge/runtime%20dependencies-4-blueviolet)](pyproject.toml)
[![Releases](https://img.shields.io/github/v/release/hamanakam1/el-verificador)](https://github.com/hamanakam1/el-verificador/releases)
[![Repo size](https://img.shields.io/github/repo-size/hamanakam1/el-verificador)](https://github.com/hamanakam1/el-verificador)
[![Language](https://img.shields.io/github/languages/top/hamanakam1/el-verificador)](https://github.com/hamanakam1/el-verificador)
[![Last commit](https://img.shields.io/github/last-commit/hamanakam1/el-verificador/commits/main)](https://github.com/hamanakam1/el-verificador/commits/main)
[![Stars](https://img.shields.io/github/stars/hamanakam1/el-verificador?style=social)](https://github.com/hamanakam1/el-verificador/stargazers)
[![Forks](https://img.shields.io/github/forks/hamanakam1/el-verificador?style=social)](https://github.com/hamanakam1/el-verificador/network/members)
[![Issues](https://img.shields.io/github/issues/hamanakam1/el-verificador)](https://github.com/hamanakam1/el-verificador/issues)
[![Contributors](https://img.shields.io/github/contributors/hamanakam1/el-verificador/graphs/contributors)](https://github.com/hamanakam1/el-verificador/graphs/contributors)
[![Commit activity](https://img.shields.io/github/commit-activity/m/hamanakam1/el-verificador)](https://github.com/hamanakam1/el-verificador/commits/main)

**The building inspector. Independent intent verification and guardrailed feedback routing for the Taskrivet pipeline.**

> *Taskrivet cannot grade its own homework. El Verificador is the gate between "code written" and "code committed".*

---

## Purpose

El Verificador is the autonomous gatekeeper that closes the loop in the Panther → El Destilador → Ticket Board → El Hidratador → Taskrivet → **El Verificador** → EdgeRecord pipeline. It independently audits Taskrivet's work before the Docker sandbox is torn down and code is merged.

## Core Responsibilities

### 1. Independent Acceptance Testing
Taskrivet cannot grade its own homework. El Verificador runs the CI/CD pipeline inside the hydrated Docker container. It executes unit tests, linters, and security scans. It only cares if the container exits with `0` and the tests pass.

### 2. Intent Alignment (Obligations)
Passing tests aren't enough — the code must solve the user's original problem. El
Verificador resolves the distilled intent into **obligations**: individually
checkable claims, each with a status and the evidence behind it. The verdict is a
pure function of those obligations.

| Obligation | What it catches |
|---|---|
| `change_present` | The work package produced no change at all |
| `acceptance_command` | An acceptance command failed — or never ran |
| `no_stub` | The agent wrote `pass` / `...` / `raise NotImplementedError` where the work belonged |
| `tests_intact` | A test was deleted, skipped, or de-asserted to force a pass |
| `scope_respected` | The agent rewrote files the work package never asked about |
| `symbol` | A symbol the goal named was never defined |
| `goal_terms` | The change shares no vocabulary with the distilled goal |

*Did the agent actually build the API route, or did it just write
`// TODO: Implement route`?* — a structurally stub body is now a violation, and
the obligation that fails names the symbol.

### 3. The Guardrailed Feedback Loop
If Taskrivet breaks the build, El Verificador does not blindly say "try again". It acts as a precise debugger:

- Captures the exact stack trace, the specific semantic diff, the failed test output, and the list of violated obligations
- Packages this into a highly bounded, strict "Correction Prompt"
- Hands the prompt back to Taskrivet for a retry
- Stops early if the working tree did not change: an agent that ignored the correction gets an escalation, not the rest of the budget
- **Crucially:** Enforces a strict iteration limit (`max_retries=3`). If Taskrivet fails three times, the loop is broken, the container is paused, and the work is flagged for a human

### 4. State Commit & Teardown
When the code passes both machine tests and the intent check, El Verificador executes the final logistical steps:

- Commits **only the files it verified** — the commit is scoped by path, never a blanket `git add -A`, and the branch is never switched underneath the operator
- Tears down the Docker containers and releases compute resources
- Writes an audit record whose hash covers the evidence it attests to, chained to the previous record

**No acceptance command ever runs on the host.** A check either runs inside the
container or reports that it did not run, and a check that did not run can never
pass. This component's job is to run agent-generated code; running it on the
operator's machine would be the one thing it must never do.

## The Complete Autonomous Cycle

1. **Panther:** Captures raw multimodal signals
2. **El Destilador:** Distills noise into a true intent Work Package
3. **Ticket Board:** Human roadmap and durable backlog triage
4. **El Hidratador:** Provisions the local Docker sandbox and DAG
5. **Taskrivet:** Executes the code inside the sandbox
6. **El Verificador:** Audits the execution, manages the retry loop, and commits the state to EdgeRecord

## Screenshots

Every image below is a **real captured CLI run**, replayed into a terminal
window — not a mock-up. The demo repository is a two-commit miniature of the
kind of project Taskrivet would be asked to modify, built and verified for
real. Reproduce the whole set with:

```bash
python scripts/capture_screenshots.py
```

How each image was produced is recorded in
[docs/screenshots/SOURCES.md](docs/screenshots/SOURCES.md).

### A passing verification

Acceptance checks ran, the intent diff was computed, the code was committed:

![El Verificador verifying a passing work package](docs/screenshots/01-verify-pass.png)

### A bounded Correction Prompt, handed back to Taskrivet

The exact failing assertion, the lint error, and the semantic diff — bounded,
and addressed to the agent that has to fix it:

![El Verificador producing a Correction Prompt for Taskrivet](docs/screenshots/02-correction-prompt.png)

### The guardrail: three attempts, then escalation

The retry budget is hard. On exhaustion the loop breaks and the work is flagged
for a human instead of being retried forever:

![El Verificador escalating after the retry budget is exhausted](docs/screenshots/03-escalate.png)

### A tamper-evident audit trail

Each decision is content-hashed and chained to the previous one, so the record
of *what was decided and why* cannot be quietly rewritten:

![El Verificador audit chain verification](docs/screenshots/04-audit-chain.png)

![El Verificador audit trail for one work package](docs/screenshots/05-audit-trail.png)

## Design Principles

- **Independent by construction** — Never trusts Taskrivet's self-reported status
- **Evidence-first** — Every decision requires concrete proof (test results, AST diff, stack traces) and cites the hash of the evidence it used
- **Deterministic verdicts** — The decision is a pure function of the resolved obligations. A model may explain the evidence; it never decides
- **Never executes untrusted code on the host** — Checks run in the container or not at all
- **Guardrailed** — Hard iteration limits, plus a no-progress guard, prevent infinite token-burning loops
- **Fail-safe** — On exhaustion, escalate to humans with full context
- **Auditable** — Every decision is content-hashed and chained, and the hash covers the evidence
- **Local-first** — Runs on your mesh network, no cloud dependencies required

## Status

**Alpha — the core loop runs, the judgement is real, the pipeline is not yet connected.**

133 tests green, `ruff` clean, `mypy --strict` clean, and both gates now run in
CI. The verification cycle runs end-to-end against real git repositories.

**What works today**

- Independent acceptance testing inside the container, with per-check timeouts
- Intent alignment through obligations, resolved deterministically
- An AST diff that sees rewritten function bodies, finds structural stubs, and
  detects weakened tests
- A bounded correction loop with a no-progress guard, whose prompt names the
  violated obligations
- A commit scoped to the verified change set
- A content-hashed, chained audit record whose hash covers the evidence

**The regression harness is a golden fixture suite.** Eight synthetic
repositories with known verdicts, verified end to end through real git:

| Fixture | Expected |
|---|---|
| `correct_impl` | pass |
| `todo_stub` | fail — `no_stub` |
| `wrong_impl` | fail — `symbol`, `goal_terms_present` |
| `test_gaming` | fail — `tests_intact` |
| `scope_creep` | fail — `scope_respected` |
| `symbol_missing` | fail — `symbol_present` |
| `empty_diff` | fail — `change_present` |
| `broken_build` | fail — `acceptance_command_0` |

**Known gaps, all tracked in [ROADMAP.md](ROADMAP.md):**

- The `taskrivet/sandbox:latest` image still does not exist, and no component
  provisions it. The sandbox image is the next thing to build.
- The audit record is written to a local SQLite ledger
  (`~/.edgerecord/ledger.sqlite`), not to a canonical EdgeRecord receipt. The
  receipt contract and its canonical hashing are specified in the roadmap.
- `scope_respected` defaults to `warn`, not `enforce`: a file outside the work
  package's `related_files` is reported and audited, but does not yet block a
  pass. Set `scope_mode: "enforce"` once the `related_files` contract is
  trustworthy.
- Ticket Board escalation is not wired up. `ESCALATE` is a return value.
- Multi-language support is Python-only. The diff engine is written against
  Python's `ast`; the roadmap specifies tree-sitter.

## What El Verificador gates

El Verificador sits downstream of
[Taskrivet](https://github.com/hamanakam1/taskrivet). Taskrivet does the work;
El Verificador decides whether that work is real.

![Taskrivet workspace](docs/screenshots/taskrivet-workspace.png)

*Taskrivet's workspace view. Every task carries an objective, acceptance
criteria, a resource scope, and a token budget. The queue shows `READY`,
`RUNNING`, `REVIEW`, `BLOCKED`, and `ACCEPTED` states, and "Accepted outcomes"
counts only work an operator explicitly accepted — activity is not completion.
The ledger behind this screenshot is a synthetic fixture.*

![Taskrivet failure ledger](docs/screenshots/taskrivet-failure-ledger.png)

*Failures stay in the record across retries. Each one carries a stable
fingerprint, so a recurring failure groups instead of quietly disappearing.
This is the evidence El Verificador's correction loop is built to answer.*

Screenshot provenance is recorded in
[docs/screenshots/SOURCES.md](docs/screenshots/SOURCES.md).

## Quick Start

```bash
# 1. Create config (read automatically from ~/.el-verificador/config.yaml)
python -m el_verificador config --init
python -m el_verificador config --show

# 2. Verify a DistilledWorkPackage
#
#    --dry-run writes nothing AND executes nothing. The acceptance commands
#    are not run on your machine; the verdict is INCONCLUSIVE unless a
#    recorded check fixture is supplied.
python -m el_verificador verify \
  --work-package path/to/work_package.json \
  --repo /path/to/repo \
  --base HEAD~1 \
  --head HEAD \
  --dry-run

# 3. Check the audit trail
python -m el_verificador audit --verify-chain
```

Exit codes: `0` pass, `1` fail or escalate, `2` inconclusive (no verdict was
reached — nothing ran).

## Verification Cycle

| Stage | What happens |
|-------|--------------|
| Acceptance testing | Unit tests, lint, security scans run inside the sandbox container, each under its own timeout |
| Intent alignment | The diff is resolved into obligations, each with a status and evidence |
| Correction loop | Failed runs produce a bounded Correction Prompt naming the violated obligations (`max_retries=3`) |
| No-progress guard | An agent that changed nothing escalates immediately instead of spending the rest of the budget |
| Exhaustion | The loop breaks and the work is flagged for a human, **with the evidence that caused it** |
| Pass | Only the verified files are committed, the sandbox is torn down, and the audit record is written |

## Documentation

| Document | What it covers |
|----------|----------------|
| [ROADMAP.md](ROADMAP.md) | The long-term build plan: verified defect list, target architecture, six phases with exit gates, integration contracts, risk register, and the decisions that need an operator |
| [SPEC.md](SPEC.md) | The system specification |
| [AGENTS.md](AGENTS.md) | Contributor conventions for agents and humans |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute |
| [SECURITY.md](SECURITY.md) | Security posture and reporting |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [docs/screenshots/](docs/screenshots/) | The captured runs shown above |
| [docs/screenshots/SOURCES.md](docs/screenshots/SOURCES.md) | How each screenshot was produced, and how to regenerate it |
| [docs/examples/](docs/examples/) | The demo repository the CLI screenshots verify |
