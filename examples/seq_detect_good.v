module seq_detect_1011 (input wire clk, input wire rst_n, input wire din, output reg found);
    localparam S0=3'd0, S1=3'd1, S10=3'd2, S101=3'd3;
    reg [2:0] state;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin state <= S0; found <= 1'b0; end
        else begin
            found <= (state == S101) && din;
            case (state)
                S0:   state <= din ? S1  : S0;
                S1:   state <= din ? S1  : S10;
                S10:  state <= din ? S101: S0;
                S101: state <= din ? S1  : S10;   // overlap: 1011 -> tail "1"
                default: state <= S0;
            endcase
        end
    end
endmodule
