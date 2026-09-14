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
module ddr3_ctrl(
        input         sys_clk,
        input         sys_rst_n,
/*************应用层接口以及时钟**********/
        output        ui_clk  ,
        output        ui_rst  ,
        output        calib_complete, //MIG init_calib_complete (raw, observability)
        output        ddr3_busy,      //置1表明ddr3忙碌
/**********写控制信号**************/
         input          ddr3_wr_start   ,   //写开始信号
         output         ddr3_wr_data_req,
         input  [127:0] ddr3_wr_data    ,    
         output         ddr3_wr_addr_req,
         input   [28:0] ddr3_wr_addr    ,
         output         ddr3_wr_done     ,
/*********读控制信号**************/
        input           ddr3_rd_start     ,  //signals about control
         output         ddr3_rd_addr_req         ,  //读地址请求信号
         input [28:0]   ddr3_rd_addr             ,  
         output         ddr3_rd_data_vld         ,
         output [127:0] ddr3_rd_data             ,
         output         ddr3_rd_done             ,    
/*************ddr3硬件接口*****************/
        output [14:0]		    ddr3_addr      ,  
        output [2:0]		    ddr3_ba        ,
        output	                    ddr3_cas_n     ,
        output [0:0]		    ddr3_ck_n      ,
        output [0:0]		    ddr3_ck_p      ,
        output [0:0]		    ddr3_cke       ,
        output	                    ddr3_ras_n     ,
        output	                    ddr3_reset_n   ,
        output	                    ddr3_we_n      ,
        inout  [15:0]		    ddr3_dq        ,
        inout  [1:0]		    ddr3_dqs_n     ,
        inout  [1:0]		    ddr3_dqs_p     ,
        output [1:0]		    ddr3_dm        ,
        output [0:0]		    ddr3_odt   
     );
 wire         init_calib_complete;//MIG初始化完成
 assign       calib_complete = init_calib_complete;
 wire         ui_clk_sync_rst;  //MIG应用接口的复位信号 
 wire         app_rdy     ;
 wire         app_wr_en      ;//写命令使能
 wire   [2:0] app_wr_cmd     ;//写命令
 wire  [28:0] app_wr_addr    ;//写地址
 wire         app_wdf_rdy    ;
 wire  [127:0]app_wdf_data   ;
 wire         app_wdf_end    ;
 wire         app_wdf_wren   ;
 wire  [15:0] app_wdf_mask   ;
 wire         ddr3_wr_req         ;//向读写仲裁模块发送读请求
 wire         ddr3_wr_ack         ;//读写仲裁模块对读请求信号应答
 wire         ddr3_wr_busy        ;//高电平表明处于写状态
 wire         app_rd_en          ;
 wire [28:0]  app_rd_addr        ;
 wire [2:0]   app_rd_cmd         ;
 wire         app_rd_data_end    ;
 wire         app_rd_data_valid  ;
 wire[127:0]  app_rd_data        ;
 wire         ddr3_rd_req             ; //读请求信号
 wire         ddr3_rd_ack             ; //读应答信号
 wire         ddr3_rd_busy            ; //置1表明现在处于写忙碌状态
 wire         app_en             ;
 wire  [2:0]  app_cmd            ;
 wire  [28:0] app_addr           ;
 //MIG还未初始化完成的时候，其他模块处于复位状态
 assign ui_rst=ui_clk_sync_rst|(!init_calib_complete);
 assign app_en    =ddr3_wr_busy?app_wr_en:app_rd_en;
 assign app_cmd   =ddr3_wr_busy?app_wr_cmd:app_rd_cmd;
 assign app_addr  =ddr3_wr_busy?app_wr_addr:app_rd_addr;
 assign ddr3_busy =ddr3_wr_busy|ddr3_rd_busy ;
 ddr3_wr_ctrl ddr3_wr_ctrl0
    (
     .ui_clk      (ui_clk ),
     .ui_rst      (ui_rst ),
     //MIG的应用接口
     .app_rdy     (app_rdy      ), //signals about command
     .app_en      (app_wr_en    ),
     .app_cmd     (app_wr_cmd   ),
     .app_addr    (app_wr_addr  ),
     .app_wdf_rdy (app_wdf_rdy  ),//signals about writing data
     .app_wdf_data(app_wdf_data ),
     .app_wdf_end (app_wdf_end  ),
     .app_wdf_wren(app_wdf_wren ),
     .app_wdf_mask(app_wdf_mask) ,
     //MIG控制器接口
     .ddr3_wr_start     (ddr3_wr_start    ) ,//写开始信号
     .ddr3_wr_req            (ddr3_wr_req      ) ,//向读写仲裁模块发送读请求
     .ddr3_wr_ack            (ddr3_wr_ack      ) ,//读写仲裁模块对读请求信号应答
     .ddr3_wr_data_req  (ddr3_wr_data_req ) ,//数据请求信号
     .ddr3_wr_data      (ddr3_wr_data     ) ,//写数据，32*8
     .ddr3_wr_addr_req  (ddr3_wr_addr_req ) ,
     .ddr3_wr_addr      (ddr3_wr_addr     ) ,
     .ddr3_wr_done      (ddr3_wr_done     ) ,
     .ddr3_wr_busy           (ddr3_wr_busy     )
    );
 ddr3_rd_ctrl  ddr3_rd_ctrl
     ( 
       //mig应用相关接口 
      .ui_clk           (ui_clk),  
      .ui_rst           (ui_rst),  
      .app_rdy          (app_rdy  ),
      .app_en           (app_rd_en  ),
      .app_addr         (app_rd_addr),
      .app_cmd          (app_rd_cmd ),
      .app_rd_data_end  (app_rd_data_end ),
      .app_rd_data_valid(app_rd_data_valid), 
      .app_rd_data      (app_rd_data),       
      //控制MIG的相关接口        
      .ddr3_rd_start   (ddr3_rd_start     ) ,  
      .ddr3_rd_req     (ddr3_rd_req       ) ,  
      .ddr3_rd_ack     (ddr3_rd_ack       ) ,  
      .ddr3_rd_addr_req(ddr3_rd_addr_req  ) ,  
      .ddr3_rd_addr    (ddr3_rd_addr      ) ,  
      .ddr3_rd_data_vld(ddr3_rd_data_vld  ) ,
      .ddr3_rd_data    (ddr3_rd_data      ) ,
      .ddr3_rd_done    (ddr3_rd_done      ) ,
      .ddr3_rd_busy    (ddr3_rd_busy      ) 
         
      );
   ddr3_arbit ddr3_arbit(
              . ui_clk      (ui_clk) ,
              . ui_rst      (ui_rst) ,
              . ddr3_wr_req      (ddr3_wr_req ) ,
              . ddr3_wr_done     (ddr3_wr_done) ,
              . ddr3_rd_req      (ddr3_rd_req ) ,
              . ddr3_rd_done     (ddr3_rd_done) ,
              . ddr3_wr_ack      (ddr3_wr_ack ) ,
              . ddr3_rd_ack      (ddr3_rd_ack )
      );
    mig_ddr3 u_mig_ddr3 (
      // Memory interface ports
     .ddr3_addr            (ddr3_addr),    // output [14:0]
     .ddr3_ba              (ddr3_ba),      // output [2:0] 
     .ddr3_cas_n           (ddr3_cas_n),   // output		
     .ddr3_ck_n            (ddr3_ck_n),    // output [0:0] 
     .ddr3_ck_p            (ddr3_ck_p),    // output [0:0] 
     .ddr3_cke             (ddr3_cke),     // output [0:0] 
     .ddr3_ras_n           (ddr3_ras_n),   // output		
     .ddr3_reset_n         (ddr3_reset_n), // output		
     .ddr3_we_n            (ddr3_we_n),    // output		
     .ddr3_dq              (ddr3_dq),      // inout [31:0] 
     .ddr3_dqs_n           (ddr3_dqs_n),   // inout  [3:0]	
     .ddr3_dqs_p           (ddr3_dqs_p),   // inout  [3:0]	
     .ddr3_dm              (ddr3_dm),      // output [3:0] 
     .ddr3_odt             (ddr3_odt),     // output [0:0] 
     .init_calib_complete  (init_calib_complete), // output
     // Application interface ports
     .app_addr         (app_addr),     // input [28:0]
     .app_cmd          (app_cmd),      // input [2:0]
     .app_en           (app_en),       // input		
     .app_wdf_mask     (app_wdf_mask), // input [31:0]
     .app_wdf_data     (app_wdf_data), // input [255:0
     .app_wdf_end      (app_wdf_end),  // input		
     .app_wdf_wren     (app_wdf_wren), // input		
     .app_rd_data      (app_rd_data),  // output [255:0]	
     .app_rd_data_end  (app_rd_data_end),  // output       
     .app_rd_data_valid(app_rd_data_valid),// output 
     .app_rdy          (app_rdy),         // output		
     .app_wdf_rdy      (app_wdf_rdy),     // output		
     .app_sr_req       (0),  // input			
     .app_ref_req      (0),  // input			
     .app_zq_req       (0),  // input			
     .app_sr_active    (),  // output			
     .app_ref_ack      (),  // output			
     .app_zq_ack       (),  // output			
     .ui_clk           (ui_clk),  // output		
     .ui_clk_sync_rst (ui_clk_sync_rst), // output 
     // System Clock port
     .sys_clk_i                (sys_clk),
     .sys_rst                  (sys_rst_n) // input 
     );
  endmodule
