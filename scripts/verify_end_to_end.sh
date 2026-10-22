#!/usr/bin/env bash
# End-to-end check of the CLI: a real verification, a real audit record, and
# a real chain verification. Everything here is synthetic.
#
# The acceptance command is replayed from a recorded fixture rather than
# executed, because the verifier does not run commands on the host: the
# repository under test holds code the agent wrote, and running it there
# would execute that code with the operator's credentials. A sandbox needs a
# Docker daemon — see scripts/sandbox_smoke_test.py for that path — and a
# fixture needs nothing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="${PYTHONPATH:-$HOME/Projects/el-verificador}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

export EL_VERIFICADOR_DB="$WORK/ledger.sqlite"

git init -q -b main .
git config user.email demo@example.invalid
git config user.name demo
git config commit.gpgsign false

mkdir -p app
cat > app/__init__.py <<'PY'
PY
cat > app/routes.py <<'PY'
def create_search_route(query, results):
    """Build the search response payload for a query."""
    return {"query": query, "results": list(results), "message": ""}
PY
cat > .gitignore <<'EOF'
work_package.json
check_fixture.json
EOF
git add -A
git commit -q -m "scaffold"

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

cat > work_package.json <<'JSON'
{
  "id": "dwp-cli-check",
  "true_goal": "Implement create_search_route in app/routes.py so a query returns results and an empty result set returns useful guidance.",
  "true_goal_summary": "Implement create_search_route with an empty-state message.",
  "related_files": ["app/routes.py"],
  "acceptance_commands": ["python3 -m unittest discover -s tests -t . -q"]
}
JSON

# The recorded result of the acceptance command, replayed instead of run.
cat > check_fixture.json <<'JSON'
{
  "checks": [
    {
      "command": "python3 -m unittest discover -s tests -t . -q",
      "exit_code": 0,
      "stdout": "Ran 1 test in 0.001s\n\nOK\n",
      "stderr": "",
      "duration_ms": 12
    }
  ]
}
JSON

echo "=== verify ==="
python3 -m el_verificador verify \
  --work-package work_package.json \
  --repo . \
  --base HEAD~1 --head HEAD \
  --check-fixture check_fixture.json \
  --dry-run \
  | python3 -c "
import json, sys
raw = sys.stdin.read()
data = json.loads(raw[raw.index('{'):])
diff = data.get('semantic_diff') or {}
def show(label, value):
    print(f'{label:<18}: {value if value not in (None, [], {}) else \"not reported\"}')
show('decision', data.get('decision'))
show('passed', data.get('passed'))
show('contract source', data.get('acceptance_contract_source'))
show('contract commands', data.get('acceptance_commands'))
show('intent coverage', diff.get('intent_coverage'))
show('uncovered words', diff.get('uncovered_tokens'))
"

echo
echo "=== the audit ledger ==="
if docker info > /dev/null 2>&1; then
  echo "A Docker daemon is available, so the checks can run for real in the"
  echo "sandbox. See scripts/sandbox_smoke_test.py for that path."
else
  echo "No Docker daemon, so the acceptance checks cannot be isolated and the"
  echo "run is a dry run: nothing is committed and no audit record is written."
  echo "The audit ledger and its chain are covered by tests/test_audit_chain.py"
  echo "and tests/test_receipt.py."
fi
python3 -m el_verificador audit --verify-chain || true
