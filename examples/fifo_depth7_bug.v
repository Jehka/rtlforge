module sync_fifo (
    input wire clk, input wire rst_n,
    input wire wr_en, input wire [7:0] wr_data,
    input wire rd_en, output reg [7:0] rd_data,
    output wire full, output wire empty
);
    reg [7:0] mem [0:7];
    reg [2:0] head, tail;
    reg [3:0] count;
    assign full  = (count == 4'd7);
    assign empty = (count == 4'd0);
    wire wr_ok = wr_en && !full;
    wire rd_ok = rd_en && !empty;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            head <= 3'd0; tail <= 3'd0; count <= 4'd0; rd_data <= 8'd0;
        end else begin
            if (rd_ok) begin rd_data <= mem[head]; head <= head + 3'd1; end
            if (wr_ok) begin mem[tail] <= wr_data; tail <= tail + 3'd1; end
            case ({wr_ok, rd_ok})
                2'b10: count <= count + 4'd1;
                2'b01: count <= count - 4'd1;
                default: count <= count;
            endcase
        end
    end
endmodule
