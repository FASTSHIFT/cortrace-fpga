// trace_ddr_stream_top
// =====================
// doc 21 S2: real trace source through the DDR ring buffer.
//
// Datapath:
//     STM32 ETM 4-bit (TRACECLK) -> trace_capture_a7 (IDDR / OVERSAMPLE)
//        -> {trace_b, trace_a} = cap_byte @ cap_valid (clk200 domain)
//        -> la_ddr_writer  (cap -> 128b AsyncFIFO -> pack -> DDR3 burst)
//        -> DDR3 ring (16 MB, ~700 ms at 25 MB/s ETM)
//        -> la_ddr_ring_streamer (drain @clk125 gearbox to 8b, monotone seq)
//        -> packetiser: [4B BE seq][PAYLOAD trace] per UDP packet
//        -> fpga_core_net STREAM -> UDP :5555 to host
//
// Differences vs trace_stream_top (STREAM=1):
//   * intermediate buffer is DDR3 (16 MB) instead of 8 KB CDC AsyncFIFO
//   * per-packet seq comes from the streamer (monotonic wrt DDR word idx,
//     wrap-independent) so NACK retransmit (doc 19 S3) can locate any packet
//   * no traceIF fallback (STREAM_FRAMED / CAP_RAW=0 removed for clarity;
//     the DDR ring is designed for the RAW nibble stream)
//   * selftest source (fixed 0x42 / ramp) kept and driven by the same CSR
//     0x0B bit as ddr_ring_selftest_top, so S1b results transfer directly
//
// Config CSRs (:5002, same map as trace_stream_top for tool reuse):
//   0x05-0x07  (removed) were IDELAY tap tuning; the front-end has no IDELAY
//   0x08  TPIU port width (4/2/1)
//   0x09  1 -> use FPGA-side ramp source (selftest, bypass trace pins)
//   0x0A  1 -> pause stream (for :5001 CSR readback while stream is running)
//   0x0B  1 -> use fixed 0x42 source (S1b diagnostic, bypass ramp too)
//   0x0C  1 -> clear r38 P0-4 diag latches (pkt_tdata bad-byte tap)
//   0x10  soft reset of trace-capture path
//
// Readouts (:5001, 0xFF page):
//   0xFF50..0xFF6F  DDR-ring status (magic 0xD1) — SAME AS ddr_ring_selftest
//   0xFF70..0xFF73  BUILD_ID (LE)
//   0xFF80..0xFF94  r38 P0-4 bad-byte diag latch (magic 0xD2)
//   -- from dbg_regfile (existing) at 0xFF10..0xFF4F
//
// r39 lesson embedded: STREAM_PKT_BYTES is bound to (PKT = PAYLOAD + 4),
// NOT left at fpga_core_net default 1024. See doc r39-onboard-root-cause.md.

`default_nettype none

module trace_ddr_stream_top #(
    // No IDELAY: IBUF->IDDR samples the centre-aligned data on the TRACECLK
    // edge directly (see trace_capture_a7). The former TAP/USE_IDELAY/
    // CAP_IDELAY_FIXED* knobs are gone -- the delay element was harmful, not
    // needed (doc 30: tap=0 gave 0% bad A-sync at 112 MHz, tap=24 gave 79%).
    parameter       TRACE_WIDTH = 4,            // power-on TPIU width (4/2/1)
    parameter [31:0] DEST_IP        = {8'd192, 8'd168, 8'd10, 8'd245},
    parameter [15:0] DEST_PORT      = 16'd5555,
    parameter integer LENGTH        = 64,       // 128-bit words per DDR3 burst
    parameter [28:0]  RING_WORDS    = 29'h0800000,  // 16 MB app-addr units
    parameter integer PKT_WORDS     = 64,       // 128-bit words per UDP packet
    parameter integer STREAM_PAYLOAD= 1024,     // trace bytes per UDP packet
    parameter integer STREAM_FIFO_DEPTH = 8192, // la_ddr_writer input CDC FIFO
    parameter [31:0] BUILD_ID       = 32'hDEADBEEF
) (
    input  wire        sys_clk_50,
    input  wire        rst_n,

    // RGMII
    input  wire        phy_rx_clk,
    input  wire [3:0]  phy_rxd,
    input  wire        phy_rx_ctl,
    output wire        phy_tx_clk,
    output wire [3:0]  phy_txd,
    output wire        phy_tx_ctl,
    output wire        phy_reset_n,
    inout  wire        phy_mdio,
    output wire        phy_mdc,

    // Trace pins from target
    input  wire        trace_clk_in,
    input  wire [3:0]  trace_data_in,

    // DDR3
    output wire [14:0] ddr3_addr,
    output wire [2:0]  ddr3_ba,
    output wire        ddr3_cas_n,
    output wire [0:0]  ddr3_ck_n,
    output wire [0:0]  ddr3_ck_p,
    output wire [0:0]  ddr3_cke,
    output wire        ddr3_ras_n,
    output wire        ddr3_reset_n,
    output wire        ddr3_we_n,
    inout  wire [15:0] ddr3_dq,
    inout  wire [1:0]  ddr3_dqs_n,
    inout  wire [1:0]  ddr3_dqs_p,
    output wire [1:0]  ddr3_dm,
    output wire [0:0]  ddr3_odt,

    output wire        led0,   // sys mmcm locked
    output wire        led1    // packet in flight
);
    wire rst = ~rst_n;

    // ============ sys MMCM (50 -> 125 / 125@90 / 100 / 200) ============
    // clk125 : ethernet / packetiser
    // clk125_90 : RGMII output-90
    // clk100 : sys reset / dbg_regfile
    // clk200 : IDELAYCTRL reference + trace_capture_a7 sample domain
    wire clkfb;
    wire clk125_u, clk125_90_u, clk100_u, clk200_u;
    wire mmcm_sys_locked;
    MMCME2_BASE #(
        .CLKIN1_PERIOD(20.0), .CLKFBOUT_MULT_F(20.0), .DIVCLK_DIVIDE(1),
        .CLKOUT0_DIVIDE_F(8.0),                                  // 125.0
        .CLKOUT1_DIVIDE(8),   .CLKOUT1_PHASE(90.0),              // 125@90
        .CLKOUT2_DIVIDE(5),                                      // 200.0
        .CLKOUT3_DIVIDE(10),                                     // 100
        .CLKOUT0_PHASE(0.0), .CLKOUT2_PHASE(0.0), .CLKOUT3_PHASE(0.0)
    ) u_sysmmcm (
        .CLKIN1(sys_clk_50), .CLKFBIN(clkfb), .CLKFBOUT(clkfb),
        .CLKOUT0(clk125_u), .CLKOUT1(clk125_90_u),
        .CLKOUT2(clk200_u), .CLKOUT3(clk100_u),
        .LOCKED(mmcm_sys_locked), .RST(rst), .PWRDWN(1'b0)
    );
    wire clk125, clk125_90, clk100, clk200;
    BUFG b0(.I(clk125_u),    .O(clk125));
    BUFG b1(.I(clk125_90_u), .O(clk125_90));
    BUFG b2(.I(clk200_u),    .O(clk200));
    BUFG b3(.I(clk100_u),    .O(clk100));

    reg [3:0] rst_sync = 4'hf;
    always @(posedge clk125 or posedge rst)
        if (rst) rst_sync <= 4'hf;
        else     rst_sync <= {rst_sync[2:0], ~mmcm_sys_locked};
    wire sys_rst = rst_sync[3];

    // Local reset sync into clk200 (la_ddr_writer + trace_capture_a7 live
    // in this domain). Avoids the timing warning from sys_rst (clk125)
    // reaching clk200 FFs without a local sync.
    reg [3:0] rst200_sync = 4'hf;
    always @(posedge clk200 or posedge rst)
        if (rst) rst200_sync <= 4'hf;
        else     rst200_sync <= {rst200_sync[2:0], ~mmcm_sys_locked};
    wire rst200 = rst200_sync[3];

    // ============ MIG 200 MHz reference clock ============
    wire sys_clk_200, clk200_locked;
    clock u_clock (
        .clk_out1(sys_clk_200),
        .resetn  (rst_n),
        .locked  (clk200_locked),
        .clk_in1 (sys_clk_50)
    );

    // ============ DDR3 controller (ui_clk = 50 MHz from MIG) ============
    wire        ui_clk, ui_rst, ddr3_busy;
    wire         wr_start_w, wr_data_req, wr_addr_req_w, wr_done_w;
    wire [127:0] wr_data_w;
    wire [28:0]  wr_addr_w;
    wire         rd_start_w, rd_addr_req_w, rd_data_vld_w, rd_done_w;
    wire [28:0]  rd_addr_w;
    wire [127:0] rd_data_w;

    wire mig_calib_raw;
    ddr3_ctrl u_ddr3 (
        .sys_clk    (sys_clk_200),
        .sys_rst_n  (clk200_locked & mmcm_sys_locked),
        .ui_clk     (ui_clk),
        .ui_rst     (ui_rst),
        .calib_complete(mig_calib_raw),
        .ddr3_busy  (ddr3_busy),
        .ddr3_wr_start(wr_start_w), .ddr3_wr_data_req(wr_data_req),
        .ddr3_wr_data(wr_data_w), .ddr3_wr_addr_req(wr_addr_req_w),
        .ddr3_wr_addr(wr_addr_w), .ddr3_wr_done(wr_done_w),
        .ddr3_rd_start(rd_start_w), .ddr3_rd_addr_req(rd_addr_req_w),
        .ddr3_rd_addr(rd_addr_w), .ddr3_rd_data_vld(rd_data_vld_w),
        .ddr3_rd_data(rd_data_w), .ddr3_rd_done(rd_done_w),
        .ddr3_addr(ddr3_addr), .ddr3_ba(ddr3_ba), .ddr3_cas_n(ddr3_cas_n),
        .ddr3_ck_n(ddr3_ck_n), .ddr3_ck_p(ddr3_ck_p), .ddr3_cke(ddr3_cke),
        .ddr3_ras_n(ddr3_ras_n), .ddr3_reset_n(ddr3_reset_n), .ddr3_we_n(ddr3_we_n),
        .ddr3_dq(ddr3_dq), .ddr3_dqs_n(ddr3_dqs_n), .ddr3_dqs_p(ddr3_dqs_p),
        .ddr3_dm(ddr3_dm), .ddr3_odt(ddr3_odt)
    );

    // ============ CSR bits (clk125) ============
    // Written by fpga_core_net's :5002 CTRL port. Multi-bit values below are
    // sampled at rest so a 2FF sync to clk200 / ui_clk is sufficient.
    wire [7:0]  csr_addr_w, csr_data_w;
    wire        csr_we_w;

    reg  [1:0]  width_csr = TRACE_WIDTH[1:0];   // encoded 4/2/1 -> 2'd0/1/2 later
    reg         selftest_csr = 0;    // 0x09
    reg         stream_pause_125 = 0;// 0x0A
    reg         src_fixed_125 = 0;   // 0x0B (S1b fixed 0x42)
    reg         diag_clr_125 = 0;    // 0x0C (r38 P0-4 latch clear pulse)
    reg         iddr_prbs_125 = 0;   // 0x0D (trace_clk PRBS into the CDC FIFO,
                                     //       stresses IDDR->CDC->DDR->UDP path)

    // IDELAY tap CSRs (0x05/0x06/0x07) are gone: the capture front-end has no
    // delay element (IBUF->IDDR direct), so there is nothing to tune.
    always @(posedge clk125) begin
        if (sys_rst) begin
            selftest_csr <= 0; stream_pause_125 <= 0; src_fixed_125 <= 0;
            diag_clr_125 <= 0; iddr_prbs_125 <= 0;
        end else if (csr_we_w) case (csr_addr_w)
            8'h08: width_csr      <= (csr_data_w == 8'd2) ? 2'd1 :
                                     (csr_data_w == 8'd1) ? 2'd2 : 2'd0;
            8'h09: selftest_csr   <= csr_data_w[0];
            8'h0A: stream_pause_125 <= csr_data_w[0];
            8'h0B: src_fixed_125    <= csr_data_w[0];
            8'h0C: diag_clr_125     <= csr_data_w[0];
            8'h0D: iddr_prbs_125    <= csr_data_w[0];
            default: ;
        endcase
    end

    // ============ trace_capture_a7 (clk200 domain) ============
    wire [7:0]  cap_byte;
    wire        cap_valid;

    trace_capture_a7 #(
        .CLK_BUF   ("BUFR_IO")
    ) u_capture (
        .rst          (rst200),
        .ref_200m     (clk200),
        .trace_clk_p  (trace_clk_in),
        .trace_data_p (trace_data_in),
        .test_src_en  (iddr_prbs_125),   // synced to trace_clk inside
        .trace_clk    (),
        .trace_a      (),
        .trace_b      (),
        .cap_byte     (cap_byte),
        .cap_valid    (cap_valid)
    );

    // ============ Source select: selftest ramp / fixed 0x42 / real trace ============
    // Sync CSR bits into clk200 (source domain).
    reg selftest_s0=0, selftest_200=0;
    reg src_fixed_s0=0, src_fixed_200=0;
    always @(posedge clk200) begin
        selftest_s0 <= selftest_csr;   selftest_200 <= selftest_s0;
        src_fixed_s0<= src_fixed_125;  src_fixed_200<= src_fixed_s0;
    end

    // Free-running ramp (200 MB/s at 200 MHz clk200 if unthrottled, but the
    // downstream la_ddr_writer applies backpressure via its input FIFO, so
    // the effective rate is the DDR write bandwidth).
    reg [7:0] ramp = 8'd0;
    reg       calib_done = 0;
    always @(posedge clk200) begin
        if (rst200) begin ramp <= 8'd0; calib_done <= 0; end
        else begin
            if (!calib_done && mig_calib_raw) calib_done <= 1'b1;
            if (selftest_200 && calib_done) ramp <= ramp + 8'd1;
        end
    end

    wire [7:0] src_byte  = selftest_200 ? (src_fixed_200 ? 8'h42 : ramp)
                                        : cap_byte;
    wire       src_valid = selftest_200 ? calib_done
                                        : cap_valid;

    // ============ la_ddr_writer (cap -> DDR3, cap_clk = clk200, ui = ui_clk) ============
    wire [28:0] wr_ptr_words;
    wire [31:0] words_written;
    wire [31:0] wr_lost_bytes;
    la_ddr_writer #(
        .LENGTH   (LENGTH),
        .IN_BYTES (1),
        .RING_BASE(29'd0),
        .RING_WORDS(RING_WORDS)
    ) u_wr (
        .cap_clk       (clk200),
        .cap_rst       (rst200),
        .cap_byte      (src_byte),
        .cap_valid_in  (src_valid),
        .freeze        (1'b0),
        .ui_clk        (ui_clk),
        .ui_rst        (ui_rst),
        .ddr3_busy     (ddr3_busy),
        .ddr3_wr_start (wr_start_w),
        .ddr3_wr_data_req(wr_data_req),
        .ddr3_wr_data  (wr_data_w),
        .ddr3_wr_addr_req(wr_addr_req_w),
        .ddr3_wr_addr  (wr_addr_w),
        .ddr3_wr_done  (wr_done_w),
        .wr_ptr_words  (wr_ptr_words),
        .words_written (words_written),
        .wr_lost_bytes (wr_lost_bytes)
    );

    // ============ la_ddr_ring_streamer (DDR3 -> 8b @ clk125) ============
    wire [7:0]  stream_tdata;
    wire        stream_tvalid;
    wire        stream_tready;
    wire [31:0] stream_seq;
    wire        stream_rtx;
    wire [28:0] rd_ptr_words;
    wire [31:0] words_drained;
    wire        ring_overrun;
    wire        nack_busy, nack_fail;

    la_ddr_ring_streamer #(
        .LENGTH   (LENGTH),
        .RING_BASE(29'd0),
        .RING_WORDS(RING_WORDS),
        .PKT_WORDS(PKT_WORDS)
    ) u_st (
        .ui_clk         (ui_clk),
        .ui_rst         (ui_rst),
        .wr_ptr_words   (wr_ptr_words),
        .wr_words_committed(words_written),
        .drain_credit   (1'b1),
        .ddr3_rd_start  (rd_start_w),
        .ddr3_rd_addr_req(rd_addr_req_w),
        .ddr3_rd_addr   (rd_addr_w),
        .ddr3_rd_data_vld(rd_data_vld_w),
        .ddr3_rd_data   (rd_data_w),
        .ddr3_rd_done   (rd_done_w),
        // S3 will wire the NACK path from :5002; tie 0 here until then.
        .nack_valid     (1'b0),
        .nack_start_seq (32'd0),
        .nack_count     (16'd0),
        .nack_busy      (nack_busy),
        .nack_fail      (nack_fail),
        .clk125         (clk125),
        .sys_rst        (sys_rst),
        .stream_tdata   (stream_tdata),
        .stream_tvalid  (stream_tvalid),
        .stream_tready  (stream_tready),
        .stream_seq     (stream_seq),
        .stream_rtx     (stream_rtx),
        .rd_ptr_words   (rd_ptr_words),
        .words_drained  (words_drained),
        .ring_overrun   (ring_overrun)
    );

    // ============ Packetiser (clk125) — [4B BE seq][PAYLOAD trace] ============
    // Identical shape to ddr_ring_selftest_top's packetiser (r39 fix embedded).
    localparam integer PKT = STREAM_PAYLOAD + 4;
    reg  [15:0] pos = 0;
    reg  [31:0] pkt_seq = 0;
    reg         pkt_active = 0;
    reg  [31:0] latched_seq = 0;

    wire        in_header = (pos < 16'd4);
    wire [7:0]  seq_byte = (pos == 16'd0) ? latched_seq[31:24] :
                           (pos == 16'd1) ? latched_seq[23:16] :
                           (pos == 16'd2) ? latched_seq[15:8]  :
                                            latched_seq[7:0];
    wire        pkt_tready;
    wire        pkt_tvalid = pkt_active & (in_header | stream_tvalid);
    assign      stream_tready = pkt_active & ~in_header & pkt_tready & pkt_tvalid;
    wire [7:0]  pkt_tdata  = in_header ? seq_byte : stream_tdata;

    always @(posedge clk125) begin
        if (sys_rst) begin pos <= 0; pkt_active <= 0; pkt_seq <= 0; latched_seq <= 0; end
        else begin
            if (!pkt_active) begin
                if (stream_tvalid && !stream_pause_125) begin
                    pkt_active  <= 1'b1;
                    pos         <= 0;
                    latched_seq <= stream_seq;
                end
            end else if (pkt_tvalid && pkt_tready) begin
                if (pos == PKT-1) begin
                    pos <= 0; pkt_seq <= pkt_seq + 1'b1; pkt_active <= 1'b0;
                end else pos <= pos + 1'b1;
            end
        end
    end

    // ============ r38 P0-4 diag latches (clk125) ============
    // Same idiom as ddr_ring_selftest_top so read_bad_byte_latch.py just works.
    reg [7:0]  bad_byte_val  = 0;
    reg [7:0]  bad_byte_pos  = 0;
    reg [31:0] bad_byte_seq  = 0;
    reg [7:0]  bad_stream_td = 0;
    reg        bad_byte_latched = 0;
    reg [31:0] bad_byte_count   = 0;
    reg [31:0] stream_pulses    = 0;
    reg [31:0] pkt_active_pulses = 0;
    reg        pkt_active_d = 0;
    always @(posedge clk125) begin
        pkt_active_d <= pkt_active;
        if (sys_rst) begin
            bad_byte_val <= 0; bad_byte_pos <= 0; bad_byte_seq <= 0;
            bad_stream_td <= 0; bad_byte_latched <= 0;
            bad_byte_count <= 0; stream_pulses <= 0; pkt_active_pulses <= 0;
        end else begin
            if (csr_we_w && csr_addr_w == 8'h0C && csr_data_w[0]) begin
                bad_byte_latched <= 0; bad_byte_count <= 0;
                stream_pulses <= 0; pkt_active_pulses <= 0;
            end
            if (pkt_active && !pkt_active_d)
                pkt_active_pulses <= pkt_active_pulses + 1'b1;
            if (stream_tvalid && stream_tready)
                stream_pulses <= stream_pulses + 1'b1;
            // Fires only in src_fixed mode (0x42 mode); real trace would
            // trigger everywhere and drown the latch.
            if (src_fixed_125 && pkt_active && !in_header &&
                pkt_tvalid && pkt_tready &&
                (pkt_tdata != 8'h42)) begin
                bad_byte_count <= bad_byte_count + 1'b1;
                if (!bad_byte_latched) begin
                    bad_byte_val     <= pkt_tdata;
                    bad_byte_pos     <= pos[7:0];
                    bad_byte_seq     <= latched_seq;
                    bad_stream_td    <= stream_tdata;
                    bad_byte_latched <= 1'b1;
                end
            end
        end
    end

    // ============ Snapshot writer/streamer status ui_clk -> clk125 ============
    // Toggle-sync so multi-bit fields cross atomically (same idiom as
    // ddr_ring_selftest_top / trace_ddr_selftest_top).
    reg        snap_tog = 0;
    reg [21:0] snap_div = 0;
    reg [31:0] snap_wrote, snap_drained, snap_wr_lost;
    reg [28:0] snap_wr_ptr, snap_rd_ptr;
    reg        snap_migcal, snap_calib, snap_overrun, snap_nack_fail;
    always @(posedge ui_clk) begin
        snap_div <= snap_div + 1'b1;
        if (&snap_div) begin
            snap_wrote     <= words_written;
            snap_drained   <= words_drained;
            snap_wr_lost   <= wr_lost_bytes;
            snap_wr_ptr    <= wr_ptr_words;
            snap_rd_ptr    <= rd_ptr_words;
            snap_migcal    <= mig_calib_raw;
            snap_calib     <= calib_done;   // ok, calib_done is in clk200; treat as quasi-static
            snap_overrun   <= ring_overrun;
            snap_nack_fail <= nack_fail;
            snap_tog       <= ~snap_tog;
        end
    end
    reg tog_s0=0, tog_s1=0, tog_s2=0;
    always @(posedge clk125) begin tog_s0<=snap_tog; tog_s1<=tog_s0; tog_s2<=tog_s1; end
    wire snap_valid = tog_s1 ^ tog_s2;
    reg [31:0] wrote_125=0, drained_125=0, wr_lost_125=0;
    reg [28:0] wrptr_125=0, rdptr_125=0;
    reg        migcal_125=0, calib_125=0, overrun_125=0, nack_fail_125=0;
    always @(posedge clk125) if (snap_valid) begin
        wrote_125     <= snap_wrote;
        drained_125   <= snap_drained;
        wr_lost_125   <= snap_wr_lost;
        wrptr_125     <= snap_wr_ptr;
        rdptr_125     <= snap_rd_ptr;
        migcal_125    <= snap_migcal;
        calib_125     <= snap_calib;
        overrun_125   <= snap_overrun;
        nack_fail_125 <= snap_nack_fail;
    end

    // ============ dbg_regfile (existing, clk125) ============
    wire [15:0] ext_addr;
    wire [7:0]  dbg_rdata;
    wire        dbg_rx_good, dbg_rx_bad, dbg_tx_valid;
    wire [1:0]  dbg_selftx_state;
    wire        dbg_selftx_stuck;
    wire        dbg_tx_fifo_ovf, dbg_rx_fifo_ovf, dbg_rx_bad_frame;
    dbg_regfile u_dbg (
        .clk(clk125), .rst(sys_rst), .clr(1'b0),
        .e_no_traceclk(1'b0), .e_mmcm_unlock(1'b0), .e_cap_overflow(overrun_125),
        .e_selftx_stuck(dbg_selftx_stuck), .e_rx_bad_frame(dbg_rx_bad_frame),
        .e_tx_fifo_ovf(dbg_tx_fifo_ovf), .e_rx_fifo_ovf(dbg_rx_fifo_ovf),
        .sys_mmcm_locked(mmcm_sys_locked),
        .trace_mmcm_locked(calib_125),
        .traceclk_active(1'b0),
        .selftx_state(dbg_selftx_state), .pkt_active(pkt_active),
        .lost_cnt(wr_lost_125),
        .gpio_clk_level(1'b0), .gpio_data_level(4'd0),
        .gpio_clk_edge(1'b0), .gpio_data_edge(4'd0),
        .addr(ext_addr[7:0]), .rdata(dbg_rdata)
    );

    // ============ Readout page mux (:5001) ============
    wire dbg_page  = (ext_addr[15:8] == 8'hFF) &&
                     (ext_addr[7:4] >= 4'h1) && (ext_addr[7:4] <= 4'h4);
    wire [7:0] ring_status =
        (ext_addr == 16'hFF50) ? 8'hD1               :   // MAGIC (S1b compatible)
        (ext_addr == 16'hFF51) ? {4'b0, nack_fail_125, overrun_125, migcal_125, calib_125} :
        (ext_addr == 16'hFF52) ? wrote_125[7:0]      :
        (ext_addr == 16'hFF53) ? wrote_125[15:8]     :
        (ext_addr == 16'hFF54) ? wrote_125[23:16]    :
        (ext_addr == 16'hFF55) ? wrote_125[31:24]    :
        (ext_addr == 16'hFF56) ? drained_125[7:0]    :
        (ext_addr == 16'hFF57) ? drained_125[15:8]   :
        (ext_addr == 16'hFF58) ? drained_125[23:16]  :
        (ext_addr == 16'hFF59) ? drained_125[31:24]  :
        (ext_addr == 16'hFF5A) ? wr_lost_125[7:0]    :
        (ext_addr == 16'hFF5B) ? wr_lost_125[15:8]   :
        (ext_addr == 16'hFF5C) ? wr_lost_125[23:16]  :
        (ext_addr == 16'hFF5D) ? wr_lost_125[31:24]  :
        (ext_addr == 16'hFF5E) ? wrptr_125[7:0]      :
        (ext_addr == 16'hFF5F) ? wrptr_125[15:8]     :
        (ext_addr == 16'hFF60) ? wrptr_125[23:16]    :
        (ext_addr == 16'hFF61) ? {3'b0, wrptr_125[28:24]} :
        (ext_addr == 16'hFF62) ? rdptr_125[7:0]      :
        (ext_addr == 16'hFF63) ? rdptr_125[15:8]     :
        (ext_addr == 16'hFF64) ? rdptr_125[23:16]    :
        (ext_addr == 16'hFF65) ? {3'b0, rdptr_125[28:24]} :
        (ext_addr == 16'hFF70) ? BUILD_ID[7:0]       :
        (ext_addr == 16'hFF71) ? BUILD_ID[15:8]      :
        (ext_addr == 16'hFF72) ? BUILD_ID[23:16]     :
        (ext_addr == 16'hFF73) ? BUILD_ID[31:24]     :
        (ext_addr == 16'hFF80) ? 8'hD2               :   // MAGIC: diag latches
        (ext_addr == 16'hFF81) ? {6'b0, src_fixed_125, bad_byte_latched} :
        (ext_addr == 16'hFF82) ? bad_byte_val        :
        (ext_addr == 16'hFF83) ? bad_stream_td       :
        (ext_addr == 16'hFF84) ? bad_byte_pos        :
        (ext_addr == 16'hFF85) ? bad_byte_seq[7:0]   :
        (ext_addr == 16'hFF86) ? bad_byte_seq[15:8]  :
        (ext_addr == 16'hFF87) ? bad_byte_seq[23:16] :
        (ext_addr == 16'hFF88) ? bad_byte_seq[31:24] :
        (ext_addr == 16'hFF89) ? bad_byte_count[7:0]    :
        (ext_addr == 16'hFF8A) ? bad_byte_count[15:8]   :
        (ext_addr == 16'hFF8B) ? bad_byte_count[23:16]  :
        (ext_addr == 16'hFF8C) ? bad_byte_count[31:24]  :
        (ext_addr == 16'hFF8D) ? stream_pulses[7:0]     :
        (ext_addr == 16'hFF8E) ? stream_pulses[15:8]    :
        (ext_addr == 16'hFF8F) ? stream_pulses[23:16]   :
        (ext_addr == 16'hFF90) ? stream_pulses[31:24]   :
        (ext_addr == 16'hFF91) ? pkt_active_pulses[7:0] :
        (ext_addr == 16'hFF92) ? pkt_active_pulses[15:8]:
        (ext_addr == 16'hFF93) ? pkt_active_pulses[23:16]:
        (ext_addr == 16'hFF94) ? pkt_active_pulses[31:24]:
        8'h00;
    wire ring_page = (ext_addr[15:8] == 8'hFF) &&
                     (ext_addr[7:4] >= 4'h5) && (ext_addr[7:4] <= 4'h9);
    wire [7:0]  ext_data;
    assign      ext_data = dbg_page  ? dbg_rdata   :
                           ring_page ? ring_status : 8'h00;

    // ============ fpga_core_net (STREAM=1, clk125) ============
    // r39 lesson: STREAM_PKT_BYTES MUST equal packetiser PKT length. Default
    // 1024 will drift the seq header into the following UDP frame's payload.
    fpga_core_net #(
        .TARGET("XILINX"),
        .STREAM(1),
        .UDP_CHECKSUM_GEN_ENABLE(0),
        .STREAM_DEST_IP(DEST_IP),
        .STREAM_DEST_PORT(DEST_PORT),
        .STREAM_PKT_BYTES(PKT[15:0])
    ) u_eth (
        .clk(clk125), .clk90(clk125_90), .rst(sys_rst),
        .btnu(1'b0), .btnl(1'b0), .btnd(1'b0), .btnr(1'b0), .btnc(1'b0),
        .sw(8'h0), .led(),
        .phy_rx_clk(phy_rx_clk), .phy_rxd(phy_rxd), .phy_rx_ctl(phy_rx_ctl),
        .phy_tx_clk(phy_tx_clk), .phy_txd(phy_txd), .phy_tx_ctl(phy_tx_ctl),
        .phy_reset_n(phy_reset_n), .phy_int_n(1'b1), .phy_pme_n(1'b1),
        .uart_rxd(1'b1), .uart_txd(),
        .dbg_rx_good_frame(dbg_rx_good), .dbg_rx_bad_fcs(dbg_rx_bad),
        .dbg_tx_axis_tvalid(dbg_tx_valid),
        .dbg_selftx_state(dbg_selftx_state), .dbg_selftx_stuck(dbg_selftx_stuck),
        .dbg_tx_fifo_overflow(dbg_tx_fifo_ovf), .dbg_rx_fifo_overflow(dbg_rx_fifo_ovf),
        .dbg_rx_bad_frame(dbg_rx_bad_frame),
        .ext_addr(ext_addr), .ext_data(ext_data),
        .csr_addr(csr_addr_w), .csr_data(csr_data_w), .csr_we(csr_we_w),
        .stream_tdata(pkt_tdata), .stream_tvalid(pkt_tvalid),
        .stream_tready(pkt_tready)
    );

    // ============ LEDs ============
    reg [24:0] hb = 0;
    always @(posedge clk125) hb <= hb + 1'b1;
    assign led0 = mmcm_sys_locked;
    assign led1 = pkt_active ? hb[24] : 1'b0;

    assign phy_mdio = 1'bz;
    assign phy_mdc  = 1'b0;

endmodule

`default_nettype wire
