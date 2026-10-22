# Contributing to El Verificador

Thank you for contributing to El Verificador — the autonomous gatekeeper for
the Taskrivet pipeline.

## Conventions (match sibling repos el-destilador / el-hidratador)

- Python 3.11+, package `el_verificador`, entry point `el_verificador.__main__:main`
- pydantic v2 data contracts in `el_verificador/schemas.py` (`extra="forbid"`)
- Ollama (local Qwen) at `http://127.0.0.1:11434`, model `qwen2.5:14b`
- EdgeRecord audit ledger at `~/.edgerecord/ledger.sqlite` (SQLite WAL)
- Config: `~/.el-verificador/config.yaml` (see `config.example.yaml`)
- Lint: ruff (line-length 100), type: mypy strict, tests: pytest
- Local-first: no cloud dependencies required; Docker optional (dry-run mode)

## Development

```sh
# Clone
git clone https://github.com/hamanakam1/el-verificador.git
cd el-verificador

# Install in editable mode
python -m pip install -e .

# Create config (optional, for non-dry-run)
python -m el_verificador config --init

# Run tests
python -m unittest discover -s tests -v

# Verify a work package offline (no Docker/git/EdgeRecord writes)
python -m el_verificador verify \
  --work-package path/to/work_package.json \
  --repo /path/to/repo \
  --base HEAD~1 --head HEAD --dry-run
```

## Before you open a PR

1. Run the test suite: `python -m unittest discover -s tests -v`
2. Add tests under `tests/` for any new behavior
3. Keep `max_retries=3` and the fail-safe escalation path intact
4. Every verification decision must produce an EdgeRecord audit record with a
   SHA256 content hash and hash chain (`previous_audit_hash`)
5. Never trust Taskrivet's self-reported status; verify independently
6. Stage only the paths you own; never run `git add -A` or `git add .`

## Security

Do not commit provider credentials, private transcripts, or real ticket data.
Keep secrets out of EdgeRecord audit payloads and Correction Prompts.

See [SECURITY.md](SECURITY.md).

## License

MIT licensed.
