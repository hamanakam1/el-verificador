# Screenshot sources

Every image in this directory is a capture of a real run, not a mock-up. This
file records what produced each one so they can be regenerated and checked.

| File | What it shows | How it was produced |
|------|----------------|--------------------|
| `taskrivet-workspace.png` | The Taskrivet desktop workspace, with a synthetic ledger behind it | `scripts/capture_taskrivet_screenshots.py` |
| `taskrivet-failure-ledger.png` | The Taskrivet failure ledger, with a synthetic ledger behind it | `scripts/capture_taskrivet_screenshots.py` |

The other images in this directory capture El Verificador itself and are
produced by `scripts/capture_screenshots.py`.

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
