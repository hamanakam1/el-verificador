# Changelog

All notable changes to El Verificador are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **An MCP surface.** `el-verificador/mcp` serves `verify_work_package`,
  `verification_status`, `verification_evidence` and `verify_receipt_chain`
  over stdio. The point is that agents stop reimplementing checks: Taskrivet
  wrote the code, and the tool that judges it runs in another process with
  another context. The `mcp` package is an optional dependency.
- **A content-addressed evidence store.** Evidence is written under the hash
  of its own bytes, so identical evidence lands in the same place rather than
  accumulating copies, nothing is ever overwritten, and every artifact can be
  re-hashed to prove it is what the decision was made from.
- **An EdgeRecord receipt chain.** The durable record is now a file-based,
  chained, canonical-hashed receipt rather than an invented SQLite table. The
  receipts conform to `factory-contracts/schemas/receipt.v1.json` and to the
  EdgeRecord receipt shape, and the canonical bytes are byte-for-byte
  identical to EdgeRecord's own `bin/era-fingerprint.py` — including the
  non-ASCII case that broke ERA-000314.
- **A verification sandbox image.** `docker/verification/Dockerfile` builds
  the image the acceptance checks run inside, with the toolchain baked in so
  the runtime container needs no network, a non-root uid, and memory, CPU and
  PID caps. A CI job builds it and runs the acceptance checks inside it.
- **The acceptance contract comes from the work package.** An explicit command
  list, or commands parsed out of the acceptance criteria, with the configured
  default as the fallback. The resolution records which source it used.
- **Ticket Board flagging on escalation.** The write is additive — existing
  tags and notes survive, the status is never changed — and it is never
  attributed to the operator. It is off by default.
- **A no-progress guard.** If the working tree did not change after a
  correction, another attempt cannot produce a different result, so the loop
  escalates instead of spending the remaining budget.
- **Weakened-test detection.** A deleted, skipped or de-asserted test is a
  violation. Adding `@pytest.mark.skip` to a test used to go unnoticed,
  because a symbol's source did not include its decorators.
- **`el-verificador metrics`.** Pass rate, escalation rate, inconclusive rate
  and the blocking obligations behind them, derived from the receipt chain.
- **A golden fixture suite** (`tests/fixtures/golden/`). Eight synthetic
  repositories with known verdicts, built into real git repositories and
  verified end to end. The diff engine had no test file at all, and the engine
  test replaced it with a mock.
- **Test modules for the diff engine, the obligations, the evidence store, the
  receipts, the git commit path, the ticket board, the acceptance contract and
  the MCP surface** — areas that had none.
- **CI gates for `ruff` and `mypy --strict`**, and dedicated jobs for the
  golden fixture suite and the sandbox smoke test.

- **Obligation-based intent alignment.** `el_verificador/obligations.py`
  resolves the distilled intent into checkable obligations — `change_present`,
  `acceptance_command`, `no_stub`, `tests_intact`, `scope_respected`,
  `symbol` and `goal_terms` — each resolving to a status backed by evidence.
  The verdict is a pure function of that list: any violation fails, any
  unproven claim means no verdict was reached, and only then does the run
  pass. The local model may explain the evidence; it never decides it.
- **A golden fixture suite** (`tests/fixtures/golden/`). Eight synthetic
  repositories with known verdicts, built into real git repositories and
  verified end to end. `correct_impl` passes; `todo_stub`, `wrong_impl`,
  `test_gaming`, `scope_creep`, `symbol_missing`, `empty_diff` and
  `broken_build` each fail, for the right reason. The diff engine had no test
  file at all, and the engine test replaced it with a mock.
- **Test modules for the diff engine, the obligations and the git commit
  path** — the three areas that had none.
- **A no-progress guard** in the correction loop. If the working tree did not
  change after a correction, another attempt cannot produce a different
  result, so the loop escalates instead of spending the remaining budget.
- **Weakened-test detection.** A deleted, skipped or de-asserted test is a
  violation. Adding `@pytest.mark.skip` to a test used to go unnoticed,
  because a symbol's source did not include its decorators.
- **A content-addressed check fixture** (`check_fixture`), so a dry run can
  replay recorded acceptance results instead of executing anything.
- **`scope_mode`** (`off` / `warn` / `enforce`) controls how a change outside
  the work package's `related_files` is treated. It defaults to `warn`, so the
  violation is reported and audited without blocking a pass until the
  `related_files` contract is trustworthy.
- **CI gates for `ruff` and `mypy --strict`**, and a dedicated job for the
  golden fixture suite.
- **Every verification decision is now recorded.** `VerifierDependencies.audit_logger`
  defaulted to `None` and nothing ever set it, so no shipped code path wrote
  an audit record at all. The ledger is now constructed by default, and a
  ledger that cannot be opened is reported on stderr rather than swallowed.
- **`el_verificador.audit` — independent verification of the audit ledger.**
  A chain proves the records were *written* consistently, not that they still
  *say* what they said: nothing recomputed a record's digest from the
  record's own content, so a record edited in place still verified. The new
  module rebuilds each audit from its stored row and recomputes the digest
  with the same canonical function the writer used, reading the ledger
  independently so a bug in the writer cannot hide from the check.
- **An end-to-end verification harness** (`tests/test_end_to_end.py`). The unit
  suite was green for four commits while the product could not complete a
  verification. The harness runs the real CLI, as a subprocess, against a real
  git repository, and asserts on the verdict it prints.
- **The settled operating decision is recorded** in `ROADMAP.md` §1.5, §1.6
  and §8.1: the least human intervention, the better. The gate is a hard
  gate, the machine retries before it escalates, and a human is a fallback
  rather than a step. The human-intervention budget makes that measurable.
- **The verification-run lifecycle diagram renders.** The specification
  targeted the Archify v1 layout while the installed renderer requires v3, so
  it could not be rendered at all. It is now valid v3, and the PNG, the
  interactive HTML and a delivery receipt are in `docs/archify/`.

### Changed

- **The receipt chain is ordered by a monotonic sequence, not by a
  timestamp.** `created_at` has second resolution, and a verifier that writes
  several receipts in the same second was being ordered by a random receipt
  id — which looks exactly like a broken chain.
- **The test suite no longer writes into the operator's real store.** Every
  test that builds a config points its evidence store and receipt chain at a
  temporary directory, so a test run leaves `~/.el-verificador/` exactly as
  it found it.
- **The goal-term matcher handles plurals**, and matches only the code the
  change produced rather than the code it replaced, so a correct
  implementation is not failed for sharing no vocabulary with its goal.
  `goal_terms_enforced: false` turns the check off without a code change.
- **The verdict is now a decision, not a score.** The old "intent coverage"
  number measured how many `related_files` were touched — it was identical
  (0.1) for a correct implementation and for a stub — and the configured 0.85
  threshold was never consulted. It is superseded by obligation coverage.
- **The Correction Prompt names the violated obligations**, and the locally
  generated analysis now reaches the prompt instead of being written to an
  attribute nothing read. The model still never decides a verdict.
- **The escalation carries its evidence.** The failing checks, the diff, the
  violated obligations and the evidence hashes travel with the escalation
  instead of being dropped at the moment they became most valuable.
- **The audit hash covers the evidence it attests to.** The content hash now
  covers the decision, the rationale, the evidence hashes and the obligation
  outcomes, rather than a single sentence of prose. The chain is ordered by a
  monotonic `seq` column and written inside one immediate transaction, so
  concurrent writers cannot fork it.
- **`~/.el-verificador/config.yaml` is read**, by default, and validated
  through the contract — an unknown key is an error rather than a silently
  ignored line. `--config` overrides it.
- **The CLI reports the obligations** it resolved, the evidence hashes it
  cited, and distinguishes *inconclusive* from *failed*. Exit codes: `0` pass,
  `1` fail or escalate, `2` inconclusive.
- **`SPEC.md` and `AGENTS.md` now describe what the code does.** Every
  mechanism carries a status marker, so the next agent cannot build on a
  capability that does not exist.

### Fixed

- **Acceptance commands could run on the host.** The dry-run path shelled out
  to the repository working directory, so the one component whose job is to
  run agent-generated code had a supported path that executed it on the
  operator's machine, with the operator's credentials, in the operator's real
  working tree. There is now no host-execution path in any mode: a check
  either runs inside the container or reports that it did not run, and a
  skipped check can never pass.
- **The commit staged every file in the working tree.** `git add -A` swept up
  changes this process did not make, and `git checkout` switched branches
  underneath the operator. The commit is now scoped by explicit path to the
  files the verifier evaluated, the branch is never switched, and a commit
  that reaches outside the scope fails loudly.
- **Every non-dry-run check raised `TypeError`.** `Container.exec_run` has no
  `timeout` parameter, so the acceptance pipeline had never actually run
  outside dry-run mode. The deadline is now enforced with a worker thread and
  an overrun is reported as exit `124`.
- **`MODIFIED` was never reported.** A symbol's "source" was a name-and-kind
  tag, so a rewritten function body compared equal to itself. A rewrite — the
  single most important change in the whole exercise — was invisible.
- **Stubs were found by grepping the file.** A symbol is now a stub when its
  body is structurally a placeholder, and stub findings are scoped to the
  diff, so a pre-existing stub the agent never touched is not its violation.
- **`VerificationConfig.__post_init__` was never called.** Pydantic v2 requires
  `model_post_init`, so the `~` in `edgerecord_db` was never expanded.
- **`datetime.utcnow()`** is deprecated and returned a naive datetime. The
  codebase now uses timezone-aware UTC throughout.
- **The escalation path carried no evidence.** The failing checks, the diff,
  the violated obligations and the evidence hashes now travel with the
  escalation instead of being dropped at the moment they became most
  valuable.
- **An inconclusive run was retried.** When the checks never executed there
  is nothing for the agent to fix, so the loop now stops and reports.
- **An inconclusive run was retried.** When the checks never executed there
  is nothing for the agent to fix, so the loop now stops and reports.
- **A missing sandbox image surfaced as a raw Docker 404.** `start()` called
  `client.containers.run()` unguarded. When the configured `docker_image`
  cannot be pulled, the SDK raises `docker.errors.ImageNotFound` and the whole
  verification dies with a raw HTTP traceback that says nothing about which
  image failed or what to do about it.
- **`DockerRunner` crashed on every non-dry-run verification.** The instance
  attribute `_docker_available` shadowed the method `_docker_available()`, so
  any call outside dry-run mode raised
  `TypeError: 'NoneType' object is not callable`. The cache attribute is now
  `_docker_ok`. Added `TestDockerRunnerLiveMode`, which exercises non-dry-run
  mode — the previous suite only covered `dry_run=True`, where the `or`
  short-circuit hid the defect.
- **`python -m el_verificador verify` crashed on every invocation.** The result
  was serialised with `model_dump(mode="json", indent=2)` and then passed to
  `json.dumps(..., indent=2)` as well.
- **Dry-run acceptance checks ran in the wrong directory.** `start()` returned
  a `ContainerHandle` without storing it, so `run_check()` fell back to
  `cwd=None`.
- **The semantic diff crashed on binary files.** Every changed file was read
  through `git show` with strict UTF-8 decoding, so a compiled bytecode file or
  an image in the diff raised `UnicodeDecodeError`. Only Python sources are
  read now, and decoding is lenient.
- **A missing working directory was reported as a missing command.** The
  dry-run path now distinguishes "working directory not found" from
  "command not found".
- **A missing sandbox image surfaced as a raw Docker 404.** `start()` now
  raises `SandboxUnavailableError` with an actionable message naming the
  image that could not be pulled, instead of letting the Docker SDK traceback
  escape. The test suite no longer depends on whether the host running it
  happens to have a Docker daemon.
- **The verifier judged two commits but committed the working tree.** Those two
  halves could not agree: if the agent's work was already committed the diff
  described it correctly and the commit step found nothing to stage; if it was
  left uncommitted the diff saw no change at all. The change under
  verification is now the difference between the base commit and the working
  tree — the state the verifier judges and the state it commits.
- **Untracked files were invisible to the whole verification.** `git diff`
  does not list them, so a brand new module was seen by no obligation, no
  symbol check and no scope check. They are now part of the change set.
- **The sandbox container exited before it could be used.** The image set
  `ENTRYPOINT ["sleep", "infinity"]` and the runner also passes the lifecycle
  command, so the container ran `sleep infinity sleep infinity`, rejected the
  second argument and exited. Every check then failed against a container
  that was no longer running, reported as a bare 409. The image uses a CMD
  now, and the runner reports an exited container with its status and exit
  code instead of a Docker SDK error.
- **An HTTP method in prose was treated as a symbol to build.** A goal that
  says "the POST /users/register endpoint" made the symbol obligation fail,
  blocking correct work for a reason that had nothing to do with the work.
  Protocol vocabulary is excluded from the symbol candidates.
- **The strict type gate could not pass.** The Docker SDK ships no `py.typed`
  and PyYAML had no stubs installed, so `mypy el_verificador` failed on seven
  findings that had nothing to do with the logic. Both are fixed; the gate is
  green.

### Added

- **Long-term build plan** — `ROADMAP.md`: a verified defect list, the
  target architecture, six phases with exit gates, the integration contracts
  El Verificador needs to author upstream, a risk register, and the decisions
  that require operator sign-off.
- **Screenshots** — `docs/screenshots/`, generated from real CLI runs by
  `scripts/capture_screenshots.py` and rasterised by
  `scripts/render_terminal.py`.
- **Demo repository** — `docs/examples/demo-project/` and
  `docs/examples/demo-base/`, the two-commit fixture the screenshots verify.
- README badges, a documentation index, and an honest status section.

### Known issues

These are documented in `ROADMAP.md` and deliberately **not** fixed in
this release, because fixing them changes what passes and what fails:

- `intent_coverage_threshold` is not enforced, and the intent-coverage score
  is structurally capped at `0.4` against a configured threshold of `0.85`.
- No EdgeRecord audit record is written by the shipped code paths — the audit
  logger is never constructed.
- `max_retry_duration_ms` is not enforced.
- Taskrivet dispatch, Ticket Board escalation, and a canonical EdgeRecord
  receipt do not exist yet.
