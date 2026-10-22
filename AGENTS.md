# El Verificador — agent instructions

El Verificador is the autonomous gatekeeper between "code written" (Taskrivet)
and "code committed" (EdgeRecord) in the Panther → El Destilador → Ticket
Board → El Hidratador → Taskrivet → El Verificador → EdgeRecord pipeline.

## Core responsibilities (from README)

1. **Independent acceptance testing** — run CI/CD (unit tests, linters,
   security scans) inside the hydrated Docker sandbox; only exit `0` counts.
2. **Intent alignment (semantic diff)** — compare actual AST/git diffs against
   the `DistilledWorkPackage` from El Destilador. Did the agent build the API
   route, or just write `// TODO: Implement route`?
3. **Guardrailed feedback loop** — on failure, capture the stack trace,
   semantic diff, and failed test output into a bounded "Correction Prompt"
   handed back to Taskrivet. Hard limit: `max_retries=3`. On exhaustion,
   pause the container and flag the ticket for human intervention.
4. **State commit & teardown** — on pass, commit code, tear down Docker, and
   fire a cryptographically secure payload to EdgeRecord.

## Conventions (match sibling repos el-destilador / el-hidratador)

- Python 3.11+, package `el_verificador`, entry point `el_verificador.__main__:main`
- pydantic v2 data contracts in `el_verificador/schemas.py` (`extra="forbid"`)
- Ollama (local Qwen) at `http://127.0.0.1:11434`, model `qwen2.5:14b`
- EdgeRecord audit ledger at `~/.edgerecord/ledger.sqlite` (SQLite WAL)
- Config: `~/.el-verificador/config.yaml` (see `config.example.yaml`)
- Lint: ruff (line-length 100), type: mypy strict, tests: pytest
- Local-first: no cloud dependencies required; Docker optional (dry-run mode)

## When editing

- Keep `max_retries=3` and the fail-safe escalation path intact.
- Every verification decision must produce an EdgeRecord audit record with a
  SHA256 content hash and hash chain (`previous_audit_hash`).
- Never trust Taskrivet's self-reported status; verify independently.
- Add tests under `tests/` for new behavior; run `pytest` before committing.
