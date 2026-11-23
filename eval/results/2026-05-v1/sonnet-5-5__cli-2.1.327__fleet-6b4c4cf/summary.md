# Comparison summary, task list `2026-05-v1`

- Model: `claude-sonnet-5-5`
- CLI version: `2.1.327`
- Fleet version: `6b4c4cf`
- Date: 2026-11-23
- Listed tasks: 8
- Complete rounds: 3 (1, 2, 3)
- Records dropped by validation: 0
- Spend, all given jobs: 14.79 dollars
- Spend, complete rounds: 14.79 dollars
- Spend counts each trial at its cost, and a trial without a cost or without a record at its arm's CLI cap plus 1.00 (plain 2.00, fleet 3.50).

## Resolve rate per arm

Approver errors (fleet trials whose question calls differ from their approver lines) are left out of the rates.

| Arm | Trials counted | Resolved | Resolve rate | Approver errors left out |
|---|---|---|---|---|
| plain | 24 | 2 | 0.083 | 0 |
| fleet | 24 | 4 | 0.167 | 0 |

## Per task

| Task | Plain resolved / trials | Fleet resolved / trials | Plain fraction | Fleet fraction | Difference (fleet minus plain) |
|---|---|---|---|---|---|
| `cs-si__eodag-2176` | 0 / 3 | 0 / 3 | 0.000 | 0.000 | 0.000 |
| `itsdnns__docsight-437` | 0 / 3 | 0 / 3 | 0.000 | 0.000 | 0.000 |
| `nesquena__hermes-webui-1818` | 0 / 3 | 0 / 3 | 0.000 | 0.000 | 0.000 |
| `nesquena__hermes-webui-2056` | 0 / 3 | 0 / 3 | 0.000 | 0.000 | 0.000 |
| `pallets-eco__wtforms-892_interface` | 0 / 3 | 0 / 3 | 0.000 | 0.000 | 0.000 |
| `schemathesis__schemathesis-3933` | 0 / 3 | 1 / 3 | 0.000 | 0.333 | 0.333 |
| `schemathesis__schemathesis-4087` | 0 / 3 | 0 / 3 | 0.000 | 0.000 | 0.000 |
| `tox-dev__tox-3931` | 2 / 3 | 3 / 3 | 0.667 | 1.000 | 0.333 |

## Mean per-task difference

Fleet pass fraction minus plain pass fraction, averaged over tasks; interval from 10000 bootstrap resamples of tasks (seed 20261120), 2.5th and 97.5th percentiles.

| Tasks used | Tasks left out (no fleet trial) | Mean | 2.5th percentile | 97.5th percentile |
|---|---|---|---|---|
| 8 | 0 | 0.083 | 0.000 | 0.208 |

## Cost, time and tokens

Ratios are fleet over plain. A total is n/a when any trial of the arm lacks the figure; the median is over tasks where both arms have the figure for every trial (per-task mean per arm); for agent time, the per-task mean is over the trials with a usable time and a task needs one such trial per arm.

| Figure | Plain total | Fleet total | Ratio of totals | Median per-task ratio | Tasks in median |
|---|---|---|---|---|---|
| Cost (dollars, run log) | 2.3971 | 12.3962 | 5.171 | 5.417 | 8 |
| Agent time (seconds) | 955.3 | n/a | n/a | 3.517 | 8 |
| Tokens | 4517351 | 18352545 | 4.063 | 4.055 | 8 |

Agent time: 7 trial(s) (plain 0, fleet 7) recorded more than 3000 seconds, the tasks' agent limit; such figures are wall-clock artefacts (for example a suspended machine) and are left out of the time figures.

Token sources: plain `runner_main_session`; fleet `final_event_per_model`, `runner_main_session`. Per-model figures sum input, output, cache read and cache creation; runner figures sum input (which includes cache) and output.
The two arms use different token sources, so the token ratio compares figures counted differently.

## Counts

| Arm | Timeouts | Budget-capped | Without a reward | Approver errors |
|---|---|---|---|---|
| plain | 0 | 0 | 0 | 0 |
| fleet | 0 | 0 | 0 | 0 |

Trials without a reward, by exception type:

| Arm | Exception type | Trials |
|---|---|---|
| n/a | n/a | 0 |

## Fleet behaviour

- Fleet trials with a saved plan: 21 of 24 (0.875)
- Fleet trials with a host-only copy (dispatch counts): 24

| Subagent type | Mean dispatches per trial |
|---|---|
| `conclave:executor` | 1.12 |
| `conclave:mech-executor` | 0.29 |

## Limits

- Not fresh: all tasks were created by 2026-05-12 and the models' training cutoff is June 2026, so the models may have seen the fixes.
- Small numbers: 8 tasks and 3 attempts per task confirm only large differences.
- Cost figures come from the run log, a file the container can write; a container that falsified it could understate spend.
- Token figures come from different sources: the final event's per-model usage (input, output, cache read, cache creation) or the runner's main-session figures (input including cache, plus output); the source of each arm is named.
