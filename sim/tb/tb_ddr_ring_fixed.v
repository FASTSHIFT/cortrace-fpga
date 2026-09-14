// tb_ddr_ring_fixed
// =================
// doc 21 S1b bring-up bug: on hardware, feeding a fixed-value byte source
// (0x42) into la_ddr_writer -> DDR ring -> la_ddr_ring_streamer produces
// scrambled bytes (0x87/0xF1/0xE1... instead of 0x42) on the network side.
//
// This bench mirrors the on-board configuration as closely as an integer-clock
// simulation allows, to answer "is it my top wiring or the FPGA environment?"
// Deviations from the working tb_la_ddr_ring baseline:
//   * cap_clk = ui_clk        (same-domain, matches ddr_ring_selftest_top)
//   * cap_valid_in stays high (no CAP_DIV divider, matches the top)
//   * source byte fixed = 0x42 (not a ramp)
// A pass here proves the top-level wiring model is correct, so any failure
// found on the board is an environment/timing artifact, not a topology bug.
// A fail here reproduces the bug in simulation for offline fixing.

`timescale 1ns/1ps
`default_nettype none

module tb_ddr_ring_fixed;
    // Clocks — cap_clk and ui_clk are the SAME clock (like on the board).
    reg clk = 0;                   // shared ui_clk / cap_clk
    reg clk125 = 0;
    always #5   clk    = ~clk;     // 100 MHz
    always #4   clk125 = ~clk125;  // 125 MHz

    reg rst_ui = 1, rst_sys = 1;

    localparam integer LENGTH     = 64;
    localparam [28:0]  RING_BASE  = 29'd0;
    // 16 MB in app-addr units (identical unit to tb_la_ddr_ring, but 1024x
    // bigger — closer to the on-board ring size).
    localparam [28:0]  RING_WORDS = 29'd524288;   // 64 KB (fits behavioural mem)
    localparam integer PKT_WORDS  = 64;

    // Source: fixed 0x42 by default; +define+RAMP flips to a monotone ramp
    // (each cycle: src_byte += 1). This is the ramp mode the on-board top
    // uses in production; the check confirms the datapath preserves ordering
    // (byte k == first_byte + k, mod 256).
    reg [7:0] src_byte = 8'h42;
    reg       src_valid = 0;
`ifdef RAMP
    always @(posedge clk) if (src_valid) src_byte <= src_byte + 8'd1;
`endif

    // ================= WRITER (cap_clk = ui_clk = clk) =================
    wire         wr_start, wr_data_req, wr_addr_req, wr_done;
    wire [127:0] wr_data;
    wire [28:0]  wr_addr;
    wire [28:0]  wr_ptr_words;
    wire [31:0]  words_written, wr_lost_bytes;

    la_ddr_writer #(
        .LENGTH(LENGTH), .IN_BYTES(1),
        .RING_BASE(RING_BASE), .RING_WORDS(RING_WORDS)
    ) u_wr (
        .cap_clk(clk), .cap_rst(rst_ui),
        .cap_byte(src_byte), .cap_valid_in(src_valid), .freeze(1'b0),
        .ui_clk(clk), .ui_rst(rst_ui),
        .ddr3_busy(1'b0),
        .ddr3_wr_start(wr_start),
        .ddr3_wr_data_req(wr_data_req),
        .ddr3_wr_data(wr_data),
        .ddr3_wr_addr_req(wr_addr_req),
        .ddr3_wr_addr(wr_addr),
        .ddr3_wr_done(wr_done),
        .wr_ptr_words(wr_ptr_words),
        .words_written(words_written),
        .wr_lost_bytes(wr_lost_bytes)
    );

    // ================= STREAMER (ui_clk + clk125) =====================
    wire         rd_start, rd_addr_req, rd_data_vld, rd_done;
    wire [28:0]  rd_addr;
    wire [127:0] rd_data;
    wire [7:0]   stream_tdata;
    wire         stream_tvalid;
    reg          stream_tready = 1'b1;
    wire [31:0]  stream_seq;
    wire         stream_rtx;
    wire [28:0]  rd_ptr_words;
    wire [31:0]  words_drained;
    wire         ring_overrun;

    la_ddr_ring_streamer #(
        .LENGTH(LENGTH), .RING_BASE(RING_BASE),
        .RING_WORDS(RING_WORDS), .PKT_WORDS(PKT_WORDS)
    ) u_st (
        .ui_clk(clk), .ui_rst(rst_ui),
        .wr_ptr_words(wr_ptr_words),
        .wr_words_committed(words_written),
        .drain_credit(1'b1),
        .ddr3_rd_start(rd_start),
        .ddr3_rd_addr_req(rd_addr_req),
        .ddr3_rd_addr(rd_addr),
        .ddr3_rd_data_vld(rd_data_vld),
        .ddr3_rd_data(rd_data),
        .ddr3_rd_done(rd_done),
        .nack_valid(1'b0), .nack_start_seq(32'd0), .nack_count(16'd0),
        .nack_busy(), .nack_fail(),
        .clk125(clk125), .sys_rst(rst_sys),
        .stream_tdata(stream_tdata), .stream_tvalid(stream_tvalid),
        .stream_tready(stream_tready),
        .stream_seq(stream_seq), .stream_rtx(stream_rtx),
        .rd_ptr_words(rd_ptr_words), .words_drained(words_drained),
        .ring_overrun(ring_overrun)
    );

    // ================= REAL vendor ctrl + arbiter (same as tb_la_ddr_ring) =
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

    wire        app_en   = wr_busy ? wr_app_en   : rd_app_en;
    wire [2:0]  app_cmd  = wr_busy ? wr_app_cmd  : rd_app_cmd;
    wire [28:0] app_addr = wr_busy ? wr_app_addr : rd_app_addr;

    ddr3_wr_ctrl #(.LENGTH(LENGTH)) u_wrc (
        .ui_clk(clk), .ui_rst(rst_ui),
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
        .ui_clk(clk), .ui_rst(rst_ui),
        .app_rdy(app_rdy), .app_en(rd_app_en), .app_addr(rd_app_addr),
        .app_cmd(rd_app_cmd), .app_rd_data_end(app_rd_data_end),
        .app_rd_data_valid(app_rd_data_valid), .app_rd_data(app_rd_data),
        .ddr3_rd_start(rd_start), .ddr3_rd_req(rd_req), .ddr3_rd_ack(rd_ack),
        .ddr3_rd_addr_req(rd_addr_req), .ddr3_rd_addr(rd_addr),
        .ddr3_rd_data_vld(rd_data_vld), .ddr3_rd_data(rd_data),
        .ddr3_rd_done(rd_done), .ddr3_rd_busy(rd_busy)
    );

    ddr3_arbit u_arb (
        .ui_clk(clk), .ui_rst(rst_ui),
        .ddr3_wr_req(wr_req), .ddr3_wr_done(wr_done),
        .ddr3_rd_req(rd_req), .ddr3_rd_done(rd_done),
        .ddr3_wr_ack(wr_ack), .ddr3_rd_ack(rd_ack)
    );

    // ---- Behavioural MIG with realistic stalls (P0-1 per r36 §4.1) ---------
    // r36 硬伤 B: the trivial MIG model in tb_la_ddr_ring never stalls, so the
    // "concurrent R/W" race window that only opens when write bursts take real
    // physical time is compressed to zero. Add four stall/latency knobs so the
    // TB can *cover* the on-board race mechanism before we accept or reject any
    // hypothesis (蓝方 partial-write / 红方 H1 FIFO leak / H5 wbuf skew / ...).
    //
    // Enabled by default (WITH_MIG_STALL default 1). Set to 0 to run the
    // baseline model (matches tb_la_ddr_ring.v).
    localparam integer WITH_MIG_STALL = 1;

    // Stall parameters (deliberately conservative; scale up if P0-1 doesn't
    // reproduce ≥0.1% pollution).
`ifdef HEAVY_STALL
    localparam integer WDF_STALL_CLK   = 200;
    localparam integer RDY_STALL_CLK   = 200;
    localparam integer RD_LAT_BASE     = 20;
    localparam integer RD_LAT_JITTER   = 80;
    localparam integer NET_STALL_EVERY = 50;
    localparam integer NET_STALL_LEN   = 30;
`else
    localparam integer WDF_STALL_CLK   = 40;   // app_wdf_rdy low after each beat
    localparam integer RDY_STALL_CLK   = 60;   // app_rdy low after each burst cmd
    localparam integer RD_LAT_BASE     = 6;    // base read latency
    localparam integer RD_LAT_JITTER   = 20;   // random jitter added to base
    localparam integer NET_STALL_EVERY = 100;  // stream_tready pulse-low every N clk
    localparam integer NET_STALL_LEN   = 10;   // ... for this many clk
`endif

    localparam integer MEM_WORDS = 8192;
    reg [127:0] mem [0:MEM_WORDS-1];
    integer i0;
    // r37 §1.3 cross-alignment: allow mem init override to prove "错值 = DDR 内容"
    // is stable across all pre-write values (not an artefact of 0x11 specifically).
`ifdef SIM_MEM_INIT_11
    localparam [127:0] MEM_INIT_VAL = 128'h11111111_11111111_11111111_11111111;
`elsif SIM_MEM_INIT_00
    localparam [127:0] MEM_INIT_VAL = 128'h00000000_00000000_00000000_00000000;
`elsif SIM_MEM_INIT_FF
    localparam [127:0] MEM_INIT_VAL = 128'hFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFF;
`else
    localparam [127:0] MEM_INIT_VAL = 128'hDEADBEEF_DEADBEEF_DEADBEEF_DEADBEEF;
`endif
    initial for (i0 = 0; i0 < MEM_WORDS; i0 = i0 + 1) mem[i0] = MEM_INIT_VAL;

    // app_wdf_rdy stall: after each accepted data beat, hold low for
    // WDF_STALL_CLK cycles (models MIG write-data FIFO drain to physical DDR3).
    reg  [15:0] wdf_stall_cnt = 0;
    reg         wdf_rdy_r     = 1'b1;
    always @(posedge clk) begin
        if (rst_ui) begin wdf_stall_cnt <= 0; wdf_rdy_r <= 1'b1; end
        else if (WITH_MIG_STALL == 0) wdf_rdy_r <= 1'b1;
        else if (app_wdf_wren && wdf_rdy_r) begin
            wdf_stall_cnt <= WDF_STALL_CLK;
            wdf_rdy_r     <= 1'b0;
        end else if (wdf_stall_cnt != 0) begin
            wdf_stall_cnt <= wdf_stall_cnt - 1'b1;
        end else begin
            wdf_rdy_r <= 1'b1;
        end
    end
    assign app_wdf_rdy = wdf_rdy_r;

    // app_rdy stall: after each accepted command (app_en pulse), hold low for
    // RDY_STALL_CLK cycles (models MIG bank activate/precharge time).
    reg  [15:0] rdy_stall_cnt = 0;
    reg         rdy_r         = 1'b1;
    always @(posedge clk) begin
        if (rst_ui) begin rdy_stall_cnt <= 0; rdy_r <= 1'b1; end
        else if (WITH_MIG_STALL == 0) rdy_r <= 1'b1;
        else if (app_en && rdy_r) begin
            rdy_stall_cnt <= RDY_STALL_CLK;
            rdy_r         <= 1'b0;
        end else if (rdy_stall_cnt != 0) begin
            rdy_stall_cnt <= rdy_stall_cnt - 1'b1;
        end else begin
            rdy_r <= 1'b1;
        end
    end
    assign app_rdy = rdy_r;

    // ---- Correct MIG write model (r37 §2.3 tb fix) --------------------
    // In real MIG, app_addr is captured on each cmd-accept and app_wdf_data
    // is captured on each data-accept; MIG internally pairs them in insertion
    // order. When we stall wdf/rdy asymmetrically, at data-accept time
    // app_addr no longer corresponds to the correct beat (since app_addr
    // advances only on cmd-accept in the writer). A naive
    // `mem[app_addr] <= app_wdf_data` model drops the address<->data pairing
    // and leaves some addresses UNWRITTEN — a tb artifact that would falsely
    // reproduce "H7" pollution.
    //
    // Fix: FIFO of pending (addr) at cmd-accept; drain into mem on data-accept.
    localparam integer WQ_DEPTH = 256;   // >= 4 bursts worth (LENGTH=64)
    reg [28:0] wq_addr [0:WQ_DEPTH-1];
    reg [7:0]  wq_head = 0, wq_tail = 0;
    integer wq_writes = 0, wq_drains = 0;
    initial for (i0 = 0; i0 < WQ_DEPTH; i0 = i0 + 1) wq_addr[i0] = 29'd0;
    // Only enqueue write commands, not read commands (rd_ctrl also drives
    // app_en when wr_busy=0 via the mux above).
    //
    // Realistic MIG semantics: cmd and data are independent streams; MIG
    // pairs them in insertion order. If data arrives before its matching cmd
    // it must WAIT (MIG buffers up to a small write-data FIFO). We model this
    // by a data queue in parallel with the addr queue; a mem write happens
    // only when BOTH heads are populated.
    wire wr_cmd_accept = wr_busy && wr_app_en && app_rdy;
    wire wdf_accept    = app_wdf_wren && app_wdf_rdy;
    reg [127:0] wq_data [0:WQ_DEPTH-1];
    reg [7:0]   wq_dh = 0, wq_dt = 0;      // data head / tail (8-bit is fine for depth<=256)
    initial for (i0 = 0; i0 < WQ_DEPTH; i0 = i0 + 1) wq_data[i0] = 128'd0;

    // Enqueue cmd addr on wr_cmd_accept, enqueue data on wdf_accept.
    always @(posedge clk) begin
        if (rst_ui) begin wq_tail <= 0; wq_dt <= 0; wq_head <= 0; wq_dh <= 0; end
        else begin
            if (wr_cmd_accept) begin
                wq_addr[wq_tail] <= wr_app_addr;
                wq_tail <= wq_tail + 1;
                wq_writes <= wq_writes + 1;
            end
            if (wdf_accept) begin
                wq_data[wq_dt] <= app_wdf_data;
                wq_dt <= wq_dt + 1;
            end
        end
    end

    // Pair heads: whenever both queues have entries, commit one mem write.
    // Using a small state to advance one commit per clk (matches MIG's
    // committed-order semantics).
    wire cmd_avail  = (wq_tail != wq_head);
    wire data_avail = (wq_dt   != wq_dh);
    always @(posedge clk) begin
        if (rst_ui) begin /* heads reset above */ end
        else if (cmd_avail && data_avail) begin
            mem[wq_addr[wq_head][15:3]] <= wq_data[wq_dh];
            wq_head   <= wq_head + 1;
            wq_dh     <= wq_dh   + 1;
            wq_drains <= wq_drains + 1;
        end
    end

    // Read path with jittery latency. Read command is app_en && cmd==1 && rdy.
    // Instead of a fixed 2-stage pipe we push {addr, valid} into a small shift
    // register keyed by RD_LAT_BASE + $random%RD_LAT_JITTER so different reads
    // can interleave differently.
    localparam integer RD_MAX_LAT = RD_LAT_BASE + RD_LAT_JITTER + 4;
    reg         rd_lat_v [0:RD_MAX_LAT-1];
    reg [127:0] rd_lat_d [0:RD_MAX_LAT-1];
    integer rk;
    initial for (rk = 0; rk < RD_MAX_LAT; rk = rk + 1) begin
        rd_lat_v[rk] = 0; rd_lat_d[rk] = 0;
    end
    reg [31:0] rand_seed = 32'h1;
    always @(posedge clk) begin
        // shift the pipe forward every clock
        for (rk = RD_MAX_LAT - 1; rk > 0; rk = rk - 1) begin
            rd_lat_v[rk] <= rd_lat_v[rk-1];
            rd_lat_d[rk] <= rd_lat_d[rk-1];
        end
        rd_lat_v[0] <= 1'b0;
        // accept a read cmd only when ready
        if (app_en && (app_cmd == 3'd1) && app_rdy) begin
            // deterministic pseudo-random jitter derived from addr to avoid $random
            // side effects on other RNG consumers.
            rand_seed <= rand_seed ^ {app_addr, 3'b0};
            // slot the data at (RD_LAT_BASE + rand%RD_LAT_JITTER)
            begin : slot_it
                integer slot;
                if (WITH_MIG_STALL == 0) slot = 2;
                else slot = RD_LAT_BASE + (rand_seed[7:0] % (RD_LAT_JITTER==0?1:RD_LAT_JITTER));
                rd_lat_v[slot] <= 1'b1;
                rd_lat_d[slot] <= mem[app_addr[15:3]];
            end
        end
    end
    assign app_rd_data       = rd_lat_d[RD_MAX_LAT-1];
    assign app_rd_data_valid = rd_lat_v[RD_MAX_LAT-1];
    assign app_rd_data_end   = rd_lat_v[RD_MAX_LAT-1];

    // Network backpressure: pulse stream_tready low every NET_STALL_EVERY clk
    // for NET_STALL_LEN clk to simulate downstream slower than writer.
    reg [15:0] net_stall_cnt = 0;
    reg        net_stall_active = 0;
    always @(posedge clk125) begin
        if (rst_sys) begin net_stall_cnt <= 0; net_stall_active <= 0; end
        else if (WITH_MIG_STALL == 0) net_stall_active <= 0;
        else begin
            net_stall_cnt <= net_stall_cnt + 1'b1;
            if (net_stall_cnt == NET_STALL_EVERY) begin
                net_stall_active <= 1'b1;
                net_stall_cnt <= 0;
            end else if (net_stall_active && net_stall_cnt == NET_STALL_LEN) begin
                net_stall_active <= 1'b0;
                net_stall_cnt <= 0;
            end
        end
    end
    // (stream_tready is a reg initialised to 1; drive it here instead)
    always @(*) stream_tready = (WITH_MIG_STALL == 0) ? 1'b1 : ~net_stall_active;

    // ---- byte tap: capture stream_tdata + log every bad byte ---------------
    // r36 §4.1 P1 step 5: dump each (rx_offset, expected, actual) so pattern
    // is inspectable offline.
    integer rx_cnt = 0;
    integer bad_cnt = 0;
    integer bad_fd  = 0;
    reg [7:0] rx_bytes [0:255];
`ifdef RAMP
    reg [7:0] rx_prev = 0;
    reg       have_prev = 0;
`endif
    initial bad_fd = $fopen("bad_bytes.csv", "w");
    always @(posedge clk125) begin
        if (stream_tvalid && stream_tready) begin
            if (rx_cnt < 256) rx_bytes[rx_cnt] <= stream_tdata;
`ifdef RAMP
            if (have_prev && stream_tdata !== ((rx_prev + 8'd1) & 8'hFF)) begin
                bad_cnt <= bad_cnt + 1;
                if (bad_fd) $fdisplay(bad_fd, "%0d,%0d,%02x,%02x,%08x,%b",
                    $time, rx_cnt, (rx_prev + 8'd1) & 8'hFF, stream_tdata,
                    stream_seq, stream_rtx);
            end
            rx_prev   <= stream_tdata;
            have_prev <= 1'b1;
`else
            if (stream_tdata !== 8'h42) begin
                bad_cnt <= bad_cnt + 1;
                if (bad_fd) $fdisplay(bad_fd, "%0d,%0d,42,%02x,%08x,%b",
                    $time, rx_cnt, stream_tdata,
                    stream_seq, stream_rtx);
            end
`endif
            rx_cnt <= rx_cnt + 1;
        end
    end

    // ---- r37 §2.3 experiment D: H7-A/B/C variant probes ------------------
    // For each streamer read burst we ask: at the moment ddr3_rd_start goes
    // high, is mem[rd_addr] already the committed "0x42×16" pattern, or is it
    // still stale (equal to MEM_INIT_VAL)? This distinguishes H7-A/C ("streamer
    // shot before writer's data physically landed") from H7-B ("wbuf/out_idx
    // race — write path itself corrupts the mem contents").
    //
    // In parallel we count end_cmd_cnt vs end_data_cnt skew inside the vendor
    // wr_ctrl: H7-A/C requires cmd finishing before data (cmd_first > 0), while
    // H7-B is orthogonal (cmd/data can be simultaneous).
    //
    // Enabled by default (SIM_H7_PROBE gate kept for future gated builds).
    localparam [127:0] COMMITTED_PATTERN_FIXED = {16{8'h42}};
    integer rd_at_stale       = 0;
    integer rd_at_committed   = 0;
    integer rd_at_other       = 0;
    integer cmd_first_cnt     = 0;
    integer data_first_cnt    = 0;
    integer same_clk_cnt      = 0;
    integer cmd_data_max_skew = 0;
    integer cur_skew          = 0;
    integer end_cmd_pulses    = 0;
    integer end_data_pulses   = 0;
    reg     cmd_pending       = 1'b0;   // cmd finished but data hasn't
    reg     data_pending      = 1'b0;   // data finished but cmd hasn't

    // Sample committed-ness of first word in the read burst on rd_start rising.
    // Note: ddr3_rd_addr is set the clk *before* rd_start goes high (per
    // streamer R_START), so on the rd_start clk we look at mem[rd_addr>>3].
    always @(posedge clk) begin
        if (rd_start) begin
`ifdef RAMP
            // In ramp mode we don't have a fixed committed pattern; compare
            // against MEM_INIT_VAL only.
            if (mem[rd_addr[15:3]] == MEM_INIT_VAL)
                rd_at_stale <= rd_at_stale + 1;
            else
                rd_at_committed <= rd_at_committed + 1;
`else
            if (mem[rd_addr[15:3]] == COMMITTED_PATTERN_FIXED)
                rd_at_committed <= rd_at_committed + 1;
            else if (mem[rd_addr[15:3]] == MEM_INIT_VAL)
                rd_at_stale <= rd_at_stale + 1;
            else
                rd_at_other <= rd_at_other + 1;
`endif
        end
    end

    // Track end_cmd_cnt vs end_data_cnt ordering *inside the same burst*.
    // A burst starts on ddr3_wr_start (from writer) and finishes when both
    // end_cmd_cnt and end_data_cnt have pulsed. We tag which one fired first.
    // Bind hierarchical ref to local wires for readability + iverilog stability.
    // end_cmd_cnt = app_en & app_rdy & (cmd_cnt==MAX_NUM)
    // end_data_cnt = app_wdf_wren & app_wdf_rdy & (data_cnt==MAX_NUM)
    // We re-derive here to avoid iverilog hierarchical-ref quirks.
    wire end_cmd_w  = wr_app_en && app_rdy && (u_wrc.cmd_cnt == (LENGTH-1));
    wire end_data_w = app_wdf_wren && app_wdf_rdy && (u_wrc.data_cnt == (LENGTH-1));
    always @(posedge clk) begin
        if (end_cmd_w)  end_cmd_pulses  <= end_cmd_pulses  + 1;
        if (end_data_w) end_data_pulses <= end_data_pulses + 1;
    end
    always @(posedge clk) begin
        if (rst_ui) begin
            cmd_pending  <= 0; data_pending <= 0; cur_skew <= 0;
        end else begin
            // Case 1: cmd was already pending (cmd fired earlier alone) and
            //         data arrives now -> H7-A/C (cmd-first) closure
            if (cmd_pending && end_data_w) begin
                cmd_first_cnt <= cmd_first_cnt + 1;
                if (cur_skew > cmd_data_max_skew) cmd_data_max_skew <= cur_skew;
                cmd_pending <= 1'b0;
                cur_skew    <= 0;
                // if cmd also fires now start a new pending pair on cmd side
                if (end_cmd_w) cmd_pending <= 1'b1;
            end
            // Case 2: data was pending, cmd arrives -> data-first closure
            else if (data_pending && end_cmd_w) begin
                data_first_cnt <= data_first_cnt + 1;
                data_pending <= 1'b0;
                cur_skew     <= 0;
                if (end_data_w) data_pending <= 1'b1;
            end
            // Case 3: both fire same clk with nothing pending -> tied
            else if (end_cmd_w && end_data_w) begin
                same_clk_cnt <= same_clk_cnt + 1;
                cur_skew     <= 0;
            end
            // Case 4: cmd alone -> start cmd-pending
            else if (end_cmd_w) begin
                cmd_pending <= 1'b1;
                cur_skew    <= 0;
            end
            // Case 5: data alone -> start data-pending
            else if (end_data_w) begin
                data_pending <= 1'b1;
                cur_skew     <= 0;
            end
            // Case 6: pending in flight -> tick skew
            else if (cmd_pending || data_pending) begin
                cur_skew <= cur_skew + 1;
            end
        end
    end

    // Per-word committedness probe: for every mem lookup driven by a read cmd
    // (app_en && app_cmd==1 && app_rdy), record whether the mem word at that
    // address is committed (COMMITTED_PATTERN_FIXED) or stale (MEM_INIT_VAL).
    // This catches H7-A/C where wr_ptr advanced before ALL words of the burst
    // were committed (rd_start-only sampling only sees the first word).
    integer rd_word_at_committed = 0;
    integer rd_word_at_stale     = 0;
    integer rd_word_at_other     = 0;
    always @(posedge clk) begin
        if (app_en && (app_cmd == 3'd1) && app_rdy) begin
`ifdef RAMP
            if (mem[app_addr[15:3]] == MEM_INIT_VAL)
                rd_word_at_stale <= rd_word_at_stale + 1;
            else
                rd_word_at_committed <= rd_word_at_committed + 1;
`else
            if (mem[app_addr[15:3]] == COMMITTED_PATTERN_FIXED)
                rd_word_at_committed <= rd_word_at_committed + 1;
            else if (mem[app_addr[15:3]] == MEM_INIT_VAL)
                rd_word_at_stale <= rd_word_at_stale + 1;
            else
                rd_word_at_other <= rd_word_at_other + 1;
`endif
        end
    end

    // ---- stimulus ----
    integer i;
    initial begin
        // reset
        rst_ui = 1; rst_sys = 1; src_valid = 0;
        #100;
        rst_ui = 0; rst_sys = 0;
        #100;
        src_valid = 1;                       // start feeding 0x42 forever

        // let it run long enough to produce many packets
        repeat (200000) @(posedge clk);

        $display("==== fixed-source diag ====");
        $display("mem init value: %h", MEM_INIT_VAL);
        $display("words_written = %0d, words_drained = %0d", words_written, words_drained);
        $display("wr_lost = %0d, ring_overrun = %0b", wr_lost_bytes, ring_overrun);
        $display("stream bytes received = %0d, non-0x42 count = %0d", rx_cnt, bad_cnt);
        $display("first 32 rx bytes:");
        for (i = 0; i < 32 && i < rx_cnt; i = i + 1)
            $write(" %02x", rx_bytes[i]);
        $display("");
        // r37 experiment D result summary
        $display("---- H7-A/B/C probe (r37 §2.3) ----");
        $display("rd_start @committed        = %0d", rd_at_committed);
        $display("rd_start @stale            = %0d", rd_at_stale);
        $display("rd_start @other            = %0d", rd_at_other);
        $display("per-word rd @committed     = %0d", rd_word_at_committed);
        $display("per-word rd @stale         = %0d", rd_word_at_stale);
        $display("per-word rd @other         = %0d", rd_word_at_other);
        $display("wq addr enqueues                : %0d", wq_writes);
        $display("wq data drains (mem writes)     : %0d", wq_drains);
        $display("end_cmd_cnt total pulses        : %0d", end_cmd_pulses);
        $display("end_data_cnt total pulses       : %0d", end_data_pulses);
        $display("end_cmd_cnt before end_data_cnt : %0d", cmd_first_cnt);
        $display("end_data_cnt before end_cmd_cnt : %0d", data_first_cnt);
        $display("same-clk                        : %0d", same_clk_cnt);
        $display("max cmd->data skew (clk)        : %0d", cmd_data_max_skew);
        if (bad_cnt == 0 && rx_cnt > 0) begin
            $display("RESULT=ALL_PASS (all received bytes == 0x42)");
        end else if (rx_cnt == 0) begin
            $display("RESULT=FAIL_NO_DATA (streamer never emitted)");
        end else begin
            $display("RESULT=FAIL_SCRAMBLED (bad_cnt=%0d)", bad_cnt);
        end
        $finish;
    end

endmodule

`default_nettype wire
