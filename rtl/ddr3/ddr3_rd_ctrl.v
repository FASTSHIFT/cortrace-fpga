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
module ddr3_rd_ctrl
    #(parameter LENGTH= 64)
    ( 
       //mig应用相关接口 
        input         ui_clk        ,  //应用接口的时钟
        input         ui_rst        ,  //应用接口的复位信号
        input         app_rdy       , //signals about command
        output  reg   app_en        ,
        output [28:0] app_addr      ,
        output [2:0]  app_cmd       ,
        input         app_rd_data_end,//这个周期的数据是最后一个
        input         app_rd_data_valid, //读取的数据有效
        input [127:0] app_rd_data, //signals about reading data       
       //控制MIG的相关接口        
        input         ddr3_rd_start   ,  //signals about control
        output reg    ddr3_rd_req     ,  //读请求信号
        input         ddr3_rd_ack     ,  //读应答信号
        output        ddr3_rd_addr_req,  //读地址请求信号
        input [28:0]  ddr3_rd_addr    ,  
        output        ddr3_rd_data_vld,
        output [127:0]ddr3_rd_data    ,
        output reg    ddr3_rd_done    ,
        output        ddr3_rd_busy    
       
    );
localparam IDLE   = 3'b001;
localparam RD_REQ = 3'b010;
localparam READ   = 3'b100;
localparam MAX_NUM =LENGTH-1 ;
reg [9:0]rd_data_cnt;
wire     add_data_cnt;
wire     end_data_cnt;

reg [9:0] cmd_cnt;
wire      add_cmd_cnt;
wire      end_cmd_cnt;
reg [2:0] state;//状态寄存器
assign app_cmd    =3'b001;
assign app_addr   =ddr3_rd_addr;
assign ddr3_rd_data_vld=app_rd_data_valid&app_rd_data_end;
assign ddr3_rd_data    =app_rd_data;
assign ddr3_rd_addr_req=app_en&app_rdy;
//app_en
always @(posedge ui_clk or posedge ui_rst) begin
    if(ui_rst)
        app_en<=1'b0;
    else if(end_cmd_cnt)
             app_en<=1'b0;
    else if(ddr3_rd_ack)
             app_en<=1'b1;
    else  app_en<=app_en;
end
//finite state machine 
always @(posedge ui_clk or posedge ui_rst ) begin
    if(ui_rst)
        state<=IDLE;
    else begin
            case (state)
               IDLE : 
                    begin
                        if(ddr3_rd_start)
                            state<=RD_REQ;
                        else 
                            state<=IDLE;
                    end
                RD_REQ:
                    begin
                        if(ddr3_rd_ack)
                             state<=READ;
                        else 
                             state<=RD_REQ;
                    end
                READ:
                    begin
                        if(end_data_cnt)
                            state<=IDLE;
                        else 
                            state<=READ;
                    end
                default: state<=IDLE;

            endcase 
    end
end
//ddr3_rd_req
always @(posedge ui_clk or posedge ui_rst) begin
    if(ui_rst)
        ddr3_rd_req<=1'b0;
    else if(ddr3_rd_ack)
            ddr3_rd_req<=1'b0;
    else if(ddr3_rd_start)
            ddr3_rd_req<=1'b1;
    else  ddr3_rd_req<=ddr3_rd_req;
end
//rd_data_cnt
always @(posedge ui_clk or posedge ui_rst) begin
    if(ui_rst)
        rd_data_cnt<='d0;
    else if (add_data_cnt) begin
            if(end_data_cnt) 
                 rd_data_cnt<='d0;
            else 
            rd_data_cnt<=rd_data_cnt +1'b1;  
    end
    else   rd_data_cnt<=rd_data_cnt; 
end
assign add_data_cnt=app_rd_data_valid;
assign end_data_cnt=app_rd_data_valid&(rd_data_cnt==MAX_NUM);
//cmd_cnt
always @(posedge ui_clk or posedge ui_rst) begin
    if(ui_rst)
        cmd_cnt<='d0;
    else if(add_cmd_cnt)begin
             if(end_cmd_cnt)
                 cmd_cnt<='d0;
             else 
                 cmd_cnt<=cmd_cnt+1'b1;
    end
    else    cmd_cnt<=cmd_cnt;
end
assign add_cmd_cnt=app_en&app_rdy;
assign end_cmd_cnt=app_en&app_rdy&(cmd_cnt==MAX_NUM);
//ddr3_rd_done
always @(posedge ui_clk or posedge ui_rst) begin
    if(ui_rst)
        ddr3_rd_done<=1'b0;
    else if(end_data_cnt)
             ddr3_rd_done<=1'b1;
    else ddr3_rd_done<=1'b0;
end
assign ddr3_rd_busy=state==READ;
endmodule
