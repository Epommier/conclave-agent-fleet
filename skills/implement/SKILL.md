---
name: implement
description: Run a task end-to-end with the plan-first methodology - understand, explore, ask only the questions the repo cannot answer, plan, loop through the approval gate until approved, then implement and verify under CLAUDE-ORCHESTRATION.md. Use only when the user invokes /conclave:implement.
disable-model-invocation: true
argument-hint: <task text | file path | URL>
---

# implement

Plan-first, always, whatever the task size.

**Step 0:** read `${CLAUDE_PLUGIN_ROOT}/guidance/CLAUDE-ORCHESTRATION.md` in full; it is not in context by default.
It governs everything below. This skill only forces the `explore_then_plan` shape and adds
the question, plan-file, and approval protocol.

**Gate rule:** gates come after Phase 1, after Phase 2, and at Phase 5 (plan approval, which is
also the launch gate). A gate is one `AskUserQuestion`, header `Gate`, question naming the
phase just finished and the next one. Exactly two options: `Go` first, label ending in
"(Recommended)", description one line saying what the next phase does; `Stop here` second,
description "end the turn; the plan file keeps the state; resume with /conclave:implement <plan path>".
`Go` → continue in the same turn. `Stop here` → end the turn; the plan file is the resumable
state. A free-text answer (the tool's built-in `Other`) is a redirection: main applies it (edits
the plan file, or redoes the affected phase), shows only the delta (the full plan at Phase 5),
and asks the same gate again. Never re-ask on unchanged content. Phase 3 is its own question batch and continues into
Phase 4 once answered. Phase 6 runs through to Phase 7 with no gate. Never skip a phase.

**Role rule:** the main session plans, briefs, triages, integrates and verifies. It never reads
repository files itself (discovery goes through `scout`) and never edits them itself
(implementation goes through `mech-executor`, `executor` or `security-executor`). Main reads
only scout reports, agent results, the plan file, and files the user pasted; main writes only
the plan file and memory; main issues no mutating Bash.
`scout` for discovery, main-session Plan synthesis, `plan-verifier` for readiness.

## Input

`$ARGUMENTS` is the task: empty → ask for it and end turn; existing path → read it; a plan
file with a `## Status` section resumes at its first pending step, re-asking the approval gate
first if the plan was never approved; URL → fetch it (`gh` for GitHub); otherwise literal text.

## Phase 1 - Understand

Restate outcome, acceptance, non-goals in your own words. List information gaps. Ask nothing yet.
End with the gate.

## Phase 2 - Explore

Exploration is one or more `scout` calls with disjoint read scopes (repo,
docs, specs, tests; memory is already in context). Main issues no Read, Glob, Grep or Bash on
repository paths. Close every gap from the scout reports; a report that leaves a gap gets a
follow-up message to that scout, not a direct read. Declare main-owned versus scout-owned
scopes before launching, launch independent scouts in one message, collect every report
before comparing across scopes. End with the gate.

## Phase 3 - Questions

Ask only what the repo, docs, specs, and memory cannot answer AND whose answer changes the
plan. One batch per turn via `AskUserQuestion`, as many as
the gap needs, each with a recommended default. No gaps → say so and continue. Otherwise the
batch's answers open Phase 4 in the same turn.

## Phase 4 - Plan

Write `~/.claude/plans/<slug>.md` (never in the repo unless asked; main picks the slug from the
task, kebab-case, at most 5 words) with: Task, Outcome, Scope,
Non-goals, Assumptions, Steps with a verify check each, Acceptance, Rollback. Every step
heading reads `### Step N — <title> (<role>, owns: <file list>, after: <Step M, ...|none>)`.
`owns` is mandatory on every code-writing step (its brief creates or edits files; verify-only,
report-only and user-action steps are not code-writing); `owns`/`after` feed
`dependency-analyzer` in Phase 6. A new gap or incoherence found while planning → one more
Phase 3 round before presenting.

## Phase 5 - Approval

ALWAYS show the full plan before the approval gate: the plan path, then the plan file's entire
content in normal output (not a summary), then the approval gate (`AskUserQuestion`, as in the
Gate rule). Never ask the gate on a plan the user has not just been shown. `Go` is the approval
and the launch. Free text → revise the file, show the full revised plan again with the changes
called out, re-ask. Never re-ask an unchanged plan. Never brief an executor before `Go`.

## Phase 6 - Implement

Count code-writing steps. 3 or more: one `dependency-analyzer` call with the plan path, paste
its JSON under `## Execution order` in the plan file, then launch wave by wave: every step of a
wave in one message, each brief naming its exclusive `owns:` set, `isolation: "worktree"` only
for waves flagged `worktree` in a git repo; integrate and verify each wave before the next.
Main may narrow the analyzer's schedule (merge waves, make a step sequential), never widen it.
1–2 code-writing steps: schedule inline from `owns`/`after`, independent steps in one message.

Follow the approved plan. Before any tool call in this phase, state which agent will make the
change; a Write, Edit, NotebookEdit or mutating Bash issued by main is a protocol failure.

Each plan step is one brief to an executor, per CLAUDE-ORCHESTRATION.md routing:
`mech-executor` for fully specified, same-shape work; `executor` for bounded judgment;
`security-executor` for anything touching PII, auth, secrets, crypto, validation or hardening.
A brief carries goal, exact files owned (exclusive), constraints, done criteria, the verify
command from the plan step, and relevant paths and rationale. Main collects each
result, runs the plan step's verify check, triages exceptions, and marks step status in the
plan file. After two failures on a brief, escalate the role or split the step, never take
the work over.

Verification follows the orchestration file: risk-triggered work gets a fresh `verifier`
against the exact acceptance claim before the report. A scope change reopens Phase 5.

## Phase 7 - Report

One table: step → evidence. Then short lists: deferred with rationale, not done, external
actions not taken.
