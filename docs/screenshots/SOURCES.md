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
| `04-audit-chain.png` | `audit --verify-chain` over a populated record: the receipt chain and the ledger cache | `scripts/capture_screenshots.py` |
| `05-audit-trail.png` | The audit trail for one work package, with each record's content hash | `scripts/capture_screenshots.py` |
| `06-config-init.png` | `config --init` writing the configuration file | `scripts/capture_screenshots.py` |

## The El Verificador images

These capture El Verificador itself. The capture script builds the two-commit
demo repository in `docs/examples/`, runs the actual `el_verificador` CLI
against it, and replays the bytes it received into a terminal window via
`scripts/render_terminal.py`.

Nothing is mocked. The demo repository is real, the failing scenario really
breaks a test, the acceptance commands really run, and the record really is a
hash chain.

**How the acceptance results are obtained.** El Verificador never runs an
acceptance command on the host — that is the one thing it must never do, and
the capture host may not have a Docker daemon. So the capture script runs the
acceptance commands itself, with the same toolchain the sandbox image carries
(`pytest`, `ruff`, `bandit`), and records their real output into
`checks.json`. The CLI replays that recording. The CLI's own output — the
verdict, the obligations, the analysis, the prompt — is real; only the
command's *result* is a recording.

`scripts/sandbox_smoke_test.py` runs the same acceptance commands against a
real container, and CI runs it on every push. The container path and the
recording path exercise the same decision code.

Regenerate with:

```sh
python scripts/capture_screenshots.py
```

The script needs `jq` on `PATH` and creates an isolated toolchain with `ruff`,
`bandit` and `pytest` under `~/.cache/el-verificador/demo-venv`.

The captures make visible what prose does not: the passing run's obligation
coverage reads `1.0` against a configured threshold of `0.85`, the failing run
names the exact assertions and the unused import that caused it, and the
three-attempt escalation is real. See `ROADMAP.md` Appendix C.

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
