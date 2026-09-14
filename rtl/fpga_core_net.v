/*

Copyright (c) 2014-2018 Alex Forencich

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

*/

// Language: Verilog 2001

`resetall
`timescale 1ns / 1ps
`default_nettype none

/*
 * FPGA core logic
 *
 * NOTE: this is a LOCAL COPY of verilog-ethernet's NexysVideo example
 * core (syn/external/verilog-ethernet/example/NexysVideo/fpga/rtl/fpga_core.v),
 * forked into the A7-Lite bring-up so the submodule stays pristine.
 * A7-Lite-specific changes vs upstream:
 *   - local_ip/gateway_ip set to 192.168.10.42 / .1 (match LAN)
 *   - eth_mac_1g_rgmii_fifo USE_CLK90 = "FALSE" (RTL8211E straps TX delay on;
 *     driving TXC 90-deg-shifted on top double-delays -> no TX. See
 *     docs/artix7-port/stage3-bringup/03-rgmii-net-link.md)
 *   - added dbg_rx_good_frame / dbg_rx_bad_fcs / dbg_tx_axis_tvalid taps
 * Module renamed fpga_core -> fpga_core_net to avoid clashing with the
 * pristine submodule definition.
 */
module fpga_core_net #
(
    parameter TARGET = "GENERIC",
    // Self-initiated UDP streaming TX (proposal 18 stage 2). When STREAM=1 a
    // standalone FSM sends UDP packets to a fixed dest (STREAM_DEST_IP:
    // STREAM_DEST_PORT) WITHOUT any RX trigger, pulling payload from the
    // stream_* AXIS input. The RX-echo TX path (:5001/:5002 etc.) is preserved
    // and arbitrated: self-TX only drives the UDP TX input when no RX-echo is
    // in flight. STREAM=0 keeps the original RX-echo-only behaviour.
    parameter STREAM = 0,
    parameter [31:0] STREAM_DEST_IP = {8'd192, 8'd168, 8'd10, 8'd245},
    parameter [15:0] STREAM_DEST_PORT = 16'd5555,
    parameter [15:0] STREAM_PKT_BYTES = 16'd1024,   // payload bytes per UDP packet
    // UDP TX checksum generation. The verilog-ethernet udp_checksum_gen stage
    // stalls under a continuous self-TX stream (header FIFO never advances ->
    // no IP frame / ARP egress; root-caused in rtl/sim/udp_tx_streamer_tb.v).
    // UDP checksum is optional for IPv4 (0 = "not computed", RFC 768), so the
    // streaming top disables it. Echo path is unaffected (it already sends
    // checksum=0). Default 1 preserves the original behaviour.
    parameter UDP_CHECKSUM_GEN_ENABLE = 1
)
(
    /*
     * Clock: 125MHz
     * Synchronous reset
     */
    input  wire       clk,
    input  wire       clk90,
    input  wire       rst,

    /*
     * GPIO
     */
    input  wire       btnu,
    input  wire       btnl,
    input  wire       btnd,
    input  wire       btnr,
    input  wire       btnc,
    input  wire [7:0] sw,
    output wire [7:0] led,

    /*
     * Ethernet: 1000BASE-T RGMII
     */
    input  wire       phy_rx_clk,
    input  wire [3:0] phy_rxd,
    input  wire       phy_rx_ctl,
    output wire       phy_tx_clk,
    output wire [3:0] phy_txd,
    output wire       phy_tx_ctl,
    output wire       phy_reset_n,
    input  wire       phy_int_n,
    input  wire       phy_pme_n,

    /*
     * UART: 115200 bps, 8N1
     */
    input  wire       uart_rxd,
    output wire       uart_txd,

    /*
     * Bring-up debug taps (added for A7-Lite net bring-up):
     *   dbg_rx_good_frame : pulses when a CRC-good RX frame completes
     *   dbg_rx_bad_fcs    : pulses on a CRC-bad RX frame (RGMII sampling off)
     *   dbg_tx_axis_tvalid: MAC has a frame to transmit (pre-RGMII; legal load)
     */
    output wire       dbg_rx_good_frame,
    output wire       dbg_rx_bad_fcs,
    output wire       dbg_tx_axis_tvalid,

    /*
     * Observability taps (proposal 30, P1). Expose internal FSM/error state
     * that was previously invisible or discarded, so the host debug regfile
     * can pinpoint which pipeline stage failed instead of guessing from
     * "0 packets". All in the clk (125MHz) domain.
     *   dbg_selftx_state   : self-TX FSM state (0=IDLE 1=HDR 2=SEND 3=BACKOFF)
     *   dbg_selftx_stuck   : pulses when ST_HDR times out (ARP not resolved) ->
     *                        the self-TX/ARP deadlock (HANDOFF 7.1); error 0x401
     *   dbg_tx_fifo_overflow / dbg_rx_fifo_overflow : MAC FIFO overflows
     *                        (were tied off and thrown away)
     *   dbg_rx_bad_frame   : MAC RX bad-frame pulse (was tied off)
     */
    output wire [1:0] dbg_selftx_state,
    output wire       dbg_selftx_stuck,
    output wire       dbg_tx_fifo_overflow,
    output wire       dbg_rx_fifo_overflow,
    output wire       dbg_rx_bad_frame,

    /*
     * External readout source (Stage-4 V1): when a UDP frame arrives on
     * port EXT_PORT (5001), the reply payload bytes come from this external
     * source instead of an echo. ext_addr is the in-frame byte position
     * (so the PC reads back an addressed table); ext_data is the byte at
     * that address. Used to stream the eye-scan results table out over UDP.
     * Tie ext_data=0 if unused.
     */
    output wire [15:0] ext_addr,
    input  wire [7:0] ext_data,

    /*
     * Control-write port (frequency-sweep CSRs, doc 15): a UDP frame to
     * CTRL_PORT (5002) carries {reg_addr, reg_value} as its first two payload
     * bytes. csr_we pulses for one clk when a valid write has been latched;
     * csr_addr/csr_data hold the address/value. Lets the PC set EYE_DELAY and
     * trigger a soft capture re-arm at runtime, no reflash. Leave unused
     * outputs disconnected if not needed.
     */
    output reg  [7:0]  csr_addr,
    output reg  [7:0]  csr_data,
    output reg         csr_we,

    /*
     * Self-initiated streaming TX input (proposal 18, STREAM=1). AXIS byte
     * stream of payload to push out over UDP to STREAM_DEST_IP:PORT. The core
     * packetises STREAM_PKT_BYTES per UDP frame. stream_tready backpressures
     * the producer (the SWO FIFO) when the TX path / ARP is busy. Tie
     * stream_tvalid=0 if unused (STREAM=0).
     */
    input  wire [7:0]  stream_tdata,
    input  wire        stream_tvalid,
    output wire        stream_tready
);

// AXI between MAC and Ethernet modules
wire [7:0] rx_axis_tdata;
wire rx_axis_tvalid;
wire rx_axis_tready;
wire rx_axis_tlast;
wire rx_axis_tuser;

wire [7:0] tx_axis_tdata;
wire tx_axis_tvalid;
wire tx_axis_tready;
wire tx_axis_tlast;
wire tx_axis_tuser;

// Ethernet frame between Ethernet modules and UDP stack
wire rx_eth_hdr_ready;
wire rx_eth_hdr_valid;
wire [47:0] rx_eth_dest_mac;
wire [47:0] rx_eth_src_mac;
wire [15:0] rx_eth_type;
wire [7:0] rx_eth_payload_axis_tdata;
wire rx_eth_payload_axis_tvalid;
wire rx_eth_payload_axis_tready;
wire rx_eth_payload_axis_tlast;
wire rx_eth_payload_axis_tuser;

wire tx_eth_hdr_ready;
wire tx_eth_hdr_valid;
wire [47:0] tx_eth_dest_mac;
wire [47:0] tx_eth_src_mac;
wire [15:0] tx_eth_type;
wire [7:0] tx_eth_payload_axis_tdata;
wire tx_eth_payload_axis_tvalid;
wire tx_eth_payload_axis_tready;
wire tx_eth_payload_axis_tlast;
wire tx_eth_payload_axis_tuser;

// IP frame connections
wire rx_ip_hdr_valid;
wire rx_ip_hdr_ready;
wire [47:0] rx_ip_eth_dest_mac;
wire [47:0] rx_ip_eth_src_mac;
wire [15:0] rx_ip_eth_type;
wire [3:0] rx_ip_version;
wire [3:0] rx_ip_ihl;
wire [5:0] rx_ip_dscp;
wire [1:0] rx_ip_ecn;
wire [15:0] rx_ip_length;
wire [15:0] rx_ip_identification;
wire [2:0] rx_ip_flags;
wire [12:0] rx_ip_fragment_offset;
wire [7:0] rx_ip_ttl;
wire [7:0] rx_ip_protocol;
wire [15:0] rx_ip_header_checksum;
wire [31:0] rx_ip_source_ip;
wire [31:0] rx_ip_dest_ip;
wire [7:0] rx_ip_payload_axis_tdata;
wire rx_ip_payload_axis_tvalid;
wire rx_ip_payload_axis_tready;
wire rx_ip_payload_axis_tlast;
wire rx_ip_payload_axis_tuser;

wire tx_ip_hdr_valid;
wire tx_ip_hdr_ready;
wire [5:0] tx_ip_dscp;
wire [1:0] tx_ip_ecn;
wire [15:0] tx_ip_length;
wire [7:0] tx_ip_ttl;
wire [7:0] tx_ip_protocol;
wire [31:0] tx_ip_source_ip;
wire [31:0] tx_ip_dest_ip;
wire [7:0] tx_ip_payload_axis_tdata;
wire tx_ip_payload_axis_tvalid;
wire tx_ip_payload_axis_tready;
wire tx_ip_payload_axis_tlast;
wire tx_ip_payload_axis_tuser;

// UDP frame connections
wire rx_udp_hdr_valid;
wire rx_udp_hdr_ready;
wire [47:0] rx_udp_eth_dest_mac;
wire [47:0] rx_udp_eth_src_mac;
wire [15:0] rx_udp_eth_type;
wire [3:0] rx_udp_ip_version;
wire [3:0] rx_udp_ip_ihl;
wire [5:0] rx_udp_ip_dscp;
wire [1:0] rx_udp_ip_ecn;
wire [15:0] rx_udp_ip_length;
wire [15:0] rx_udp_ip_identification;
wire [2:0] rx_udp_ip_flags;
wire [12:0] rx_udp_ip_fragment_offset;
wire [7:0] rx_udp_ip_ttl;
wire [7:0] rx_udp_ip_protocol;
wire [15:0] rx_udp_ip_header_checksum;
wire [31:0] rx_udp_ip_source_ip;
wire [31:0] rx_udp_ip_dest_ip;
wire [15:0] rx_udp_source_port;
wire [15:0] rx_udp_dest_port;
wire [15:0] rx_udp_length;
wire [15:0] rx_udp_checksum;
wire [7:0] rx_udp_payload_axis_tdata;
wire rx_udp_payload_axis_tvalid;
wire rx_udp_payload_axis_tready;
wire rx_udp_payload_axis_tlast;
wire rx_udp_payload_axis_tuser;

wire tx_udp_hdr_valid;
wire tx_udp_hdr_ready;
wire [5:0] tx_udp_ip_dscp;
wire [1:0] tx_udp_ip_ecn;
wire [7:0] tx_udp_ip_ttl;
wire [31:0] tx_udp_ip_source_ip;
wire [31:0] tx_udp_ip_dest_ip;
wire [15:0] tx_udp_source_port;
wire [15:0] tx_udp_dest_port;
wire [15:0] tx_udp_length;
wire [15:0] tx_udp_checksum;
wire [7:0] tx_udp_payload_axis_tdata;
wire tx_udp_payload_axis_tvalid;
wire tx_udp_payload_axis_tready;
wire tx_udp_payload_axis_tlast;
wire tx_udp_payload_axis_tuser;

wire [7:0] rx_fifo_udp_payload_axis_tdata;
wire rx_fifo_udp_payload_axis_tvalid;
wire rx_fifo_udp_payload_axis_tready;
wire rx_fifo_udp_payload_axis_tlast;
wire rx_fifo_udp_payload_axis_tuser;

wire [7:0] tx_fifo_udp_payload_axis_tdata;
wire tx_fifo_udp_payload_axis_tvalid;
wire tx_fifo_udp_payload_axis_tready;
wire tx_fifo_udp_payload_axis_tlast;
wire tx_fifo_udp_payload_axis_tuser;

// Configuration
// MAC is locally-administered (first octet 0x02): CA:FE + A7 nods to the
// Artix-7. IP .42 is "the answer" and avoids the previously-recycled .200.
wire [47:0] local_mac   = 48'h02_CA_FE_A7_7E_5C;
wire [31:0] local_ip    = {8'd192, 8'd168, 8'd10,  8'd42};
wire [31:0] gateway_ip  = {8'd192, 8'd168, 8'd10,  8'd1};
wire [31:0] subnet_mask = {8'd255, 8'd255, 8'd255, 8'd0};

// IP ports not used
assign rx_ip_hdr_ready = 1;
assign rx_ip_payload_axis_tready = 1;

assign tx_ip_hdr_valid = 0;
assign tx_ip_dscp = 0;
assign tx_ip_ecn = 0;
assign tx_ip_length = 0;
assign tx_ip_ttl = 0;
assign tx_ip_protocol = 0;
assign tx_ip_source_ip = 0;
assign tx_ip_dest_ip = 0;
assign tx_ip_payload_axis_tdata = 0;
assign tx_ip_payload_axis_tvalid = 0;
assign tx_ip_payload_axis_tlast = 0;
assign tx_ip_payload_axis_tuser = 0;

// ------------------------------------------------------------------
// V0 (Stage-4) hook: FPGA-originated golden frame egress test.
//   - UDP port 1234 : original loopback/echo (network regression)
//   - UDP port 5000 : reply with an FPGA-internal GOLDEN frame instead
//                     of the received bytes. Proves FPGA-sourced bytes
//                     traverse the UDP egress byte-exact on real silicon
//                     (the foundation every later trace stage stands on).
// We reuse ALL the verified echo header/length/handshake machinery and
// only substitute the payload DATA at the FIFO input. So the PC sends
// exactly GOLDEN_LEN bytes to :5000 and must get GOLDEN_LEN golden bytes
// back. See docs/artix7-port/PLAN_STAGE4.md (V0).
// ------------------------------------------------------------------
wire golden_cond = rx_udp_dest_port == 16'd5000;
wire ext_cond    = rx_udp_dest_port == 16'd5001;
wire ctrl_cond   = rx_udp_dest_port == 16'd5002;
wire match_cond = (rx_udp_dest_port == 16'd1234) || golden_cond || ext_cond || ctrl_cond;
wire no_match = !match_cond;

// latched "this frame targets the golden/ext port", aligned with match_cond_reg
reg golden_reg = 0;
reg ext_reg = 0;
reg ctrl_reg = 0;

// payload byte position within the current frame (counts FIFO-input beats)
reg [15:0] golden_idx = 0;
// paged readout base: the first two received payload bytes of the request
// set a 16-bit base address, so the PC can page through a buffer larger
// than one UDP payload. ext_addr = base + position. (Reply bytes 0..1 are
// therefore not meaningful data; the PC discards them.)
reg [15:0] ext_base = 0;
always @(posedge clk) begin
    if (rst) begin
        golden_idx <= 0;
        ext_base   <= 0;
    end else if (rx_fifo_udp_payload_axis_tvalid && rx_fifo_udp_payload_axis_tready) begin
        if (rx_fifo_udp_payload_axis_tlast)
            golden_idx <= 0;
        else
            golden_idx <= golden_idx + 1'b1;
        if (golden_idx == 16'd0) ext_base[7:0]  <= rx_udp_payload_axis_tdata;
        if (golden_idx == 16'd1) ext_base[15:8] <= rx_udp_payload_axis_tdata;
    end
end

// Control-write port (:5002) CSR latch. On a ctrl frame, byte0 -> csr_addr,
// byte1 -> csr_data; pulse csr_we for one cycle at end-of-frame so the write
// is atomic (both bytes captured). ctrl_reg (set in the dispatch latch below)
// gates this to ctrl frames only.
reg [7:0] ctrl_addr_l = 0, ctrl_data_l = 0;
always @(posedge clk) begin
    csr_we <= 1'b0;
    if (rst) begin
        ctrl_addr_l <= 0; ctrl_data_l <= 0;
        csr_addr <= 0; csr_data <= 0;
    end else if (rx_fifo_udp_payload_axis_tvalid && rx_fifo_udp_payload_axis_tready) begin
        if (ctrl_reg) begin
            if (golden_idx == 16'd0) ctrl_addr_l <= rx_udp_payload_axis_tdata;
            if (golden_idx == 16'd1) ctrl_data_l <= rx_udp_payload_axis_tdata;
            if (rx_fifo_udp_payload_axis_tlast) begin
                csr_addr <= ctrl_addr_l;
                csr_data <= ctrl_data_l;
                csr_we   <= 1'b1;     // one-cycle write strobe
            end
        end
    end
end

// external readout addressing: base (from request bytes 0..1) + position.
// Data starts at request position 2 (after the 2 base bytes), so by then
// ext_base is fully latched and ext_addr is contiguous from `base`.
//   reply[p] (p>=2) = source[ base + (p-2) ]
// NOTE: the CAP_RAW BRAM read (`rrd`) has 1 cycle of latency, so the FIRST
// data byte of each reply actually repeats source[base] (a stale read). An
// RTL address-advance was tried but did not reliably remove it on real
// readout (AXI handshake stall on the first beat). trace_dump compensates
// deterministically by requesting n+1 bytes per page and dropping the leading
// duplicate (verified 0.000% on real trace, doc 14 §31). Keep the simple
// position mapping here.
wire [15:0] ext_pos = (golden_idx >= 16'd2) ? (golden_idx - 16'd2) : 16'd0;
assign ext_addr = ext_base + ext_pos;

// GOLDEN pattern: a 4-byte TPIU full-sync prefix (FF FF FF 7F) ONCE at the
// start of the frame, then a monotonic ramp 0xC0,0xC1,... that continues
// across the whole frame (wraps at 256). The ramp catches byte
// duplication / drops / off-by-one; the one-shot sync prefix makes the
// frame recognisable on the wire and mirrors a real TPIU frame header.
reg [7:0] golden_byte;
always @(*) begin
    case (golden_idx)
        16'd0: golden_byte = 8'hFF;
        16'd1: golden_byte = 8'hFF;
        16'd2: golden_byte = 8'hFF;
        16'd3: golden_byte = 8'h7F;
        default: golden_byte = 8'hC0 + golden_idx[7:0];
    endcase
end

reg match_cond_reg = 0;
reg no_match_reg = 0;

always @(posedge clk) begin
    if (rst) begin
        match_cond_reg <= 0;
        no_match_reg <= 0;
        golden_reg <= 0;
        ext_reg <= 0;
        ctrl_reg <= 0;
    end else begin
        if (rx_udp_payload_axis_tvalid) begin
            if ((!match_cond_reg && !no_match_reg) ||
                (rx_udp_payload_axis_tvalid && rx_udp_payload_axis_tready && rx_udp_payload_axis_tlast)) begin
                match_cond_reg <= match_cond;
                no_match_reg <= no_match;
                golden_reg <= golden_cond;
                ext_reg <= ext_cond;
                ctrl_reg <= ctrl_cond;
            end
        end else begin
            match_cond_reg <= 0;
            no_match_reg <= 0;
            golden_reg <= 0;
            ext_reg <= 0;
            ctrl_reg <= 0;
        end
    end
end

// ---- TX UDP input: RX-echo (always) optionally arbitrated with self-TX ----
generate
if (STREAM == 0) begin : g_echo_only
    // Original behaviour: TX UDP driven purely by the RX-echo path.
    assign tx_udp_hdr_valid = rx_udp_hdr_valid && match_cond;
    assign rx_udp_hdr_ready = (tx_eth_hdr_ready && match_cond) || no_match;
    assign tx_udp_ip_dscp = 0;
    assign tx_udp_ip_ecn = 0;
    assign tx_udp_ip_ttl = 64;
    assign tx_udp_ip_source_ip = local_ip;
    assign tx_udp_ip_dest_ip = rx_udp_ip_source_ip;
    assign tx_udp_source_port = rx_udp_dest_port;
    assign tx_udp_dest_port = rx_udp_source_port;
    assign tx_udp_length = rx_udp_length;
    assign tx_udp_checksum = 0;

    assign tx_udp_payload_axis_tdata  = tx_fifo_udp_payload_axis_tdata;
    assign tx_udp_payload_axis_tvalid = tx_fifo_udp_payload_axis_tvalid;
    assign tx_fifo_udp_payload_axis_tready = tx_udp_payload_axis_tready;
    assign tx_udp_payload_axis_tlast  = tx_fifo_udp_payload_axis_tlast;
    assign tx_udp_payload_axis_tuser  = tx_fifo_udp_payload_axis_tuser;

    assign stream_tready = 1'b0;

    // No self-TX FSM in echo-only mode: report IDLE, never stuck.
    assign dbg_selftx_state = 2'd0;
    assign dbg_selftx_stuck = 1'b0;
end else begin : g_stream
    // Self-initiated streaming TX (proposal 18 stage 2), arbitrated with the
    // RX-echo path. Priority: an in-flight RX-echo reply (so :5001/:5002 still
    // work); when idle, the self-TX FSM sends a UDP packet of STREAM_PKT_BYTES
    // to the fixed dest, pulling payload from stream_*.
    //
    // FSM: IDLE -> wait until stream has data AND no echo in flight -> assert
    // tx_udp_hdr_valid with fixed dest -> SEND payload bytes (count to
    // STREAM_PKT_BYTES, tlast on the last) -> back to IDLE.

    // RX-echo wants the TX path this cycle?
    wire echo_req = rx_udp_hdr_valid && match_cond;
    // self-TX owns the path only during HDR and SEND (not IDLE or BACKOFF)
    wire self_busy = (st == ST_HDR) || (st == ST_SEND);

    // RX-echo header handshake: only when self-TX is idle (echo has priority on
    // a fresh request, but cannot interrupt an in-flight self packet).
    // The single udp_complete header-valid input is the OR of echo and self-TX.
    assign tx_udp_hdr_valid = (echo_req && !self_busy) || (st == ST_HDR);
    assign rx_udp_hdr_ready  = ((tx_eth_hdr_ready && match_cond) || no_match) && !self_busy;

    // header field mux: echo uses RX-derived fields, self-TX uses fixed dest
    wire use_self_hdr = (st == ST_HDR);
    assign tx_udp_ip_dscp = 0;
    assign tx_udp_ip_ecn  = 0;
    assign tx_udp_ip_ttl  = 64;
    assign tx_udp_ip_source_ip = local_ip;
    assign tx_udp_ip_dest_ip = use_self_hdr ? STREAM_DEST_IP   : rx_udp_ip_source_ip;
    assign tx_udp_source_port = use_self_hdr ? STREAM_DEST_PORT : rx_udp_dest_port;
    assign tx_udp_dest_port   = use_self_hdr ? STREAM_DEST_PORT : rx_udp_source_port;
    assign tx_udp_length      = use_self_hdr ? (16'd8 + STREAM_PKT_BYTES) : rx_udp_length;
    assign tx_udp_checksum = 0;

    // self header valid is folded into tx_udp_hdr_valid above (ST_HDR).

    // payload mux: echo from tx_fifo, self from stream_*. On self-TX underrun
    // (send_pad) we substitute zero data with forced tvalid so the promised
    // STREAM_PKT_BYTES always complete (never truncate a UDP frame).
    assign tx_udp_payload_axis_tdata  = self_busy ? (send_pad ? 8'h00 : stream_tdata)
                                                  : tx_fifo_udp_payload_axis_tdata;
    assign tx_udp_payload_axis_tvalid = self_busy ? (st == ST_SEND && (stream_tvalid || send_pad))
                                                  : tx_fifo_udp_payload_axis_tvalid;
    assign tx_udp_payload_axis_tlast  = self_busy ? (st == ST_SEND && (bcnt == STREAM_PKT_BYTES-1))
                                                  : tx_fifo_udp_payload_axis_tlast;
    assign tx_udp_payload_axis_tuser  = self_busy ? 1'b0 : tx_fifo_udp_payload_axis_tuser;
    assign tx_fifo_udp_payload_axis_tready = !self_busy && tx_udp_payload_axis_tready;
    // only pull real stream bytes when NOT padding
    assign stream_tready = (st == ST_SEND) && !send_pad && tx_udp_payload_axis_tready;

    // ARP-deadlock breaker: if ST_HDR waits too long for hdr_ready (ARP not
    // resolved), back off to IDLE so the echo/ARP RX path is unblocked. The
    // ARP module can then complete its request/reply cycle; next time around
    // the cache will be warm and ST_HDR will pass immediately.
    // After a timeout, enter a cooldown (ST_BACKOFF) before retrying to avoid
    // flooding the network with rapid ARP request bursts.
    localparam ST_IDLE = 2'd0, ST_HDR = 2'd1, ST_SEND = 2'd2, ST_BACKOFF = 2'd3;
    reg [1:0]  st;
    reg [15:0] bcnt;

    reg [19:0] hdr_timeout = 0;   // ~8ms at 125MHz
    wire hdr_stuck = hdr_timeout[19];
    reg [25:0] backoff_cnt = 0;   // ~500ms at 125MHz (2^26/125M)
    wire backoff_done = backoff_cnt[25];
    // ST_SEND starvation guard: a FINITE stream source (e.g. the DDR3 ring
    // readback) can underrun mid-packet; without a guard the FSM waits forever
    // for the promised STREAM_PKT_BYTES and wedges the SHARED TX path (echo
    // :5001 included). If no payload beat is accepted for ~8ms, abort the
    // packet back to IDLE so the path is freed. (The continuous real-time trace
    // stream never triggers this.)
    reg [19:0] send_timeout = 0;
    wire send_stuck = send_timeout[19];
    // pad mode: in ST_SEND, stream underran long enough -> finish packet with
    // zeros instead of waiting (or truncating). ~8ms underrun triggers padding.
    wire send_pad = (st == ST_SEND) && !stream_tvalid && send_stuck;

    always @(posedge clk) begin
        if (rst) begin
            st <= ST_IDLE; bcnt <= 0; hdr_timeout <= 0; backoff_cnt <= 0;
            send_timeout <= 0;
        end else case (st)
            ST_IDLE: begin
                hdr_timeout <= 0;
                if (stream_tvalid && !echo_req) begin
                    st <= ST_HDR; bcnt <= 0;
                end
            end
            ST_HDR: begin
                hdr_timeout <= hdr_timeout + 1'b1;
                send_timeout <= 0;
                if (tx_udp_hdr_ready)
                    st <= ST_SEND;
                else if (hdr_stuck) begin
                    st <= ST_BACKOFF;
                    backoff_cnt <= 0;
                end
            end
            ST_SEND:
                // A UDP frame MUST deliver exactly tx_udp_length bytes; you
                // cannot abandon it mid-payload (that jams udp_complete and
                // wedges the whole TX datapath, echo included). On a finite
                // stream underrun we therefore PAD with zeros (see the payload
                // mux: send_pad forces tvalid + zero data) to complete the
                // packet cleanly, then return to IDLE.
                if (tx_udp_payload_axis_tvalid && tx_udp_payload_axis_tready) begin
                    if (!send_pad) send_timeout <= 0;
                    if (bcnt == STREAM_PKT_BYTES-1) begin
                        st <= ST_IDLE; bcnt <= 0; send_timeout <= 0;
                    end else bcnt <= bcnt + 1'b1;
                end else if (!send_pad) begin
                    send_timeout <= send_timeout + 1'b1;
                end
            ST_BACKOFF: begin
                backoff_cnt <= backoff_cnt + 1'b1;
                if (backoff_done)
                    st <= ST_IDLE;  // retry after cooldown
            end
        endcase
    end

    // Observability taps (proposal 30): current FSM state + a one-cycle pulse
    // when HDR times out (the self-TX/ARP deadlock signature -> error 0x401).
    assign dbg_selftx_state = st;
    assign dbg_selftx_stuck = (st == ST_HDR) && hdr_stuck;
end
endgenerate

assign rx_fifo_udp_payload_axis_tdata = ext_reg    ? ext_data :
                                        golden_reg ? golden_byte :
                                                     rx_udp_payload_axis_tdata;
assign rx_fifo_udp_payload_axis_tvalid = rx_udp_payload_axis_tvalid && match_cond_reg;
assign rx_udp_payload_axis_tready = (rx_fifo_udp_payload_axis_tready && match_cond_reg) || no_match_reg;
assign rx_fifo_udp_payload_axis_tlast = rx_udp_payload_axis_tlast;
assign rx_fifo_udp_payload_axis_tuser = rx_udp_payload_axis_tuser;

// Place first payload byte onto LEDs
reg valid_last = 0;
reg [7:0] led_reg = 0;

always @(posedge clk) begin
    if (rst) begin
        led_reg <= 0;
    end else begin
        if (tx_udp_payload_axis_tvalid) begin
            if (!valid_last) begin
                led_reg <= tx_udp_payload_axis_tdata;
                valid_last <= 1'b1;
            end
            if (tx_udp_payload_axis_tlast) begin
                valid_last <= 1'b0;
            end
        end
    end
end

//assign led = sw;
assign led = led_reg;
assign phy_reset_n = !rst;

// bring-up debug
assign dbg_tx_axis_tvalid = tx_axis_tvalid;

assign uart_txd = 0;

eth_mac_1g_rgmii_fifo #(
    .TARGET(TARGET),
    .IODDR_STYLE("IODDR"),
    .CLOCK_INPUT_STYLE("BUFR"),
    .USE_CLK90("FALSE"),
    .ENABLE_PADDING(1),
    .MIN_FRAME_LENGTH(64),
    .TX_FIFO_DEPTH(4096),
    .TX_FRAME_FIFO(1),
    .RX_FIFO_DEPTH(4096),
    .RX_FRAME_FIFO(1)
)
eth_mac_inst (
    .gtx_clk(clk),
    .gtx_clk90(clk90),
    .gtx_rst(rst),
    .logic_clk(clk),
    .logic_rst(rst),

    .tx_axis_tdata(tx_axis_tdata),
    .tx_axis_tvalid(tx_axis_tvalid),
    .tx_axis_tready(tx_axis_tready),
    .tx_axis_tlast(tx_axis_tlast),
    .tx_axis_tuser(tx_axis_tuser),

    .rx_axis_tdata(rx_axis_tdata),
    .rx_axis_tvalid(rx_axis_tvalid),
    .rx_axis_tready(rx_axis_tready),
    .rx_axis_tlast(rx_axis_tlast),
    .rx_axis_tuser(rx_axis_tuser),

    .rgmii_rx_clk(phy_rx_clk),
    .rgmii_rxd(phy_rxd),
    .rgmii_rx_ctl(phy_rx_ctl),
    .rgmii_tx_clk(phy_tx_clk),
    .rgmii_txd(phy_txd),
    .rgmii_tx_ctl(phy_tx_ctl),

    .tx_fifo_overflow(dbg_tx_fifo_overflow),
    .tx_fifo_bad_frame(),
    .tx_fifo_good_frame(),
    .rx_error_bad_frame(dbg_rx_bad_frame),
    .rx_error_bad_fcs(dbg_rx_bad_fcs),
    .rx_fifo_overflow(dbg_rx_fifo_overflow),
    .rx_fifo_bad_frame(),
    .rx_fifo_good_frame(dbg_rx_good_frame),
    .speed(),

    .cfg_ifg(8'd12),
    .cfg_tx_enable(1'b1),
    .cfg_rx_enable(1'b1)
);

eth_axis_rx
eth_axis_rx_inst (
    .clk(clk),
    .rst(rst),
    // AXI input
    .s_axis_tdata(rx_axis_tdata),
    .s_axis_tvalid(rx_axis_tvalid),
    .s_axis_tready(rx_axis_tready),
    .s_axis_tlast(rx_axis_tlast),
    .s_axis_tuser(rx_axis_tuser),
    // Ethernet frame output
    .m_eth_hdr_valid(rx_eth_hdr_valid),
    .m_eth_hdr_ready(rx_eth_hdr_ready),
    .m_eth_dest_mac(rx_eth_dest_mac),
    .m_eth_src_mac(rx_eth_src_mac),
    .m_eth_type(rx_eth_type),
    .m_eth_payload_axis_tdata(rx_eth_payload_axis_tdata),
    .m_eth_payload_axis_tvalid(rx_eth_payload_axis_tvalid),
    .m_eth_payload_axis_tready(rx_eth_payload_axis_tready),
    .m_eth_payload_axis_tlast(rx_eth_payload_axis_tlast),
    .m_eth_payload_axis_tuser(rx_eth_payload_axis_tuser),
    // Status signals
    .busy(),
    .error_header_early_termination()
);

eth_axis_tx
eth_axis_tx_inst (
    .clk(clk),
    .rst(rst),
    // Ethernet frame input
    .s_eth_hdr_valid(tx_eth_hdr_valid),
    .s_eth_hdr_ready(tx_eth_hdr_ready),
    .s_eth_dest_mac(tx_eth_dest_mac),
    .s_eth_src_mac(tx_eth_src_mac),
    .s_eth_type(tx_eth_type),
    .s_eth_payload_axis_tdata(tx_eth_payload_axis_tdata),
    .s_eth_payload_axis_tvalid(tx_eth_payload_axis_tvalid),
    .s_eth_payload_axis_tready(tx_eth_payload_axis_tready),
    .s_eth_payload_axis_tlast(tx_eth_payload_axis_tlast),
    .s_eth_payload_axis_tuser(tx_eth_payload_axis_tuser),
    // AXI output
    .m_axis_tdata(tx_axis_tdata),
    .m_axis_tvalid(tx_axis_tvalid),
    .m_axis_tready(tx_axis_tready),
    .m_axis_tlast(tx_axis_tlast),
    .m_axis_tuser(tx_axis_tuser),
    // Status signals
    .busy()
);

udp_complete #(
    .UDP_CHECKSUM_GEN_ENABLE(UDP_CHECKSUM_GEN_ENABLE)
)
udp_complete_inst (
    .clk(clk),
    .rst(rst),
    // Ethernet frame input
    .s_eth_hdr_valid(rx_eth_hdr_valid),
    .s_eth_hdr_ready(rx_eth_hdr_ready),
    .s_eth_dest_mac(rx_eth_dest_mac),
    .s_eth_src_mac(rx_eth_src_mac),
    .s_eth_type(rx_eth_type),
    .s_eth_payload_axis_tdata(rx_eth_payload_axis_tdata),
    .s_eth_payload_axis_tvalid(rx_eth_payload_axis_tvalid),
    .s_eth_payload_axis_tready(rx_eth_payload_axis_tready),
    .s_eth_payload_axis_tlast(rx_eth_payload_axis_tlast),
    .s_eth_payload_axis_tuser(rx_eth_payload_axis_tuser),
    // Ethernet frame output
    .m_eth_hdr_valid(tx_eth_hdr_valid),
    .m_eth_hdr_ready(tx_eth_hdr_ready),
    .m_eth_dest_mac(tx_eth_dest_mac),
    .m_eth_src_mac(tx_eth_src_mac),
    .m_eth_type(tx_eth_type),
    .m_eth_payload_axis_tdata(tx_eth_payload_axis_tdata),
    .m_eth_payload_axis_tvalid(tx_eth_payload_axis_tvalid),
    .m_eth_payload_axis_tready(tx_eth_payload_axis_tready),
    .m_eth_payload_axis_tlast(tx_eth_payload_axis_tlast),
    .m_eth_payload_axis_tuser(tx_eth_payload_axis_tuser),
    // IP frame input
    .s_ip_hdr_valid(tx_ip_hdr_valid),
    .s_ip_hdr_ready(tx_ip_hdr_ready),
    .s_ip_dscp(tx_ip_dscp),
    .s_ip_ecn(tx_ip_ecn),
    .s_ip_length(tx_ip_length),
    .s_ip_ttl(tx_ip_ttl),
    .s_ip_protocol(tx_ip_protocol),
    .s_ip_source_ip(tx_ip_source_ip),
    .s_ip_dest_ip(tx_ip_dest_ip),
    .s_ip_payload_axis_tdata(tx_ip_payload_axis_tdata),
    .s_ip_payload_axis_tvalid(tx_ip_payload_axis_tvalid),
    .s_ip_payload_axis_tready(tx_ip_payload_axis_tready),
    .s_ip_payload_axis_tlast(tx_ip_payload_axis_tlast),
    .s_ip_payload_axis_tuser(tx_ip_payload_axis_tuser),
    // IP frame output
    .m_ip_hdr_valid(rx_ip_hdr_valid),
    .m_ip_hdr_ready(rx_ip_hdr_ready),
    .m_ip_eth_dest_mac(rx_ip_eth_dest_mac),
    .m_ip_eth_src_mac(rx_ip_eth_src_mac),
    .m_ip_eth_type(rx_ip_eth_type),
    .m_ip_version(rx_ip_version),
    .m_ip_ihl(rx_ip_ihl),
    .m_ip_dscp(rx_ip_dscp),
    .m_ip_ecn(rx_ip_ecn),
    .m_ip_length(rx_ip_length),
    .m_ip_identification(rx_ip_identification),
    .m_ip_flags(rx_ip_flags),
    .m_ip_fragment_offset(rx_ip_fragment_offset),
    .m_ip_ttl(rx_ip_ttl),
    .m_ip_protocol(rx_ip_protocol),
    .m_ip_header_checksum(rx_ip_header_checksum),
    .m_ip_source_ip(rx_ip_source_ip),
    .m_ip_dest_ip(rx_ip_dest_ip),
    .m_ip_payload_axis_tdata(rx_ip_payload_axis_tdata),
    .m_ip_payload_axis_tvalid(rx_ip_payload_axis_tvalid),
    .m_ip_payload_axis_tready(rx_ip_payload_axis_tready),
    .m_ip_payload_axis_tlast(rx_ip_payload_axis_tlast),
    .m_ip_payload_axis_tuser(rx_ip_payload_axis_tuser),
    // UDP frame input
    .s_udp_hdr_valid(tx_udp_hdr_valid),
    .s_udp_hdr_ready(tx_udp_hdr_ready),
    .s_udp_ip_dscp(tx_udp_ip_dscp),
    .s_udp_ip_ecn(tx_udp_ip_ecn),
    .s_udp_ip_ttl(tx_udp_ip_ttl),
    .s_udp_ip_source_ip(tx_udp_ip_source_ip),
    .s_udp_ip_dest_ip(tx_udp_ip_dest_ip),
    .s_udp_source_port(tx_udp_source_port),
    .s_udp_dest_port(tx_udp_dest_port),
    .s_udp_length(tx_udp_length),
    .s_udp_checksum(tx_udp_checksum),
    .s_udp_payload_axis_tdata(tx_udp_payload_axis_tdata),
    .s_udp_payload_axis_tvalid(tx_udp_payload_axis_tvalid),
    .s_udp_payload_axis_tready(tx_udp_payload_axis_tready),
    .s_udp_payload_axis_tlast(tx_udp_payload_axis_tlast),
    .s_udp_payload_axis_tuser(tx_udp_payload_axis_tuser),
    // UDP frame output
    .m_udp_hdr_valid(rx_udp_hdr_valid),
    .m_udp_hdr_ready(rx_udp_hdr_ready),
    .m_udp_eth_dest_mac(rx_udp_eth_dest_mac),
    .m_udp_eth_src_mac(rx_udp_eth_src_mac),
    .m_udp_eth_type(rx_udp_eth_type),
    .m_udp_ip_version(rx_udp_ip_version),
    .m_udp_ip_ihl(rx_udp_ip_ihl),
    .m_udp_ip_dscp(rx_udp_ip_dscp),
    .m_udp_ip_ecn(rx_udp_ip_ecn),
    .m_udp_ip_length(rx_udp_ip_length),
    .m_udp_ip_identification(rx_udp_ip_identification),
    .m_udp_ip_flags(rx_udp_ip_flags),
    .m_udp_ip_fragment_offset(rx_udp_ip_fragment_offset),
    .m_udp_ip_ttl(rx_udp_ip_ttl),
    .m_udp_ip_protocol(rx_udp_ip_protocol),
    .m_udp_ip_header_checksum(rx_udp_ip_header_checksum),
    .m_udp_ip_source_ip(rx_udp_ip_source_ip),
    .m_udp_ip_dest_ip(rx_udp_ip_dest_ip),
    .m_udp_source_port(rx_udp_source_port),
    .m_udp_dest_port(rx_udp_dest_port),
    .m_udp_length(rx_udp_length),
    .m_udp_checksum(rx_udp_checksum),
    .m_udp_payload_axis_tdata(rx_udp_payload_axis_tdata),
    .m_udp_payload_axis_tvalid(rx_udp_payload_axis_tvalid),
    .m_udp_payload_axis_tready(rx_udp_payload_axis_tready),
    .m_udp_payload_axis_tlast(rx_udp_payload_axis_tlast),
    .m_udp_payload_axis_tuser(rx_udp_payload_axis_tuser),
    // Status signals
    .ip_rx_busy(),
    .ip_tx_busy(),
    .udp_rx_busy(),
    .udp_tx_busy(),
    .ip_rx_error_header_early_termination(),
    .ip_rx_error_payload_early_termination(),
    .ip_rx_error_invalid_header(),
    .ip_rx_error_invalid_checksum(),
    .ip_tx_error_payload_early_termination(),
    .ip_tx_error_arp_failed(),
    .udp_rx_error_header_early_termination(),
    .udp_rx_error_payload_early_termination(),
    .udp_tx_error_payload_early_termination(),
    // Configuration
    .local_mac(local_mac),
    .local_ip(local_ip),
    .gateway_ip(gateway_ip),
    .subnet_mask(subnet_mask),
    .clear_arp_cache(0)
);

axis_fifo #(
    .DEPTH(8192),
    .DATA_WIDTH(8),
    .KEEP_ENABLE(0),
    .ID_ENABLE(0),
    .DEST_ENABLE(0),
    .USER_ENABLE(1),
    .USER_WIDTH(1),
    .FRAME_FIFO(0)
)
udp_payload_fifo (
    .clk(clk),
    .rst(rst),

    // AXI input
    .s_axis_tdata(rx_fifo_udp_payload_axis_tdata),
    .s_axis_tkeep(0),
    .s_axis_tvalid(rx_fifo_udp_payload_axis_tvalid),
    .s_axis_tready(rx_fifo_udp_payload_axis_tready),
    .s_axis_tlast(rx_fifo_udp_payload_axis_tlast),
    .s_axis_tid(0),
    .s_axis_tdest(0),
    .s_axis_tuser(rx_fifo_udp_payload_axis_tuser),

    // AXI output
    .m_axis_tdata(tx_fifo_udp_payload_axis_tdata),
    .m_axis_tkeep(),
    .m_axis_tvalid(tx_fifo_udp_payload_axis_tvalid),
    .m_axis_tready(tx_fifo_udp_payload_axis_tready),
    .m_axis_tlast(tx_fifo_udp_payload_axis_tlast),
    .m_axis_tid(),
    .m_axis_tdest(),
    .m_axis_tuser(tx_fifo_udp_payload_axis_tuser),

    // Status
    .status_overflow(),
    .status_bad_frame(),
    .status_good_frame()
);

endmodule

`resetall
