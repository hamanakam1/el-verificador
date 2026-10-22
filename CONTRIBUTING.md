# Contributing to El Verificador

Thank you for contributing to El Verificador — the autonomous gatekeeper for
the Taskrivet pipeline.

## Conventions (match sibling repos el-destilador / el-hidratador)

- Python 3.11+, package `el_verificador`, entry point `el_verificador.__main__:main`
- pydantic v2 data contracts in `el_verificador/schemas.py` (`extra="forbid"`)
- Intent alignment in `el_verificador/obligations.py`; the decision function
  there is pure and has no model in its path
- Ollama (local Qwen) at `http://127.0.0.1:11434`; the model composes the
  analysis in a Correction Prompt and **never decides a verdict**
- Durable record: EdgeRecord receipts at `~/.el-verificador/receipts/`. The
  ledger at `~/.edgerecord/ledger.sqlite` is a derived query cache over them.
- Evidence store: content-addressed, at `~/.el-verificador/evidence/`
- Config: `~/.el-verificador/config.yaml` (see `config.example.yaml`), read
  automatically and validated through the contract
- Lint: ruff (line-length 100), type: mypy strict, tests: pytest
- Local-first: no cloud dependencies required

## Development

```sh
# Clone
git clone https://github.com/hamanakam1/el-verificador.git
cd el-verificador

# Install in editable mode
python -m pip install -e '.[dev]'

# Build the verification sandbox image
./scripts/build_sandbox_image.sh

# Create config (optional, for non-dry-run)
python -m el_verificador config --init

# Run the tests
python -m pytest
python -m unittest discover -s tests -v

# Lint and type-check (both run in CI)
ruff check .
mypy el_verificador

# Verify a work package offline (no writes, no execution)
python -m el_verificador verify \
  --work-package path/to/work_package.json \
  --repo /path/to/repo \
  --base HEAD~1 --head HEAD --dry-run

# Run the acceptance checks inside a real container
python scripts/sandbox_smoke_test.py

# Regenerate the README screenshots from real CLI runs
python scripts/capture_screenshots.py
```

## The golden fixture suite

`tests/fixtures/golden/` holds synthetic repositories with **known verdicts**.
Each is built into a real git repository and verified end to end through the
real diff engine, the real obligation evaluator and the real decision function.

A fixture is a directory with a `base/` tree, a `head/` tree, a
`work_package.json` and an `expected.json` that names the expected decision
**and** the expected violated obligations:

```json
{
  "decision": "fail",
  "violations": ["no_stub"]
}
```

**A change to the decision path is not finished without a fixture that proves
the new verdict.** The diff engine had no test file at all for four commits and
the engine test replaced it with a mock, which is how a diff engine that could
not see a rewritten function body shipped green.

Adding a fixture:

1. Create `tests/fixtures/golden/<name>/` with `base/`, `head/`,
   `work_package.json` and `expected.json`.
2. Run `python -m pytest tests/test_golden_fixtures.py -v`. Every fixture must
   reach its own verdict, for the right reason.
3. The harness is hermetic: it writes nothing into `~/.el-verificador/`.

## Screenshots

`docs/screenshots/` holds real captured CLI output, rendered to PNG by
`scripts/render_terminal.py` and captured by `scripts/capture_screenshots.py`.
The capture script builds the two-commit demo repository in `docs/examples/`,
runs the actual CLI against it, and replays the bytes it received.

Regenerate them whenever the CLI output shape changes, and review the diff:
a stale screenshot is worse than no screenshot.

```sh
python scripts/capture_screenshots.py            # writes docs/screenshots/*.png
python scripts/render_terminal.py capture.txt -o out.png --title "demo"
```

The capture script needs `jq` on `PATH` and creates an isolated toolchain
(with `ruff`, `bandit` and `pytest`) under `~/.cache/el-verificador/demo-venv`.
It runs the acceptance commands itself and records their output, because the
capture host may not have a Docker daemon and the verifier never runs a
command on the host.

## Before you open a PR

1. Run the gates: `python -m pytest`, `ruff check .`, `mypy el_verificador`
2. Add a golden fixture for any change to what passes or fails
3. Add tests under `tests/` for any new behaviour
4. Keep `max_retries=3` and the fail-safe escalation path intact
5. Every verification decision must cite at least one evidence hash, and the
   receipt's content hash must cover that evidence
6. Never trust Taskrivet's self-reported status; verify independently
7. Never let an acceptance command run on the host
8. Stage only the paths you own; never run `git add -A` or `git add .`

## Security

Do not commit provider credentials, private transcripts, or real ticket data.
Keep secrets out of receipts, the evidence store and Correction Prompts.

See [SECURITY.md](SECURITY.md).

## License

MIT licensed.
