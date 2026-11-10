# M1a — spec-question answer accuracy (20261109-160109)

model `qwen2.5-coder:32b`, temperature 0, depth 20, ≤5 questions per problem, seed 7  
skipped: {'no spec': 0, 'formal not a difference': 46}

Every question comes from a design that **failed** its testbench, so design-level accuracy on these is 0%. Chance on A/B is 50%.

| subset | questions | correct | 95% CI | NEITHER | unparsed |
|---|---|---|---|---|---|
| all | 263 | 41 (15.6%) | [11.7, 20.5] | 149 | 0 |
| logic | 255 | 36 (14.1%) | [10.4, 18.9] | 146 | 0 |
| init-only | 8 | 5 (62.5%) | [30.6, 86.3] | 3 | 0 |
| reference was A | 142 | 9 (6.3%) | [3.4, 11.6] | 69 | 0 |
| reference was B | 121 | 32 (26.4%) | [19.4, 34.9] | 80 | 0 |
| difference at cycle 1 | 126 | 15 (11.9%) | [7.3, 18.7] | 87 | 0 |
| cycles 2-4 | 82 | 23 (28.0%) | [19.5, 38.6] | 33 | 0 |
| cycles 5-8 | 47 | 3 (6.4%) | [2.2, 17.2] | 26 | 0 |
| cycles 9+ | 8 | 0 (0.0%) | [0.0, 32.4] | 3 | 0 |

## Per problem

| problem | questions | correct |
|---|---|---|
| Prob034_dff8 | 1 | 1 |
| Prob050_kmap1 | 5 | 0 |
| Prob053_m2014_q4d | 4 | 2 |
| Prob057_kmap2 | 5 | 0 |
| Prob060_m2014_q4k | 2 | 0 |
| Prob062_bugs_mux2 | 1 | 0 |
| Prob063_review2015_shiftcount | 4 | 3 |
| Prob066_edgecapture | 5 | 0 |
| Prob068_countbcd | 5 | 0 |
| Prob070_ece241_2013_q2 | 5 | 1 |
| Prob074_ece241_2014_q4 | 4 | 0 |
| Prob078_dualedge | 2 | 0 |
| Prob079_fsm3onehot | 5 | 3 |
| Prob080_timer | 5 | 0 |
| Prob082_lfsr32 | 5 | 2 |
| Prob083_mt2015_q4b | 4 | 0 |
| Prob084_ece241_2013_q12 | 1 | 0 |
| Prob086_lfsr5 | 2 | 0 |
| Prob088_ece241_2014_q5b | 3 | 1 |
| Prob089_ece241_2014_q5a | 4 | 0 |
| Prob091_2012_q2b | 5 | 0 |
| Prob092_gatesv100 | 4 | 1 |
| Prob093_ece241_2014_q3 | 5 | 0 |
| Prob098_circuit7 | 5 | 3 |
| Prob099_m2014_q6c | 5 | 0 |
| Prob101_circuit4 | 5 | 4 |
| Prob102_circuit3 | 5 | 3 |
| Prob103_circuit2 | 5 | 0 |
| Prob104_mt2015_muxdff | 3 | 2 |
| Prob107_fsm1s | 1 | 1 |
| Prob109_fsm1 | 1 | 0 |
| Prob110_fsm2 | 2 | 0 |
| Prob111_fsm2s | 1 | 0 |
| Prob112_always_case2 | 5 | 0 |
| Prob113_2012_q1g | 5 | 0 |
| Prob115_shift18 | 5 | 0 |
| Prob116_m2014_q3 | 4 | 0 |
| Prob117_circuit9 | 3 | 1 |
| Prob119_fsm3 | 1 | 0 |
| Prob120_fsm3s | 5 | 0 |
| Prob121_2014_q3bfsm | 5 | 1 |
| Prob122_kmap4 | 1 | 0 |
| Prob124_rule110 | 1 | 0 |
| Prob125_kmap3 | 5 | 0 |
| Prob127_lemmings1 | 2 | 0 |
| Prob128_fsm_ps2 | 5 | 1 |
| Prob129_ece241_2013_q8 | 5 | 5 |
| Prob133_2014_q3fsm | 5 | 0 |
| Prob134_2014_q3c | 4 | 0 |
| Prob135_m2014_q6b | 5 | 0 |
| Prob136_m2014_q6 | 5 | 0 |
| Prob138_2012_q2fsm | 5 | 0 |
| Prob139_2013_q2bfsm | 5 | 0 |
| Prob140_fsm_hdlc | 5 | 0 |
| Prob141_count_clock | 4 | 0 |
| Prob142_lemmings2 | 5 | 3 |
| Prob143_fsm_onehot | 5 | 0 |
| Prob145_circuit8 | 5 | 1 |
| Prob147_circuit10 | 5 | 0 |
| Prob148_2013_q2afsm | 5 | 1 |
| Prob149_ece241_2013_q4 | 5 | 1 |
| Prob150_review2015_fsmonehot | 5 | 0 |
| Prob151_review2015_fsm | 5 | 0 |
| Prob153_gshare | 5 | 0 |
| Prob154_fsm_ps2data | 5 | 0 |
| Prob156_review2015_fancytimer | 4 | 0 |

Problems where at least one question was answered correctly: 21/66

Raw prompts and answers: `m1a_qwen2.5-coder_32b_20261109-160109.jsonl`
