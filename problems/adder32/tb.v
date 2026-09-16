// Trusted testbench -- NEVER shown to the design model, NEVER regenerated.
// Reference: {cout, sum} == a + b + cin, registered, one cycle of latency.
`timescale 1ns/1ps

module tb;
    reg         clk = 0;
    reg         rst_n = 1;
    reg  [31:0] a = 0, b = 0;
    reg         cin = 0;
    wire [31:0] sum;
    wire        cout;

    integer errors = 0;
    integer i;

    // expected values for the operands applied at the previous edge
    reg [32:0] exp = 33'd0;
    reg        have = 1'b0;

    adder32 dut (.clk(clk), .rst_n(rst_n), .a(a), .b(b), .cin(cin),
                 .sum(sum), .cout(cout));

    always #5 clk = ~clk;

    // Apply operands, then check the result registered at that edge.
    task apply(input [31:0] ta, input [31:0] tb_, input tcin,
               input [511:0] label);
        begin
            @(negedge clk);
            a = ta; b = tb_; cin = tcin;
            exp = {1'b0, ta} + {1'b0, tb_} + {32'd0, tcin};
            @(posedge clk);
            #1;
            have = 1'b1;
            if ({cout, sum} !== exp) begin
                $display("MISMATCH: %0s -- a=%0h b=%0h cin=%0b, expected %0h, got %0h (time %0t)",
                         label, ta, tb_, tcin, exp, {cout, sum}, $time);
                errors = errors + 1;
            end
        end
    endtask

    initial begin
        // --- asynchronous reset
        #1 rst_n = 0;
        #1;
        if (sum !== 32'd0 || cout !== 1'b0) begin
            $display("MISMATCH: outputs must clear asynchronously on reset, got %0h/%0b",
                     sum, cout);
            errors = errors + 1;
        end
        @(negedge clk);
        rst_n = 1;

        // --- directed corners
        apply(32'h0000_0000, 32'h0000_0000, 1'b0, "zero");
        apply(32'h0000_0000, 32'h0000_0000, 1'b1, "carry in only");
        apply(32'hFFFF_FFFF, 32'h0000_0001, 1'b0, "wrap to zero with carry");
        apply(32'hFFFF_FFFF, 32'hFFFF_FFFF, 1'b1, "all ones plus carry");
        apply(32'h7FFF_FFFF, 32'h0000_0001, 1'b0, "signed boundary");
        apply(32'h8000_0000, 32'h8000_0000, 1'b0, "msb carry out");
        apply(32'h0000_FFFF, 32'h0000_0001, 1'b0, "carry across bit 16");
        apply(32'h00FF_FFFF, 32'h0000_0001, 1'b0, "carry across bit 24");
        apply(32'hFFFF_FFFE, 32'h0000_0001, 1'b1, "long carry chain");
        apply(32'hAAAA_AAAA, 32'h5555_5555, 1'b0, "alternating, no carry");
        apply(32'hAAAA_AAAA, 32'h5555_5555, 1'b1, "alternating, full ripple");

        // --- reset mid-stream clears the registered outputs
        @(negedge clk);
        rst_n = 0;
        #2;
        if (sum !== 32'd0 || cout !== 1'b0) begin
            $display("MISMATCH: reset must clear registered outputs");
            errors = errors + 1;
        end
        @(negedge clk);
        rst_n = 1;

        // --- randomised
        for (i = 0; i < 500; i = i + 1)
            apply($random, $random, $random, "random vectors");

        if (!have) begin
            $display("TB_FAIL: no vectors applied");
            errors = errors + 1;
        end

        if (errors == 0)
            $display("TB_PASS");
        else
            $display("TB_FAIL: %0d mismatch(es)", errors);
        $finish;
    end

    initial begin
        #200000;
        $display("TB_FAIL: timeout");
        $finish;
    end
endmodule
