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
// 2022.09.17     Ao Guohua     1.0          Original
//                                                      
// --------------------------------------------------------------------
// --------------------------------------------------------------------
module ddr3_wr_ctrl
  #(parameter LENGTH = 64)
   (
     input          ui_clk,
     input          ui_rst,
     //MIG的应用接口
     input          app_rdy,//signals about command
     output         app_en,
     output   [2:0] app_cmd,
     output  [28:0] app_addr,
     input          app_wdf_rdy,//signals about writing data
     output  [127:0]app_wdf_data,
     output         app_wdf_end,
     output    reg  app_wdf_wren,
     output   [15:0]app_wdf_mask,
     //MIG控制器接口
     input          ddr3_wr_start,   //写开始信号
     output   reg   ddr3_wr_req,     //向读写仲裁模块发送写请求
     input          ddr3_wr_ack,     //读写仲裁模块对写请求信号应答
     output         ddr3_wr_data_req,//数据请求信号
     input  [127:0] ddr3_wr_data,    //写数据，16*8=128
     output         ddr3_wr_addr_req,
     input   [28:0] ddr3_wr_addr,
     output   reg   ddr3_wr_done,
     output         ddr3_wr_busy
   );
  localparam  IDLE    = 3'b001;
  localparam  WR_REQ  = 3'b010;
  localparam  WRITE   = 3'b100;
  localparam  MAX_NUM = LENGTH-1;
  //control signals about data_cnt
  reg [9:0]data_cnt;
  wire add_data_cnt;
  wire end_data_cnt;
 //control signals about  cmd_cnt
  reg [9:0]cmd_cnt;
  wire add_cmd_cnt;
  wire end_cmd_cnt;
  reg [2:0]state;//状态寄存器
  assign app_wdf_mask  =    'd0;   //数据掩码设置为0
  assign app_cmd       =    3'b000;
  assign app_addr      =    ddr3_wr_addr;
  assign app_wdf_end   =    app_wdf_wren&app_wdf_rdy;//每一个周期的信号都是最后一个数据
  assign ddr3_wr_data_req   =    app_wdf_wren&app_wdf_rdy;//高电平请求数据
  assign ddr3_wr_addr_req   =    app_en&app_rdy;
  assign app_wdf_data  =    ddr3_wr_data;
  assign app_en=state  ==   WRITE;
  assign ddr3_wr_busy=state==WRITE;
  //finite state machine
  always @(posedge ui_clk or posedge ui_rst )begin
    if(ui_rst)
      state<=IDLE;
    else begin
            case(state)
              IDLE:
                begin
                    if(ddr3_wr_start)
                      state<=WR_REQ;
                    else
                      state<=IDLE;
                end
              WR_REQ:
                begin
                  if(ddr3_wr_ack)
                    state<=WRITE ;
                  else
                    state<=WR_REQ;
                end
              WRITE:
                begin
                  if(end_cmd_cnt)
                    state<=IDLE;
                  else
                    state<=WRITE;
                end
              default:
                 state<=IDLE;
            endcase
    end
  end
//data_cnt
always @(posedge ui_clk or posedge ui_rst )begin
    if(ui_rst)
        data_cnt<='d0;
    else if(add_data_cnt)begin
            if(end_data_cnt)
                data_cnt<='d0;
            else 
                data_cnt<=data_cnt+1'b1;
    end
    else data_cnt<=data_cnt;
end
assign add_data_cnt=app_wdf_wren&app_wdf_rdy;
assign end_data_cnt=app_wdf_wren&app_wdf_rdy&(data_cnt==MAX_NUM);

//cmd_cnt
always @(posedge ui_clk or posedge ui_rst )begin
    if(ui_rst)
        cmd_cnt<='d0;
    else if(add_cmd_cnt)begin
            if(end_cmd_cnt)
                cmd_cnt<='d0;
            else 
                cmd_cnt<=cmd_cnt+1'b1;
    end
    else  cmd_cnt<=cmd_cnt;      
end
assign add_cmd_cnt=app_en&app_rdy;
assign end_cmd_cnt=app_en&app_rdy&(cmd_cnt==MAX_NUM);
//ddr3_wr_req
always @(posedge ui_clk or posedge ui_rst )begin
    if(ui_rst)
        ddr3_wr_req<=1'b0;
    else if(ddr3_wr_ack)
            ddr3_wr_req<=1'b0;
    else if(ddr3_wr_start)
            ddr3_wr_req<=1'b1;
    else   ddr3_wr_req<=ddr3_wr_req;
end
//app_wdf_en
always @(posedge ui_clk or posedge ui_rst )begin
    if(ui_rst)
        app_wdf_wren<=1'b0;
    else if(ddr3_wr_ack)
             app_wdf_wren<=1'b1;
    else if(end_data_cnt)
            app_wdf_wren<=1'b0;
    else   app_wdf_wren<=app_wdf_wren;
end
//ddr3_wr_done
always @(posedge ui_clk or posedge ui_rst )begin
    if(ui_rst)
         ddr3_wr_done<=1'b0;
    else if(end_cmd_cnt)
             ddr3_wr_done<=1'b1;
    else ddr3_wr_done<=1'b0;
end
endmodule
