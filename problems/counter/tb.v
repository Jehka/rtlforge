// Trusted testbench -- NEVER shown to the design model, NEVER regenerated.
// Protocol: print MISMATCH lines for each divergence, then TB_PASS or TB_FAIL.
`timescale 1ns/1ps

module tb;
    reg        clk = 0;
    reg        rst_n = 1;
    reg        en = 0;
    wire [3:0] count;

    integer errors = 0;
    reg  [3:0] expected = 4'd0;

    counter_en dut (.clk(clk), .rst_n(rst_n), .en(en), .count(count));

    always #5 clk = ~clk;

    task check(input [511:0] label);
        begin
            if (count !== expected) begin
                $display("MISMATCH: %0s -- expected count=%0d, got %0d (time %0t)",
                         label, expected, count, $time);
                errors = errors + 1;
            end
        end
    endtask

    integer i;
    initial begin
        // Drive a real negedge on rst_n -- a signal initialised to 0 never
        // transitions, so an async-reset always block would never trigger.
        #1 rst_n = 0;
        #1;
        expected = 4'd0;
        check("during async reset");

        // reset should clear even mid-cycle, with en high
        en = 1;
        #6 check("reset holds count at 0 while en high");

        // --- release reset on a clean edge
        @(negedge clk);
        rst_n = 1;

        // --- count 20 enabled cycles, exercising the wrap at 15
        for (i = 0; i < 20; i = i + 1) begin
            @(posedge clk);
            expected = expected + 4'd1;   // wraps naturally at 4 bits
            #1 check("counting with en=1");
        end

        // --- hold when disabled
        @(negedge clk);
        en = 0;
        for (i = 0; i < 4; i = i + 1) begin
            @(posedge clk);
            #1 check("holding with en=0");
        end

        // --- resume
        @(negedge clk);
        en = 1;
        for (i = 0; i < 3; i = i + 1) begin
            @(posedge clk);
            expected = expected + 4'd1;
            #1 check("resumed counting");
        end

        // --- asynchronous reset mid-count, away from any clock edge
        @(negedge clk);
        #2 rst_n = 0;
        #1;
        expected = 4'd0;
        check("async reset takes effect without a clock edge");

        @(negedge clk);
        rst_n = 1;
        @(posedge clk);
        expected = expected + 4'd1;
        #1 check("counts again after reset release");

        if (errors == 0)
            $display("TB_PASS");
        else
            $display("TB_FAIL: %0d mismatch(es)", errors);
        $finish;
    end

    // Watchdog: a design that never advances must not hang the runner.
    initial begin
        #100000;
        $display("TB_FAIL: timeout");
        $finish;
    end
endmodule
