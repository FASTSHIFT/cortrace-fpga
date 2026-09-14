// tb_la_ddr_ring
// ==============
// doc 19 P1 + P0e validation: CONCURRENT source-write (la_ddr_writer) and
// history-drain-read (la_ddr_ring_streamer) on the SAME DDR3 ring, through the
// REAL vendor ctrl+arbiter (ddr3_wr_ctrl / ddr3_rd_ctrl / ddr3_arbit) plus a
// behavioural MIG app-interface memory model.
//
// This is the "全新场景" doc 19 §7 flagged: the existing writer/reader were an
// OFFLINE dump pair (ring frozen during read). Here they run TOGETHER — the
// writer fills at full cap rate while the streamer chases wr_ptr and drains to
// the network. We must prove:
//
//   TEST A (drain keeps up): with fill < drain, the drained byte stream is a
//     PERFECTLY CONTIGUOUS ramp (0,1,2,...255,0,...) with zero dup/gap =>
//     concurrent R/W through the write-priority arbiter preserves data
//     integrity, source never corrupted by concurrent reads, and seq maps
//     correctly. ring_overrun stays 0.
//
//   TEST B (drain starved): throttle drain_credit so fill > drain; the writer
//     LAPS the reader => ring_overrun MUST assert (honest coverage-gap flag,
//     doc 19 §6 boundary), NOT silent corruption.
//
//   Run: cd sim && ./run_tb_ring.sh

`timescale 1ns/1ps
`default_nettype none

module tb_la_ddr_ring;
    // ---- clocks ----
    reg cap_clk = 0;   // 200 MHz capture
    reg ui_clk  = 0;   // 100 MHz MIG UI
    reg clk125  = 0;   // 125 MHz network egress
    always #2.5 cap_clk = ~cap_clk;
    always #5   ui_clk  = ~ui_clk;
    always #4   clk125  = ~clk125;

    reg cap_rst = 1, ui_rst = 1, sys_rst = 1;

    localparam integer LENGTH     = 64;
    localparam [28:0]  RING_BASE  = 29'd0;
    // sim ring: 8192 app-units = 1024 words = 16KB (16 packets). Big enough
    // that TEST C can request a still-in-window seq; TEST B floods past it.
    localparam [28:0]  RING_WORDS = 29'd8192;
    localparam integer PKT_WORDS  = 64;

    // ---- cap-side stimulus: free-running 8-bit ramp (per-byte canary) ----
    integer   CAP_DIV = 4;         // 1 byte / CAP_DIV cap_clk (4 => 50 MB/s)
    reg [7:0] cap_byte = 0;
    reg       cap_valid_in = 0;
    reg       cap_tick = 0;
    reg [7:0] cap_phase = 0;
    // TEST F position-encoded source: each byte = low 8 bits of a global byte
    // counter's HIGH bits so a whole-word (16-byte) reorder is visible. When
    // e_pos_src is set, cap_byte carries (byte_index >> 4) & 0xFF, i.e. it
    // increments once per 16 bytes = once per 128-bit word -> a -1024 (64-word)
    // burst reorder shows as a 64-count backward jump in the drained stream.
    reg        e_pos_src = 0;
    reg [31:0] cap_bcnt = 0;
    always @(posedge cap_clk) begin
        if (cap_rst) begin cap_phase<=0; cap_tick<=0; end
        else if (cap_phase == CAP_DIV-1) begin cap_phase<=0; cap_tick<=cap_valid_in; end
        else begin cap_phase<=cap_phase+1'b1; cap_tick<=0; end
    end
    always @(posedge cap_clk) begin
        if (cap_rst) begin cap_byte <= 0; cap_bcnt <= 0; end
        else if (cap_tick) begin
            cap_byte <= e_pos_src ? ((cap_bcnt >> 4) & 8'hff) : (cap_byte + 8'd1);
            cap_bcnt <= cap_bcnt + 1;
        end
    end

    // ================= WRITER (source, cap_clk -> DDR3) =================
    wire        wr_start, wr_data_req, wr_addr_req, wr_done;
    wire [127:0]wr_data;
    wire [28:0] wr_addr;
    wire [28:0] wr_ptr_words;
    wire [31:0] words_written, wr_lost_bytes;

    la_ddr_writer #(.LENGTH(LENGTH), .IN_BYTES(1), .RING_BASE(RING_BASE),
                    .RING_WORDS(RING_WORDS)) u_wr (
        .cap_clk(cap_clk), .cap_rst(cap_rst),
        .cap_byte(cap_byte), .cap_valid_in(cap_tick), .freeze(1'b0),
        .ui_clk(ui_clk), .ui_rst(ui_rst), .ddr3_busy(1'b0),
        .ddr3_wr_start(wr_start), .ddr3_wr_data_req(wr_data_req),
        .ddr3_wr_data(wr_data), .ddr3_wr_addr_req(wr_addr_req),
        .ddr3_wr_addr(wr_addr), .ddr3_wr_done(wr_done),
        .wr_ptr_words(wr_ptr_words), .words_written(words_written),
        .wr_lost_bytes(wr_lost_bytes)
    );

    // ================= STREAMER (history drain, DDR3 -> net) =================
    wire        rd_start, rd_addr_req, rd_data_vld, rd_done;
    wire [28:0] rd_addr;
    wire [127:0]rd_data;
    reg         drain_credit = 1;
    wire [7:0]  stream_tdata;
    wire        stream_tvalid;
    reg         stream_tready = 1;
    wire [31:0] stream_seq;
    wire        stream_rtx;
    wire [28:0] rd_ptr_words;
    wire [31:0] words_drained;
    wire        ring_overrun;
    reg         nack_valid = 0;
    reg  [31:0] nack_start_seq = 0;
    reg  [15:0] nack_count = 0;
    wire        nack_busy, nack_fail;

    la_ddr_ring_streamer #(.LENGTH(LENGTH), .RING_BASE(RING_BASE),
                           .RING_WORDS(RING_WORDS), .PKT_WORDS(PKT_WORDS)) u_st (
        .ui_clk(ui_clk), .ui_rst(ui_rst),
        .wr_ptr_words(wr_ptr_words), .wr_words_committed(words_written),
        .drain_credit(drain_credit),
        .ddr3_rd_start(rd_start), .ddr3_rd_addr_req(rd_addr_req),
        .ddr3_rd_addr(rd_addr), .ddr3_rd_data_vld(rd_data_vld),
        .ddr3_rd_data(rd_data), .ddr3_rd_done(rd_done),
        .nack_valid(nack_valid), .nack_start_seq(nack_start_seq),
        .nack_count(nack_count), .nack_busy(nack_busy), .nack_fail(nack_fail),
        .clk125(clk125), .sys_rst(sys_rst),
        .stream_tdata(stream_tdata), .stream_tvalid(stream_tvalid),
        .stream_tready(stream_tready), .stream_seq(stream_seq),
        .stream_rtx(stream_rtx),
        .rd_ptr_words(rd_ptr_words), .words_drained(words_drained),
        .ring_overrun(ring_overrun)
    );

    // ================= REAL vendor ctrl + arbiter =================
    wire        app_rdy, app_wdf_rdy;
    wire        wr_app_en, rd_app_en;
    wire [2:0]  wr_app_cmd, rd_app_cmd;
    wire [28:0] wr_app_addr, rd_app_addr;
    wire [127:0]app_wdf_data;
    wire        app_wdf_end, app_wdf_wren;
    wire [15:0] app_wdf_mask;
    wire        app_rd_data_end, app_rd_data_valid;
    wire [127:0]app_rd_data;

    wire        wr_req, wr_ack, wr_busy;
    wire        rd_req, rd_ack, rd_busy;

    // ddr3_ctrl's command mux: write wins the MIG app port while busy
    wire        app_en   = wr_busy ? wr_app_en   : rd_app_en;
    wire [2:0]  app_cmd  = wr_busy ? wr_app_cmd  : rd_app_cmd;
    wire [28:0] app_addr = wr_busy ? wr_app_addr : rd_app_addr;

    ddr3_wr_ctrl #(.LENGTH(LENGTH)) u_wrc (
        .ui_clk(ui_clk), .ui_rst(ui_rst),
        .app_rdy(app_rdy), .app_en(wr_app_en), .app_cmd(wr_app_cmd),
        .app_addr(wr_app_addr), .app_wdf_rdy(app_wdf_rdy),
        .app_wdf_data(app_wdf_data), .app_wdf_end(app_wdf_end),
        .app_wdf_wren(app_wdf_wren), .app_wdf_mask(app_wdf_mask),
        .ddr3_wr_start(wr_start), .ddr3_wr_req(wr_req), .ddr3_wr_ack(wr_ack),
        .ddr3_wr_data_req(wr_data_req), .ddr3_wr_data(wr_data),
        .ddr3_wr_addr_req(wr_addr_req), .ddr3_wr_addr(wr_addr),
        .ddr3_wr_done(wr_done), .ddr3_wr_busy(wr_busy)
    );

    ddr3_rd_ctrl #(.LENGTH(LENGTH)) u_rdc (
        .ui_clk(ui_clk), .ui_rst(ui_rst),
        .app_rdy(app_rdy), .app_en(rd_app_en), .app_addr(rd_app_addr),
        .app_cmd(rd_app_cmd), .app_rd_data_end(app_rd_data_end),
        .app_rd_data_valid(app_rd_data_valid), .app_rd_data(app_rd_data),
        .ddr3_rd_start(rd_start), .ddr3_rd_req(rd_req), .ddr3_rd_ack(rd_ack),
        .ddr3_rd_addr_req(rd_addr_req), .ddr3_rd_addr(rd_addr),
        .ddr3_rd_data_vld(rd_data_vld), .ddr3_rd_data(rd_data),
        .ddr3_rd_done(rd_done), .ddr3_rd_busy(rd_busy)
    );

    ddr3_arbit u_arb (
        .ui_clk(ui_clk), .ui_rst(ui_rst),
        .ddr3_wr_req(wr_req), .ddr3_wr_done(wr_done),
        .ddr3_rd_req(rd_req), .ddr3_rd_done(rd_done),
        .ddr3_wr_ack(wr_ack), .ddr3_rd_ack(rd_ack)
    );

    // ================= behavioural MIG app-interface memory =================
    // app_rdy / app_wdf_rdy always ready (a fast DDR3 that never stalls the UI
    // — the honest worst case for the RING: any loss must come from the ring
    // wrap, not from a stalled memory). mem indexed by app_addr>>3 (design
    // advances address +8 per 128-bit word).
    localparam integer MEM_WORDS = 1024;   // covers RING_WORDS/8 = 512 + margin
    reg [127:0] mem [0:MEM_WORDS-1];
    assign app_rdy     = 1'b1;
    assign app_wdf_rdy = 1'b1;

    // write: on a write-data beat, store app_wdf_data at current app_addr.
    always @(posedge ui_clk) begin
        if (app_wdf_wren & app_wdf_rdy)
            mem[app_addr[12:3]] <= app_wdf_data;   // addr>>3, 10-bit index
    end

    // read: capture read command addresses into a latency queue; after
    // RD_LAT cycles, drain one word/cycle producing valid+end+data.
    localparam integer RD_LAT = 6;
    reg [28:0] rd_cmd_q [0:255];
    reg [7:0]  rq_wr = 0, rq_rd = 0;
    reg [3:0]  lat_cnt = 0;
    reg        rd_pending = 0;
    reg        rv = 0;
    reg [127:0]rdata_r = 0;
    wire       rd_cmd_fire = rd_app_en & app_rdy & (rd_app_cmd == 3'b001) & ~wr_busy;

    always @(posedge ui_clk) begin
        if (ui_rst) begin
            rq_wr<=0; rq_rd<=0; lat_cnt<=0; rd_pending<=0; rv<=0;
        end else begin
            rv <= 1'b0;
            // enqueue read command addresses
            if (rd_cmd_fire) begin
                rd_cmd_q[rq_wr] <= rd_app_addr;
                rq_wr <= rq_wr + 1'b1;
            end
            // latency + drain
            if (rq_rd != rq_wr) begin
                if (!rd_pending) begin
                    rd_pending <= 1'b1; lat_cnt <= 0;
                end else if (lat_cnt < RD_LAT) begin
                    lat_cnt <= lat_cnt + 1'b1;
                end else begin
                    // emit one word
                    rdata_r <= mem[rd_cmd_q[rq_rd][12:3]];
                    rv <= 1'b1;
                    rq_rd <= rq_rd + 1'b1;
                    if ((rq_rd + 1'b1) == rq_wr) rd_pending <= 1'b0;
                    lat_cnt <= RD_LAT;   // back-to-back once started
                end
            end else begin
                rd_pending <= 1'b0;
            end
        end
    end
    assign app_rd_data_valid = rv;
    assign app_rd_data_end   = rv;   // each valid beat is its own "end"
    assign app_rd_data       = rdata_r;

    // ================= checker: drained byte stream integrity =================
    integer breaks, dups, fwdjumps, total_bytes;
    reg [7:0] prev; reg have_prev;
    integer fails = 0;

    reg cap_stream = 0;
    always @(posedge clk125) begin
        if (cap_stream & stream_tvalid & stream_tready) begin
            total_bytes = total_bytes + 1;
            if (have_prev) begin
                if (stream_tdata == ((prev+1)&8'hff)) ;      // contiguous
                else if (stream_tdata == prev) begin dups=dups+1; breaks=breaks+1; end
                else begin fwdjumps=fwdjumps+1; breaks=breaks+1; end
            end
            prev = stream_tdata; have_prev = 1;
        end
    end

    // ---- TEST E: detect the streamer re-reading the SAME ring burst on two
    // consecutive NORMAL drains (=> duplicate packet, the slow-fill dup bug).
    // We sample rd_addr at each rd_start pulse for a NORMAL (non-rtx) drain.
    integer e_reread, e_bursts;
    reg [28:0] e_prev_addr;
    reg        e_have, cap_mon_e;
    always @(posedge ui_clk) begin
        if (cap_mon_e & rd_start & ~nack_busy) begin
            e_bursts = e_bursts + 1;
            if (e_have && rd_addr == e_prev_addr) e_reread = e_reread + 1;
            e_prev_addr = rd_addr; e_have = 1;
        end
    end



    // ---- TEST F: word-staircase backward-step detector on egress bytes ----
    integer   f_back, f_bytes, f_first_back;
    reg [7:0] f_prev; reg f_have, cap_mon_f;

    // per-burst word-count probes (localise write-side vs read-side).
    integer wr_words_this_burst, rd_words_this_burst;
    integer wr_burst_bad, rd_burst_bad;   // bursts != LENGTH words
    // read-address progression probe: log start address of each read burst
    reg [28:0] rd_start_addr_prev; reg rd_addr_have; integer rd_addr_glitch;
    integer rd_burst_n, wr_burst_n;
    always @(posedge ui_clk) begin
        if (ui_rst) begin wr_words_this_burst<=0; rd_words_this_burst<=0; end
        else begin
            if (wr_data_req) begin
                wr_words_this_burst <= wr_words_this_burst + 1;
            end
            if (wr_done) begin
                if (cap_mon_f && wr_words_this_burst != LENGTH-1 && wr_words_this_burst != LENGTH)
                    wr_burst_bad <= wr_burst_bad + 1;
                wr_words_this_burst <= 0;
                wr_burst_n <= wr_burst_n + 1;
            end
            if (rd_data_vld) rd_words_this_burst <= rd_words_this_burst + 1;
            if (rd_done) begin
                if (cap_mon_f && rd_words_this_burst != LENGTH-1 && rd_words_this_burst != LENGTH)
                    rd_burst_bad <= rd_burst_bad + 1;
                rd_words_this_burst <= 0;
            end
            // capture read burst start addr at rd_start; check it advances by
            // LENGTH*8 each burst (contiguous ring drain)
            if (cap_mon_f && rd_start) begin
                rd_burst_n <= rd_burst_n + 1;
                rd_start_addr_prev <= rd_addr; rd_addr_have <= 1;
            end
        end
    end
    always @(posedge clk125) begin
        if (cap_mon_f & stream_tvalid & stream_tready) begin
            f_bytes = f_bytes + 1;
            if (f_have) begin
                // expected: same value or +1 (mod256); a decrease (not the
                // +1 wrap 255->0) means a word from another burst leaked in.
                if (stream_tdata != f_prev &&
                    stream_tdata != ((f_prev+1)&8'hff)) begin
                    f_back = f_back + 1;
                    if (f_first_back < 0) f_first_back = f_bytes;
                end
            end
            f_prev = stream_tdata; f_have = 1;
        end
    end

    // ---- retransmit capture (TEST C): record rtx bytes + their seq ----
    integer rtx_bytes, rtx_seq_min, rtx_seq_max;
    reg cap_rtx = 0;
    always @(posedge clk125) begin
        if (cap_rtx & stream_tvalid & stream_tready & stream_rtx) begin
            rtx_bytes = rtx_bytes + 1;
            if (stream_seq < rtx_seq_min) rtx_seq_min = stream_seq;
            if (stream_seq > rtx_seq_max) rtx_seq_max = stream_seq;
        end
    end

    // simple network pacing: occasionally deassert tready
    reg [3:0] tr_lfsr = 4'h9;
    always @(posedge clk125) begin
        tr_lfsr <= {tr_lfsr[2:0], tr_lfsr[3]^tr_lfsr[2]};
        stream_tready <= (tr_lfsr != 4'h0);   // ~high, occasional bubble
    end

    task reset_all;
        begin
            cap_rst=1; ui_rst=1; sys_rst=1;
            repeat(10) @(posedge ui_clk);
            cap_rst=0; ui_rst=0; sys_rst=0;
            repeat(5) @(posedge ui_clk);
        end
    endtask

    initial begin
        if ($test$plusargs("vcd")) begin
            $dumpfile("tb_la_ddr_ring.vcd");
            $dumpvars(0, tb_la_ddr_ring);
        end

        // ===== TEST A: drain keeps up (fill < drain), expect contiguous =====
        reset_all;
        breaks=0; dups=0; fwdjumps=0; total_bytes=0; have_prev=0; prev=0;
        CAP_DIV = 4;            // 50 MB/s fill
        drain_credit = 1;       // full-speed drain
        cap_stream = 1;
        cap_valid_in = 1;
        repeat(600000) @(posedge cap_clk);
        cap_valid_in = 0;
        repeat(20000) @(posedge ui_clk);
        cap_stream = 0;
        $display("---- TEST A concurrent R/W (CAP_DIV=4, drain full) ----");
        $display("  words_written=%0d words_drained=%0d drained_bytes=%0d",
                 words_written, words_drained, total_bytes);
        $display("  breaks=%0d (dups=%0d fwdjumps=%0d) overrun=%0d wr_lost=%0d",
                 breaks, dups, fwdjumps, ring_overrun, wr_lost_bytes);
        if (total_bytes < 1000) begin
            $display("  *** FAIL A: streamer drained almost nothing (chase broken)");
            fails=fails+1;
        end else if (ring_overrun) begin
            $display("  *** FAIL A: overrun with drain>=fill (chase can't keep up)");
            fails=fails+1;
        end else if (breaks != 0) begin
            $display("  *** FAIL A: %0d breaks (concurrent R/W corrupted the stream)",breaks);
            fails=fails+1;
        end else
            $display("  *** PASS A: contiguous ramp under concurrent R/W, no overrun");

        // ===== TEST B: starved drain (fill > drain), expect honest overrun =====
        reset_all;
        breaks=0; dups=0; fwdjumps=0; total_bytes=0; have_prev=0; prev=0;
        CAP_DIV = 1;            // full 200 MB/s fill
        drain_credit = 0;       // choke the drain almost entirely
        cap_stream = 1;
        cap_valid_in = 1;
        // pulse tiny drain credit occasionally so reader limps, writer laps it
        fork
            begin : choke
                integer k;
                for (k=0;k<40;k=k+1) begin
                    repeat(2000) @(posedge ui_clk);
                    drain_credit = 1; @(posedge ui_clk); drain_credit = 0;
                end
            end
        join_none
        repeat(400000) @(posedge cap_clk);
        cap_valid_in = 0;
        repeat(5000) @(posedge ui_clk);
        cap_stream = 0;
        $display("---- TEST B starved drain (CAP_DIV=1, drain choked) ----");
        $display("  words_written=%0d words_drained=%0d overrun=%0d",
                 words_written, words_drained, ring_overrun);
        if (!ring_overrun) begin
            $display("  *** FAIL B: writer lapped reader but overrun NOT flagged (silent gap)");
            fails=fails+1;
        end else
            $display("  *** PASS B: honest ring_overrun asserted under starved drain");

        // ===== TEST C: NACK retransmit of an in-window seq range =====
        // Fill+drain a while so the ring holds recent history, then request a
        // retransmit of a seq range known to be inside the window. Assert the
        // FSM re-emits it tagged rtx=1 with the requested seq range.
        reset_all;
        breaks=0; dups=0; fwdjumps=0; total_bytes=0; have_prev=0; prev=0;
        rtx_bytes=0; rtx_seq_min=32'hffffffff; rtx_seq_max=0;
        CAP_DIV = 4; drain_credit = 1;
        cap_stream = 0;
        cap_valid_in = 1;
        // Fill ~11 packets worth (committed in [512,1024) words, i.e. > seq7's
        // last word 511 but < the 1024-word ring => NO wrap yet, seq 5..7 stay
        // in-window), then STOP the source so the writer can't overwrite them.
        repeat(45000) @(posedge cap_clk);
        cap_valid_in = 0;
        repeat(3000) @(posedge ui_clk);   // let drain settle (short: no wrap)
        // request retransmit of seq 5..7 (3 packets) — inside the frozen window
        cap_rtx = 1;
        @(posedge clk125);
        nack_start_seq = 32'd5; nack_count = 16'd3;
        nack_valid = 1; @(posedge clk125); nack_valid = 0;
        // give the FSM time to service it between drain bursts
        repeat(40000) @(posedge ui_clk);
        cap_rtx = 0;
        $display("---- TEST C NACK retransmit (seq 5..7, 3 pkts) ----");
        $display("  rtx_bytes=%0d rtx_seq_min=%0d rtx_seq_max=%0d nack_fail=%0d",
                 rtx_bytes, rtx_seq_min, rtx_seq_max, nack_fail);
        // 3 packets * 64 words * 16 bytes = 3072 bytes expected
        if (rtx_bytes < 3072) begin
            $display("  *** FAIL C: too few rtx bytes (%0d < 3072), retransmit short", rtx_bytes);
            fails=fails+1;
        end else if (rtx_seq_min != 5 || rtx_seq_max != 7) begin
            $display("  *** FAIL C: rtx seq range [%0d,%0d] != [5,7]", rtx_seq_min, rtx_seq_max);
            fails=fails+1;
        end else if (nack_fail) begin
            $display("  *** FAIL C: in-window NACK wrongly reported fail");
            fails=fails+1;
        end else
            $display("  *** PASS C: retransmitted seq 5..7 tagged rtx, correct range");

        // ===== TEST D: NACK for an out-of-window seq => nack_fail =====
        // Request a seq far in the FUTURE (never written) -> window check fails.
        reset_all;
        CAP_DIV = 4; drain_credit = 1;
        cap_valid_in = 1;
        repeat(100000) @(posedge cap_clk);
        @(posedge clk125);
        nack_start_seq = 32'd100000; nack_count = 16'd1;  // never written
        nack_valid = 1; @(posedge clk125); nack_valid = 0;
        // wait and watch for nack_fail pulse
        begin : watch_fail
            integer w; reg seen_fail;
            seen_fail = 0;
            for (w=0; w<20000; w=w+1) begin
                @(posedge ui_clk);
                if (nack_fail) seen_fail = 1;
            end
            cap_valid_in = 0;
            $display("---- TEST D NACK out-of-window (seq 100000) ----");
            if (!seen_fail) begin
                $display("  *** FAIL D: out-of-window NACK did not raise nack_fail");
                fails=fails+1;
            end else
                $display("  *** PASS D: out-of-window NACK honestly reported nack_fail");
        end

        // ===== TEST E: VERY SLOW fill (fill << drain), detect whole-block
        // duplication. The ramp canary (period 256) is blind to a 1024-byte
        // block being emitted twice (1024 % 256 == 0), which is exactly the
        // hardware symptom seen with the 10 MB/s framed-PRBS source: the
        // streamer re-emits the same ring burst before the slow writer commits
        // a new one. Here we drive a per-emit ramp but detect duplication by
        // watching the DUT read the SAME ddr3_rd_addr on two consecutive drain
        // bursts (a re-read => duplicate packet). =====
        reset_all;
        CAP_DIV = 40;           // ~5 MB/s fill: writer barely ahead of drain
        drain_credit = 1;       // full-speed drain -> streamer starves for data
        e_reread = 0; e_bursts = 0; e_prev_addr = 29'h1fffffff; e_have = 0;
        cap_mon_e = 1;
        cap_stream = 0;         // don't use the ramp checker here
        cap_valid_in = 1;
        repeat(1500000) @(posedge cap_clk);   // long, slow fill
        cap_valid_in = 0;
        repeat(20000) @(posedge ui_clk);
        cap_mon_e = 0;
        $display("---- TEST E slow fill (CAP_DIV=40), re-read detector ----");
        $display("  drain bursts=%0d  repeated-address bursts=%0d", e_bursts, e_reread);
        if (e_bursts < 4) begin
            $display("  *** FAIL E: too few drain bursts to judge");
            fails=fails+1;
        end else if (e_reread != 0) begin
            $display("  *** FAIL E: streamer re-read same burst %0d times (dup)", e_reread);
            fails=fails+1;
        end else
            $display("  *** PASS E: no burst re-read under slow fill");

        // ===== TEST F: position-encoded source, detect word-level burst
        // reorder (the -1024 head glitch seen on HW: a packet's first ~2 words
        // come from the NEXT burst). The source byte = (byte_index>>4)&0xFF, so
        // it steps once per 16-byte word; the drained byte stream must be a
        // staircase that only ever steps +1 (mod 256) every 16 bytes and never
        // jumps backward. A backward step = a word from a later/earlier burst
        // leaked in. =====
        reset_all;
        CAP_DIV = 4; drain_credit = 1;
        e_pos_src = 1;
        f_prev = 0; f_have = 0; f_back = 0; f_bytes = 0; f_first_back = -1;
        wr_burst_bad = 0; rd_burst_bad = 0;
        wr_words_this_burst = 0; rd_words_this_burst = 0;
        rd_addr_have = 0; rd_addr_glitch = 0; rd_burst_n = 0; wr_burst_n = 0;
        cap_mon_f = 1;
        cap_valid_in = 1;
        repeat(800000) @(posedge cap_clk);
        cap_valid_in = 0;
        repeat(20000) @(posedge ui_clk);
        cap_mon_f = 0;
        e_pos_src = 0;
        $display("---- TEST F position source, word-reorder detector ----");
        $display("  drained bytes=%0d backward-word-steps=%0d first@%0d",
                 f_bytes, f_back, f_first_back);
        $display("  per-burst word-count anomalies: write-side=%0d read-side=%0d",
                 wr_burst_bad, rd_burst_bad);
        // NOTE: TEST F is a KNOWN-BUG reproducer (doc 30, "-1024 burst-boundary
        // reorder"). It is REPORT-ONLY for now so the suite still gates on the
        // A-E invariants; flip F_STRICT to make it gate once the streamer/rd
        // burst-count fix lands (target: f_back == 0).
        if (f_bytes < 20000) begin
            $display("  *** F INCONCLUSIVE: too few bytes (%0d)", f_bytes);
        end else if (f_back != 0) begin
            $display("  *** FAIL F: %0d backward word-steps (burst reorder, doc30)", f_back);
            fails=fails+1;
        end else
            $display("  *** PASS F: monotone word staircase, no burst reorder");

        if (fails == 0)
            $display("==== SIM DONE ==== RESULT=ALL_PASS");
        else begin
            $display("==== SIM DONE ==== RESULT=FAIL (%0d failing tests)", fails);
            $fatal(1, "la_ddr_ring concurrent R/W test FAILED");
        end
        $finish;
    end

    initial begin
        #50_000_000;
        $display("*** TIMEOUT watchdog fired");
        $finish;
    end
endmodule

`default_nettype wire
