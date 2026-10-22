# Changelog

All notable changes to El Verificador are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **The acceptance contract can come from the work package.** The default
  contract is still three Python commands, but a work package may now declare
  its own — as an explicit `acceptance_commands` list, or as a fenced code
  block inside its acceptance criteria — and the verifier runs that instead.
  A Go project verified with `pytest -q` was not verified at all. Which
  contract was used is recorded with the result and in the audit record, so a
  decision can be checked against what it actually measured.

### Fixed

- **Acceptance checks fell back to running on the host.** When Docker was
  unavailable or `dry_run` was set, the checks ran with `subprocess.run` in
  the repository under test — code the agent wrote, with the operator's
  credentials, in the operator's working tree, with the operator's network. A
  `conftest.py` is arbitrary code, and pytest imports it before the tests
  run. The host fallback is now opt-in (`allow_host_execution`, or
  `--allow-host-execution`); without it the check is refused with exit code
  `125` and an explanation, and the verification fails closed.
- **The retry loop spent its whole budget on an unchanged repository.** After
  a failed attempt the loop handed the Correction Prompt back and re-ran the
  same checks, with nothing checking whether the agent had acted on it. An
  agent that ignored the prompt burned three attempts to learn nothing. The
  repository is now fingerprinted before each attempt — head commit, changed
  paths, and how much each changed — and the loop escalates immediately with
  the reason when two consecutive attempts see the same fingerprint.
  `VerificationResult.escalation_reason` carries the reason.
- **The audit hash did not cover the evidence it claimed to protect.** The
  content hash covered the work package, the decision, the rationale, two
  booleans, and the timestamp — not the key evidence, the attempt count, the
  repository, the check results, or the diff. Editing a stored record's
  evidence left its hash intact. The hash now covers the whole decision,
  serialised as canonical JSON and hashed with SHA-256.
- **The audit trail had two definitions of its own hash.** The writer
  recomputed the digest with a second copy of the formula and overwrote the
  value the model had computed. There is now one definition, shared by the
  model, the writer, and the verifier.
- **The chain verified links but not contents.** A removed record broke the
  chain; an edited one did not. `verify_chain()` now re-hashes every record
  from its stored columns and names the record that broke and why.
  `chain_status()` returns the reason, and the CLI prints it.
- **The chain was ordered by timestamp**, so two decisions written in the
  same millisecond could be verified out of order. It now follows insertion
  order.

### Fixed

- **The commit path staged the entire working tree.** `GitOps.commit()` ran
  `git add -A`, so a passing verification committed the operator's untracked
  files — reproduced on the demo repository, the resulting commit contained a
  `.env` secrets file, an entire virtualenv, and the operator's unrelated
  notes. The set of paths to stage is now an explicit argument and there is
  no blanket add. The commit is refused when the index already holds staged
  work the verification did not review, and when the working tree's copy of a
  reviewed file differs from the commit that was actually inspected.
  `VerificationResult.commit_note` records what happened, and the audit
  record carries it.
- **The escalation path discarded its evidence.** When the retry budget was
  exhausted, the result was rebuilt with no check results and no semantic
  diff, so the audit record for an escalation said `intent_coverage=no_diff`.
  The last attempt's check results and semantic diff are now carried into the
  escalating result, and the audit lists the failed checks explicitly.
- **The semantic diff could not see a rewritten function body.** Symbols were
  compared by a tag (`"FunctionDef:name"`) that was identical at both
  revisions, so `MODIFIED` was never emitted and a changed function looked
  untouched. Symbols now carry their real source segment, and they are matched
  by kind and qualified name so `A.render` and `B.render` are not confused.
- **The intent-coverage score measured the wrong thing.** It was computed
  against the symbols found in *stub* lines, so for clean code it collapsed to
  "how many declared files were touched" and scored a correct implementation
  and a stub identically. The score is now the weighted combination of the
  declared scope that was touched and the goal's checkable claims — the
  identifiers the goal names — compared token by token against symbol names
  and bodies. Prose is excluded from the denominator, because it cannot be
  checked against code. It is reported with its inputs: goal words, covered
  words, uncovered words, the identifiers the goal named, and the touched
  related files. The audit record carries the same evidence.
- **A change that ignored the goal entirely could still pass.** The
  acceptance commands can all exit zero while the agent edits something
  unrelated. If the goal names identifiers and the change provides none of
  them, verification now fails. The rule has no false positives: a change
  that contains none of the things the goal asked for is not an attempt at
  the goal.
- **`fail_on_todo_stubs` was never read.** The option was documented and
  referenced zero times in the engine. It is honoured now; the default stays
  `true`, so decisions are unchanged.
- **The model's Correction Prompt was generated and discarded.** The text was
  smuggled onto the contract with `object.__setattr__` and never read by
  `render()`. It is a real field now and appears in a clearly labelled
  *Advisor's Note*, after the deterministic evidence.
- **Stub detection matched prose.** `pass\s*$` matched "bypass", and `\.\.\.`
  matched an ellipsis in a docstring. A stub is now a function, method, or
  class whose whole body is `pass`, `...`, or `raise NotImplementedError`, and
  a TODO counts only when it is a real comment.
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
