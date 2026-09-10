# Overlapping "1011" Sequence Detector

Design a module `seq_detect_1011` that detects the bit pattern `1011` on a
serial input.

| Port    | Dir    | Width | Description                            |
|---------|--------|-------|----------------------------------------|
| `clk`   | input  | 1     | Clock, rising edge active              |
| `rst_n` | input  | 1     | Asynchronous reset, active low         |
| `din`   | input  | 1     | Serial data input, sampled on `posedge`|
| `found` | output | 1     | Detection pulse                        |

Behaviour:

- `din` is sampled on each rising clock edge. The pattern is received
  most-significant bit first: `1`, then `0`, then `1`, then `1`.
- `found` is a **registered** output. It goes high for **exactly one clock
  cycle**, in the cycle immediately following the clock edge on which the
  final `1` of the pattern was sampled.
- Detection is **overlapping**: after a match, the trailing bits may serve as
  the start of the next match. The input stream `1011011` therefore produces
  two detections.
- On `rst_n` low, the detector returns to its initial state and `found` clears
  to 0 immediately (asynchronous).
