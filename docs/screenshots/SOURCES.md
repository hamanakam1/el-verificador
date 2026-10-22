# Screenshot sources

Every image in this directory is a capture of a real run, not a mock-up. This
file records what produced each one so they can be regenerated and checked.

| File | What it shows | How it was produced |
|------|----------------|--------------------|
| `taskrivet-workspace.png` | The Taskrivet desktop workspace, with a synthetic ledger behind it | `scripts/capture_taskrivet_screenshots.py` |
| `taskrivet-failure-ledger.png` | The Taskrivet failure ledger, with a synthetic ledger behind it | `scripts/capture_taskrivet_screenshots.py` |
| `01-verify-pass.png` | El Verificador verifying a passing work package: three acceptance checks green, intent diff computed, code committed | `scripts/capture_screenshots.py` |
| `02-correction-prompt.png` | The bounded Correction Prompt handed back to Taskrivet — the real failing assertion, the real lint error, the real semantic diff | `scripts/capture_screenshots.py` |
| `03-escalate.png` | The retry guardrail: three attempts, all failed, `decision: escalate` | `scripts/capture_screenshots.py` |
| `04-audit-chain.png` | `audit --verify-chain` over a populated ledger | `scripts/capture_screenshots.py` |
| `05-audit-trail.png` | The audit hash chain: each record's `previous_audit_hash` pointing at its predecessor's `content_hash` | `scripts/capture_screenshots.py` |
| `06-config-init.png` | `config --init` writing the configuration file | `scripts/capture_screenshots.py` |

## The El Verificador images

These capture El Verificador itself. The capture script builds the two-commit
demo repository in `docs/examples/`, runs the actual `el_verificador` CLI
against it, and replays the bytes it received into a terminal window via
`scripts/render_terminal.py`.

Nothing is mocked. The acceptance commands in the demo (`pytest`, `ruff`,
`bandit`) really run; the failing scenario really breaks a test; the audit
chain really is a SQLite hash chain.

Regenerate with:

```sh
python scripts/capture_screenshots.py
```

The script needs `jq` on `PATH` and creates an isolated toolchain with `ruff`,
`bandit` and `pytest` under `~/.cache/el-verificador/demo-venv`.

Two things the El Verificador captures make visible that prose does not: the
intent-coverage score reads `0.30` against a configured threshold of `0.85`
while the run still passes, and the three-attempt escalation is real. See
`ROADMAP.md` Appendix C.

## The Taskrivet images

El Verificador sits downstream of
[Taskrivet](https://github.com/hamanakam1/taskrivet): Taskrivet executes the
work, El Verificador decides whether that work is real. The Taskrivet desktop
is therefore part of this repository's story — it is the thing being gated.

Taskrivet is a local application, so photographing it without publishing
anything private needs care. The capture script:

1. builds a **synthetic** Taskrivet ledger in a temporary directory using the
   Taskrivet CLI (`task-add`, `profile-add`, `start`, `checkpoint`, `finish`,
   `accept`, `fail`);
2. launches the desktop against that ledger with a temporary Electron
   user-data directory, so the real ledger, the real window state, and any
   other running instance are untouched;
3. captures the renderer through the DevTools protocol, which needs no
   operating-system screen-recording permission.

Every task title, objective, acceptance criterion, failure summary, and
fingerprint in the images comes from that synthetic ledger. No real ticket
data, no private installation path, and no live terminal output is shown. The
terminal panel is captured in its empty state for the same reason.

Regenerate with:

```sh
python scripts/capture_taskrivet_screenshots.py --outdir docs/screenshots
```

The script needs a local Taskrivet checkout. Point it at one with
`--taskrivet /path/to/taskrivet` (default: `~/Projects/taskrivet`), Node with
the Taskrivet desktop dependencies installed, and a macOS host for the desktop
build.
