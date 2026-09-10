// Trusted testbench -- NEVER shown to the design model, NEVER regenerated.
// Reference model: a 4-bit shift register. `found` is registered, so the
// expected value at any moment is whether the four most recently sampled bits
// equalled 1011 at the previous rising edge.
`timescale 1ns/1ps

module tb;
    reg  clk = 0;
    reg  rst_n = 1;
    reg  din = 0;
    wire found;

    integer errors = 0;
    integer detections = 0;
    integer i;

    // golden reference
    reg [3:0] shifted = 4'b0000;
    reg       exp_found = 1'b0;

    seq_detect_1011 dut (.clk(clk), .rst_n(rst_n), .din(din), .found(found));

    always #5 clk = ~clk;

    // Reference updates on the same edge as the DUT.
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            shifted   <= 4'b0000;
            exp_found <= 1'b0;
        end else begin
            shifted   <= {shifted[2:0], din};
            exp_found <= ({shifted[2:0], din} == 4'b1011);
        end
    end

    task check(input [511:0] label);
        begin
            if (found !== exp_found) begin
                $display("MISMATCH: %0s -- expected found=%0b, got %0b (time %0t)",
                         label, exp_found, found, $time);
                errors = errors + 1;
            end
        end
    endtask

    // Drive one bit: change din away from the edge, then check after it.
    task send(input bit_in, input [511:0] label);
        begin
            @(negedge clk);
            din = bit_in;
            @(posedge clk);
            #1 check(label);
            if (exp_found) detections = detections + 1;
        end
    endtask

    initial begin
        // --- async reset
        #1 rst_n = 0;
        #1;
        if (found !== 1'b0) begin
            $display("MISMATCH: found must clear asynchronously on reset, got %0b",
                     found);
            errors = errors + 1;
        end
        @(negedge clk);
        rst_n = 1;

        // --- exact pattern, isolated
        send(1, "1011 bit0");
        send(0, "1011 bit1");
        send(1, "1011 bit2");
        send(1, "1011 bit3 -- expect detection");

        // --- found must fall again; a level-held output fails here
        send(0, "cycle after detection -- found must deassert");

        // --- overlapping: 1011011 contains two matches
        send(1, "overlap 1");
        send(0, "overlap 0");
        send(1, "overlap 1");
        send(1, "overlap 1 -- first match");
        send(0, "overlap 0");
        send(1, "overlap 1");
        send(1, "overlap 1 -- second match (overlapping)");

        // --- near misses that must NOT fire
        send(1, "near-miss 1");
        send(0, "near-miss 0");
        send(0, "near-miss 0");
        send(1, "near-miss 1 -- 1001, no detection");
        send(1, "near-miss 1");
        send(1, "near-miss 1");
        send(1, "near-miss 1 -- run of ones, no detection");

        // --- reset mid-pattern must discard partial progress
        send(1, "pre-reset 1");
        send(0, "pre-reset 0");
        @(negedge clk);
        rst_n = 0;
        #2 rst_n = 1;
        send(1, "post-reset 1");
        send(1, "post-reset 1 -- partial pattern was discarded, no detection");

        // --- randomised stream against the reference model
        for (i = 0; i < 400; i = i + 1) begin
            send($random, "random stream");
        end

        if (detections < 3) begin
            $display("TB_FAIL: only %0d detection(s) observed -- stimulus too weak",
                     detections);
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
