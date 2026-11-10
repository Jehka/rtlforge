> **RUN ABORTED** — aborted after 3 consecutive failed calls at question 51: ERROR failed after 4 attempts: HTTPConnectionPool(host='127.0.0.1', port=56665): Read timed out. (read timeout=180)
  If this mentions tokens-per-minute, lower --max-tokens: the ceiling is reserved ag

# M1a — spec-question answer accuracy (20261110-062318)

model `qwen3.8:27b` (reasoning effort low), temperature 0, depth 20, ≤5 questions per problem, seed 7  
skipped: {'no spec': 0, 'formal not a difference': 47}

Every question comes from a design that **failed** its testbench, so design-level accuracy on these is 0%. Chance on A/B is 50%.

| subset | questions | correct | 95% CI | NEITHER | no answer (truncated/empty) | unparsed | correct among A/B |
|---|---|---|---|---|---|---|---|
| all | 48 | 32 (66.7%) | [52.5, 78.3] | 4 | 0 | 0 | 32/44 (72.7%) |
| logic | 43 | 30 (69.8%) | [54.9, 81.4] | 4 | 0 | 0 | 30/39 (76.9%) |
| init-only | 5 | 2 (40.0%) | [11.8, 76.9] | 0 | 0 | 0 | 2/5 (40.0%) |
| reference was A | 24 | 14 (58.3%) | [38.8, 75.5] | 1 | 0 | 0 | 14/23 (60.9%) |
| reference was B | 24 | 18 (75.0%) | [55.1, 88.0] | 3 | 0 | 0 | 18/21 (85.7%) |
| difference at cycle 1 | 29 | 21 (72.4%) | [54.3, 85.3] | 4 | 0 | 0 | 21/25 (84.0%) |
| cycles 2-4 | 14 | 7 (50.0%) | [26.8, 73.2] | 0 | 0 | 0 | 7/14 (50.0%) |
| cycles 5-8 | 1 | 1 (100.0%) | [20.7, 100.0] | 0 | 0 | 0 | 1/1 (100.0%) |
| cycles 9+ | 4 | 3 (75.0%) | [30.1, 95.4] | 0 | 0 | 0 | 3/4 (75.0%) |

## Per problem

| problem | questions | correct |
|---|---|---|
| Prob034_dff8 | 1 | 0 |
| Prob050_kmap1 | 5 | 5 |
| Prob053_m2014_q4d | 2 | 0 |
| Prob057_kmap2 | 5 | 5 |
| Prob060_m2014_q4k | 1 | 1 |
| Prob062_bugs_mux2 | 1 | 0 |
| Prob063_review2015_shiftcount | 4 | 0 |
| Prob066_edgecapture | 5 | 3 |
| Prob068_countbcd | 5 | 4 |
| Prob070_ece241_2013_q2 | 5 | 4 |
| Prob074_ece241_2014_q4 | 3 | 3 |
| Prob078_dualedge | 1 | 1 |
| Prob079_fsm3onehot | 5 | 4 |
| Prob080_timer | 5 | 2 |

Problems where at least one question was answered correctly: 10/14

Raw prompts and answers: `m1a_qwen3.8_27b_20261110-062318.jsonl`
