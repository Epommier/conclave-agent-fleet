#!/usr/bin/env python3
"""PreToolUse hook that answers AskUserQuestion during an eval run.

Picks the "(recommended)" option (or the first one), logs each answer as a
JSON line, and fails closed: every error path exits 2 so the tool call is
blocked instead of going through unanswered.
"""
import datetime
import json
import os
import sys
import tempfile


def choose(question):
    labels = [o["label"] for o in question["options"]]
    recommended = [l for l in labels if "(recommended)" in l.lower()]
    if question.get("multiSelect"):
        return ", ".join(recommended) if recommended else labels[0]
    return recommended[0] if recommended else labels[0]


def main():
    if os.environ.get("CONCLAVE_EVAL") != "1":
        print("conclave eval approver invoked outside an eval run",
              file=sys.stderr)
        sys.exit(2)
    event = json.load(sys.stdin)
    if event.get("tool_name") != "AskUserQuestion":
        return
    tool_input = event["tool_input"]
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("no questions in tool_input")
    answers, lines = {}, []
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for q in questions:
        if not q.get("question") or not q.get("options"):
            raise ValueError("question without text or options")
        answer = choose(q)
        answers[q["question"]] = answer
        lines.append(json.dumps({
            "ts": now, "session_id": event.get("session_id"),
            "header": q.get("header"), "question": q["question"],
            "options": [o["label"] for o in q["options"]], "answer": answer,
        }))
    log = os.environ.get("CONCLAVE_APPROVER_LOG") or os.path.join(
        tempfile.gettempdir(), "conclave-approver.jsonl")
    with open(log, "a") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {**tool_input, "answers": answers},
    }}))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:
        print(f"conclave eval approver failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        sys.exit(2)
