// tb_prbs_cdc — reproduce the framed-PRBS path through the REAL axis_async_fifo
// (8-bit, trace_clk->ref_200m) exactly as trace_capture_a7 wires it, plus the
// la_ddr_writer 16-byte packer, and check the drained byte stream against the
// known framed-PRBS reference. Goal: reproduce the "1 byte in 16 is 2 words
// stale" hardware symptom in simulation so we can see WHICH stage introduces it.
//
// Run: iverilog -g2012 -o /tmp/tb_prbs_cdc.out \
//        tb_prbs_cdc.v ../../../external/verilog-ethernet/lib/axis/rtl/axis_async_fifo.v
//      vvp /tmp/tb_prbs_cdc.out
`timescale 1ns/1ps
`default_nettype none

module tb_prbs_cdc;
    reg trace_clk = 0;
    reg ref_200m  = 0;
    reg rst = 1;

    // 10.2 MHz trace_clk (period ~98 ns) ; 200 MHz ref (5 ns)
    always #49 trace_clk = ~trace_clk;
    always #2.5 ref_200m = ~ref_200m;

    initial begin repeat(8) @(posedge ref_200m); rst = 0; end

    // ---- framed PRBS source in trace_clk (verbatim from trace_capture_a7) ----
    localparam [12:0] BLK_LEN = 13'd8192;
    reg  [12:0] blkpos = 0;
    reg  [31:0] prbs   = 32'h1;
    wire [31:0] prbs_x1  = prbs    ^ (prbs    << 13);
    wire [31:0] prbs_x2  = prbs_x1 ^ (prbs_x1 >> 17);
    wire [31:0] prbs_nxt = prbs_x2 ^ (prbs_x2 << 5);
    reg [7:0] marker;
    always @(*) case (blkpos[2:0])
        3'd0: marker=8'hA5; 3'd1: marker=8'h5A; 3'd2: marker=8'hC3; 3'd3: marker=8'h3C;
        3'd4: marker=8'hF0; 3'd5: marker=8'h0F; 3'd6: marker=8'h99; default: marker=8'h66;
    endcase
    wire in_marker = (blkpos < 13'd8);
    wire [7:0] test_byte = in_marker ? marker : prbs[7:0];
    always @(posedge trace_clk) begin
        if (!rst) begin
            blkpos <= (blkpos==BLK_LEN-1) ? 13'd0 : blkpos+13'd1;
            prbs   <= (blkpos < 13'd8) ? 32'h1 : prbs_nxt;
        end
    end

    reg [7:0] tclk_byte = 0;
    reg       tclk_push = 0;
    always @(posedge trace_clk) begin
        tclk_byte <= test_byte;
        tclk_push <= ~rst;
    end

    // ---- CDC FIFO (verbatim wiring) ----
    wire fifo_s_ready;
    wire [7:0] fifo_m_data;
    wire fifo_m_valid;
    wire fifo_m_ready = fifo_m_valid;
    wire cap_valid = fifo_m_valid & fifo_m_ready;
    wire [7:0] cap_byte = fifo_m_data;

    axis_async_fifo #(.DEPTH(32), .DATA_WIDTH(8),
        .KEEP_ENABLE(0), .LAST_ENABLE(0), .USER_ENABLE(0), .FRAME_FIFO(0)
    ) u_cdc (
        .s_clk(trace_clk), .s_rst(rst),
        .s_axis_tdata(tclk_byte), .s_axis_tkeep(1'b0),
        .s_axis_tvalid(tclk_push), .s_axis_tready(fifo_s_ready),
        .s_axis_tlast(1'b0), .s_axis_tid(8'h0), .s_axis_tdest(8'h0), .s_axis_tuser(1'b0),
        .m_clk(ref_200m), .m_rst(rst),
        .m_axis_tdata(fifo_m_data), .m_axis_tkeep(),
        .m_axis_tvalid(fifo_m_valid), .m_axis_tready(fifo_m_ready),
        .m_axis_tlast(), .m_axis_tid(), .m_axis_tdest(), .m_axis_tuser(),
        .s_pause_req(1'b0), .s_pause_ack(), .m_pause_req(1'b0), .m_pause_ack(),
        .s_status_depth(), .s_status_depth_commit(), .s_status_overflow(),
        .s_status_bad_frame(), .s_status_good_frame(),
        .m_status_depth(), .m_status_depth_commit(), .m_status_overflow(),
        .m_status_bad_frame(), .m_status_good_frame()
    );

    // ---- la_ddr_writer packer (verbatim: {cap_word[119:0], cap_byte}) ----
    reg [127:0] cap_word = 0;
    reg [4:0]   cap_bidx = 0;
    reg         word_valid = 0;
    wire [127:0] cap_word_next = {cap_word[119:0], cap_byte};
`ifdef FIX_LATCH
    // FIX: latch the completed word into a dedicated register on the
    // completing cycle, and present THAT to the consumer. cap_word_full then
    // does not depend on the next (17th) byte that may arrive on the valid
    // cycle.
    reg [127:0] cap_word_latched = 0;
    always @(posedge ref_200m) begin
        word_valid <= 1'b0;
        if (rst) begin cap_bidx <= 0; cap_word <= 0; end
        else if (cap_valid) begin
            cap_word <= cap_word_next;
            if (cap_bidx == 5'd15) begin
                cap_bidx <= 0; word_valid <= 1'b1;
                cap_word_latched <= cap_word_next;   // sampled THIS cycle
            end else cap_bidx <= cap_bidx + 1'b1;
        end
    end
    wire [127:0] cap_word_full = cap_word_latched;
`else
    // ORIGINAL (buggy): combinational full word sampled when the REGISTERED
    // word_valid is high -- one cycle late, so if a 17th byte arrives on that
    // cycle cap_word_next already shifted it in.
    always @(posedge ref_200m) begin
        word_valid <= 1'b0;
        if (rst) begin cap_bidx <= 0; cap_word <= 0; end
        else if (cap_valid) begin
            cap_word <= cap_word_next;
            if (cap_bidx == 5'd15) begin cap_bidx <= 0; word_valid <= 1'b1; end
            else cap_bidx <= cap_bidx + 1'b1;
        end
    end
    wire [127:0] cap_word_full = cap_word_next;
`endif

    // ---- SELF-CHECK: the drained bytes (big-endian within word) must equal a
    // locally-regenerated framed-PRBS reference. This is the lane-skew
    // regression: with the buggy combinational-full-word packer (default here)
    // every 16th byte is 2 words stale; with FIX_LATCH the stream is exact.
    // The RTL that ships (la_ddr_writer) now packs via axis_async_fifo_adapter
    // and is covered end-to-end by tb_la_ddr_ring TEST F; this tb keeps a fast,
    // focused check of the pack-completion timing in isolation.
    //
    // Reference generator: mirror the emit-then-advance framed PRBS byte stream
    // and compare the drained bytes to it. We lock on the first drained byte
    // (which is the marker byte at blkpos 0) so both sides start aligned.
    integer nbytes = 0, errors = 0, wi;
    reg [7:0] b, r_exp;
    // reference state (blocking-updated inside the per-byte loop)
    reg [12:0] r_blkpos = 0;
    reg [31:0] r_prbs = 32'h1;
    reg        r_started = 0;
    reg [63:0] lock_sr = 0;   // rolling last-8-bytes, to lock on the full marker
    localparam [63:0] MARKER8 = 64'hA55A_C33C_F00F_9966;

    function [31:0] xs32(input [31:0] s);
        reg [31:0] a, c;
        begin
            a = s ^ (s << 13);
            c = a ^ (a >> 17);
            xs32 = c ^ (c << 5);
        end
    endfunction
    function [7:0] mk(input [2:0] p);
        case (p)
            3'd0: mk=8'hA5; 3'd1: mk=8'h5A; 3'd2: mk=8'hC3; 3'd3: mk=8'h3C;
            3'd4: mk=8'hF0; 3'd5: mk=8'h0F; 3'd6: mk=8'h99; default: mk=8'h66;
        endcase
    endfunction

    always @(posedge ref_200m) begin
        if (!rst && word_valid) begin
            for (wi = 15; wi >= 0; wi = wi - 1) begin
                b = cap_word_full[wi*8 +: 8];
                if (!r_started) begin
                    // lock on the FULL 8-byte marker (a lone 0xA5 also occurs
                    // in PRBS payload). After the 8th marker byte, the next
                    // byte is payload index 0 (prbs seed low byte).
                    lock_sr = {lock_sr[55:0], b};
                    if (lock_sr == MARKER8) begin
                        r_started = 1;
                        r_blkpos  = 13'd8;   // marker consumed; next is payload
                        r_prbs    = 32'h1;
                    end
                end else begin
                    r_exp = (r_blkpos < 13'd8) ? mk(r_blkpos[2:0]) : r_prbs[7:0];
                    if (b !== r_exp) errors = errors + 1;
                    nbytes = nbytes + 1;
                    r_prbs   = (r_blkpos < 13'd8) ? 32'h1 : xs32(r_prbs);
                    r_blkpos = (r_blkpos==BLK_LEN-1) ? 13'd0 : r_blkpos+13'd1;
                end
            end
        end
    end

    initial begin
        #2_000_000;   // ~2 ms sim -> ~20k trace_clk bytes
        $display("tb_prbs_cdc: checked %0d packed bytes, errors=%0d", nbytes, errors);
        if (nbytes < 4096)
            $display("==== SIM DONE ==== RESULT=FAIL (too few bytes checked)");
        else if (errors != 0)
            $display("==== SIM DONE ==== RESULT=FAIL (%0d byte errors)", errors);
        else
            $display("==== SIM DONE ==== RESULT=ALL_PASS");
        $finish;
    end
endmodule
`default_nettype wire
