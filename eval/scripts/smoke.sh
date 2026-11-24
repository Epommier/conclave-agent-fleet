#!/usr/bin/env bash
# Smoke test: one unattended /conclave:implement run on a throwaway repo, with
# eval/hooks/approver.py answering AskUserQuestion through a PreToolUse hook.
# Why the flags (measured on Claude Code 2.1.327):
# - A hook alone is not enough: -p only offers AskUserQuestion when a permission host
#   channel is attached (--input-format stream-json --permission-prompt-tool stdio).
# - With that channel the hook answers, and --permission-mode dontAsk denies anything
#   off the allow list, so nothing waits on the absent host after stdin closes.
# - Any folder named .claude is write-protected in unattended runs; no allow rule,
#   --add-dir or permitting hook helps, so eval/hooks/redirect_plans.py rewrites
#   ~/.claude/plans/<name>.md into $RUN_DIR/plans.
# SPENDS REAL MONEY: up to about $BUDGET dollars (default 3) of API usage.
# Run: eval/scripts/smoke.sh   (env overrides: MODEL=sonnet BUDGET=3)
set -euo pipefail

MODEL="${MODEL:-sonnet}"
BUDGET="${BUDGET:-3}"
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="$(mktemp -d)"
PLANS="$HOME/.claude/plans"
echo "RUN_DIR=$RUN_DIR"

mkdir "$RUN_DIR/repo"
mkdir -m 700 "$RUN_DIR/plans"
cd "$RUN_DIR/repo"
git init -q
cat > greet.py <<'EOF'
def greet(name):
    return "Hello " + name


if __name__ == "__main__":
    print(greet("Ada"))
EOF
git config user.name "eval"
git config user.email "eval@example.invalid"
git add greet.py
git commit -qm "initial greet"
BASE="$(git rev-parse HEAD)"

ls -1 "$PLANS" 2>/dev/null | sort > "$RUN_DIR/plans_before.txt" || true

SETTINGS="$(python3 - "$RUN_DIR" "$PLUGIN_DIR" <<'EOF'
import json, sys
run_dir, plugin_dir = sys.argv[1:]
cmd = (f"CONCLAVE_EVAL=1 CONCLAVE_APPROVER_LOG='{run_dir}/approver.jsonl' "
       f"python3 '{plugin_dir}/eval/hooks/approver.py'")
redirect = (f"CONCLAVE_EVAL=1 CONCLAVE_PLANS_DIR='{run_dir}/plans' "
            f"CONCLAVE_REDIRECT_LOG='{run_dir}/redirect.jsonl' "
            f"python3 -I '{plugin_dir}/eval/hooks/redirect_plans.py'")
print(json.dumps({"hooks": {"PreToolUse": [{"matcher": "AskUserQuestion",
      "hooks": [{"type": "command", "command": cmd, "timeout": 30}]},
      {"matcher": "Write|Edit|Read",
      "hooks": [{"type": "command", "command": redirect, "timeout": 10}]}]}}))
EOF
)"

PROMPT="/conclave:implement In greet.py, change greet() so that it returns the greeting with a comma and an exclamation mark, for example 'Hello, Ada!'. Keep the change to that one function; no tests or other files are needed."
MSG="$(python3 -c 'import json, sys; print(json.dumps({"type": "user", "message": {"role": "user", "content": sys.argv[1]}}))' "$PROMPT")"

set +e
printf '%s\n' "$MSG" | timeout 1500 claude -p \
  --input-format stream-json --permission-prompt-tool stdio \
  --plugin-dir "$PLUGIN_DIR" --settings "$SETTINGS" --model "$MODEL" \
  --permission-mode dontAsk \
  --allowedTools "Read,Glob,Grep,Edit,Write,Agent,AskUserQuestion,Skill,TodoWrite,Bash(git status *),Bash(git diff *),Bash(git log *)" \
  --max-budget-usd "$BUDGET" \
  --output-format stream-json --verbose \
  > "$RUN_DIR/transcript.jsonl" 2> "$RUN_DIR/stderr.txt"
CLAUDE_EXIT=${PIPESTATUS[1]}
set -e
echo "claude exit code: $CLAUDE_EXIT"

echo "--- final result line"
RUN_OK=0
python3 - "$RUN_DIR/transcript.jsonl" <<'EOF' || RUN_OK=1
import json, sys
result = None
for line in open(sys.argv[1]):
    try:
        d = json.loads(line)
    except ValueError:
        continue
    if isinstance(d, dict) and d.get("type") == "result":
        result = d
if result is None:
    print("no result line")
    sys.exit(1)
for k in ("total_cost_usd", "is_error", "subtype", "num_turns"):
    if k in result:
        print(f"{k}: {json.dumps(result[k])}")
print("permission_denials:")
for d in result.get("permission_denials") or []:
    inp = d.get("tool_input") or {}
    print(f"  {d.get('tool_name')} {inp.get('file_path') or inp.get('command') or ''}")
sys.exit(1 if result.get("is_error") else 0)
EOF

echo "--- approver log"
cat "$RUN_DIR/approver.jsonl" 2>/dev/null || echo "(no approver log)"

echo "--- redirect log"
cat "$RUN_DIR/redirect.jsonl" 2>/dev/null || echo "(no redirect log)"

echo "--- new files in $PLANS"
ls -1 "$PLANS" 2>/dev/null | sort | comm -13 "$RUN_DIR/plans_before.txt" - || true

echo "--- checks"
FAILED=0
check() { if [ "$2" = 0 ]; then echo "PASS $1"; else echo "FAIL $1"; FAILED=1; fi; }

GATE=0
if [ ! -f "$RUN_DIR/approver.jsonl" ]; then
  echo "(no approver log)"
  GATE=1
else
  python3 - "$RUN_DIR/approver.jsonl" <<'EOF' || GATE=1
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
sys.exit(0 if any(r.get("header") == "Gate" and str(r.get("answer", "")).startswith("Go")
                  for r in rows) else 1)
EOF
fi
check gate_answered "$GATE"

EDITED=1
if ! git diff --quiet HEAD -- greet.py || ! git diff --quiet "$BASE" HEAD -- greet.py; then
  EDITED=0
fi
check file_edited "$EDITED"

PLAN_SAVED=1
for f in "$RUN_DIR/plans"/*.md; do
  if [ -f "$f" ] && [ ! -L "$f" ]; then PLAN_SAVED=0; fi
done
check plan_saved "$PLAN_SAVED"
check run_ok "$RUN_OK"

exit "$FAILED"
