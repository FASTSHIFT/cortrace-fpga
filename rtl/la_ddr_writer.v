// la_ddr_writer
// =============
// Proposal 32 P2b (method X): on-chip logic-analyzer BLACK BOX writer. Takes
// the raw trace bytes tapped DIRECTLY at the capture source (trace_capture_
// direct.cap_byte, in the TRACECLK domain) and records them into DDR3 through
// an INDEPENDENT AsyncFIFO — separate from the real-time UDP path — so the
// black box is an untainted ground-truth copy for decoder cross-check.
//
//   cap_byte/cap_valid (TRACECLK)                          ui_clk (50MHz, MIG)
//        │                                                      │
//        └─► AsyncFIFO (8-bit, TRACECLK→ui_clk) ─► pack 16 bytes ─► 128-bit
//            words ─► on every 64 words (=1KB), issue one ddr3_wr_start burst
//            to a ring address; wrap at RING_WORDS.
//
// Byte packing: cap_byte[k] is placed big-endian into the 128-bit word, byte 0
// = MS byte, so the host reads back a contiguous byte stream identical to the
// tapped order. Loss accounting: if the AsyncFIFO fills (DDR3 write can't keep
// up — should never happen at 12.8MB/s in vs 800MB/s DDR3, but we count it),
// wr_lost increments.
//
// This module does NOT read back; la_ddr_reader (P2b-2) streams the ring out
// over :5556. Here we expose write pointer + byte count + overflow for :5001.

`default_nettype none

module la_ddr_writer #(
    parameter integer LENGTH     = 64,          // 128-bit words per DDR3 burst
                                                // (must match ddr3_wr_ctrl)
    parameter integer IN_BYTES   = 1,           // bytes accepted per cap_clk:
                                                // 1 = single-edge 200 MSPS,
                                                // 2 = IDDR dual-edge 400 MSPS
    parameter [28:0]  RING_BASE  = 29'd0,       // ring start (app word address)
    parameter [28:0]  RING_WORDS = 29'd0100000  // ring size in 128-bit words
                                                // (0x100000 = 1M words = 16MB)
) (
    // ---- capture-source side (TRACECLK domain) ----
    input  wire        cap_clk,
    input  wire        cap_rst,
    input  wire [IN_BYTES*8-1:0] cap_byte,      // IN_BYTES samples, byte 0 =
                                                // OLDEST (goes to MS end first)
    input  wire        cap_valid_in,    // raw capture valid
    input  wire        freeze,          // when 1, stop accepting new bytes
                                        // (clk125-domain level; static ring for
                                        // a clean readback snapshot)

    // ---- DDR3 side (ui_clk domain), drives ddr3_ctrl write interface ----
    input  wire        ui_clk,
    input  wire        ui_rst,
    input  wire        ddr3_busy,       // arbiter busy (from ddr3_ctrl)
    output reg         ddr3_wr_start,
    input  wire        ddr3_wr_data_req,
    output wire [127:0]ddr3_wr_data,
    input  wire        ddr3_wr_addr_req,
    output reg  [28:0] ddr3_wr_addr,
    input  wire        ddr3_wr_done,

    // ---- status (ui_clk domain) ----
    output reg  [28:0] wr_ptr_words,    // current ring write pointer (words)
    output reg  [31:0] words_written,   // total 128-bit words committed
    output reg  [31:0] wr_lost_bytes    // bytes dropped (AsyncFIFO full)
);
    // ------------- AsyncFIFO: cap_clk 128-bit -> ui_clk 128-bit -------------
    // METHOD X v2 (litescope/OLS-style, WIDTH-MATCHED): the fatal flaw of v1
    // was a THROUGHPUT mismatch, not a flow-control bug. v1 fed the async FIFO
    // 8 bits/cap_clk (200 MB/s) but the ui_clk pack path could only pop 1
    // byte/ui_clk = 100 MB/s (MIG UI = 400MHz DDR3 / 4:1 PHY). A 2:1 permanent
    // overrun => ~half the bytes dropped => the "86% step=2" the canary saw.
    // Simulation (tb_la_ddr_writer) proved this: at matched 100 MB/s v1 still
    // scattered gaps because the pack path ALSO froze during each DDR3 burst.
    //
    // v2 packs 16 bytes into a 128-bit word IN THE cap_clk DOMAIN, then pushes
    // whole words through a 128-bit-wide async FIFO. Now:
    //   * FIFO write rate = 200M/16 = 12.5 Mword/s
    //   * FIFO read rate  = up to 100 Mword/s (one word/ui_clk)
    // an 8:1 drain margin, so steady state NEVER overflows. Loss can only
    // happen on a genuine DDR3 stall, and then it is flagged (sticky) and the
    // byte stream stays contiguous up to the stall point (canary monotonic).
    // Bonus: no per-byte stall gating on the FIFO write port, so the DRC
    // REQP-1839 async-control-on-BRAM warning goes away.
    reg frz0=0, frz1=0;
    always @(posedge cap_clk) begin frz0<=freeze; frz1<=frz0; end

    // STANDARD-IP packing + CDC: the byte->128-bit grouping AND the
    // cap_clk->ui_clk crossing are done by ONE proven Alex Forencich block,
    // axis_async_fifo_adapter (verilog-ethernet). This replaces the former
    // hand-rolled shift-register packer + separate async FIFO, whose
    // combinational full-word was sampled by a one-cycle-late registered
    // valid -> on back-to-back bytes one byte position came out 2 words stale
    // (doc 30). Letting the adapter own tvalid/tkeep/tlast eliminates that
    // whole class of "data vs valid off-by-one" bug.
    //
    // Byte order: the adapter is LITTLE-ENDIAN (first input byte -> LS lane),
    // but the whole downstream chain (DDR readback, gearbox stream_tdata =
    // word[127:120] first) expects BIG-ENDIAN (byte 0 = MS lane). We byte-
    // reverse the adapter's 128-bit output so the on-wire byte order is
    // unchanged from the previous packer -- no downstream edits needed.
    localparam integer IN_BITS = IN_BYTES*8;

    wire         fifo_s_ready;
    wire [127:0] adapter_out_data;
    wire         fifo_out_valid, fifo_out_ready;

    // gate input on freeze (mirrors the old ~frz1 write gate)
    wire         s_valid = cap_valid_in & ~frz1;

    axis_async_fifo_adapter #(
        .DEPTH(4096),                 // 4096 input BYTES -> 256 x128b words
        .S_DATA_WIDTH(IN_BITS),
        .M_DATA_WIDTH(128),
        .S_KEEP_ENABLE(IN_BYTES > 1), // per-byte keep only when >1 byte in
        .M_KEEP_ENABLE(1),
        .ID_ENABLE(0), .DEST_ENABLE(0), .USER_ENABLE(0),
        .FRAME_FIFO(0)
    ) u_afifo (
        .s_clk(cap_clk), .s_rst(cap_rst),
        .s_axis_tdata(cap_byte),
        .s_axis_tkeep({IN_BYTES{1'b1}}),
        .s_axis_tvalid(s_valid), .s_axis_tready(fifo_s_ready),
        .s_axis_tlast(1'b0), .s_axis_tid(8'h0), .s_axis_tdest(8'h0), .s_axis_tuser(1'b0),
        .m_clk(ui_clk), .m_rst(ui_rst),
        .m_axis_tdata(adapter_out_data), .m_axis_tkeep(),
        .m_axis_tvalid(fifo_out_valid), .m_axis_tready(fifo_out_ready),
        .m_axis_tlast(), .m_axis_tid(), .m_axis_tdest(), .m_axis_tuser(),
        .s_pause_req(1'b0), .s_pause_ack(), .m_pause_req(1'b0), .m_pause_ack(),
        .s_status_depth(), .s_status_depth_commit(), .s_status_overflow(),
        .s_status_bad_frame(), .s_status_good_frame(),
        .m_status_depth(), .m_status_depth_commit(), .m_status_overflow(),
        .m_status_bad_frame(), .m_status_good_frame()
    );

    // Byte-reverse LE adapter word -> BE word the datapath expects.
    wire [127:0] fifo_out_data;
    genvar bi;
    generate
        for (bi = 0; bi < 16; bi = bi + 1) begin : g_bswap
            assign fifo_out_data[bi*8 +: 8] =
                   adapter_out_data[(15-bi)*8 +: 8];
        end
    endgenerate

    // Overflow honesty: the adapter back-pressures via s_axis_tready, but the
    // capture source is free-running (cap_valid_in can't stall). If a valid
    // input byte arrives while the adapter can't accept it (~fifo_s_ready), it
    // is lost -- latch that in the cap domain and expose via wr_lost_bytes so a
    // capture with a hard end (not silent gaps) is observable.
    reg overflow_sticky = 0;
    always @(posedge cap_clk or posedge cap_rst) begin
        if (cap_rst)
            overflow_sticky <= 1'b0;
        else if (s_valid & ~fifo_s_ready)
            overflow_sticky <= 1'b1;   // a captured byte was dropped
    end
    reg ovf_s0=0, ovf_s1=0;
    always @(posedge ui_clk) begin
        ovf_s0 <= overflow_sticky; ovf_s1 <= ovf_s0;
        wr_lost_bytes <= {31'd0, ovf_s1};
    end

    // ---------------- ui_clk: PING-PONG stage 128-bit words for DDR3 ---------
    // Two banks: the producer fills one bank while the DDR write FSM reads out
    // the other. They NEVER index the same array during a burst, so a burst can
    // never commit with a half-refilled buffer. The former SINGLE wbuf +
    // burst_ready/burst_done handshake had a producer/consumer race: at low
    // fill rate a burst committed while its buffer was only partly refilled,
    // reusing the previous burst's tail (odd-burst 2-word head + stale tail =
    // the -1024 burst-reorder, doc 30 / tb TEST F).
    //
    // Bank ownership is tracked by two 1-bit "sequence" pointers whose
    // difference (0,1,2) is the number of filled-but-uncommitted banks:
    //   fill_seq  : increments when the producer completes a bank
    //   commit_seq: increments when the FSM finishes committing a bank
    // occupancy = fill_seq - commit_seq (2-bit). Full when occupancy==2.
    reg [127:0] wbuf0 [0:LENGTH-1];
    reg [127:0] wbuf1 [0:LENGTH-1];
    reg  [1:0]  fill_seq = 0, commit_seq = 0;
    reg  [9:0]  word_idx = 0;
    reg         burst_done = 0;     // pulse when a DDR3 burst completes
    wire        fill_bank   = fill_seq[0];
    wire        commit_bank = commit_seq[0];
    wire [1:0]  occupancy   = fill_seq - commit_seq;
    wire        burst_ready = (occupancy != 2'd0);

    // accept a word while a free bank exists (occupancy < 2)
    assign fifo_out_ready = fifo_out_valid & (occupancy != 2'd2) & ~ui_rst;

    always @(posedge ui_clk) begin
        if (ui_rst) begin
            word_idx <= 0; fill_seq <= 0;
        end else if (fifo_out_valid & fifo_out_ready) begin
            if (fill_bank) wbuf1[word_idx] <= fifo_out_data;
            else           wbuf0[word_idx] <= fifo_out_data;
            if (word_idx == LENGTH-1) begin
                word_idx <= 0;
                fill_seq <= fill_seq + 2'd1;   // bank complete -> hand off
            end else begin
                word_idx <= word_idx + 1'b1;
            end
        end
    end

    // ---------------- ui_clk: DDR3 write-burst FSM ----------------
    // On burst_ready, pulse ddr3_wr_start; feed wbuf[] words on ddr3_wr_data_req;
    // advance ring address on wr_done.
    localparam W_IDLE=0, W_START=1, W_RUN=2, W_DONE=3;
    reg [1:0] wst = W_IDLE;
    reg [9:0] out_idx = 0;

    // read out of the CURRENT commit bank (stable: the producer is filling the
    // OTHER bank, so out_idx-indexed data can't be overwritten mid-burst).
    assign ddr3_wr_data = commit_bank ? wbuf1[out_idx] : wbuf0[out_idx];

    always @(posedge ui_clk) begin
        if (ui_rst) begin
            wst <= W_IDLE; ddr3_wr_start <= 0; out_idx <= 0; burst_done <= 0;
            ddr3_wr_addr <= RING_BASE; wr_ptr_words <= RING_BASE;
            words_written <= 0; commit_seq <= 0;
        end else begin
            ddr3_wr_start <= 1'b0;
            burst_done    <= 1'b0;
            case (wst)
                W_IDLE:
                    if (burst_ready) begin
                        ddr3_wr_addr <= wr_ptr_words;
                        ddr3_wr_start <= 1'b1;
                        out_idx <= 0;
                        wst <= W_START;
                    end
                W_START: wst <= W_RUN;   // let wr_ctrl accept the start
                W_RUN: begin
                    if (ddr3_wr_data_req) begin
                        if (out_idx == LENGTH-1) out_idx <= 0;
                        else out_idx <= out_idx + 1'b1;
                    end
                    // CRITICAL: the vendor ddr3_wr_ctrl passes app_addr =
                    // ddr3_wr_addr through unchanged and issues LENGTH commands;
                    // the DATA SOURCE must advance the address +8 per app
                    // command (4:1 PHY, one 128-bit UI word = 8 DDR3 column
                    // addrs) like ddr3_generate_data. Holding it constant made
                    // all 64 words of a burst hit the SAME address -> readback
                    // was the last word repeated 64x (period-16 duplication).
                    if (ddr3_wr_addr_req)
                        ddr3_wr_addr <= ddr3_wr_addr + 29'd8;
                    if (ddr3_wr_done) wst <= W_DONE;
                end
                W_DONE: begin
                    words_written <= words_written + LENGTH;
                    // app address advances +8 per 128-bit word, so a burst
                    // spans LENGTH*8 in app-address units. Advance the ring
                    // write pointer by the SAME amount so bursts are contiguous
                    // and don't overlap.
                    if (wr_ptr_words + (LENGTH<<3) >= RING_BASE + RING_WORDS)
                        wr_ptr_words <= RING_BASE;
                    else
                        wr_ptr_words <= wr_ptr_words + (LENGTH<<3);
                    burst_done <= 1'b1;
                    commit_seq <= commit_seq + 2'd1;  // retire committed bank
                    wst <= W_IDLE;
                end
            endcase
        end
    end

endmodule

`default_nettype wire
