#!/usr/bin/env bash
# End-to-end check of the CLI: a real verification, a real audit record, and
# a real chain verification. Everything here is synthetic.
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
  "acceptance_commands": ["python3 -c \"print('acceptance check ran')\""]
}
JSON

echo "=== verify ==="
python3 -m el_verificador verify \
  --work-package work_package.json \
  --repo . \
  --base HEAD~1 --head HEAD \
  --allow-host-execution \
  | python3 -c "
import json, sys
raw = sys.stdin.read()
data = json.loads(raw[raw.index('{'):])
print('decision          :', data['decision'])
print('passed            :', data['passed'])
print('contract source   :', data['acceptance_contract_source'])
print('contract commands :', data['acceptance_commands'])
print('intent coverage   :', data['semantic_diff']['intent_coverage'])
print('uncovered words   :', data['semantic_diff']['uncovered_tokens'])
"

echo
echo "=== audit chain ==="
python3 -m el_verificador audit --verify-chain

echo
echo "=== the record that was written ==="
python3 - <<'PY'
import json, os, sqlite3
from pathlib import Path
db = Path(os.environ["EL_VERIFICADOR_DB"])
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM verification_audits").fetchall()
print("records written:", len(rows))
for row in rows:
    print("  decision  :", row["decision"])
    print("  passed    :", bool(row["passed"]))
    print("  hash      :", row["content_hash"][:16])
    for item in json.loads(row["key_evidence"]):
        print("    -", item)
PY
