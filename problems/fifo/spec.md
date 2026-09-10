# Synchronous FIFO, 8 deep x 8 bits

Design a module `sync_fifo` implementing a first-in first-out buffer. All
ports are in a single clock domain.

| Port      | Dir    | Width | Description                              |
|-----------|--------|-------|------------------------------------------|
| `clk`     | input  | 1     | Clock, rising edge active                |
| `rst_n`   | input  | 1     | Asynchronous reset, active low           |
| `wr_en`   | input  | 1     | Write request                            |
| `wr_data` | input  | 8     | Data to write                            |
| `rd_en`   | input  | 1     | Read request                             |
| `rd_data` | output | 8     | Data read out, registered                |
| `full`    | output | 1     | High when the FIFO holds 8 entries       |
| `empty`   | output | 1     | High when the FIFO holds 0 entries       |

Behaviour:

- Depth is exactly 8 entries of 8 bits.
- A write occurs on a rising clock edge when `wr_en` is high and `full` is low.
  A write requested while `full` is high is **ignored** and must not corrupt
  the stored data.
- A read occurs on a rising clock edge when `rd_en` is high and `empty` is low.
  A read requested while `empty` is high is **ignored**.
- `rd_data` is **registered**: the value of the entry being read appears on
  `rd_data` in the cycle following the clock edge on which the read occurred.
  `rd_data` holds its previous value when no read occurs.
- Data is returned in the order it was written.
- Simultaneous `wr_en` and `rd_en` in the same cycle is legal when the FIFO is
  neither full nor empty; both operations take effect and the occupancy is
  unchanged.
- On `rst_n` low, the FIFO empties immediately (asynchronous): `empty` goes
  high, `full` goes low, and the contents are discarded.
