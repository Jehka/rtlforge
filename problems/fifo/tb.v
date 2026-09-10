// Trusted testbench -- NEVER shown to the design model, NEVER regenerated.
// Reference model: a software queue driven by the same rules as the spec.
// rd_data is registered, so the expected read value is checked one cycle after
// the read is accepted.
`timescale 1ns/1ps

module tb;
    localparam WIDTH = 8;
    localparam DEPTH = 8;

    reg              clk = 0;
    reg              rst_n = 1;
    reg              wr_en = 0;
    reg  [WIDTH-1:0] wr_data = 0;
    reg              rd_en = 0;
    wire [WIDTH-1:0] rd_data;
    wire             full, empty;

    integer errors = 0;
    integer i, wrote, read_back;

    // ---- golden reference queue
    reg [WIDTH-1:0] q [0:DEPTH-1];
    integer         head = 0, tail = 0, count = 0;
    reg [WIDTH-1:0] exp_rd_data = 0;   // value rd_data should show now
    reg             have_rd = 0;        // any read accepted since reset?

    sync_fifo dut (
        .clk(clk), .rst_n(rst_n),
        .wr_en(wr_en), .wr_data(wr_data),
        .rd_en(rd_en), .rd_data(rd_data),
        .full(full), .empty(empty)
    );

    always #5 clk = ~clk;

    task fail(input [511:0] label);
        begin
            $display("MISMATCH: %0s (time %0t)", label, $time);
            errors = errors + 1;
        end
    endtask

    // Compare flags against the reference after every edge.
    task check_flags(input [511:0] label);
        begin
            if (full !== (count == DEPTH)) begin
                $display("MISMATCH: %0s -- expected full=%0b (count=%0d), got %0b (time %0t)",
                         label, (count == DEPTH), count, full, $time);
                errors = errors + 1;
            end
            if (empty !== (count == 0)) begin
                $display("MISMATCH: %0s -- expected empty=%0b (count=%0d), got %0b (time %0t)",
                         label, (count == 0), count, empty, $time);
                errors = errors + 1;
            end
        end
    endtask

    // One clock of stimulus. Applies the same accept rules as the spec to the
    // reference model, then compares.
    task step(input do_wr, input [WIDTH-1:0] data, input do_rd,
              input [511:0] label);
        reg wr_ok, rd_ok;
        begin
            @(negedge clk);
            wr_en   = do_wr;
            wr_data = data;
            rd_en   = do_rd;

            wr_ok = do_wr && (count < DEPTH);
            rd_ok = do_rd && (count > 0);

            // rd_data is registered: the entry read AT this edge is visible
            // immediately after it. Capture the expectation before the edge.
            if (rd_ok) begin
                exp_rd_data = q[head];
                have_rd = 1;
            end
            // otherwise exp_rd_data holds -- the spec requires rd_data to hold

            @(posedge clk);
            #1;

            if (have_rd && (rd_data !== exp_rd_data)) begin
                $display("MISMATCH: %0s -- expected rd_data=%0h, got %0h (time %0t)",
                         label, exp_rd_data, rd_data, $time);
                errors = errors + 1;
            end

            if (rd_ok) begin
                head  = (head + 1) % DEPTH;
                count = count - 1;
            end
            if (wr_ok) begin
                q[tail] = data;
                tail  = (tail + 1) % DEPTH;
                count = count + 1;
            end

            check_flags(label);
        end
    endtask

    initial begin
        // --- async reset
        #1 rst_n = 0;
        #1;
        if (empty !== 1'b1) fail("empty must be high during async reset");
        if (full  !== 1'b0) fail("full must be low during async reset");
        @(negedge clk);
        rst_n = 1;
        count = 0; head = 0; tail = 0; have_rd = 0;

        // --- fill exactly to full
        for (i = 0; i < DEPTH; i = i + 1)
            step(1, 8'hA0 + i[7:0], 0, "filling");
        if (full !== 1'b1) fail("full must be high after 8 writes");

        // --- overflow: writes while full must be ignored, not corrupting
        step(1, 8'hFF, 0, "write while full must be ignored");
        step(1, 8'hFE, 0, "write while full must be ignored");

        // --- drain and verify FIFO ordering survived the overflow attempts
        for (i = 0; i < DEPTH; i = i + 1)
            step(0, 8'h00, 1, "draining in order");
        step(0, 8'h00, 0, "settle after drain");
        if (empty !== 1'b1) fail("empty must be high after draining all entries");

        // --- underflow: reads while empty must be ignored
        step(0, 8'h00, 1, "read while empty must be ignored");
        step(0, 8'h00, 1, "read while empty must be ignored");

        // --- simultaneous read and write, occupancy must hold
        step(1, 8'h11, 0, "seed one entry");
        for (i = 0; i < 12; i = i + 1)
            step(1, 8'h20 + i[7:0], 1, "simultaneous read and write");

        // --- wrap the pointers several times over
        for (i = 0; i < 40; i = i + 1) begin
            step(1, i[7:0], 0, "wrap fill");
            step(0, 8'h00, 1, "wrap drain");
        end

        // --- reset mid-traffic discards contents
        step(1, 8'hC1, 0, "pre-reset write");
        step(1, 8'hC2, 0, "pre-reset write");
        @(negedge clk);
        rst_n = 0;
        #2;
        if (empty !== 1'b1) fail("reset must empty the FIFO immediately");
        @(negedge clk);
        rst_n = 1;
        count = 0; head = 0; tail = 0; have_rd = 0;

        // --- randomised stress against the reference
        for (i = 0; i < 600; i = i + 1)
            step($random, $random, $random, "random traffic");

        // --- sanity: the stimulus actually exercised both extremes
        if (errors == 0)
            $display("TB_PASS");
        else
            $display("TB_FAIL: %0d mismatch(es)", errors);
        $finish;
    end

    initial begin
        #500000;
        $display("TB_FAIL: timeout");
        $finish;
    end
endmodule
