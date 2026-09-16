// BUG: drops the carry-out. Functionally wrong, catches a weak testbench.
module adder32 (
    input wire clk, input wire rst_n,
    input wire [31:0] a, input wire [31:0] b, input wire cin,
    output reg [31:0] sum, output reg cout
);
    wire [31:0] s = a + b + {31'd0, cin};
    always @(posedge clk or negedge rst_n)
        if (!rst_n) begin sum <= 32'd0; cout <= 1'b0; end
        else        begin sum <= s;     cout <= 1'b0; end
endmodule
