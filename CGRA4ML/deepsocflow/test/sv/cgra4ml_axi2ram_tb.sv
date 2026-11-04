/* 
    Top module for simulation
    Components
    RTL on-chip Top
        - DNN engine
        - Controller
            -BRAM and register
        - LLPP register port, driven by the C++ harness
        - 3 Alex DMAs
    3 Zip cpus converting AXI4 requests to ram rw requests
*/
`timescale 1ns/1ps
`define VERILOG
`include "../../rtl/defines.svh"
`include "config_tb.svh"
`undef  VERILOG

module cgra4ml_axi2ram_tb #(
    // Parameters for DNN engine
    parameter   ROWS                    = `ROWS               ,
                COLS                    = `COLS               ,
                X_BITS                  = `X_BITS             , 
                K_BITS                  = `K_BITS             , 
                Y_BITS                  = `Y_BITS             ,
                Y_OUT_BITS              = `Y_OUT_BITS         ,
                M_DATA_WIDTH_HF_CONV    = COLS  * ROWS  * Y_BITS,
                M_DATA_WIDTH_HF_CONV_DW = ROWS  * Y_BITS,

                AXI_WIDTH               = `AXI_WIDTH  ,
                AXI_MAX_BURST_LEN       = `AXI_MAX_BURST_LEN,
                W_BPT                   = `W_BPT,

                OUT_ADDR_WIDTH          = 10,
                OUT_BITS                = 32,
    // Parameters for controller
                SRAM_RD_DATA_WIDTH      = 256,
                SRAM_RD_DEPTH           = `MAX_N_BUNDLES,
                COUNTER_WIDTH           = 16,
                AXI_ADDR_WIDTH          = 32,
                AXIL_WIDTH              = 32,
                AXI_LEN_WIDTH           = 32,
                AXIL_BASE_ADDR          = `CONFIG_BASEADDR,
    
    // Register port parameters
                DATA_WR_WIDTH           = 32,
                DATA_RD_WIDTH           = 32,
                AXIL_ADDR_WIDTH              = 40,
                STRB_WIDTH              = 4,
                TIMEOUT                 = 2,

    // Alex AXI DMA RD
                AXI_DATA_WIDTH_PS       = AXI_WIDTH,
                //AXI_ADDR_WIDTH          = 32, same as above
                AXI_STRB_WIDTH          = (AXI_WIDTH/8),
                AXI_ID_WIDTH            = 6,
                AXIS_DATA_WIDTH         = AXI_WIDTH,//AXIL_DATA_WIDTH,
                AXIS_KEEP_ENABLE        = 1,//(AXIS_DATA_WIDTH>8),
                AXIS_KEEP_WIDTH         = (AXI_WIDTH/8),//(AXIS_DATA_WIDTH/8),
                AXIS_LAST_ENABLE        = 1,
                AXIS_ID_ENABLE          = 0,
                AXIS_ID_WIDTH           = 6,
                AXIS_DEST_ENABLE        = 0,
                AXIS_DEST_WIDTH         = 8,
                AXIS_USER_ENABLE        = 1,
                AXIS_USER_WIDTH         = 1,
                LEN_WIDTH               = 32,
                TAG_WIDTH               = 8,
                ENABLE_SG               = 0,
                ENABLE_UNALIGNED        = 1,
    
    // Parameters for zip cpu
		        C_S_AXI_ID_WIDTH	    = 6,
		        C_S_AXI_DATA_WIDTH	    = AXI_WIDTH,
		        C_S_AXI_ADDR_WIDTH	    = 32,
		        OPT_LOCK                = 1'b0,
		        OPT_LOCKID              = 1'b1,
		        OPT_LOWPOWER            = 1'b0,
    // Randomizer for AXI4 requests
                VALID_PROB              = `VALID_PROB,
                READY_PROB              = `READY_PROB,

    localparam	LSB = $clog2(C_S_AXI_DATA_WIDTH)-3                
)(
    input  wire                   clk,
    input  wire                   rstn,

    // SoC LLPP0 register port, driven by the C++ harness (sim/sim.cpp)
    input  wire [11:0]            s_llpp_awid,
    input  wire [31:0]            s_llpp_awaddr,
    input  wire [7:0]             s_llpp_awlen,
    input  wire                   s_llpp_awvalid,
    output wire                   s_llpp_awready,
    input  wire [DATA_WR_WIDTH-1:0] s_llpp_wdata,
    input  wire [STRB_WIDTH-1:0]  s_llpp_wstrb,
    input  wire                   s_llpp_wlast,
    input  wire                   s_llpp_wvalid,
    output wire                   s_llpp_wready,
    output wire [11:0]            s_llpp_bid,
    output wire [1:0]             s_llpp_bresp,
    output wire                   s_llpp_bvalid,
    input  wire                   s_llpp_bready,
    input  wire [11:0]            s_llpp_arid,
    input  wire [31:0]            s_llpp_araddr,
    input  wire [7:0]             s_llpp_arlen,
    input  wire                   s_llpp_arvalid,
    output wire                   s_llpp_arready,
    output wire [DATA_RD_WIDTH-1:0] s_llpp_rdata,
    output wire [11:0]            s_llpp_rid,
    output wire [1:0]             s_llpp_rresp,
    output wire                   s_llpp_rlast,
    output wire                   s_llpp_rvalid,
    input  wire                   s_llpp_rready,

    // Shared-DDR AXI ports, served by the C++ harness (sim/ngultra_runtime_sim.cpp)
    output wire [AXI_ID_WIDTH-1:0] host_pixel_arid,
    output wire [31:0] host_pixel_araddr,
    output wire [7:0] host_pixel_arlen,
    output wire [2:0] host_pixel_arsize,
    output wire [1:0] host_pixel_arburst,
    output wire host_pixel_arvalid,
    input  wire host_pixel_arready,
    input  wire [AXI_ID_WIDTH-1:0] host_pixel_rid,
    input  wire [AXI_WIDTH-1:0] host_pixel_rdata,
    input  wire [1:0] host_pixel_rresp,
    input  wire host_pixel_rlast,
    input  wire host_pixel_rvalid,
    output wire host_pixel_rready,
    output wire [AXI_ID_WIDTH-1:0] host_weight_arid,
    output wire [31:0] host_weight_araddr,
    output wire [7:0] host_weight_arlen,
    output wire [2:0] host_weight_arsize,
    output wire [1:0] host_weight_arburst,
    output wire host_weight_arvalid,
    input  wire host_weight_arready,
    input  wire [AXI_ID_WIDTH-1:0] host_weight_rid,
    input  wire [AXI_WIDTH-1:0] host_weight_rdata,
    input  wire [1:0] host_weight_rresp,
    input  wire host_weight_rlast,
    input  wire host_weight_rvalid,
    output wire host_weight_rready,
    output wire [AXI_ID_WIDTH-1:0] host_output_awid,
    output wire [31:0] host_output_awaddr,
    output wire [7:0] host_output_awlen,
    output wire [2:0] host_output_awsize,
    output wire [1:0] host_output_awburst,
    output wire host_output_awvalid,
    input  wire host_output_awready,
    output wire [AXI_WIDTH-1:0] host_output_wdata,
    output wire [AXI_WIDTH/8-1:0] host_output_wstrb,
    output wire host_output_wlast,
    output wire host_output_wvalid,
    input  wire host_output_wready,
    input  wire [AXI_ID_WIDTH-1:0] host_output_bid,
    input  wire [1:0] host_output_bresp,
    input  wire host_output_bvalid,
    output wire host_output_bready
`ifdef CGRA4ML_NGULTRA_HOST_DIAGNOSTICS
    ,output wire dbg_pixel_ar,
    output wire [31:0] dbg_pixel_araddr,
    output wire [7:0] dbg_pixel_arlen,
    output wire dbg_weight_ar,
    output wire [31:0] dbg_weight_araddr,
    output wire [7:0] dbg_weight_arlen,
    output wire dbg_output_aw,
    output wire [31:0] dbg_output_awaddr,
    output wire [7:0] dbg_output_awlen,
    output wire dbg_pixel_r,
    output wire dbg_pixel_rlast,
    output wire [1:0] dbg_pixel_rresp,
    output wire dbg_weight_r,
    output wire dbg_weight_rlast,
    output wire [1:0] dbg_weight_rresp,
    output wire dbg_output_w,
    output wire dbg_output_wlast,
    output wire dbg_output_b,
    output wire [1:0] dbg_output_bresp
`endif
);

// AXI ports from top on-chip module
    wire [AXI_ID_WIDTH-1:0]    m_axi_pixel_arid;
    wire [AXI_ADDR_WIDTH-1:0]  m_axi_pixel_araddr;
    wire [7:0]                 m_axi_pixel_arlen;
    wire [2:0]                 m_axi_pixel_arsize;
    wire [1:0]                 m_axi_pixel_arburst;
    wire                       m_axi_pixel_arlock;
    wire [3:0]                 m_axi_pixel_arcache;
    wire [2:0]                 m_axi_pixel_arprot;
    wire                       m_axi_pixel_arvalid;
    wire                       m_axi_pixel_arvalid_zipcpu;
    wire                       m_axi_pixel_arready;
    wire                       m_axi_pixel_arready_zipcpu;
    wire [AXI_ID_WIDTH-1:0]    m_axi_pixel_rid;
    wire [AXI_DATA_WIDTH_PS-1:0]  m_axi_pixel_rdata;
    wire [1:0]                 m_axi_pixel_rresp;
    wire                       m_axi_pixel_rlast;
    wire                       m_axi_pixel_rvalid;
    wire                       m_axi_pixel_rvalid_zipcpu;
    wire                       m_axi_pixel_rready;
    wire                       m_axi_pixel_rready_zipcpu;

    wire [AXI_ID_WIDTH-1:0]    m_axi_weights_arid;
    wire [AXI_ADDR_WIDTH-1:0]  m_axi_weights_araddr;
    wire [7:0]                 m_axi_weights_arlen;
    wire [2:0]                 m_axi_weights_arsize;
    wire [1:0]                 m_axi_weights_arburst;
    wire                       m_axi_weights_arlock;
    wire [3:0]                 m_axi_weights_arcache;
    wire [2:0]                 m_axi_weights_arprot;
    wire                       m_axi_weights_arvalid;
    wire                       m_axi_weights_arvalid_zipcpu;
    wire                       m_axi_weights_arready;
    wire                       m_axi_weights_arready_zipcpu;
    wire [AXI_ID_WIDTH-1:0]    m_axi_weights_rid;
    wire [AXI_DATA_WIDTH_PS-1:0]  m_axi_weights_rdata;
    wire [1:0]                 m_axi_weights_rresp;
    wire                       m_axi_weights_rlast;
    wire                       m_axi_weights_rvalid;
    wire                       m_axi_weights_rvalid_zipcpu;
    wire                       m_axi_weights_rready;
    wire                       m_axi_weights_rready_zipcpu;

    wire [AXI_ID_WIDTH-1:0]    m_axi_output_awid;
    wire [AXI_ADDR_WIDTH-1:0]  m_axi_output_awaddr;
    wire [7:0]                 m_axi_output_awlen;
    wire [2:0]                 m_axi_output_awsize;
    wire [1:0]                 m_axi_output_awburst;
    wire                       m_axi_output_awlock;
    wire [3:0]                 m_axi_output_awcache;
    wire [2:0]                 m_axi_output_awprot;
    wire                       m_axi_output_awvalid;
    wire                       m_axi_output_awvalid_zipcpu;
    wire                       m_axi_output_awready;
    wire                       m_axi_output_awready_zipcpu;
    wire [AXI_DATA_WIDTH_PS-1:0]  m_axi_output_wdata;
    wire [AXI_STRB_WIDTH-1:0]  m_axi_output_wstrb;
    wire                       m_axi_output_wlast;
    wire                       m_axi_output_wvalid;
    wire                       m_axi_output_wvalid_zipcpu;
    wire                       m_axi_output_wready;
    wire                       m_axi_output_wready_zipcpu;
    wire [AXI_ID_WIDTH-1:0]    m_axi_output_bid;
    wire [1:0]                 m_axi_output_bresp;
    wire                       m_axi_output_bvalid;
    wire                       m_axi_output_bvalid_zipcpu;
    wire                       m_axi_output_bready;
    wire                       m_axi_output_bready_zipcpu;

`ifdef CGRA4ML_NGULTRA_HOST_DIAGNOSTICS
    assign dbg_pixel_ar = m_axi_pixel_arvalid && m_axi_pixel_arready;
    assign dbg_pixel_araddr = m_axi_pixel_araddr;
    assign dbg_pixel_arlen = m_axi_pixel_arlen;
    assign dbg_weight_ar = m_axi_weights_arvalid && m_axi_weights_arready;
    assign dbg_weight_araddr = m_axi_weights_araddr;
    assign dbg_weight_arlen = m_axi_weights_arlen;
    assign dbg_output_aw = m_axi_output_awvalid && m_axi_output_awready;
    assign dbg_output_awaddr = m_axi_output_awaddr;
    assign dbg_output_awlen = m_axi_output_awlen;
    assign dbg_pixel_r = m_axi_pixel_rvalid && m_axi_pixel_rready;
    assign dbg_pixel_rlast = m_axi_pixel_rlast;
    assign dbg_pixel_rresp = m_axi_pixel_rresp;
    assign dbg_weight_r = m_axi_weights_rvalid && m_axi_weights_rready;
    assign dbg_weight_rlast = m_axi_weights_rlast;
    assign dbg_weight_rresp = m_axi_weights_rresp;
    assign dbg_output_w = m_axi_output_wvalid && m_axi_output_wready;
    assign dbg_output_wlast = m_axi_output_wlast;
    assign dbg_output_b = m_axi_output_bvalid && m_axi_output_bready;
    assign dbg_output_bresp = m_axi_output_bresp;
`endif

`define CHECK_SHARED_AXI_STABLE(VALID, READY, PAYLOAD) \
    assert property (@(posedge clk) disable iff (!rstn) \
        (VALID && !READY) |=> (VALID && $stable(PAYLOAD))) \
        else $fatal(1, "shared-model AXI VALID/payload changed under backpressure");
    `CHECK_SHARED_AXI_STABLE(m_axi_pixel_arvalid, m_axi_pixel_arready,
                             {m_axi_pixel_arid, m_axi_pixel_araddr, m_axi_pixel_arlen, m_axi_pixel_arsize, m_axi_pixel_arburst})
    `CHECK_SHARED_AXI_STABLE(m_axi_pixel_rvalid, m_axi_pixel_rready,
                             {m_axi_pixel_rid, m_axi_pixel_rdata, m_axi_pixel_rresp, m_axi_pixel_rlast})
    `CHECK_SHARED_AXI_STABLE(m_axi_weights_arvalid, m_axi_weights_arready,
                             {m_axi_weights_arid, m_axi_weights_araddr, m_axi_weights_arlen, m_axi_weights_arsize, m_axi_weights_arburst})
    `CHECK_SHARED_AXI_STABLE(m_axi_weights_rvalid, m_axi_weights_rready,
                             {m_axi_weights_rid, m_axi_weights_rdata, m_axi_weights_rresp, m_axi_weights_rlast})
    `CHECK_SHARED_AXI_STABLE(m_axi_output_awvalid, m_axi_output_awready,
                             {m_axi_output_awid, m_axi_output_awaddr, m_axi_output_awlen, m_axi_output_awsize, m_axi_output_awburst})
    `CHECK_SHARED_AXI_STABLE(m_axi_output_wvalid, m_axi_output_wready,
                             {m_axi_output_wdata, m_axi_output_wstrb, m_axi_output_wlast})
    `CHECK_SHARED_AXI_STABLE(m_axi_output_bvalid, m_axi_output_bready,
                             {m_axi_output_bid, m_axi_output_bresp})
`undef CHECK_SHARED_AXI_STABLE

    assign host_pixel_arid = m_axi_pixel_arid;
    assign host_pixel_araddr = m_axi_pixel_araddr;
    assign host_pixel_arlen = m_axi_pixel_arlen;
    assign host_pixel_arsize = m_axi_pixel_arsize;
    assign host_pixel_arburst = m_axi_pixel_arburst;
    assign host_pixel_arvalid = m_axi_pixel_arvalid;
    assign m_axi_pixel_arready = host_pixel_arready;
    assign m_axi_pixel_rid = host_pixel_rid;
    assign m_axi_pixel_rdata = host_pixel_rdata;
    assign m_axi_pixel_rresp = host_pixel_rresp;
    assign m_axi_pixel_rlast = host_pixel_rlast;
    assign m_axi_pixel_rvalid = host_pixel_rvalid;
    assign host_pixel_rready = m_axi_pixel_rready;

    assign host_weight_arid = m_axi_weights_arid;
    assign host_weight_araddr = m_axi_weights_araddr;
    assign host_weight_arlen = m_axi_weights_arlen;
    assign host_weight_arsize = m_axi_weights_arsize;
    assign host_weight_arburst = m_axi_weights_arburst;
    assign host_weight_arvalid = m_axi_weights_arvalid;
    assign m_axi_weights_arready = host_weight_arready;
    assign m_axi_weights_rid = host_weight_rid;
    assign m_axi_weights_rdata = host_weight_rdata;
    assign m_axi_weights_rresp = host_weight_rresp;
    assign m_axi_weights_rlast = host_weight_rlast;
    assign m_axi_weights_rvalid = host_weight_rvalid;
    assign host_weight_rready = m_axi_weights_rready;

    assign host_output_awid = m_axi_output_awid;
    assign host_output_awaddr = m_axi_output_awaddr;
    assign host_output_awlen = m_axi_output_awlen;
    assign host_output_awsize = m_axi_output_awsize;
    assign host_output_awburst = m_axi_output_awburst;
    assign host_output_awvalid = m_axi_output_awvalid;
    assign m_axi_output_awready = host_output_awready;
    assign host_output_wdata = m_axi_output_wdata;
    assign host_output_wstrb = m_axi_output_wstrb;
    assign host_output_wlast = m_axi_output_wlast;
    assign host_output_wvalid = m_axi_output_wvalid;
    assign m_axi_output_wready = host_output_wready;
    assign m_axi_output_bid = host_output_bid;
    assign m_axi_output_bresp = host_output_bresp;
    assign m_axi_output_bvalid = host_output_bvalid;
    assign host_output_bready = m_axi_output_bready;

`ifdef CGRA4ML_2M_SIM
axi_cgra4ml_2m #(
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
) OC_TOP (
    .clk(clk),
    .rstn(rstn),
    .s_llpp_awaddr(s_llpp_awaddr),
    .s_llpp_awvalid(s_llpp_awvalid),
    .s_llpp_awready(s_llpp_awready),
    .s_llpp_wdata(s_llpp_wdata),
    .s_llpp_wstrb(s_llpp_wstrb),
    .s_llpp_wvalid(s_llpp_wvalid),
    .s_llpp_wready(s_llpp_wready),
    .s_llpp_bresp(s_llpp_bresp),
    .s_llpp_bvalid(s_llpp_bvalid),
    .s_llpp_bready(s_llpp_bready),
    .s_llpp_araddr(s_llpp_araddr),
    .s_llpp_arvalid(s_llpp_arvalid),
    .s_llpp_arready(s_llpp_arready),
    .s_llpp_rdata(s_llpp_rdata),
    .s_llpp_rresp(s_llpp_rresp),
    .s_llpp_rvalid(s_llpp_rvalid),
    .s_llpp_rready(s_llpp_rready),
    .s_llpp_awid(s_llpp_awid),
    .s_llpp_awlen(s_llpp_awlen),
    .s_llpp_wlast(s_llpp_wlast),
    .s_llpp_bid(s_llpp_bid),
    .s_llpp_arid(s_llpp_arid),
    .s_llpp_arlen(s_llpp_arlen),
    .s_llpp_rid(s_llpp_rid),
    .s_llpp_rlast(s_llpp_rlast),

    .m_axi_shared_arid(m_axi_pixel_arid),
    .m_axi_shared_araddr(m_axi_pixel_araddr),
    .m_axi_shared_arlen(m_axi_pixel_arlen),
    .m_axi_shared_arsize(m_axi_pixel_arsize),
    .m_axi_shared_arburst(m_axi_pixel_arburst),
    .m_axi_shared_arlock(m_axi_pixel_arlock),
    .m_axi_shared_arcache(m_axi_pixel_arcache),
    .m_axi_shared_arprot(m_axi_pixel_arprot),
    .m_axi_shared_arvalid(m_axi_pixel_arvalid),
    .m_axi_shared_arready(m_axi_pixel_arready),
    .m_axi_shared_rid(m_axi_pixel_rid),
    .m_axi_shared_rdata(m_axi_pixel_rdata),
    .m_axi_shared_rresp(m_axi_pixel_rresp),
    .m_axi_shared_rlast(m_axi_pixel_rlast),
    .m_axi_shared_rvalid(m_axi_pixel_rvalid),
    .m_axi_shared_rready(m_axi_pixel_rready),
    .m_axi_shared_awid(m_axi_output_awid),
    .m_axi_shared_awaddr(m_axi_output_awaddr),
    .m_axi_shared_awlen(m_axi_output_awlen),
    .m_axi_shared_awsize(m_axi_output_awsize),
    .m_axi_shared_awburst(m_axi_output_awburst),
    .m_axi_shared_awlock(m_axi_output_awlock),
    .m_axi_shared_awcache(m_axi_output_awcache),
    .m_axi_shared_awprot(m_axi_output_awprot),
    .m_axi_shared_awvalid(m_axi_output_awvalid),
    .m_axi_shared_awready(m_axi_output_awready),
    .m_axi_shared_wdata(m_axi_output_wdata),
    .m_axi_shared_wstrb(m_axi_output_wstrb),
    .m_axi_shared_wlast(m_axi_output_wlast),
    .m_axi_shared_wvalid(m_axi_output_wvalid),
    .m_axi_shared_wready(m_axi_output_wready),
    .m_axi_shared_bid(m_axi_output_bid),
    .m_axi_shared_bresp(m_axi_output_bresp),
    .m_axi_shared_bvalid(m_axi_output_bvalid),
    .m_axi_shared_bready(m_axi_output_bready),

    .m_axi_weights_arid(m_axi_weights_arid),
    .m_axi_weights_araddr(m_axi_weights_araddr),
    .m_axi_weights_arlen(m_axi_weights_arlen),
    .m_axi_weights_arsize(m_axi_weights_arsize),
    .m_axi_weights_arburst(m_axi_weights_arburst),
    .m_axi_weights_arlock(m_axi_weights_arlock),
    .m_axi_weights_arcache(m_axi_weights_arcache),
    .m_axi_weights_arprot(m_axi_weights_arprot),
    .m_axi_weights_arvalid(m_axi_weights_arvalid),
    .m_axi_weights_arready(m_axi_weights_arready),
    .m_axi_weights_rid(m_axi_weights_rid),
    .m_axi_weights_rdata(m_axi_weights_rdata),
    .m_axi_weights_rresp(m_axi_weights_rresp),
    .m_axi_weights_rlast(m_axi_weights_rlast),
    .m_axi_weights_rvalid(m_axi_weights_rvalid),
    .m_axi_weights_rready(m_axi_weights_rready)
);
`else
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
) OC_TOP (
    .*
);
`endif

endmodule
