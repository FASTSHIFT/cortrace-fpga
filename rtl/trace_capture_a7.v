// trace_capture_a7
// =================
// Source-synchronous DDR capture front-end for the Cortex-M parallel TRACE
// port on Artix-7 (Xilinx 7-series).
//
// Replaces ECP5's IDDRX1F used in orbtrace's `glue.py` with the equivalent
// 7-series primitive:  IBUF -> IDDR (DDR_CLK_EDGE=SAME_EDGE_PIPELINED).
//
// NO IDELAY. The STM32 TPIU drives the parallel port CENTRE-ALIGNED (data is
// stable AROUND the TRACECLK edge, confirmed on the LA), so the IDDR samples
// the eye centre by sampling on the edge directly -- exactly like the upstream
// litex DDRInput. An earlier port added a FIXED IDELAY tap to "fix" an IDDR
// input-hold STA violation, but on-board that tap only pushed the (already
// centred) data OUT of the eye: at 112 MHz pin, tap=24 gave 79% bad A-sync
// while tap=0 gave 0%. So the delay element was not just unnecessary but
// harmful; it is removed. Sampling phase is frequency-independent, so ONE
// bitstream covers every TRACECLK the board's SI supports (~<=56 MHz pin
// clean; 112 MHz is board-SI-limited, not a capture-logic limit).
//
// Output stream is the same {trace_a, trace_b} pair (rising-edge nibble and
// falling-edge nibble) that traceIF.v already consumes, plus the packed
// {trace_b,trace_a} raw byte per TRACECLK period exported through a gray-code
// async FIFO to the ref_200m domain.
//
// Inputs (board side):
//   trace_clk_p      : TRACECLK from target (must land on a CC pin in xdc)
//   trace_data_p[3:0]: TRACED0..3 from target
//   ref_200m         : capture-domain read clock for the CDC FIFO (>=TRACECLK)
//   rst              : asynchronous reset (active high)
//   test_src_en      : 1 => push a framed xorshift32 PRBS through the SAME CDC
//                      FIFO instead of the IDDR sample (link stress/BER test)
//
// Outputs:
//   trace_clk        : recovered trace clock (BUFG/BUFR'd) — feeds traceIF
//   trace_a[3:0]     : rising-edge sample of TRACED
//   trace_b[3:0]     : falling-edge sample of TRACED
//   cap_byte/cap_valid: raw byte per TRACECLK period in the ref_200m domain

`default_nettype none

module trace_capture_a7 #(
    // Clock buffering for TRACECLK:
    //   "BUFG"     : global clock buffer (OOC-friendly, larger insertion delay).
    //   "BUFR_IO"  : BUFIO drives the IDDR bit-clock + BUFR drives the fabric
    //                clock. Region-local, tighter source-sync window between
    //                TRACECLK and the IDDR C pins — the shipped choice.
    parameter CLK_BUF = "BUFG"
) (
    input  wire        rst,
    input  wire        ref_200m,

    // Trace pins from target
    input  wire        trace_clk_p,
    input  wire [3:0]  trace_data_p,

    // Link stress test: when 1, the byte pushed into the trace_clk->ref_200m
    // CDC FIFO is a trace_clk-domain framed xorshift32 PRBS instead of the IDDR
    // sample {iddr_b,iddr_a}. Exercises the SAME CDC FIFO + downstream DDR/UDP
    // path with a violently-changing, host-reproducible pattern so a byte
    // drop/dup shows up as a PRBS break. Synced to trace_clk inside (g_iddr).
    input  wire        test_src_en,

    // Captured outputs to traceIF
    output wire        trace_clk,
    output wire [3:0]  trace_a,      // rising-edge sample
    output wire [3:0]  trace_b,      // falling-edge sample

    // Raw byte capture in the ref_200m domain (one byte per TRACECLK period,
    // {falling nibble, rising nibble}) crossed atomically via the gray-code
    // async FIFO. cap_valid pulses when cap_byte is freshly popped.
    output wire [7:0]  cap_byte,
    output wire        cap_valid
);

    // ------------------------------------------------------------------
    // Clock path: TRACECLK -> IBUF -> { BUFG | BUFIO+BUFR }.
    //   trace_clk_io  : the clock that drives the IDDR C pins (sampling)
    //   trace_clk     : the fabric-side clock (traceIF runs on this)
    // For BUFG both are the same net; for BUFR_IO the IDDR uses the BUFIO
    // output while the fabric uses the (divide-by-1) BUFR output.
    // ------------------------------------------------------------------
    wire trace_clk_ibuf;
    wire trace_clk_io;     // -> IDDR C
    IBUF u_ibuf_clk (.I(trace_clk_p), .O(trace_clk_ibuf));

    generate
        if (CLK_BUF == "BUFR_IO") begin : g_bufr
            BUFIO u_bufio_clk (.I(trace_clk_ibuf), .O(trace_clk_io));
            BUFR #(.BUFR_DIVIDE("BYPASS")) u_bufr_clk (
                .I(trace_clk_ibuf), .O(trace_clk), .CE(1'b1), .CLR(1'b0)
            );
        end else begin : g_bufg
            BUFG u_bufg_clk (.I(trace_clk_ibuf), .O(trace_clk));
            assign trace_clk_io = trace_clk;
        end
    endgenerate

    // ------------------------------------------------------------------
    // Per-lane input path: IBUF straight to IDDR (no delay element). The
    // centre-aligned STM32 data is sampled at the eye centre by clocking IDDR
    // on the TRACECLK edge.
    // ------------------------------------------------------------------
    wire [3:0] data_ibuf;
    genvar i;
    generate
        for (i = 0; i < 4; i = i + 1) begin : g_lane
            IBUF u_ibuf (.I(trace_data_p[i]), .O(data_ibuf[i]));
        end
    endgenerate

    // ------------------------------------------------------------------
    // IDDR: DDR input register sampled on the TRACECLK edges (Q1 rising, Q2
    // falling). One byte per TRACECLK period crosses to ref_200m through a
    // gray-code async FIFO (atomic, gap-tolerant).
    // ------------------------------------------------------------------
    generate begin : g_iddr
        wire [3:0] iddr_a, iddr_b;
        genvar j;
        for (j = 0; j < 4; j = j + 1) begin : g_iddr_lane
            IDDR #(
                .DDR_CLK_EDGE ("SAME_EDGE_PIPELINED"),
                .INIT_Q1      (1'b0),
                .INIT_Q2      (1'b0),
                .SRTYPE       ("ASYNC")
            ) u_iddr (
                .Q1 (iddr_a[j]),  // rising-edge sample
                .Q2 (iddr_b[j]),  // falling-edge sample
                .C  (trace_clk_io), // BUFIO (BUFR_IO) or BUFG net
                .CE (1'b1),
                .D  (data_ibuf[j]),
                .R  (rst),
                .S  (1'b0)
            );
        end
        assign trace_a = iddr_a;
        assign trace_b = iddr_b;

        // ---- gap-tolerant raw-byte export for CAP_RAW ----
        // The byte crosses trace_clk -> ref_200m through a PROPER async FIFO
        // (gray-code pointers) so the transfer is ATOMIC: the reader only ever
        // sees a fully-written entry, never a torn one. Gap tolerance: the
        // WRITE port is clocked by trace_clk, so a stopped TRACECLK simply
        // stops pushing (no garbage). Depth 32 is ample: read rate (ref_200m)
        // >= write rate (TRACECLK), so it never backs up in steady state.

        // Sync the (slow, level) test-enable into trace_clk.
        reg test_en_s0 = 1'b0, test_en_tclk = 1'b0;
        always @(posedge trace_clk) begin
            test_en_s0   <= test_src_en;
            test_en_tclk <= test_en_s0;
        end

        // FRAMED xorshift32 PRBS. A free-running PRBS is unlockable once the
        // link drops a byte (a dropped byte skips a state, desyncing forever),
        // so we FRAME it: every 8192-byte block starts with an 8-byte fixed
        // marker and RESEEDS the PRBS to 0x1. The host locks on the marker,
        // then knows the exact byte sequence; marker-to-marker spacing measures
        // dropped bytes directly (8192 = no loss).
        //   block[0..7]    = marker A5 5A C3 3C F0 0F 99 66  (prbs held at seed)
        //   block[8..8191] = xorshift32 low-byte stream from seed 0x1
        localparam [12:0] BLK_LEN = 13'd8192;
        reg  [12:0] blkpos = 13'd0;
        reg  [31:0] prbs   = 32'h1;
        wire [31:0] prbs_x1  = prbs    ^ (prbs    << 13);
        wire [31:0] prbs_x2  = prbs_x1 ^ (prbs_x1 >> 17);
        wire [31:0] prbs_nxt = prbs_x2 ^ (prbs_x2 << 5);

        reg [7:0] marker;
        always @(*) case (blkpos[2:0])
            3'd0: marker = 8'hA5;  3'd1: marker = 8'h5A;
            3'd2: marker = 8'hC3;  3'd3: marker = 8'h3C;
            3'd4: marker = 8'hF0;  3'd5: marker = 8'h0F;
            3'd6: marker = 8'h99;  default: marker = 8'h66;
        endcase
        wire        in_marker = (blkpos < 13'd8);
        wire [7:0]  test_byte = in_marker ? marker : prbs[7:0];

        always @(posedge trace_clk) begin
            if (test_en_tclk) begin
                blkpos <= (blkpos == BLK_LEN - 1) ? 13'd0 : (blkpos + 13'd1);
                prbs   <= (blkpos < 13'd8) ? 32'h1 : prbs_nxt;
            end else begin
                blkpos <= 13'd0;
                prbs   <= 32'h1;
            end
        end

        reg [7:0] tclk_byte = 8'b0;
        reg       tclk_push = 1'b0;
        always @(posedge trace_clk) begin
            tclk_byte <= test_en_tclk ? test_byte : {iddr_b, iddr_a};
            tclk_push <= 1'b1;               // push one byte per TRACECLK period
        end

        wire        fifo_s_ready;
        wire [7:0]  fifo_m_data;
        wire        fifo_m_valid;
        wire        fifo_m_ready = fifo_m_valid;

        axis_async_fifo #(
            .DEPTH(32), .DATA_WIDTH(8),
            .KEEP_ENABLE(0), .LAST_ENABLE(0), .USER_ENABLE(0), .FRAME_FIFO(0)
        ) u_cdc_fifo (
            .s_clk(trace_clk), .s_rst(rst),
            .s_axis_tdata(tclk_byte), .s_axis_tkeep(1'b0),
            .s_axis_tvalid(tclk_push), .s_axis_tready(fifo_s_ready),
            .s_axis_tlast(1'b0), .s_axis_tid(8'h0), .s_axis_tdest(8'h0),
            .s_axis_tuser(1'b0),
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
        assign cap_valid = fifo_m_valid & fifo_m_ready;
        assign cap_byte  = fifo_m_data;
    end
    endgenerate

endmodule

`default_nettype wire
