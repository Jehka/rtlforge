// Naive ripple-carry: functionally correct, slow. The kind of thing an LLM
// writes first and the timing loop should improve on.
module adder32 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire        cin,
    output reg  [31:0] sum,
    output reg         cout
);
    wire [32:0] carry;
    wire [31:0] s;
    assign carry[0] = cin;
    genvar i;
    generate
        for (i = 0; i < 32; i = i + 1) begin : stage
            assign s[i]       = a[i] ^ b[i] ^ carry[i];
            assign carry[i+1] = (a[i] & b[i]) | (carry[i] & (a[i] ^ b[i]));
        end
    endgenerate
    always @(posedge clk or negedge rst_n)
        if (!rst_n) begin sum <= 32'd0; cout <= 1'b0; end
        else        begin sum <= s;     cout <= carry[32]; end
endmodule
