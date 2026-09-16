// The same 4-bit-group carry algorithm as adder32_cla_fixed.v, expressed as
// arithmetic rather than explicit AND/OR carry equations.
//
// Written to test whether the model's carry-lookahead lost to a plain ripple
// adder because of the algorithm or because of how it was written. ABC
// restructures arithmetic freely; explicit gate equations it must take more
// literally, so a textbook-optimal structure written at gate level can block
// the optimisation that would have made it fast.
//
// Compare all four under STA at 1.2ns:
//   adder32_ripple.v          plain a+b+cin
//   adder32_cla_fixed.v       gate-level carry-lookahead
//   adder32_cla_arithmetic.v  same algorithm, arithmetic
//   adder32_fast.v            carry-select, arithmetic
// Same carry-lookahead intent, expressed as arithmetic instead of explicit
// gate equations, so ABC is free to restructure it.
module adder32 (
    input wire clk, input wire rst_n,
    input wire [31:0] a, input wire [31:0] b, input wire cin,
    output reg [31:0] sum, output reg cout
);
    wire [8:0]  c;
    wire [31:0] s;
    assign c[0] = cin;
    genvar i;
    generate
        for (i = 0; i < 8; i = i + 1) begin : grp
            wire [4:0] part = {1'b0, a[4*i+3:4*i]} + {1'b0, b[4*i+3:4*i]}
                              + {4'd0, c[i]};
            assign s[4*i+3:4*i] = part[3:0];
            assign c[i+1] = part[4];
        end
    endgenerate
    always @(posedge clk or negedge rst_n)
        if (!rst_n) begin sum <= 32'd0; cout <= 1'b0; end
        else        begin sum <= s;     cout <= c[8]; end
endmodule
