// byte_rate_led.v -- LED brightness/blink modulated by byte throughput.
//
// A byte counter increments on every asserted `tick` (typically an AXI-Stream
// tvalid & tready handshake, or a capture-side cap_valid strobe), and the LED
// output toggles on bit [BLINK_BIT] of that counter. That makes the visible
// blink FREQUENCY proportional to the byte rate:
//   f_toggle = byte_rate / 2^(BLINK_BIT+1)
// After IDLE_CYCLES clock cycles without a tick, `led` is forced to 0 so a
// stopped stream shows as dark rather than freezing at the last polarity.
//
// The output is active-high (1 = light). Invert externally if the board is
// common-anode (A7-Lite is; the top module handles that).
//
// SPDX-License-Identifier: MIT
`default_nettype none

module byte_rate_led #(
    // Toggle on this bit of the byte counter. clk=125 MHz, tick=1B, BIT=21 ->
    // 18 Hz at 75 MB/s, 0.18 Hz at 750 kB/s. Adjust to shift the visible band.
    parameter integer BLINK_BIT = 21,
    // How many clk cycles without a tick before we call the stream "idle" and
    // force the LED dark. Sized so idle_cnt fits in IDLE_CNT_WIDTH bits.
    parameter integer IDLE_CYCLES    = 62_500_000,  // ~500 ms @ 125 MHz
    parameter integer IDLE_CNT_WIDTH = 27
) (
    input  wire clk,
    input  wire rst,
    input  wire tick,   // one pulse per byte
    output wire led     // active-high; invert externally for active-low pads
);
    reg [BLINK_BIT:0]          byte_cnt = 0;
    reg [IDLE_CNT_WIDTH-1:0]   idle_cnt = {IDLE_CNT_WIDTH{1'b1}};

    localparam [IDLE_CNT_WIDTH-1:0] IDLE_MAX =
        {IDLE_CNT_WIDTH{1'b1}};

    always @(posedge clk) begin
        if (rst) begin
            byte_cnt <= 0;
            idle_cnt <= IDLE_MAX;
        end else if (tick) begin
            byte_cnt <= byte_cnt + 1'b1;
            idle_cnt <= 0;
        end else if (idle_cnt != IDLE_MAX) begin
            idle_cnt <= idle_cnt + 1'b1;
        end
    end

    wire active = (idle_cnt < IDLE_CYCLES[IDLE_CNT_WIDTH-1:0]);
    assign led = active & byte_cnt[BLINK_BIT];
endmodule

`default_nettype wire
