// Minimal behavioural stubs for the Xilinx 7-series primitives used by
// trace_capture_a7, so the OVERSAMPLE pairing logic can be checked under
// iverilog (which has no UNISIM library). These are NOT timing-accurate —
// they only model the functional passthrough/sampling needed for the
// edge-aligned capture testbench.
`default_nettype none

module IBUF (output wire O, input wire I);
    assign O = I;
endmodule

module BUFG (output wire O, input wire I);
    assign O = I;
endmodule

module BUFIO (output wire O, input wire I);
    assign O = I;
endmodule

module BUFR #(parameter BUFR_DIVIDE = "BYPASS") (
    output wire O, input wire I, input wire CE, input wire CLR);
    assign O = I;
endmodule

// IDELAYCTRL: report ready immediately.
module IDELAYCTRL (output reg RDY, input wire REFCLK, input wire RST);
    always @(posedge REFCLK or posedge RST)
        if (RST) RDY <= 1'b0; else RDY <= 1'b1;
endmodule

// IDELAYE2: functional passthrough (no delay modelled).
module IDELAYE2 #(
    parameter IDELAY_TYPE = "FIXED",
    parameter DELAY_SRC = "IDATAIN",
    parameter HIGH_PERFORMANCE_MODE = "FALSE",
    parameter integer IDELAY_VALUE = 0,
    parameter SIGNAL_PATTERN = "DATA",
    parameter real REFCLK_FREQUENCY = 200.0,
    parameter CINVCTRL_SEL = "FALSE",
    parameter PIPE_SEL = "FALSE"
) (
    input  wire C, input wire REGRST, input wire LD, input wire CE,
    input  wire INC, input wire CINVCTRL, input wire [4:0] CNTVALUEIN,
    input  wire IDATAIN, input wire DATAIN, input wire LDPIPEEN,
    output wire DATAOUT, output wire [4:0] CNTVALUEOUT
);
    assign DATAOUT = IDATAIN;
    assign CNTVALUEOUT = CNTVALUEIN;
endmodule

// IDDR: sample D on both edges of C.
module IDDR #(
    parameter DDR_CLK_EDGE = "SAME_EDGE_PIPELINED",
    parameter INIT_Q1 = 1'b0, parameter INIT_Q2 = 1'b0,
    parameter SRTYPE = "ASYNC"
) (
    output reg Q1, output reg Q2,
    input wire C, input wire CE, input wire D, input wire R, input wire S
);
    initial begin Q1 = INIT_Q1; Q2 = INIT_Q2; end
    always @(posedge C or posedge R) if (R) Q1 <= INIT_Q1; else if (CE) Q1 <= D;
    always @(negedge C or posedge R) if (R) Q2 <= INIT_Q2; else if (CE) Q2 <= D;
endmodule

`default_nettype wire
