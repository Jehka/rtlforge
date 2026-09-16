# 32-bit Registered Adder

Design a module `adder32` that adds two 32-bit numbers.

| Port    | Dir    | Width | Description                          |
|---------|--------|-------|--------------------------------------|
| `clk`   | input  | 1     | Clock, rising edge active            |
| `rst_n` | input  | 1     | Asynchronous reset, active low       |
| `a`     | input  | 32    | First operand                        |
| `b`     | input  | 32    | Second operand                       |
| `cin`   | input  | 1     | Carry in                             |
| `sum`   | output | 32    | Registered sum                       |
| `cout`  | output | 1     | Registered carry out                 |

Behaviour:

- On each rising clock edge with `rst_n` high, the module registers the
  33-bit result of `a + b + cin`: the low 32 bits appear on `sum` and the
  carry out on `cout`, both visible in the cycle following the edge.
- On `rst_n` low, `sum` and `cout` clear to 0 immediately (asynchronous).
- The addition is purely combinational between the input ports and the
  output registers. There is **exactly one clock cycle** of latency from
  inputs to outputs; do not add pipeline stages, as that would change the
  interface timing the testbench checks.

The combinational adder sits on the critical path, so how it is structured
determines the achievable clock frequency.
