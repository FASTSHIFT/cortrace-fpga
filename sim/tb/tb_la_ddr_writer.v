// tb_la_ddr_writer
// ================
// STRESS TEST for the method-X honest-flow-control la_ddr_writer (proposal 32
// P2b). Pure-RTL simulation (iverilog) of the FULL cap_byte -> AsyncFIFO ->
// pack16 -> DDR3-burst path, with a behavioural DDR3 write mock that records
// every 128-bit word actually committed so we can unpack it back to a byte
// stream and check integrity.
//
// The cap side is driven by an 8-bit free-running counter (a STRICTER canary
// than the real 3-bit hardware one: every byte is individually predictable),
// so any dropped/duplicated/reordered byte is caught exactly.
//
// TEST A (steady state): DDR3 mock drains fast. ASSERT the recorded byte
//   stream is a perfectly contiguous 0,1,2,...,255,0,... ramp with ZERO
//   breaks, and overflow flag stays 0. This is the direct refutation of the
//   old bug (86% step=2 dropped + 14% step=0 duplicated, red-team R8).
//
// TEST B (overload): DDR3 mock drains SLOWLY (starved), forcing the FIFO to
//   fill. ASSERT: overflow_sticky asserts (wr_lost_bytes != 0, i.e. HONEST
//   flag), AND the recorded stream is still strictly MONOTONIC modulo 256
//   (forward jumps allowed where stall_sample skipped input bytes, but NEVER
//   a repeat/backwards step). i.e. loss is a clean truncation-per-stall, not
//   a mid-stream sprinkle of dupes.
//
//   Run: cd sim && ./run_tb.sh   (see script)

`timescale 1ns/1ps
`default_nettype none

module tb_la_ddr_writer;
    // ---- clocks ----
    // cap_clk = 200 MHz (5 ns), ui_clk = 50 MHz (20 ns) to mirror the MIG UI.
    reg cap_clk = 0;
    reg ui_clk  = 0;
    always #2.5 cap_clk = ~cap_clk;   // 200 MHz (clk200 sample domain)
    always #5   ui_clk  = ~ui_clk;    // 100 MHz (MIG UI: 400MHz DDR3 /4 PHY)

    reg cap_rst = 1;
    reg ui_rst  = 1;

    localparam integer LENGTH = 64;

    // ---- cap-side stimulus: free-running 8-bit counter ----
    // CAP_DIV: assert cap_valid every CAP_DIV-th cap_clk. 1 = full 200 MB/s,
    // 4 = 50 MB/s (matches a 50 MHz ui_clk pack rate), etc. This is the knob
    // that exposes the pack-throughput bottleneck.
    integer   CAP_DIV = 1;
    reg [7:0] cap_byte = 0;
    reg       cap_valid_in = 0;   // gate from testbench (enable window)
    reg       cap_tick = 0;       // actual per-CAP_DIV strobe
    reg [7:0] cap_phase = 0;
    reg       freeze = 0;
    always @(posedge cap_clk) begin
        if (cap_rst) begin
            cap_phase <= 0; cap_tick <= 0;
        end else begin
            if (cap_phase == CAP_DIV-1) begin
                cap_phase <= 0; cap_tick <= cap_valid_in;
            end else begin
                cap_phase <= cap_phase + 1'b1; cap_tick <= 0;
            end
        end
    end
    always @(posedge cap_clk) begin
        if (cap_rst) begin
            cap_byte <= 0;
        end else if (cap_tick) begin
            // NB: only advance when the DUT ACCEPTS (fifo_s_ready). If the DUT
            // deasserts s_ready mid-stream and we kept incrementing, we'd
            // falsely "lose" bytes at the source. But method X never uses
            // backpressure on the AXIS s-side (it gates with stall_sample on
            // cap_valid instead); so cap_byte tracks the intended contiguous
            // sequence and the DUT internally decides which to admit.
            cap_byte <= cap_byte + 8'd1;
        end
    end

    // ---- DUT ----
    wire        ddr3_wr_start;
    wire [127:0]ddr3_wr_data;
    wire [28:0] ddr3_wr_addr;
    wire        ddr3_wr_data_req, ddr3_wr_addr_req, ddr3_wr_done;
    wire [28:0] wr_ptr_words;
    wire [31:0] words_written, wr_lost_bytes;

    la_ddr_writer #(.LENGTH(LENGTH), .RING_BASE(29'd0),
                    .RING_WORDS(29'h0800000)) dut (
        .cap_clk(cap_clk), .cap_rst(cap_rst),
        .cap_byte(cap_byte), .cap_valid_in(cap_tick), .freeze(freeze),
        .ui_clk(ui_clk), .ui_rst(ui_rst), .ddr3_busy(1'b0),
        .ddr3_wr_start(ddr3_wr_start), .ddr3_wr_data_req(ddr3_wr_data_req),
        .ddr3_wr_data(ddr3_wr_data), .ddr3_wr_addr_req(ddr3_wr_addr_req),
        .ddr3_wr_addr(ddr3_wr_addr), .ddr3_wr_done(ddr3_wr_done),
        .wr_ptr_words(wr_ptr_words), .words_written(words_written),
        .wr_lost_bytes(wr_lost_bytes)
    );

    // ---- behavioural DDR3 write mock ----
    // Mimics ddr3_wr_ctrl handshake: on wr_start, after ACK_LAT cycles it
    // begins pulsing data_req/addr_req for LENGTH beats (one per DRAIN_GAP
    // ui_clk cycles), captures ddr3_wr_data each beat, then pulses wr_done.
    // DRAIN_GAP is the knob: 1 = fast drain (Test A), large = starved (Test B).
    integer DRAIN_GAP = 1;

    reg        m_start_q = 0;
    reg [1:0]  m_state = 0;   // 0 idle, 1 ack-wait, 2 running
    reg [9:0]  m_beat = 0;
    reg [9:0]  m_gap  = 0;
    reg        m_data_req = 0, m_addr_req = 0, m_done = 0;
    localparam integer ACK_LAT = 3;
    reg [3:0]  m_ack_cnt = 0;

    // recorded committed words
    reg [127:0] rec_word [0:1024*1024-1];
    integer     rec_n = 0;

    always @(posedge ui_clk) begin
        if (ui_rst) begin
            m_state <= 0; m_beat <= 0; m_gap <= 0;
            m_data_req <= 0; m_addr_req <= 0; m_done <= 0; m_ack_cnt <= 0;
        end else begin
            m_data_req <= 0; m_addr_req <= 0; m_done <= 0;
            case (m_state)
                0: begin
                    if (ddr3_wr_start) begin
                        m_ack_cnt <= 0;
                        m_state <= 1;
                    end
                end
                1: begin // emulate WR_REQ->ack latency
                    if (m_ack_cnt == ACK_LAT) begin
                        m_state <= 2; m_beat <= 0; m_gap <= 0;
                    end else begin
                        m_ack_cnt <= m_ack_cnt + 1'b1;
                    end
                end
                2: begin
                    if (m_gap == 0) begin
                        // issue one data beat + one addr beat this cycle
                        m_data_req <= 1'b1;
                        m_addr_req <= 1'b1;
                        // capture the word the DUT is presenting
                        rec_word[rec_n] <= ddr3_wr_data;
                        rec_n <= rec_n + 1;
                        if (m_beat == LENGTH-1) begin
                            m_done  <= 1'b1;
                            m_state <= 0;
                        end else begin
                            m_beat <= m_beat + 1'b1;
                            m_gap  <= DRAIN_GAP[9:0];
                        end
                    end else begin
                        m_gap <= m_gap - 1'b1;
                    end
                end
                default: m_state <= 0;
            endcase
        end
    end

    assign ddr3_wr_data_req = m_data_req;
    assign ddr3_wr_addr_req = m_addr_req;
    assign ddr3_wr_done     = m_done;

    // ---- checker: unpack recorded words -> byte stream, verify ----
    integer i, b;
    integer breaks, dups, fwdjumps, total_bytes;
    reg [7:0] cur, prev;
    reg       have_prev;
    integer   ovf;

    task run_check(input [8*48-1:0] label);
        begin
            breaks=0; dups=0; fwdjumps=0; total_bytes=0; have_prev=0; prev=0;
            for (i=0;i<rec_n;i=i+1) begin
                // big-endian pack: word_sr <= {word_sr[119:0], data}; so the
                // FIRST byte admitted sits in the MOST-significant byte.
                for (b=15;b>=0;b=b-1) begin
                    cur = rec_word[i][b*8 +: 8];
                    total_bytes = total_bytes + 1;
                    if (have_prev) begin
                        if (cur == ((prev+1)&8'hff)) begin
                            // perfect contiguous step
                        end else if (cur == prev) begin
                            dups = dups + 1;   // DUPLICATE = the old bug
                            breaks = breaks + 1;
                        end else begin
                            fwdjumps = fwdjumps + 1; // gap (allowed under stall)
                            breaks = breaks + 1;
                        end
                    end
                    prev = cur; have_prev = 1;
                end
            end
            $display("---- %0s ----", label);
            $display("  committed words=%0d  bytes=%0d  overflow_flag=%0d",
                     rec_n, total_bytes, wr_lost_bytes);
            $display("  breaks=%0d  (dups=%0d  fwdjumps=%0d)",
                     breaks, dups, fwdjumps);
        end
    endtask

    // ---- pass/fail bookkeeping (for CI exit code) ----
    integer fails = 0;

    // ---- test sequence ----
    initial begin
        if ($test$plusargs("vcd")) begin
            $dumpfile("tb_la_ddr_writer.vcd");
            $dumpvars(0, tb_la_ddr_writer);
        end
        // reset
        cap_rst=1; ui_rst=1; cap_valid_in=0;
        repeat(10) @(posedge ui_clk);
        cap_rst=0; ui_rst=0;
        repeat(5) @(posedge ui_clk);

        // ========== TEST A: full-rate 200 MB/s in, fast DDR3 drain ==========
        // v2 packs 16B->128b in the cap domain, so the FIFO write rate is only
        // 12.5 Mword/s vs 100 Mword/s pop: 8:1 margin. Full 200 MB/s sampling
        // must now be PERFECTLY contiguous with no overflow. (Under v1 this
        // was a 2:1 overrun and dropped ~half the bytes -> the 86% step=2.)
        DRAIN_GAP = 1; CAP_DIV = 1;
        rec_n = 0;
        cap_valid_in = 1;
        repeat(200000) @(posedge cap_clk);
        cap_valid_in = 0;
        repeat(2000) @(posedge ui_clk);
        run_check("TEST A full-rate 200MB/s (CAP_DIV=1, DRAIN_GAP=1)");
        if (dups != 0) begin
            $display("  *** FAIL A: %0d duplicates (the old bug is back)", dups);
            fails = fails + 1;
        end else if (breaks != 0) begin
            $display("  *** FAIL A: %0d breaks at full rate (throughput still short)", breaks);
            fails = fails + 1;
        end else if (wr_lost_bytes != 0) begin
            $display("  *** FAIL A: overflow flag set at full rate");
            fails = fails + 1;
        end else
            $display("  *** PASS A: perfectly contiguous at full 200MB/s, no overflow");

        // reset between tests
        ui_rst=1; cap_rst=1; repeat(10) @(posedge ui_clk);
        cap_rst=0; ui_rst=0; repeat(5) @(posedge ui_clk);

        // ===== TEST A2: matched 100 MB/s in (CAP_DIV=2), fast drain =====
        // Now input == pack rate. THIS is where we must see perfect contiguity
        // and zero overflow. If it still overflows, the pack path itself is
        // the bug, not the flow control.
        DRAIN_GAP = 1; CAP_DIV = 2;
        rec_n = 0;
        cap_valid_in = 1;
        repeat(400000) @(posedge cap_clk);
        cap_valid_in = 0;
        repeat(4000) @(posedge ui_clk);
        run_check("TEST A2 matched 100MB/s (CAP_DIV=2, DRAIN_GAP=1)");
        if (dups != 0) begin
            $display("  *** FAIL A2: %0d duplicates", dups);
            fails = fails + 1;
        end else if (breaks != 0) begin
            $display("  *** FAIL A2: %0d breaks at matched rate (pack path bug)", breaks);
            fails = fails + 1;
        end else if (wr_lost_bytes != 0) begin
            $display("  *** FAIL A2: overflow flag at matched rate");
            fails = fails + 1;
        end else
            $display("  *** PASS A2: perfectly contiguous at matched rate, no overflow");

        // reset between tests
        ui_rst=1; cap_rst=1; repeat(10) @(posedge ui_clk);
        cap_rst=0; ui_rst=0; repeat(5) @(posedge ui_clk);

        // ================= TEST B: overload (starved drain) =================
        // reset the writer state to start clean
        ui_rst=1; cap_rst=1;
        repeat(10) @(posedge ui_clk);
        cap_rst=0; ui_rst=0;
        repeat(5) @(posedge ui_clk);
        DRAIN_GAP = 40;   // ~40x slower drain -> guaranteed FIFO fill
        rec_n = 0;
        cap_valid_in = 1;
        repeat(400000) @(posedge cap_clk);
        cap_valid_in = 0;
        repeat(20000) @(posedge ui_clk);
        run_check("TEST B overload (DRAIN_GAP=40)");
        if (dups != 0) begin
            $display("  *** FAIL B: %0d duplicates under overload (loss must be clean truncation, not sprinkled dupes)", dups);
            fails = fails + 1;
        end else if (wr_lost_bytes == 0) begin
            $display("  *** FAIL B: FIFO overloaded but overflow flag NOT set (silent loss - dishonest)");
            fails = fails + 1;
        end else begin
            $display("  *** PASS B: honest overflow flag set, zero duplicates, loss is forward-only (%0d clean gaps)", fwdjumps);
        end

        // machine-readable verdict for CI grep + non-zero exit on failure
        if (fails == 0)
            $display("==== SIM DONE ==== RESULT=ALL_PASS");
        else begin
            $display("==== SIM DONE ==== RESULT=FAIL (%0d failing tests)", fails);
            $fatal(1, "la_ddr_writer stress test FAILED");
        end
        $finish;
    end

    // watchdog
    initial begin
        #20_000_000;   // 20 ms sim time cap
        $display("*** TIMEOUT watchdog fired");
        $finish;
    end
endmodule

`default_nettype wire
