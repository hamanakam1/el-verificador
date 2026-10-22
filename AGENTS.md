# El Verificador — agent instructions

El Verificador is the autonomous gatekeeper between "code written" (Taskrivet)
and "code committed" (EdgeRecord) in the Panther → El Destilador → Ticket
Board → El Hidratador → Taskrivet → El Verificador → EdgeRecord pipeline.

## Core responsibilities

1. **Independent acceptance testing** — run CI/CD (unit tests, linters,
   security scans) inside the Docker sandbox; only exit `0` counts.
2. **Intent alignment (obligations)** — resolve the distilled intent into
   checkable obligations, each with a status and evidence. Did the agent build
   the API route, or just write `// TODO: Implement route`?
3. **Guardrailed feedback loop** — on failure, capture the stack trace, the
   semantic diff, the failed test output and the violated obligations into a
   bounded "Correction Prompt" handed back to Taskrivet. Hard limit:
   `max_retries=3`, plus a no-progress guard. On exhaustion, escalate to a
   human **with the evidence attached**.
4. **State commit & teardown** — on pass, commit **only the verified files**,
   tear down Docker, and write the audit record.

## Conventions (match sibling repos el-destilador / el-hidratador)

- Python 3.11+, package `el_verificador`, entry point `el_verificador.__main__:main`
- pydantic v2 data contracts in `el_verificador/schemas.py` (`extra="forbid"`)
- Intent alignment in `el_verificador/obligations.py`; the decision function
  there is pure and has no model in its path
- Ollama (local Qwen) at `http://127.0.0.1:11434`; the model composes the
  analysis in a Correction Prompt and **never decides a verdict**
- Audit ledger at `~/.edgerecord/ledger.sqlite` (SQLite WAL) — a local ledger
  for now, not yet a canonical EdgeRecord receipt
- Config: `~/.el-verificador/config.yaml` (see `config.example.yaml`), read
  automatically and validated through the contract
- Lint: ruff (line-length 100), type: mypy strict, tests: pytest
- Local-first: no cloud dependencies required

## When editing

- Keep `max_retries=3` and the fail-safe escalation path intact.
- **Never let an acceptance command run on the host.** A check either runs in
  the container or reports that it did not run. If you add a path that executes
  a command, it must be inside the container.
- **Never commit with a blanket add.** The commit is scoped to the verified
  change set. Stage only the paths you own; never `git add -A` or `git add .`.
- Every verification decision must cite at least one evidence hash, and the
  audit record's content hash must cover that evidence — not just the prose.
- Never trust Taskrivet's self-reported status; verify independently.
- A model may *propose*; deterministic code *decides*. Do not put a model call
  in the decision path.
- Add tests under `tests/` for new behavior; run `pytest` before committing.
- Every rule the verifier enforces needs a test that fails when the rule is
  removed. The non-dry-run Docker path shipped broken for four commits
  because only the dry-run path was covered.
- `ruff check .` and `mypy el_verificador` must both be clean. They run in CI.
- **The golden fixture suite is the regression harness.** A change to the
  decision path without a fixture that proves the new verdict is not finished.
  Fixtures live in `tests/fixtures/golden/` and are built into real git
  repositories by `tests/test_golden_fixtures.py`.

## Screenshots

`docs/screenshots/` holds real captured CLI output, not mock-ups.

- `scripts/render_terminal.py` — replays ANSI terminal bytes into a PNG.
- `scripts/capture_screenshots.py` — builds the demo repository in
  `docs/examples/`, runs the actual CLI against it, and writes the PNGs.

Regenerate them when the CLI output shape changes:

```sh
python scripts/capture_screenshots.py
```

Requires `jq` on `PATH`. Creates an isolated toolchain with `ruff`, `bandit`
and `pytest` under `~/.cache/el-verificador/demo-venv`.

## Roadmap

`ROADMAP.md` is the long-term build plan. It carries a verified defect
list (D1–D26) with file references. Before changing behaviour that affects
what passes or fails, check the roadmap — several defects are recorded as
deliberately unfixed pending an operator decision.

