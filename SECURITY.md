# Security Boundaries

## Trust model

El Verificador is a trusted, single-user local verification tool, not an
authorization server or sandbox. Anyone who can use the EdgeRecord database can
perform verification decisions. The Python API and database should not be
exposed to untrusted clients, and the database should not be placed on a shared
host.

## EdgeRecord ledger

The ledger records verification decisions and evidence locally. It does not
redact secrets automatically. Store it outside public repositories; the default
is `~/.edgerecord/ledger.sqlite`. New storage directories use mode 0700 and the
database uses mode 0600. Existing directory permissions are not modified. Review
permissions when selecting a custom location. Use SQLite's backup API for
consistent live backups.

## Correction Prompts

Correction Prompts handed back to Taskrivet contain stack traces, failed test
output, and semantic diffs. Review them before forwarding to ensure no secrets
or private transcripts are included.

## Cryptographic guarantees

- Each EdgeRecord audit record is content-hashed with SHA256 for immutability.
- `previous_audit_hash` chains records for tamper evidence.
- `EdgeRecordLogger.verify_chain()` validates the full hash chain.

These protect the integrity of the audit trail, not the confidentiality of its
contents.

## Reporting

Report sensitive vulnerabilities through GitHub private vulnerability reporting
if enabled, rather than placing secrets or working exploits in public issues.
If that channel is unavailable, open a minimal issue requesting a private
contact channel.
