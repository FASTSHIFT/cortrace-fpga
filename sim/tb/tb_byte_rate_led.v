// tb_byte_rate_led.v -- self-checking test for the byte-rate LED indicator.
//
// Scenarios:
//   A) continuous ticks toggle the LED every 2^BLINK_BIT ticks
//   B) stopping ticks makes the LED go dark after IDLE_CYCLES cycles
//   C) resuming ticks reactivates the LED
//   D) pad polarity: byte_rate_led is active-high, invert externally
//
// SPDX-License-Identifier: MIT
`timescale 1ns/1ps

module tb_byte_rate_led;
    // Small params so the sim is fast: toggle every 2^3 = 8 ticks, idle after
    // 40 clk cycles without a tick.
    localparam integer BLINK_BIT = 3;
    localparam integer IDLE_CYCLES = 40;
    localparam integer IDLE_CNT_WIDTH = 8;
    localparam integer TOGGLE_PERIOD_TICKS = (1 << BLINK_BIT); // 8

    reg  clk = 0;
    reg  rst = 1;
    reg  tick = 0;
    wire led;

    byte_rate_led #(
        .BLINK_BIT(BLINK_BIT),
        .IDLE_CYCLES(IDLE_CYCLES),
        .IDLE_CNT_WIDTH(IDLE_CNT_WIDTH)
    ) dut (
        .clk(clk), .rst(rst), .tick(tick), .led(led)
    );

    always #5 clk = ~clk;  // 100 MHz sim clock

    // Track LED transitions.
    integer led_toggles = 0;
    reg     led_prev = 0;
    always @(posedge clk) begin
        if (led !== led_prev) led_toggles = led_toggles + 1;
        led_prev <= led;
    end

    integer fails = 0;
    integer i;
    integer toggles_start;
    reg     led_at_idle;

    initial begin
        // release reset
        repeat (4) @(posedge clk);
        rst <= 0;
        @(posedge clk);

        // --- (A) continuous ticks: LED must toggle every TOGGLE_PERIOD_TICKS ticks.
        // Feed 4 * TOGGLE_PERIOD_TICKS = 32 ticks -> expect ~4 toggles
        // (allow a startup transition, so >= 3).
        toggles_start = led_toggles;
        for (i = 0; i < 4 * TOGGLE_PERIOD_TICKS; i = i + 1) begin
            tick <= 1;
            @(posedge clk);
        end
        tick <= 0;
        @(posedge clk);
        $display("[A] continuous ticks: %0d LED toggles (expect >=3)",
                 led_toggles - toggles_start);
        if ((led_toggles - toggles_start) < 3) begin
            $display("  *** FAIL A: LED not toggling under continuous ticks");
            fails = fails + 1;
        end

        // --- (B) stop ticks: after IDLE_CYCLES the LED must be 0 (dark).
        // Wait well past the idle threshold.
        repeat (IDLE_CYCLES + 20) @(posedge clk);
        led_at_idle = led;
        $display("[B] idle after %0d cycles: led=%0d (expect 0)",
                 IDLE_CYCLES + 20, led_at_idle);
        if (led_at_idle !== 1'b0) begin
            $display("  *** FAIL B: LED did not go dark after idle");
            fails = fails + 1;
        end

        // --- (C) resume: LED must come back to life.
        toggles_start = led_toggles;
        for (i = 0; i < 4 * TOGGLE_PERIOD_TICKS; i = i + 1) begin
            tick <= 1;
            @(posedge clk);
        end
        tick <= 0;
        @(posedge clk);
        $display("[C] resumed ticks: %0d LED toggles (expect >=3)",
                 led_toggles - toggles_start);
        if ((led_toggles - toggles_start) < 3) begin
            $display("  *** FAIL C: LED did not resume toggling");
            fails = fails + 1;
        end

        // --- (D) reset while active -> LED back to 0.
        rst <= 1;
        repeat (3) @(posedge clk);
        rst <= 0;
        @(posedge clk);
        if (led !== 1'b0) begin
            $display("  *** FAIL D: LED not zero right after reset");
            fails = fails + 1;
        end

        if (fails == 0)
            $display("==== SIM DONE ==== RESULT=ALL_PASS");
        else begin
            $display("==== SIM DONE ==== RESULT=FAIL (%0d)", fails);
            $fatal(1, "byte_rate_led tests FAILED");
        end
        $finish;
    end

    // Safety: absolute time cap.
    initial begin
        #200000;
        $display("==== SIM DONE ==== RESULT=FAIL_TIMEOUT");
        $fatal(1, "sim watchdog");
    end
endmodule
