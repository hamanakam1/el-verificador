#!/usr/bin/env python3
"""Capture the Taskrivet desktop screenshots used by this repository's README.

The capture is driven entirely by a synthetic ledger built in a temporary
directory, so no real task, no live terminal, and no private path is ever
shown. The renderer is photographed through the DevTools protocol, which does
not require operating-system screen-recording permission.

    python scripts/capture_taskrivet_screenshots.py --outdir docs/screenshots

Requires: macOS, Node with the Taskrivet desktop dependencies installed, and
a local Taskrivet checkout (default ~/Projects/taskrivet).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# Synthetic ledger content. Every string here is invented for the fixture.
TASKS = [
    (
        "search-empty-state",
        "Render useful guidance when a search returns no results",
        "Empty search shows guidance; the populated path is unchanged; a regression test covers both",
        "demo-repo/search",
        8000,
    ),
    (
        "rate-limit-backoff",
        "Back off cleanly when a provider returns a rate-limit response",
        "Retry delay doubles up to a ceiling; the attempt count is recorded on the session",
        "demo-repo/provider",
        12000,
    ),
    (
        "session-resume-recovery",
        "Recover a session after the local service restarts",
        "A crashed session is marked failed and can be reconciled by an operator",
        "demo-repo/sessions",
        6000,
    ),
    (
        "context-packet-trimming",
        "Keep a context packet inside its byte budget",
        "Low-priority content is dropped and the omission is reported in diagnostics",
        "demo-repo/context",
        5000,
    ),
    (
        "docs-readme-refresh",
        "Refresh the quick-start section so it matches the current CLI",
        "Every command in the section runs as written and the output is quoted correctly",
        "demo-repo/docs",
        4000,
    ),
    (
        "telemetry-lag-guard",
        "Bound the lag between a recorded checkpoint and the rendered dashboard",
        "A stale checkpoint is labelled as stale instead of being shown as current",
        "demo-repo/dashboard",
        7000,
    ),
    (
        "schema-drift-guard",
        "Detect a source schema that no longer matches the recorded contract",
        "A drifted schema raises a recorded failure with a stable fingerprint",
        "demo-repo/schema",
        6000,
    ),
]

ACCEPTED_TASK = "docs-readme-refresh"
REVIEW_TASK = "telemetry-lag-guard"
FAILED_TASK = "schema-drift-guard"


def run_taskrivet(taskrivet: Path, db: Path, *args: str) -> dict:
    env = dict(os.environ)
    env["TASKRIVET_DB"] = str(db)
    env["PYTHONPATH"] = str(taskrivet)
    result = subprocess.run(
        [sys.executable, "-m", "taskrivet", *args],
        cwd=str(taskrivet),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def build_ledger(taskrivet: Path, db: Path) -> None:
    """Create a synthetic ledger with one accepted, one in review, one failed task."""
    run_taskrivet(taskrivet, db, "init")
    for task_id, objective, acceptance, scope, budget in TASKS:
        run_taskrivet(
            taskrivet,
            db,
            "task-add",
            task_id,
            "--objective",
            objective,
            "--acceptance",
            acceptance,
            "--kind",
            "code",
            "--scope",
            scope,
            "--token-budget",
            str(budget),
        )
    run_taskrivet(
        taskrivet, db, "profile-add", "local-coder",
        "--kind", "code", "--context-limit", "16000",
        "--cost", "zero_incremental", "--ttl", "3600",
    )

    accepted = run_taskrivet(taskrivet, db, "start", ACCEPTED_TASK, "--owner", "terminal-1")
    run_taskrivet(
        taskrivet, db, "checkpoint", accepted["session_id"],
        "--summary", "Rewrote the quick-start block and ran every command in it",
        "--evidence", "Diff 4a91c2; 7 of 7 commands reproduced",
        "--next", "Operator review of the rendered section", "--tokens", "1180",
    )
    run_taskrivet(
        taskrivet, db, "finish", accepted["session_id"],
        "--evidence", "Reviewable diff 4a91c2 plus captured command output",
    )
    run_taskrivet(
        taskrivet, db, "accept", ACCEPTED_TASK,
        "--reason", "Operator read the diff and the reproduced command output",
    )

    review = run_taskrivet(taskrivet, db, "start", REVIEW_TASK, "--owner", "terminal-3")
    run_taskrivet(
        taskrivet, db, "checkpoint", review["session_id"],
        "--summary", "Added a staleness comparison to the dashboard renderer",
        "--evidence", "Diff 91bc0e; renderer test asserts the stale label",
        "--next", "Wire the comparison into the live view", "--tokens", "940",
    )
    run_taskrivet(
        taskrivet, db, "finish", review["session_id"],
        "--evidence", "Reviewable diff 91bc0e and the renderer test result",
    )

    failed = run_taskrivet(taskrivet, db, "start", FAILED_TASK, "--owner", "terminal-1")
    run_taskrivet(
        taskrivet, db, "checkpoint", failed["session_id"],
        "--summary", "Compared the live schema with the recorded contract",
        "--evidence", "Diff 3c77a1; 4 fields differ from the contract",
        "--next", "Record the drift as a failure", "--tokens", "610",
    )
    run_taskrivet(
        taskrivet, db, "fail", failed["session_id"],
        "--category", "tool_error",
        "--summary", "Schema comparison tool exited with a non-zero status",
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_targets(port: int, timeout: float = 60.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=5
            ) as response:
                targets = json.loads(response.read())
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                return pages
        except Exception:
            pass
        time.sleep(1)
    raise TimeoutError("the desktop did not expose a page target")


def capture(port: int, out: Path, script: str | None, width: int, height: int) -> None:
    """Take one DevTools-protocol screenshot of the desktop renderer."""
    pages = wait_for_targets(port)
    websocket_url = pages[0]["webSocketDebuggerUrl"]
    driver = Path(__file__).resolve().parent / "_cdp_capture.mjs"
    subprocess.run(
        [
            "node", str(driver), websocket_url, str(out), str(width), str(height),
            script or "",
        ],
        check=True,
    )


CDP_DRIVER = """// Minimal DevTools-protocol screenshot driver.
//   node _cdp_capture.mjs <websocketUrl> <out.png> <width> <height> [script]
import { writeFileSync } from "node:fs";

const [, , wsUrl, out, widthArg, heightArg, script] = process.argv;
const width = Number(widthArg || 1400);
const height = Number(heightArg || 920);

const ws = new WebSocket(wsUrl);
let id = 0;
const pending = new Map();

const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    const messageId = ++id;
    pending.set(messageId, { resolve, reject });
    ws.send(JSON.stringify({ id: messageId, method, params }));
  });

await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, { once: true });
  ws.addEventListener("error", reject, { once: true });
});

ws.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message));
    else resolve(message.result);
  }
});

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width, height, deviceScaleFactor: 2, mobile: false,
});

// Give the app time to read the ledger before interacting with it.
await new Promise((resolve) => setTimeout(resolve, 3000));

if (script) {
  const result = await send("Runtime.evaluate", {
    expression: script, awaitPromise: true, returnByValue: true,
  });
  if (result.exceptionDetails) {
    console.error("script failed: " + JSON.stringify(result.exceptionDetails.text));
    process.exit(1);
  }
  await new Promise((resolve) => setTimeout(resolve, 2000));
}

const { data } = await send("Page.captureScreenshot", { format: "png" });
writeFileSync(out, Buffer.from(data, "base64"));
ws.close();
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", default="docs/screenshots")
    parser.add_argument("--taskrivet", default=str(Path.home() / "Projects" / "taskrivet"))
    parser.add_argument("--width", type=int, default=1400)
    parser.add_argument("--height", type=int, default=920)
    args = parser.parse_args()

    taskrivet = Path(args.taskrivet).expanduser().resolve()
    if not (taskrivet / "taskrivet" / "cli.py").exists():
        print(f"no Taskrivet checkout at {taskrivet}", file=sys.stderr)
        return 1
    desktop = taskrivet / "desktop"
    if not (desktop / "node_modules" / "electron").exists():
        print("the Taskrivet desktop dependencies are not installed", file=sys.stderr)
        return 1

    outdir = Path(args.outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    driver = Path(__file__).resolve().parent / "_cdp_capture.mjs"
    driver.write_text(CDP_DRIVER)

    with tempfile.TemporaryDirectory(prefix="taskrivet-capture-") as workdir:
        work = Path(workdir)
        db = work / "ledger.sqlite"
        build_ledger(taskrivet, db)

        port = free_port()
        env = dict(os.environ)
        env["TASKRIVET_DB"] = str(db)
        env.pop("ELECTRON_RUN_AS_NODE", None)
        process = subprocess.Popen(
            [
                "npx", "electron", ".",
                f"--user-data-dir={work / 'electron-profile'}",
                f"--remote-debugging-port={port}",
            ],
            cwd=str(desktop),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            capture(port, outdir / "taskrivet-workspace.png", None, args.width, args.height)
            capture(
                port,
                outdir / "taskrivet-failure-ledger.png",
                "document.querySelector('#failures-nav').click()",
                args.width,
                args.height,
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
            driver.unlink(missing_ok=True)

    print(f"wrote {outdir / 'taskrivet-workspace.png'}")
    print(f"wrote {outdir / 'taskrivet-failure-ledger.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
