# M1a — spec-question answer accuracy (20261110-120038)

model `qwen3.8:27b` (reasoning effort low), temperature 0, depth 20, ≤5 questions per problem, seed 7  
skipped: {'no spec': 0, 'formal not a difference': 46}

Every question comes from a design that **failed** its testbench, so design-level accuracy on these is 0%. Chance on A/B is 50%.

| subset | questions | correct | 95% CI | NEITHER | no answer (truncated/empty) | unparsed | correct among A/B |
|---|---|---|---|---|---|---|---|
| all | 263 | 217 (82.5%) | [77.5, 86.6] | 13 | 20 | 0 | 217/230 (94.3%) |
| logic | 255 | 215 (84.3%) | [79.3, 88.3] | 12 | 20 | 0 | 215/223 (96.4%) |
| init-only | 8 | 2 (25.0%) | [7.1, 59.1] | 1 | 0 | 0 | 2/7 (28.6%) |
| reference was A | 142 | 121 (85.2%) | [78.4, 90.1] | 7 | 10 | 0 | 121/125 (96.8%) |
| reference was B | 121 | 96 (79.3%) | [71.3, 85.6] | 6 | 10 | 0 | 96/105 (91.4%) |
| difference at cycle 1 | 107 | 87 (81.3%) | [72.9, 87.6] | 9 | 5 | 0 | 87/93 (93.5%) |
| cycles 2-4 | 101 | 77 (76.2%) | [67.1, 83.5] | 4 | 13 | 0 | 77/84 (91.7%) |
| cycles 5-8 | 46 | 45 (97.8%) | [88.7, 99.6] | 0 | 1 | 0 | 45/45 (100.0%) |
| cycles 9+ | 9 | 8 (88.9%) | [56.5, 98.0] | 0 | 1 | 0 | 8/8 (100.0%) |

## Per problem

| problem | questions | correct |
|---|---|---|
| Prob034_dff8 | 1 | 0 |
| Prob050_kmap1 | 5 | 5 |
| Prob053_m2014_q4d | 4 | 0 |
| Prob057_kmap2 | 5 | 5 |
| Prob060_m2014_q4k | 2 | 2 |
| Prob062_bugs_mux2 | 1 | 0 |
| Prob063_review2015_shiftcount | 4 | 0 |
| Prob066_edgecapture | 5 | 5 |
| Prob068_countbcd | 5 | 5 |
| Prob070_ece241_2013_q2 | 5 | 2 |
| Prob074_ece241_2014_q4 | 4 | 4 |
| Prob078_dualedge | 2 | 2 |
| Prob079_fsm3onehot | 5 | 5 |
| Prob080_timer | 5 | 5 |
| Prob082_lfsr32 | 5 | 4 |
| Prob083_mt2015_q4b | 4 | 4 |
| Prob084_ece241_2013_q12 | 1 | 1 |
| Prob086_lfsr5 | 2 | 2 |
| Prob088_ece241_2014_q5b | 3 | 3 |
| Prob089_ece241_2014_q5a | 4 | 2 |
| Prob091_2012_q2b | 5 | 5 |
| Prob092_gatesv100 | 4 | 1 |
| Prob093_ece241_2014_q3 | 5 | 1 |
| Prob098_circuit7 | 5 | 2 |
| Prob099_m2014_q6c | 5 | 5 |
| Prob101_circuit4 | 5 | 5 |
| Prob102_circuit3 | 5 | 5 |
| Prob103_circuit2 | 5 | 5 |
| Prob104_mt2015_muxdff | 3 | 0 |
| Prob107_fsm1s | 1 | 1 |
| Prob109_fsm1 | 1 | 1 |
| Prob110_fsm2 | 2 | 2 |
| Prob111_fsm2s | 1 | 1 |
| Prob112_always_case2 | 5 | 5 |
| Prob113_2012_q1g | 5 | 5 |
| Prob115_shift18 | 5 | 3 |
| Prob116_m2014_q3 | 4 | 4 |
| Prob117_circuit9 | 3 | 1 |
| Prob119_fsm3 | 1 | 1 |
| Prob120_fsm3s | 5 | 5 |
| Prob121_2014_q3bfsm | 5 | 5 |
| Prob122_kmap4 | 1 | 1 |
| Prob124_rule110 | 1 | 0 |
| Prob125_kmap3 | 5 | 5 |
| Prob127_lemmings1 | 2 | 2 |
| Prob128_fsm_ps2 | 5 | 5 |
| Prob129_ece241_2013_q8 | 5 | 5 |
| Prob133_2014_q3fsm | 5 | 5 |
| Prob134_2014_q3c | 4 | 2 |
| Prob135_m2014_q6b | 5 | 5 |
| Prob136_m2014_q6 | 5 | 5 |
| Prob138_2012_q2fsm | 5 | 5 |
| Prob139_2013_q2bfsm | 5 | 5 |
| Prob140_fsm_hdlc | 5 | 5 |
| Prob141_count_clock | 4 | 4 |
| Prob142_lemmings2 | 5 | 5 |
| Prob143_fsm_onehot | 5 | 5 |
| Prob145_circuit8 | 5 | 0 |
| Prob147_circuit10 | 5 | 2 |
| Prob148_2013_q2afsm | 5 | 5 |
| Prob149_ece241_2013_q4 | 5 | 5 |
| Prob150_review2015_fsmonehot | 5 | 5 |
| Prob151_review2015_fsm | 5 | 5 |
| Prob153_gshare | 5 | 4 |
| Prob154_fsm_ps2data | 5 | 5 |
| Prob156_review2015_fancytimer | 4 | 3 |

Problems where at least one question was answered correctly: 59/66

Raw prompts and answers: `m1a_qwen3.8_27b_20261110-120038.jsonl`
