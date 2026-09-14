// la_ddr_ring_streamer
// ====================
// doc 19 P1: the DDR3 HISTORY RING drain path for NACK selective retransmit.
//
// Unlike la_ddr_reader (a ONE-SHOT armed snapshot readback used offline, ring
// frozen during read), this streamer runs CONCURRENTLY with la_ddr_writer on
// the SAME ring: the writer fills at full ETM rate (never back-pressured), and
// this streamer continuously CHASES the writer's wr_ptr, draining committed
// history out to the network at a paced (<= link reliable throughput) rate.
//
//   writer(ui_clk): wr_ptr_words advances +LENGTH*8 per burst, wraps RING_WORDS
//   streamer(ui_clk): rd_ptr_words chases wr_ptr; reads a LENGTH-word burst
//     whenever (a) >= LENGTH*8 app-units of fresh data are available AND
//     (b) the egress FIFO has room. Emits each 128-bit word as 16 big-endian
//     bytes, tagged with a monotonically increasing seq = word_index/PKT_WORDS.
//
// CONCURRENCY (doc 19 §7 P0e — "全新场景"): writer and streamer are BOTH in the
// ui_clk domain, so wr_ptr is read SAME-DOMAIN (no CDC metastability on the
// chased pointer — a real risk the doc flagged, here eliminated by clocking).
// The two share ddr3_ctrl via ddr3_arbit, which gives the WRITE burst priority
// (arbit: `if(wr_req) WRITE else if(rd_req) READ`). So the ETM source-write is
// NEVER starved by a retransmit/drain read: reads only slot into the idle gaps
// between write bursts. This is the property that makes "source never
// back-pressured" hold even under concurrent readback.
//
// seq <-> DDR address binding (doc 19 §4): seq = word_index / PKT_WORDS, where
// word_index counts 128-bit words from RING_BASE. A NACK for seq S therefore
// maps directly back to ring word S*PKT_WORDS (no side index table) — that is
// what P2's retransmit FSM uses.
//
// OVERRUN honesty (doc 19 §6 boundary): if the writer LAPS the reader (fill
// rate > drain rate for long enough that wr_ptr wraps past rd_ptr), the oldest
// un-drained history is overwritten = a permanent coverage gap. We DETECT this
// (ring_overrun sticky) rather than silently emitting corrupted/rewound data.
// This is the L1 boundary made observable, not a bug.

`default_nettype none

module la_ddr_ring_streamer #(
    parameter integer LENGTH     = 64,          // 128-bit words per DDR3 burst
    parameter [28:0]  RING_BASE  = 29'd0,       // ring start (app word address)
    parameter [28:0]  RING_WORDS = 29'h0800000, // ring size in app-addr units
                                                // (=+8/128-bit-word; 0x800000 => 1M words => 16MB)
    parameter integer PKT_WORDS  = 64           // 128-bit words per network packet
                                                // (64 => 1KB payload). seq counts these.
) (
    // ---- ui_clk domain: chase + DDR3 read burst ----
    input  wire        ui_clk,
    input  wire        ui_rst,
    input  wire [28:0] wr_ptr_words,   // LIVE writer pointer (SAME ui_clk domain)
    input  wire [31:0] wr_words_committed, // writer's monotonic committed-word
                                       // count (la_ddr_writer.words_written).
                                       // Used for overrun via ABSOLUTE backlog
                                       // (pointer-mod arithmetic can't tell a
                                       // full-lap-ahead writer from alongside).
    input  wire        drain_credit,   // 1 = pacing allows another burst this
                                       // cycle (constant-rate token from a
                                       // clk-divider; gates drain to <= link
                                       // reliable throughput). Tie high for
                                       // full-speed drain.

    // ---- DDR3 read interface (ui_clk), drives ddr3_ctrl read side ----
    output reg         ddr3_rd_start,
    input  wire        ddr3_rd_addr_req,
    output reg  [28:0] ddr3_rd_addr,
    input  wire        ddr3_rd_data_vld,
    input  wire [127:0]ddr3_rd_data,
    input  wire        ddr3_rd_done,

    // ---- retransmit request (doc 19 P2, clk125 domain from CTRL :5002) ----
    input  wire        nack_valid,      // 1-cyc pulse: a NACK request is present
    input  wire [31:0] nack_start_seq,  // first missing packet seq
    input  wire [15:0] nack_count,      // number of packets to retransmit
    output reg         nack_busy,       // high while servicing a retransmit
    output reg         nack_fail,       // pulse: requested seq fell out of the
                                        // DDR history window (permanent gap)

    // ---- byte stream out (clk125) with per-packet seq ----
    input  wire        clk125,
    input  wire        sys_rst,
    output wire [7:0]  stream_tdata,
    output wire        stream_tvalid,
    input  wire        stream_tready,
    output wire [31:0] stream_seq,      // seq of the packet the current byte
                                        // belongs to (monotonic word_index/PKT_WORDS)
    output wire        stream_rtx,      // 1 = current byte is a RETRANSMITTED packet

    // ---- observability ----
    output reg  [28:0] rd_ptr_words,    // current ring read pointer (ui_clk)
    output reg  [31:0] words_drained,   // total 128-bit words drained (monotonic)
    output reg         ring_overrun     // sticky: writer lapped reader (gap)
);
    localparam integer PKT_APP = PKT_WORDS * 8;  // app-addr units per packet
    // ring capacity in 128-bit words (RING_WORDS is app-addr units, +8/word)
    localparam [31:0] RING_CAP_WORDS = {3'd0, RING_WORDS} >> 3;

    // ---------------- ui_clk: chase FSM ----------------
    // available fresh words (in app-addr units) = (wr_ptr - rd_ptr) mod RING.
    // We hold rd_ptr; when >= one burst is available and pacing+FIFO allow, we
    // issue a burst. All arithmetic in app-addr (+8/word) units.
    wire [29:0] wr_ext = {1'b0, wr_ptr_words};
    wire [29:0] rd_ext = {1'b0, rd_ptr_words};
    // forward distance writer is ahead of reader, modulo ring size
    wire [29:0] avail_raw = (wr_ext >= rd_ext)
                          ? (wr_ext - rd_ext)
                          : (wr_ext + {1'b0, RING_WORDS} - rd_ext);
    wire        burst_avail = (avail_raw >= (LENGTH<<3));

    // ABSOLUTE backlog = words the writer has committed but the streamer has
    // not yet drained. Uses monotonic counters (not wrapped pointers), so a
    // writer a full lap ahead is unambiguous. If backlog exceeds the ring
    // capacity minus one burst, the writer is about to overwrite undrained
    // history => permanent coverage gap.
    wire [31:0] backlog = wr_words_committed - words_drained;
    wire        overrun_now = (backlog > (RING_CAP_WORDS - (LENGTH)));

    // egress FIFO room (declared below); a whole burst must fit unpaused.
    wire fifo_has_room;

    // ---- NACK request CDC: clk125 -> ui_clk (doc 19 P2) ----
    // nack_valid is a 1-cyc pulse in clk125; start_seq/count are stable around
    // it (driven from the slow CTRL path). Toggle-sync the pulse, 2-FF the
    // payload.
    reg nack_tog = 0;
    always @(posedge clk125) if (nack_valid) nack_tog <= ~nack_tog;
    reg nt0=0, nt1=0, nt2=0;
    always @(posedge ui_clk) begin nt0<=nack_tog; nt1<=nt0; nt2<=nt1; end
    wire rtx_req_ui = nt1 ^ nt2;
    reg [31:0] nseq_s0=0, nseq_ui=0;
    reg [15:0] ncnt_s0=0, ncnt_ui=0;
    always @(posedge ui_clk) begin
        nseq_s0<=nack_start_seq; nseq_ui<=nseq_s0;
        ncnt_s0<=nack_count;     ncnt_ui<=ncnt_s0;
    end

    // states: R_* normal drain, X_* retransmit (share the single DDR rd port)
    localparam R_IDLE=3'd0, R_START=3'd1, R_RUN=3'd2, R_NEXT=3'd3,
               X_START=3'd4, X_RUN=3'd5, X_NEXT=3'd6, X_FAIL=3'd7;
    reg [2:0]  rst_state = R_IDLE;

    // per-word egress into the wide async FIFO (one 128-bit word/cycle)
    wire        f_wr_ready;
    reg         f_wr_valid;
    reg  [127:0]f_wr_data;
    reg  [31:0] f_wr_seq;      // seq accompanying this word
    reg         f_wr_rtx;      // 1 = retransmitted word
    // word_index (from ring base) of the word currently being read out
    reg  [28:0] cur_word_idx;  // = (rd_ptr - RING_BASE) / 8

    // retransmit bookkeeping (ui_clk)
    reg         rtx_pending = 0;
    reg  [31:0] rtx_abs_word = 0;   // absolute (monotonic) word being resent
    reg  [31:0] rtx_words_left = 0; // words remaining in this NACK request
    // ring capacity mask (power-of-2 ring assumed; true for sim 512 & real 1M)
    wire [31:0] RING_MASK = RING_CAP_WORDS - 32'd1;
    // ring app-address of the current rtx absolute word
    wire [28:0] rtx_ring_addr = RING_BASE + ((rtx_abs_word & RING_MASK) << 3);
    // window check: data exists AND not yet overwritten by the writer.
    wire rtx_written  = ((rtx_abs_word + rtx_words_left) <= wr_words_committed);
    wire rtx_in_ring  = ((wr_words_committed - rtx_abs_word) <= RING_CAP_WORDS);
    wire rtx_window_ok= rtx_written & rtx_in_ring;

    always @(posedge ui_clk) begin
        if (ui_rst) begin
            rst_state    <= R_IDLE;
            ddr3_rd_start<= 1'b0;
            ddr3_rd_addr <= RING_BASE;
            rd_ptr_words <= RING_BASE;
            cur_word_idx <= 29'd0;
            f_wr_valid   <= 1'b0;
            f_wr_rtx     <= 1'b0;
            words_drained<= 32'd0;
            ring_overrun <= 1'b0;
            rtx_pending  <= 1'b0;
            nack_busy    <= 1'b0;
            nack_fail    <= 1'b0;
        end else begin
            ddr3_rd_start <= 1'b0;
            f_wr_valid    <= 1'b0;
            nack_fail     <= 1'b0;

            // latch an incoming NACK (retransmit request) — priority over drain
            if (rtx_req_ui & ~rtx_pending) begin
                rtx_pending    <= 1'b1;
                rtx_abs_word    <= nseq_ui * PKT_WORDS;
                rtx_words_left  <= ncnt_ui * PKT_WORDS;
            end

            case (rst_state)
                R_IDLE: begin
                    if (rtx_pending) begin
                        nack_busy <= 1'b1;
                        // window check before spending a DDR burst
                        if (!rtx_window_ok)
                            rst_state <= X_FAIL;
                        else if (fifo_has_room) begin
                            ddr3_rd_addr <= rtx_ring_addr;
                            rst_state <= X_START;
                        end
                    end else if (burst_avail & fifo_has_room & drain_credit) begin
                        ddr3_rd_addr <= rd_ptr_words;
                        cur_word_idx <= (rd_ptr_words - RING_BASE) >> 3;
                        rst_state    <= R_START;
                        nack_busy    <= 1'b0;
                    end else
                        nack_busy    <= 1'b0;
                end
                // ---- normal drain burst ----
                R_START: begin
                    ddr3_rd_start <= 1'b1;
                    rst_state <= R_RUN;
                end
                R_RUN: begin
                    if (ddr3_rd_data_vld) begin
                        f_wr_data  <= ddr3_rd_data;
                        // MONOTONIC seq: absolute drained-word index / PKT_WORDS
                        // (wrap-independent, so a NACK for seq S maps to a
                        // unique DDR address across ring laps).
`ifdef SIM_TAG_SEQ
                        f_wr_seq   <= 32'hA5A5_5A5A;   // r36 P0-2 H1 probe
`else
                        f_wr_seq   <= words_drained / PKT_WORDS;
`endif
                        f_wr_rtx   <= 1'b0;
                        f_wr_valid <= 1'b1;
                        cur_word_idx <= cur_word_idx + 29'd1;
                        words_drained <= words_drained + 32'd1;
                    end
                    if (ddr3_rd_addr_req)
                        ddr3_rd_addr <= ddr3_rd_addr + 29'd8;
                    if (ddr3_rd_done) rst_state <= R_NEXT;
                end
                R_NEXT: begin
                    if (rd_ptr_words + (LENGTH<<3) >= RING_BASE + RING_WORDS)
                        rd_ptr_words <= RING_BASE;
                    else
                        rd_ptr_words <= rd_ptr_words + (LENGTH<<3);
                    rst_state <= R_IDLE;
                end
                // ---- retransmit burst (doc 19 P2/P3) ----
                X_START: begin
                    ddr3_rd_start <= 1'b1;
                    rst_state <= X_RUN;
                end
                X_RUN: begin
                    if (ddr3_rd_data_vld) begin
                        f_wr_data  <= ddr3_rd_data;
`ifdef SIM_TAG_SEQ
                        f_wr_seq   <= 32'hA5A5_5A5A;   // r36 P0-2 H1 probe
`else
                        f_wr_seq   <= rtx_abs_word / PKT_WORDS;
`endif
                        f_wr_rtx   <= 1'b1;               // mark retransmit
                        f_wr_valid <= 1'b1;
                        rtx_abs_word   <= rtx_abs_word + 32'd1;
                        rtx_words_left <= rtx_words_left - 32'd1;
                    end
                    if (ddr3_rd_addr_req)
                        ddr3_rd_addr <= ddr3_rd_addr + 29'd8;
                    if (ddr3_rd_done) rst_state <= X_NEXT;
                end
                X_NEXT: begin
                    if (rtx_words_left == 32'd0) begin
                        rtx_pending <= 1'b0;   // whole NACK serviced
                        nack_busy   <= 1'b0;
                        rst_state   <= R_IDLE;
                    end else begin
                        rst_state   <= R_IDLE; // re-enter: next rtx burst (or
                                               // yield to a pending drain slot)
                    end
                end
                X_FAIL: begin
                    // requested seq fell out of the DDR history window:
                    // permanent coverage gap. Report and drop the request.
                    nack_fail   <= 1'b1;
                    rtx_pending <= 1'b0;
                    nack_busy   <= 1'b0;
                    rst_state   <= R_IDLE;
                end
                default: rst_state <= R_IDLE;
            endcase

            // ---- overrun watchdog (independent of FSM) ----
            // Absolute backlog exceeding ring capacity => writer overwrote
            // history the streamer had not yet drained: honest coverage gap.
            if (overrun_now)
                ring_overrun <= 1'b1;
        end
    end

    // ---------------- ui_clk -> clk125 async FIFO (rtx + seq + data) --------
    // Pack {rtx, seq, data} = 161 bits so each byte carries its packet seq and
    // retransmit flag to the network stage without a side channel.
    localparam integer FIFO_WORDS = 512;
    wire [160:0] s_fifo_in  = {f_wr_rtx, f_wr_seq, f_wr_data};
    wire [160:0] m_fifo_out;
    wire         m_valid, m_ready;
    wire [$clog2(FIFO_WORDS):0] s_depth;
    assign fifo_has_room = (s_depth < (FIFO_WORDS - 2*LENGTH));

    axis_async_fifo #(
        .DEPTH(FIFO_WORDS), .DATA_WIDTH(161),
        .KEEP_ENABLE(0), .LAST_ENABLE(0), .USER_ENABLE(0), .FRAME_FIFO(0)
    ) u_sfifo (
        .s_clk(ui_clk), .s_rst(ui_rst),
        .s_axis_tdata(s_fifo_in), .s_axis_tkeep(20'h0),
        .s_axis_tvalid(f_wr_valid), .s_axis_tready(f_wr_ready),
        .s_axis_tlast(1'b0), .s_axis_tid(8'h0), .s_axis_tdest(8'h0), .s_axis_tuser(1'b0),
        .m_clk(clk125), .m_rst(sys_rst),
        .m_axis_tdata(m_fifo_out), .m_axis_tkeep(),
        .m_axis_tvalid(m_valid), .m_axis_tready(m_ready),
        .m_axis_tlast(), .m_axis_tid(), .m_axis_tdest(), .m_axis_tuser(),
        .s_pause_req(1'b0), .s_pause_ack(), .m_pause_req(1'b0), .m_pause_ack(),
        .s_status_depth(s_depth), .s_status_depth_commit(), .s_status_overflow(),
        .s_status_bad_frame(), .s_status_good_frame(),
        .m_status_depth(), .m_status_depth_commit(), .m_status_overflow(),
        .m_status_bad_frame(), .m_status_good_frame()
    );

    // ---------------- clk125 gearbox: 128-bit word -> 16 bytes ----------------
    reg  [127:0] gb_word = 0;
    reg  [31:0]  gb_seq  = 0;
    reg          gb_rtx  = 0;
    reg  [4:0]   gb_cnt  = 0;      // 0 => empty
    wire         gb_empty = (gb_cnt == 0);
    assign m_ready       = gb_empty;
    assign stream_tvalid = ~gb_empty;
    assign stream_tdata  = gb_word[127:120];
    assign stream_seq    = gb_seq;
    assign stream_rtx    = gb_rtx;
    always @(posedge clk125) begin
        if (sys_rst) begin
            gb_cnt <= 0; gb_word <= 0; gb_seq <= 0; gb_rtx <= 0;
        end else if (gb_empty) begin
            if (m_valid) begin
                gb_word <= m_fifo_out[127:0];
                gb_seq  <= m_fifo_out[159:128];
                gb_rtx  <= m_fifo_out[160];
                gb_cnt  <= 5'd16;
            end
        end else if (stream_tready) begin
            gb_word <= {gb_word[119:0], 8'h00};
            gb_cnt  <= gb_cnt - 1'b1;
        end
    end

endmodule

`default_nettype wire
