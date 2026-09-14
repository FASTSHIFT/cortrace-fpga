// tb_adapter_order — feed 0,1,2,...,15 as 8-bit into axis_async_fifo_adapter
// (8->128, CDC) and print the 128-bit output word so we know the byte order
// (LSB-first vs MSB-first) before wiring it into the datapath.
`timescale 1ns/1ps
`default_nettype none
module tb_adapter_order;
    reg s_clk=0, m_clk=0, rst=1;
    always #5 s_clk=~s_clk;   // 100M source
    always #2.5 m_clk=~m_clk; // 200M sink
    initial begin repeat(6) @(posedge m_clk); rst=0; end

    reg [7:0] cnt=0; reg sv=0;
    wire s_ready;
    always @(posedge s_clk) begin
        if (rst) begin cnt<=0; sv<=0; end
        else begin
            sv<=1;
            if (sv && s_ready) cnt<=cnt+1;
        end
    end

    wire [127:0] m_data; wire m_valid; reg m_ready=1;
    wire [15:0] m_keep;

    axis_async_fifo_adapter #(
        .DEPTH(64), .S_DATA_WIDTH(8), .M_DATA_WIDTH(128),
        .S_KEEP_ENABLE(0), .M_KEEP_ENABLE(1), .ID_ENABLE(0),
        .DEST_ENABLE(0), .USER_ENABLE(0), .FRAME_FIFO(0)
    ) dut (
        .s_clk(s_clk), .s_rst(rst),
        .s_axis_tdata(cnt), .s_axis_tkeep(1'b1), .s_axis_tvalid(sv),
        .s_axis_tready(s_ready), .s_axis_tlast(1'b0),
        .s_axis_tid(8'h0), .s_axis_tdest(8'h0), .s_axis_tuser(1'b0),
        .m_clk(m_clk), .m_rst(rst),
        .m_axis_tdata(m_data), .m_axis_tkeep(m_keep), .m_axis_tvalid(m_valid),
        .m_axis_tready(m_ready), .m_axis_tlast(), .m_axis_tid(), .m_axis_tdest(), .m_axis_tuser(),
        .s_pause_req(1'b0), .s_pause_ack(), .m_pause_req(1'b0), .m_pause_ack(),
        .s_status_depth(), .s_status_depth_commit(), .s_status_overflow(),
        .s_status_bad_frame(), .s_status_good_frame(),
        .m_status_depth(), .m_status_depth_commit(), .m_status_overflow(),
        .m_status_bad_frame(), .m_status_good_frame()
    );

    // The adapter is documented (and relied on by la_ddr_writer's byte-reverse)
    // to be LITTLE-ENDIAN: the first input byte lands in the LS lane. For input
    // 0,1,..,15 the first output word must be 0f0e0d0c0b0a09080706050403020100.
    integer got=0, errors=0;
    reg [127:0] exp0 = 128'h0f0e0d0c0b0a09080706050403020100;
    always @(posedge m_clk) begin
        if (!rst && m_valid && m_ready) begin
            got=got+1;
            if (got<=3) $display("word %0d = %032x  keep=%04x", got, m_data, m_keep);
            if (got==1 && m_data !== exp0) errors=errors+1;
            if (got==1 && m_keep !== 16'hffff) errors=errors+1;
        end
    end
    initial begin
        #20000;
        if (got < 1)
            $display("==== SIM DONE ==== RESULT=FAIL (no adapter output word)");
        else if (errors != 0)
            $display("==== SIM DONE ==== RESULT=FAIL (adapter byte order not little-endian)");
        else
            $display("==== SIM DONE ==== RESULT=ALL_PASS");
        $finish;
    end
endmodule
`default_nettype wire
