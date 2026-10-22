# Changelog

All notable changes to El Verificador are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

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
