// The model's carry-lookahead adder with its single off-by-one corrected.
// Functionally correct; use it to measure what the attempted transformation
// would have achieved had it been written correctly.
module adder32 (
    input  wire        clk, input wire rst_n,
    input  wire [31:0] a, input wire [31:0] b, input wire cin,
    output reg  [31:0] sum, output reg cout
);
    wire [31:0] p = a ^ b;
    wire [31:0] g = a & b;
    wire [7:0] gp, gg;
    genvar i;
    generate
        for (i = 0; i < 8; i=i+1) begin : GROUP
            assign gp[i] = p[4*i+3] & p[4*i+2] & p[4*i+1] & p[4*i];
            assign gg[i] = g[4*i+3] | (p[4*i+3] & g[4*i+2]) |
                           (p[4*i+3] & p[4*i+2] & g[4*i+1]) |
                           (p[4*i+3] & p[4*i+2] & p[4*i+1] & g[4*i]);
        end
    endgenerate
    wire [8:0] c_group;
    assign c_group[0] = cin;
    generate
        for (i = 0; i < 8; i=i+1) begin : CARRY_GROUP
            assign c_group[i+1] = gg[i] | (gp[i] & c_group[i]);
        end
    endgenerate
    wire [31:0] c;
    generate
        for (i = 0; i < 8; i=i+1) begin : BIT_CARRY
            assign c[4*i]   = g[4*i] | (p[4*i] & c_group[i]);
            assign c[4*i+1] = g[4*i+1] | (p[4*i+1] & g[4*i]) |
                              (p[4*i+1] & p[4*i] & c_group[i]);
            assign c[4*i+2] = g[4*i+2] | (p[4*i+2] & g[4*i+1]) |
                              (p[4*i+2] & p[4*i+1] & g[4*i]) |
                              (p[4*i+2] & p[4*i+1] & p[4*i] & c_group[i]);
            assign c[4*i+3] = g[4*i+3] | (p[4*i+3] & g[4*i+2]) |
                              (p[4*i+3] & p[4*i+2] & g[4*i+1]) |
                              (p[4*i+3] & p[4*i+2] & p[4*i+1] & g[4*i]) |
                              (p[4*i+3] & p[4*i+2] & p[4*i+1] & p[4*i] & c_group[i]);
        end
    endgenerate
    wire [31:0] sum_comb = p ^ {c[30:0], cin};  // carry IN, not carry OUT
    wire cout_comb = c[31];
    always @(posedge clk or negedge rst_n)
        if (!rst_n) begin sum <= 32'b0; cout <= 1'b0; end
        else        begin sum <= sum_comb; cout <= cout_comb; end
endmodule
