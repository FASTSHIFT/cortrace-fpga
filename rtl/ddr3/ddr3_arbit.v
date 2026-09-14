// --------------------------------------------------------------------
// Copyright (c) 2022 by MicroPhase Technologies Inc. 
// --------------------------------------------------------------------
//
// Permission:
//
//   MicroPhase grants permission to use and modify this code for use
//   in synthesis for all MicroPhase Development Boards.
//   Other use of this code, including the selling 
//   ,duplication, or modification of any portion is strictly prohibited.
//
// Disclaimer:
//
//   This VHDL/Verilog or C/C++ source code is intended as a design reference
//   which illustrates how these types of functions can be implemented.
//   It is the user's responsibility to verify their design for
//   consistency and functionality through the use of formal
//   verification methods.  MicroPhase provides no warranty regarding the use 
//   or functionality of this code.
//
// --------------------------------------------------------------------
//           
//                     MicroPhase Technologies Inc
//                     Shanghai, China
//
//                     web: http://www.microphase.cn/   
//                     email: support@microphase.cn
//
// --------------------------------------------------------------------
// --------------------------------------------------------------------
//   
// Major Functions:   
//
// --------------------------------------------------------------------
// --------------------------------------------------------------------
//
//  Revision History    
//  Date          By            Revision    Change Description
//---------------------------------------------------------------------
// 2022.09.19     Ao Guohua     1.0          Original
//                                                      
// --------------------------------------------------------------------
// --------------------------------------------------------------------
module ddr3_arbit(
            input      ui_clk,
            input      ui_rst,
            input      ddr3_wr_req,
            input      ddr3_wr_done,
            input      ddr3_rd_req,
            input      ddr3_rd_done,
            output reg ddr3_wr_ack,
            output reg ddr3_rd_ack 
    );
localparam IDLE = 4'b0001 ;
localparam ARBIT =4'b0010 ;
localparam WRITE =4'b0100 ;
localparam READ  =4'b1000 ;
reg [3:0] state;
//finite state machine
always @(posedge ui_clk or posedge ui_rst) begin
    if(ui_rst)
        state<=IDLE;
    else begin
        case(state)
            IDLE:
                begin
                    state<=ARBIT;
                end
            ARBIT:
                begin
                    if(ddr3_wr_req)
                        state<=WRITE;
                    else if(ddr3_rd_req)
                            state<=READ;
                    else state<=ARBIT;
                end
            WRITE:
                begin
                    if(ddr3_wr_done)
                        state<=ARBIT;
                    else 
                        state<=WRITE;
                end
            READ:
                begin
                    if(ddr3_rd_done)
                        state<=ARBIT;
                    else 
                        state<=READ;
                end
            default:  state<=IDLE;
        endcase
    end
end
//ddr3_wr_ack
always @(posedge ui_clk or posedge ui_rst) begin
    if(ui_rst)
        ddr3_wr_ack<=1'b0;
    else if(state==ARBIT&&ddr3_wr_req)
                ddr3_wr_ack<=1'b1;
    else  ddr3_wr_ack<=1'b0;
end
//ddr3_rd_ack
always @(posedge ui_clk or posedge ui_rst) begin
    if(ui_rst)
        ddr3_rd_ack<=1'b0;
    else if(state==ARBIT&&(!ddr3_wr_req)&&ddr3_rd_req)
            ddr3_rd_ack<=1'b1;
    else   ddr3_rd_ack<=1'b0;
end
endmodule
