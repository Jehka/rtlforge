# 4-bit Up Counter with Enable

Design a module `counter_en` with the following interface:

| Port    | Dir    | Width | Description                          |
|---------|--------|-------|--------------------------------------|
| `clk`   | input  | 1     | Clock, rising edge active            |
| `rst_n` | input  | 1     | Asynchronous reset, active low       |
| `en`    | input  | 1     | Count enable, active high            |
| `count` | output | 4     | Current count value                  |

Behaviour:

- On `rst_n` low, `count` clears to 0 immediately (asynchronous).
- On each rising edge of `clk` with `rst_n` high and `en` high, `count`
  increments by 1.
- When `en` is low, `count` holds its value.
- The counter wraps from 15 back to 0.
