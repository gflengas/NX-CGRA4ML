/*
    Two-master top wrapper of the core, with the SoC LLPP register port.
      - m_axi_shared  : weights DMA read channels + output DMA write channels
      - m_axi_weights : pixel DMA read channels (pixel reads on their own SoC
                        port, so they never share one with output writes)
      - s_llpp        : SoC LLPP0 port, served by ngultra_llpp_regs

    No AXI arbiter is needed: the shared data port only combines disjoint
    channel sets (the read DMA uses AR/R, the output DMA AW/W/B).
*/
`timescale 1ns/1ps
`define VERILOG
`include "defines.svh"
`undef VERILOG

module axi_cgra4ml_2m #(
                // For engine
    parameter   ROWS                    = `ROWS               ,
                COLS                    = `COLS               ,
                X_BITS                  = `X_BITS             ,
                K_BITS                  = `K_BITS             ,
                Y_BITS                  = `Y_BITS             ,
                Y_OUT_BITS              = `Y_OUT_BITS         ,
                M_DATA_WIDTH_HF_CONV    = COLS  * ROWS  * Y_BITS,
                M_DATA_WIDTH_HF_CONV_DW = ROWS  * Y_BITS,

                // Full AXI
                AXI_WIDTH               = `AXI_WIDTH          ,
                AXI_ID_WIDTH            = 6,
                AXI_STRB_WIDTH          = (AXI_WIDTH/8),
                AXI_MAX_BURST_LEN       = `AXI_MAX_BURST_LEN  ,
                AXI_ADDR_WIDTH          = 32,
                // Controller registers (32-bit words) and the SoC LLPP port
                AXIL_WIDTH              = 32,
                AXIL_ADDR_WIDTH         = 40,
                STRB_WIDTH              = 4,
                LLPP_ID_WIDTH           = 12,
                LLPP_ADDR_WIDTH         = 32,
                W_BPT                   = `W_BPT
) (
    input  wire                       clk,
    input  wire                       rstn,

    // SoC LLPP0 port (AXI, single-beat 32-bit register accesses)
    input  wire [LLPP_ID_WIDTH-1:0]   s_llpp_awid,
    input  wire [LLPP_ADDR_WIDTH-1:0] s_llpp_awaddr,
    input  wire [7:0]                 s_llpp_awlen,
    input  wire                       s_llpp_awvalid,
    output wire                       s_llpp_awready,
    input  wire [AXIL_WIDTH-1:0]      s_llpp_wdata,
    input  wire [STRB_WIDTH-1:0]      s_llpp_wstrb,
    input  wire                       s_llpp_wlast,
    input  wire                       s_llpp_wvalid,
    output wire                       s_llpp_wready,
    output wire [LLPP_ID_WIDTH-1:0]   s_llpp_bid,
    output wire [1:0]                 s_llpp_bresp,
    output wire                       s_llpp_bvalid,
    input  wire                       s_llpp_bready,
    input  wire [LLPP_ID_WIDTH-1:0]   s_llpp_arid,
    input  wire [LLPP_ADDR_WIDTH-1:0] s_llpp_araddr,
    input  wire [7:0]                 s_llpp_arlen,
    input  wire                       s_llpp_arvalid,
    output wire                       s_llpp_arready,
    output wire [AXIL_WIDTH-1:0]      s_llpp_rdata,
    output wire [LLPP_ID_WIDTH-1:0]   s_llpp_rid,
    output wire [1:0]                 s_llpp_rresp,
    output wire                       s_llpp_rlast,
    output wire                       s_llpp_rvalid,
    input  wire                       s_llpp_rready,

    /*
     * Shared AXI4 master interface:
     *   read  path -> pixel DMA
     *   write path -> output DMA
     */
    output wire [AXI_ID_WIDTH-1:0]    m_axi_shared_arid,
    output wire [AXI_ADDR_WIDTH-1:0]  m_axi_shared_araddr,
    output wire [7:0]                 m_axi_shared_arlen,
    output wire [2:0]                 m_axi_shared_arsize,
    output wire [1:0]                 m_axi_shared_arburst,
    output wire                       m_axi_shared_arlock,
    output wire [3:0]                 m_axi_shared_arcache,
    output wire [2:0]                 m_axi_shared_arprot,
    output wire                       m_axi_shared_arvalid,
    input  wire                       m_axi_shared_arready,
    input  wire [AXI_ID_WIDTH-1:0]    m_axi_shared_rid,
    input  wire [AXI_WIDTH-1:0]       m_axi_shared_rdata,
    input  wire [1:0]                 m_axi_shared_rresp,
    input  wire                       m_axi_shared_rlast,
    input  wire                       m_axi_shared_rvalid,
    output wire                       m_axi_shared_rready,
    output wire [AXI_ID_WIDTH-1:0]    m_axi_shared_awid,
    output wire [AXI_ADDR_WIDTH-1:0]  m_axi_shared_awaddr,
    output wire [7:0]                 m_axi_shared_awlen,
    output wire [2:0]                 m_axi_shared_awsize,
    output wire [1:0]                 m_axi_shared_awburst,
    output wire                       m_axi_shared_awlock,
    output wire [3:0]                 m_axi_shared_awcache,
    output wire [2:0]                 m_axi_shared_awprot,
    output wire                       m_axi_shared_awvalid,
    input  wire                       m_axi_shared_awready,
    output wire [AXI_WIDTH-1:0]       m_axi_shared_wdata,
    output wire [AXI_STRB_WIDTH-1:0]  m_axi_shared_wstrb,
    output wire                       m_axi_shared_wlast,
    output wire                       m_axi_shared_wvalid,
    input  wire                       m_axi_shared_wready,
    input  wire [AXI_ID_WIDTH-1:0]    m_axi_shared_bid,
    input  wire [1:0]                 m_axi_shared_bresp,
    input  wire                       m_axi_shared_bvalid,
    output wire                       m_axi_shared_bready,

    /*
     * Dedicated AXI4 read master for weights
     */
    output wire [AXI_ID_WIDTH-1:0]    m_axi_weights_arid,
    output wire [AXI_ADDR_WIDTH-1:0]  m_axi_weights_araddr,
    output wire [7:0]                 m_axi_weights_arlen,
    output wire [2:0]                 m_axi_weights_arsize,
    output wire [1:0]                 m_axi_weights_arburst,
    output wire                       m_axi_weights_arlock,
    output wire [3:0]                 m_axi_weights_arcache,
    output wire [2:0]                 m_axi_weights_arprot,
    output wire                       m_axi_weights_arvalid,
    input  wire                       m_axi_weights_arready,
    input  wire [AXI_ID_WIDTH-1:0]    m_axi_weights_rid,
    input  wire [AXI_WIDTH-1:0]       m_axi_weights_rdata,
    input  wire [1:0]                 m_axi_weights_rresp,
    input  wire                       m_axi_weights_rlast,
    input  wire                       m_axi_weights_rvalid,
    output wire                       m_axi_weights_rready
);

// LLPP register slave on the core's register port.
wire                       reg_ext_wr_en, reg_ext_rd_en;
wire [AXIL_ADDR_WIDTH-1:0] reg_ext_wr_addr, reg_ext_rd_addr;
wire [AXIL_WIDTH-1:0]      reg_ext_wr_data, reg_ext_rd_data;

ngultra_llpp_regs #(
    .ID_WIDTH(LLPP_ID_WIDTH),
    .ADDR_WIDTH(LLPP_ADDR_WIDTH),
    .DATA_WIDTH(AXIL_WIDTH),
    .REG_ADDR_BITS(12),
    .REG_WIDTH(AXIL_ADDR_WIDTH)
) LLPP_REGS (
    .clk(clk),
    .rstn(rstn),
    .s_awid(s_llpp_awid),
    .s_awaddr(s_llpp_awaddr),
    .s_awlen(s_llpp_awlen),
    .s_awvalid(s_llpp_awvalid),
    .s_awready(s_llpp_awready),
    .s_wdata(s_llpp_wdata),
    .s_wstrb(s_llpp_wstrb),
    .s_wlast(s_llpp_wlast),
    .s_wvalid(s_llpp_wvalid),
    .s_wready(s_llpp_wready),
    .s_bid(s_llpp_bid),
    .s_bresp(s_llpp_bresp),
    .s_bvalid(s_llpp_bvalid),
    .s_bready(s_llpp_bready),
    .s_arid(s_llpp_arid),
    .s_araddr(s_llpp_araddr),
    .s_arlen(s_llpp_arlen),
    .s_arvalid(s_llpp_arvalid),
    .s_arready(s_llpp_arready),
    .s_rdata(s_llpp_rdata),
    .s_rid(s_llpp_rid),
    .s_rresp(s_llpp_rresp),
    .s_rlast(s_llpp_rlast),
    .s_rvalid(s_llpp_rvalid),
    .s_rready(s_llpp_rready),
    .reg_wr_en(reg_ext_wr_en),
    .reg_wr_addr(reg_ext_wr_addr),
    .reg_wr_data(reg_ext_wr_data),
    .reg_rd_en(reg_ext_rd_en),
    .reg_rd_addr(reg_ext_rd_addr),
    .reg_rd_data(reg_ext_rd_data)
);

axi_cgra4ml #(
    .ROWS(ROWS),
    .COLS(COLS),
    .X_BITS(X_BITS),
    .K_BITS(K_BITS),
    .Y_BITS(Y_BITS),
    .Y_OUT_BITS(Y_OUT_BITS),
    .M_DATA_WIDTH_HF_CONV(M_DATA_WIDTH_HF_CONV),
    .M_DATA_WIDTH_HF_CONV_DW(M_DATA_WIDTH_HF_CONV_DW),
    .AXI_WIDTH(AXI_WIDTH),
    .AXI_ID_WIDTH(AXI_ID_WIDTH),
    .AXI_STRB_WIDTH(AXI_STRB_WIDTH),
    .AXI_MAX_BURST_LEN(AXI_MAX_BURST_LEN),
    .AXI_ADDR_WIDTH(AXI_ADDR_WIDTH),
    .AXIL_WIDTH(AXIL_WIDTH),
    .AXIL_ADDR_WIDTH(AXIL_ADDR_WIDTH),
    .STRB_WIDTH(STRB_WIDTH),
    .W_BPT(W_BPT)
) core (
    .clk(clk),
    .rstn(rstn),
    .reg_ext_wr_en(reg_ext_wr_en),
    .reg_ext_wr_addr(reg_ext_wr_addr),
    .reg_ext_wr_data(reg_ext_wr_data),
    .reg_ext_rd_en(reg_ext_rd_en),
    .reg_ext_rd_addr(reg_ext_rd_addr),
    .reg_ext_rd_data(reg_ext_rd_data),

    .m_axi_pixel_arid(m_axi_weights_arid),
    .m_axi_pixel_araddr(m_axi_weights_araddr),
    .m_axi_pixel_arlen(m_axi_weights_arlen),
    .m_axi_pixel_arsize(m_axi_weights_arsize),
    .m_axi_pixel_arburst(m_axi_weights_arburst),
    .m_axi_pixel_arlock(m_axi_weights_arlock),
    .m_axi_pixel_arcache(m_axi_weights_arcache),
    .m_axi_pixel_arprot(m_axi_weights_arprot),
    .m_axi_pixel_arvalid(m_axi_weights_arvalid),
    .m_axi_pixel_arready(m_axi_weights_arready),
    .m_axi_pixel_rid(m_axi_weights_rid),
    .m_axi_pixel_rdata(m_axi_weights_rdata),
    .m_axi_pixel_rresp(m_axi_weights_rresp),
    .m_axi_pixel_rlast(m_axi_weights_rlast),
    .m_axi_pixel_rvalid(m_axi_weights_rvalid),
    .m_axi_pixel_rready(m_axi_weights_rready),

    .m_axi_weights_arid(m_axi_shared_arid),
    .m_axi_weights_araddr(m_axi_shared_araddr),
    .m_axi_weights_arlen(m_axi_shared_arlen),
    .m_axi_weights_arsize(m_axi_shared_arsize),
    .m_axi_weights_arburst(m_axi_shared_arburst),
    .m_axi_weights_arlock(m_axi_shared_arlock),
    .m_axi_weights_arcache(m_axi_shared_arcache),
    .m_axi_weights_arprot(m_axi_shared_arprot),
    .m_axi_weights_arvalid(m_axi_shared_arvalid),
    .m_axi_weights_arready(m_axi_shared_arready),
    .m_axi_weights_rid(m_axi_shared_rid),
    .m_axi_weights_rdata(m_axi_shared_rdata),
    .m_axi_weights_rresp(m_axi_shared_rresp),
    .m_axi_weights_rlast(m_axi_shared_rlast),
    .m_axi_weights_rvalid(m_axi_shared_rvalid),
    .m_axi_weights_rready(m_axi_shared_rready),

    .m_axi_output_awid(m_axi_shared_awid),
    .m_axi_output_awaddr(m_axi_shared_awaddr),
    .m_axi_output_awlen(m_axi_shared_awlen),
    .m_axi_output_awsize(m_axi_shared_awsize),
    .m_axi_output_awburst(m_axi_shared_awburst),
    .m_axi_output_awlock(m_axi_shared_awlock),
    .m_axi_output_awcache(m_axi_shared_awcache),
    .m_axi_output_awprot(m_axi_shared_awprot),
    .m_axi_output_awvalid(m_axi_shared_awvalid),
    .m_axi_output_awready(m_axi_shared_awready),
    .m_axi_output_wdata(m_axi_shared_wdata),
    .m_axi_output_wstrb(m_axi_shared_wstrb),
    .m_axi_output_wlast(m_axi_shared_wlast),
    .m_axi_output_wvalid(m_axi_shared_wvalid),
    .m_axi_output_wready(m_axi_shared_wready),
    .m_axi_output_bid(m_axi_shared_bid),
    .m_axi_output_bresp(m_axi_shared_bresp),
    .m_axi_output_bvalid(m_axi_shared_bvalid),
    .m_axi_output_bready(m_axi_shared_bready)
);

endmodule
