# Security Boundaries

## Trust model

El Verificador is a local verification tool that runs **agent-generated code**.
It is a trusted process that hosts untrusted work, and its own isolation is
the thing that keeps a bad change from reaching the operator's machine.

The Python API, the MCP server and the record directories should not be exposed
to untrusted clients, and the record directories should not be placed on a
shared host.

## The sandbox

Acceptance commands run inside a Docker container. That is the whole point of
the component, and the boundaries are not negotiable:

- **No acceptance command ever runs on the host**, in any mode, including dry
  run. A check either runs in the container, is replayed from a recorded
  fixture, or reports that it did not run. A check that did not run can never
  pass, so a dry run can never manufacture a verdict.
- A result must carry an explicit integer exit status. Missing, boolean,
  fractional, or string statuses cannot stand in for exit `0`. Invalid or
  duplicate replay records are skipped and cannot satisfy acceptance.
- The container runs as a non-root uid, with the network disabled by default and
  with memory, CPU and PID caps.
- Each check has a wall-clock limit. An actual runner timeout retains exit
  `124`, invalidates the sandbox immediately, and attempts to kill and inspect
  that container within a five-second cleanup window. A successful kill call
  alone is not proof: fresh `Running: false` state or Docker `NotFound` is
  required to confirm termination. Remaining checks are marked skipped.
- A timed-out verification escalates with its check results, lifecycle evidence,
  and receipt. It does not compute a semantic diff, request a correction, or
  commit, even when termination is confirmed. A command that exits `124` itself
  without overrunning the runner deadline remains an ordinary failed check.
- An unavailable daemon or an unconfirmed stop can leave the sandbox running.
  The runner retains its identity and refuses a new start until teardown can
  confirm termination. Final teardown may retry cleanup; a late worker result
  cannot replace the recorded timeout. Only a new explicit verification may
  provision a fresh sandbox after confirmed cleanup.
- The sandbox image (`docker/verification/Dockerfile`) carries the acceptance
  toolchain, so nothing has to be fetched while untrusted code is on the
  machine. A project-specific image is the operator's business.

The `dry_run` mode exists so the verifier can be exercised without a Docker
daemon. It is not a way to run the checks on the host.

## The commit path

The verifier commits code, so its staging is a data-loss risk in a shared
checkout:

- The commit is scoped by explicit path to the files the verifier evaluated. A
  blanket `git add -A` is never used.
- The branch is never switched. A commit against a branch other than the
  current one is refused rather than forced.
- A commit that reaches outside the verified change set fails loudly instead of
  being recorded as a success.

## The record

Two things are written, and they are not the same thing:

- **The receipt chain** (`~/.el-verificador/receipts/`) — the durable record.
  Append-only JSON receipts, chained to the previous one, fingerprinted with
  EdgeRecord's canonical rule so anyone with the bytes can recompute them.
- **The ledger** (`~/.edgerecord/ledger.sqlite`) — a derived query cache over
  the receipts. Rebuildable, never authoritative.

On POSIX local filesystems, `ReceiptChain` serializes cooperating writers with
an exclusive `flock` from predecessor validation through sequence assignment,
fingerprinting and atomic publication. Readers hold a shared lock while loading
and validating a consistent snapshot. The persistent `.receipt-chain.lock` file
must not be removed or replaced while the store is in use. Creating it requires
write access to the directory; once it exists, readers can open it read-only.
Lock acquisition failures refuse the operation instead of continuing unlocked.

These advisory locks do not protect against programs that edit receipts directly,
and network filesystems and non-POSIX platforms are not supported for this lock
protocol. Calls wait while another process holds an incompatible lock; the
operating system releases it when that process exits. Atomic publication and
locking do not add a power-loss durability guarantee, and an abrupt exit before
publication can leave an ignored temporary file.

The evidence store (`~/.el-verificador/evidence/`) is content-addressed: the
path is the hash, so nothing is ever overwritten and every artifact can be
re-hashed to prove it is what the decision was made from.

Neither the record nor the evidence store redacts secrets automatically. Store
them outside public repositories. Review permissions when selecting a custom
location.

## Correction Prompts

Correction Prompts handed back to Taskrivet contain stack traces, failed test
output, semantic diffs and a locally generated analysis. They may therefore
contain secrets that appear in the code under test. Review them before
forwarding to an untrusted recipient.

A Correction Prompt never invites the agent to change the gate: not the
configuration, not the check fixture, not the acceptance commands, not the
thresholds. The rendered prompt says so explicitly, and the prompt composer's
instructions forbid suggesting otherwise.

## Ticket Board writes

Escalation can flag a ticket on the local dashboard. The write is deliberately
narrow:

- It is **additive**. The ticket's existing tags survive, and its notes are
  read and preserved rather than replaced.
- It never changes the ticket's `status` or `priority`, because a status
  change can imply that work may proceed.
- It is never attributed to the operator. The dashboard's own route hardcodes
  `actor="operator"`, and its code carries a warning about an untagged
  coordination post landing on a ticket as `sender="Mike", role="human"` — data
  nobody owned. This component names itself.
- It is **off by default** (`ticket_board_enabled: false`). Writing to
  somebody else's board is the operator's decision.

## Cryptographic guarantees

- Each receipt is content-hashed with SHA256 over a canonical body that
  includes the decision, the rationale, the evidence hashes and the obligation
  outcomes.
- `previous_fingerprint` chains receipts for tamper evidence.
- The canonical bytes are byte-for-byte identical to EdgeRecord's own
  `bin/era-fingerprint.py`, including the non-ASCII case that broke
  ERA-000314.
- `ReceiptChain.verify_chain()` validates every link and every fingerprint.
- Unreadable or malformed receipts invalidate the chain; appending to a corrupt
  chain is refused without changing existing records. Evidence retrieval
  verifies that its bytes still match the requested content hash.
- A receipt ID must be a safe filename component. Publication refuses an
  existing receipt atomically, including a destination created concurrently.
  Distinct concurrent appends are not serialized yet; a resulting chain fork
  is detected on read and requires operator investigation.

These protect the integrity of the record, not the confidentiality of its
contents.

## Reporting

Report sensitive vulnerabilities through GitHub private vulnerability reporting
if enabled, rather than placing secrets or working exploits in public issues.
If that channel is unavailable, open a minimal issue requesting a private
contact channel.
