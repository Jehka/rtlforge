// Carry-select: same function, shorter critical path. What a successful
// timing repair should look like.
module adder32 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire        cin,
    output reg  [31:0] sum,
    output reg         cout
);
    wire [16:0] lo   = {1'b0, a[15:0]} + {1'b0, b[15:0]} + {16'd0, cin};
    wire [16:0] hi0  = {1'b0, a[31:16]} + {1'b0, b[31:16]};
    wire [16:0] hi1  = {1'b0, a[31:16]} + {1'b0, b[31:16]} + 17'd1;
    wire [16:0] hi   = lo[16] ? hi1 : hi0;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) begin sum <= 32'd0; cout <= 1'b0; end
        else        begin sum <= {hi[15:0], lo[15:0]}; cout <= hi[16]; end
endmodule
