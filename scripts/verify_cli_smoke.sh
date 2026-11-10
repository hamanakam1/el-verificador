#!/usr/bin/env bash
# End-to-end check of the CLI against a synthetic repository.
#
# The repository is built here, so its test code is this script's own and
# running it on the host is safe. The verifier's default is to refuse that:
# acceptance commands come from the work package and normally run inside the
# Docker sandbox, because a repository under test holds code the agent wrote,
# and running it with the operator's credentials is code execution.
# `--allow-host-execution` is the documented opt-in for a repository you
# trust, which this one is by construction. With a Docker daemon available
# the same commands run in the sandbox instead, and no opt-in is needed.
#
# The run is synthetic throughout: no real repository, no real task, no
# private path, and no network.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="${PYTHONPATH:-$ROOT/..}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

export EL_VERIFICADOR_DB="$WORK/ledger.sqlite"

git init -q -b main .
git config user.email demo@example.invalid
git config user.name demo
git config commit.gpgsign false

mkdir -p app tests
: > app/__init__.py
# `unittest discover -s tests -t .` imports the start directory, so it has to
# be a package. Without this the acceptance command fails to even collect.
: > tests/__init__.py

cat > app/routes.py <<'PY'
def create_search_route(query, results):
    """Build the search response payload for a query."""
    return {"query": query, "results": list(results), "message": ""}
PY

cat > tests/test_routes.py <<'PY'
import unittest

from app.routes import create_search_route


class SearchRouteTests(unittest.TestCase):
    def test_populated_results_are_returned(self):
        payload = create_search_route("q", ["a"])
        self.assertEqual(payload["results"], ["a"])


if __name__ == "__main__":
    unittest.main()
PY

cat > .gitignore <<'EOF'
work_package.json
EOF

git add -A
git commit -q -m "scaffold"

# The implementation: the empty result set now returns guidance.
cat > app/routes.py <<'PY'
def create_search_route(query, results):
    """Build the search response payload for a query."""
    trimmed = query.strip()
    if not trimmed:
        return {"query": "", "results": [], "message": "Type a search term to begin."}
    if not results:
        return {"query": trimmed, "results": [], "message": "No matches. Try a broader term."}
    return {"query": trimmed, "results": list(results), "message": ""}
PY

git add -A
git commit -q -m "implement the route"

# The acceptance contract travels with the work package, so the verifier runs
# the command the work actually specified rather than a hardcoded list.
cat > work_package.json <<'JSON'
{
  "id": "dwp-cli-check",
  "true_goal": "Implement create_search_route in app/routes.py so a query returns results and an empty result set returns useful guidance.",
  "true_goal_summary": "Implement create_search_route with an empty-state message.",
  "related_files": ["app/routes.py", "tests/test_routes.py"],
  "acceptance_commands": ["python3 -m unittest discover -s tests -t . -q"]
}
JSON

echo "=== verify ==="
set +e
python3 -m el_verificador verify \
  --work-package work_package.json \
  --repo . \
  --base HEAD~1 --head HEAD \
  --dry-run \
  --allow-host-execution \
  > verify.json
status=$?
set -e

python3 - "$status" <<'PY'
import json
import sys

status = int(sys.argv[1])
raw = open("verify.json", encoding="utf-8").read()
data = json.loads(raw[raw.index("{"):])
diff = data.get("semantic_diff") or {}


def show(label, value):
    print(f"{label:<18}: {value if value not in (None, [], {}) else 'not reported'}")


show("decision", data.get("decision"))
show("passed", data.get("passed"))
show("contract source", data.get("acceptance_contract_source"))
show("contract commands", data.get("acceptance_commands"))
show("intent coverage", diff.get("intent_coverage"))
show("uncovered words", diff.get("uncovered_tokens"))
show("files changed", diff.get("files_changed"))

checks = data.get("check_results") or []
for check in checks:
    print(f"check             : {check.get('command')} -> exit {check.get('exit_code')}")

problems = []
if status != 0:
    problems.append(f"the CLI exited {status}")
if data.get("decision") != "pass":
    problems.append(f"decision is {data.get('decision')!r}, expected 'pass'")
if data.get("acceptance_contract_source") != "work-package":
    problems.append("the acceptance contract did not come from the work package")
if not checks or any(c.get("exit_code") != 0 for c in checks):
    problems.append("an acceptance command did not succeed")

if problems:
    print()
    for problem in problems:
        print(f"FAIL: {problem}")
    raise SystemExit(1)
print()
print("OK: the CLI verified the work package end to end.")
PY

echo
echo "=== the audit ledger ==="
# A dry run writes no audit record: the decision that outlives a run is the
# one that was written down, and a dry run writes nothing down. The ledger
# and its chain are covered by tests/test_audit_chain.py. This check confirms
# the ledger is reachable and its chain is intact.
python3 -m el_verificador audit --verify-chain
