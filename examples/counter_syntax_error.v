module counter (
    input  wire       clk,
    input  wire       rst_n,
    output reg  [3:0] count
);
    always @(posedge clk) begin
        if (!rst_n)
            count <= 4'b0
        else
            count <= count + 1;
    end
endmodule
