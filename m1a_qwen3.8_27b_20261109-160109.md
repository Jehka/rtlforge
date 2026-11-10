# M1a — spec-question answer accuracy (20261109-160109)

model `qwen3.8:27b` (reasoning effort low), temperature 0, depth 20, ≤5 questions per problem, seed 7  
skipped: {'no spec': 0, 'formal not a difference': 46}

Every question comes from a design that **failed** its testbench, so design-level accuracy on these is 0%. Chance on A/B is 50%.

| subset | questions | correct | 95% CI | NEITHER | unparsed |
|---|---|---|---|---|---|
| all | 263 | 190 (72.2%) | [66.5, 77.3] | 16 | 36 |
| logic | 255 | 188 (73.7%) | [68.0, 78.7] | 16 | 36 |
| init-only | 8 | 2 (25.0%) | [7.1, 59.1] | 0 | 0 |
| reference was A | 142 | 108 (76.1%) | [68.4, 82.3] | 6 | 16 |
| reference was B | 121 | 82 (67.8%) | [59.0, 75.4] | 10 | 20 |
| difference at cycle 1 | 126 | 94 (74.6%) | [66.4, 81.4] | 14 | 6 |
| cycles 2-4 | 82 | 49 (59.8%) | [48.9, 69.7] | 1 | 24 |
| cycles 5-8 | 47 | 40 (85.1%) | [72.3, 92.6] | 1 | 6 |
| cycles 9+ | 8 | 7 (87.5%) | [52.9, 97.8] | 0 | 0 |

## Per problem

| problem | questions | correct |
|---|---|---|
| Prob034_dff8 | 1 | 0 |
| Prob050_kmap1 | 5 | 5 |
| Prob053_m2014_q4d | 4 | 0 |
| Prob057_kmap2 | 5 | 5 |
| Prob060_m2014_q4k | 2 | 1 |
| Prob062_bugs_mux2 | 1 | 0 |
| Prob063_review2015_shiftcount | 4 | 0 |
| Prob066_edgecapture | 5 | 4 |
| Prob068_countbcd | 5 | 4 |
| Prob070_ece241_2013_q2 | 5 | 3 |
| Prob074_ece241_2014_q4 | 4 | 4 |
| Prob078_dualedge | 2 | 0 |
| Prob079_fsm3onehot | 5 | 5 |
| Prob080_timer | 5 | 4 |
| Prob082_lfsr32 | 5 | 2 |
| Prob083_mt2015_q4b | 4 | 2 |
| Prob084_ece241_2013_q12 | 1 | 1 |
| Prob086_lfsr5 | 2 | 1 |
| Prob088_ece241_2014_q5b | 3 | 3 |
| Prob089_ece241_2014_q5a | 4 | 2 |
| Prob091_2012_q2b | 5 | 5 |
| Prob092_gatesv100 | 4 | 0 |
| Prob093_ece241_2014_q3 | 5 | 1 |
| Prob098_circuit7 | 5 | 3 |
| Prob099_m2014_q6c | 5 | 5 |
| Prob101_circuit4 | 5 | 5 |
| Prob102_circuit3 | 5 | 4 |
| Prob103_circuit2 | 5 | 5 |
| Prob104_mt2015_muxdff | 3 | 0 |
| Prob107_fsm1s | 1 | 0 |
| Prob109_fsm1 | 1 | 1 |
| Prob110_fsm2 | 2 | 2 |
| Prob111_fsm2s | 1 | 1 |
| Prob112_always_case2 | 5 | 5 |
| Prob113_2012_q1g | 5 | 3 |
| Prob115_shift18 | 5 | 1 |
| Prob116_m2014_q3 | 4 | 3 |
| Prob117_circuit9 | 3 | 2 |
| Prob119_fsm3 | 1 | 1 |
| Prob120_fsm3s | 5 | 5 |
| Prob121_2014_q3bfsm | 5 | 5 |
| Prob122_kmap4 | 1 | 1 |
| Prob124_rule110 | 1 | 0 |
| Prob125_kmap3 | 5 | 5 |
| Prob127_lemmings1 | 2 | 2 |
| Prob128_fsm_ps2 | 5 | 5 |
| Prob129_ece241_2013_q8 | 5 | 5 |
| Prob133_2014_q3fsm | 5 | 3 |
| Prob134_2014_q3c | 4 | 4 |
| Prob135_m2014_q6b | 5 | 5 |
| Prob136_m2014_q6 | 5 | 5 |
| Prob138_2012_q2fsm | 5 | 5 |
| Prob139_2013_q2bfsm | 5 | 5 |
| Prob140_fsm_hdlc | 5 | 4 |
| Prob141_count_clock | 4 | 1 |
| Prob142_lemmings2 | 5 | 5 |
| Prob143_fsm_onehot | 5 | 5 |
| Prob145_circuit8 | 5 | 0 |
| Prob147_circuit10 | 5 | 0 |
| Prob148_2013_q2afsm | 5 | 5 |
| Prob149_ece241_2013_q4 | 5 | 0 |
| Prob150_review2015_fsmonehot | 5 | 5 |
| Prob151_review2015_fsm | 5 | 5 |
| Prob153_gshare | 5 | 4 |
| Prob154_fsm_ps2data | 5 | 4 |
| Prob156_review2015_fancytimer | 4 | 4 |

Problems where at least one question was answered correctly: 54/66

Raw prompts and answers: `m1a_qwen3.8_27b_20261109-160109.jsonl`
