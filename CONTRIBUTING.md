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

# Regenerate the README screenshots from real CLI runs
python scripts/capture_screenshots.py
```

## Taskrivet integration

El Verificador can record its verification outcomes in Taskrivet's own
coordination ledger, so a Correction Prompt reaches the system that can act on
it rather than stopping at a printout.

```sh
python -m el_verificador verify \
  --work-package path/to/work_package.json \
  --repo /path/to/repo \
  --base HEAD~1 --head HEAD \
  --taskrivet-db ~/.taskrivet/ledger.sqlite \
  --taskrivet-task my-task-id \
  --taskrivet-scope repo/api
```

The integration is inert unless **both** `--taskrivet-db` and
`--taskrivet-task` are supplied.

`el_verificador/taskrivet_adapter.py` drives Taskrivet's JSON CLI instead of
reimplementing its rules. Two things to keep in mind when editing it:

- **Read the work-package schema defensively.** `IntentPackage` and
  `CorrectionPrompt` evolve, and the adapter has to work with both the older
  and the newer shape. A field accessed directly (`prompt.violations`,
  `intent.acceptance_criteria`) raises `AttributeError` against the other
  shape, which turns a schema change into a red build. Use `getattr` with a
  fallback, and let the tests build prompts through the schema-aware helper.
- **A failure must have an owning session.** If no session is open, open one
  rather than dropping the failure record. A failure that is never written
  leaves the task looking like it is still running.

The integration test drives a real Taskrivet ledger through the whole
lifecycle, so it is skipped when no Taskrivet checkout is present.

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

## Before you open a PR

1. Run the test suite: `python -m unittest discover -s tests -v`
2. Add tests under `tests/` for any new behavior
3. Keep `max_retries=3` and the fail-safe escalation path intact
4. Every verification decision must produce an EdgeRecord audit record with a
   SHA256 content hash and hash chain (`previous_audit_hash`)
5. Never trust Taskrivet's self-reported status; verify independently
6. Stage only the paths you own; never run `git add -A` or `git add .`
7. When you change a shared file that another branch is also editing, stage
   only your own hunks (`git apply --cached` with a hand-built patch, or
   `git hash-object` + `git update-index --cacheinfo`). Do not commit another
   contributor's in-flight work, and do not stash, reset, or clean a shared
   checkout to get your own change in.
8. Test against the **committed** schema, not only the working tree. CI
   builds the committed state, so a change that only works against uncommitted
   work fails there. A throwaway worktree at `HEAD` reproduces it:

   ```sh
   git worktree add --detach /tmp/verify-ci HEAD
   cp <your changed files> /tmp/verify-ci/...
   (cd /tmp/verify-ci && python -m unittest discover -s tests -v)
   git worktree remove /tmp/verify-ci --force
   ```

## Security

Do not commit provider credentials, private transcripts, or real ticket data.
Keep secrets out of EdgeRecord audit payloads and Correction Prompts.

See [SECURITY.md](SECURITY.md).

## License

MIT licensed.
