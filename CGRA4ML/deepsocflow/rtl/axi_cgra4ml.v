/* 
    Top module for simulation
    Components
    RTL on-chip Top
        - DNN engine
        - Controller
            -BRAM and register
        - controller register port, driven by the LLPP slave in axi_cgra4ml_2m.v
        - 3 Alex DMAs
    Top module for simulation also includes 3 zip cpus converting AXI4 requests to ram rw requests
    Unused ports: connect 0 for input, do not connect for output
*/
/*
    TODO:
        - Merge the params
        - Addr convertion PS (byte-addressed) to PL (word-addressed)
*/
`timescale 1ns/1ps
`define VERILOG
`include "defines.svh"
`undef  VERILOG

module axi_cgra4ml #(
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
                AXI_WIDTH               = `AXI_WIDTH   ,
                AXI_ID_WIDTH            = 6,
                AXI_STRB_WIDTH          = (AXI_WIDTH/8),
                AXI_MAX_BURST_LEN       = `AXI_MAX_BURST_LEN,
                AXI_ADDR_WIDTH          = 32,
                // Controller registers
                AXIL_WIDTH              = 32,
                AXIL_ADDR_WIDTH              = 40,
                STRB_WIDTH              = 4,
                W_BPT                   = `W_BPT              

) (
    input  wire                   clk,
    input  wire                   rstn,

    // Controller registers, from the LLPP slave in axi_cgra4ml_2m.v
    input  wire                       reg_ext_wr_en,
    input  wire [AXIL_ADDR_WIDTH-1:0] reg_ext_wr_addr,
    input  wire [AXIL_WIDTH-1:0]      reg_ext_wr_data,
    input  wire                       reg_ext_rd_en,
    input  wire [AXIL_ADDR_WIDTH-1:0] reg_ext_rd_addr,
    output wire [AXIL_WIDTH-1:0]      reg_ext_rd_data,
    /*
        * AXI 4 Master interface
    */
    // Pixel
    output wire [AXI_ID_WIDTH-1:0]    m_axi_pixel_arid,
    output wire [AXI_ADDR_WIDTH-1:0]  m_axi_pixel_araddr,
    output wire [7:0]                 m_axi_pixel_arlen,
    output wire [2:0]                 m_axi_pixel_arsize,
    output wire [1:0]                 m_axi_pixel_arburst,
    output wire                       m_axi_pixel_arlock,
    output wire [3:0]                 m_axi_pixel_arcache,
    output wire [2:0]                 m_axi_pixel_arprot,
    output wire                       m_axi_pixel_arvalid,
    input  wire                       m_axi_pixel_arready,
    input  wire [AXI_ID_WIDTH-1:0]    m_axi_pixel_rid,
    input  wire [AXI_WIDTH   -1:0]    m_axi_pixel_rdata,
    input  wire [1:0]                 m_axi_pixel_rresp,
    input  wire                       m_axi_pixel_rlast,
    input  wire                       m_axi_pixel_rvalid,
    output wire                       m_axi_pixel_rready,
    // Weights
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
    input  wire [AXI_WIDTH   -1:0]  m_axi_weights_rdata,
    input  wire [1:0]                 m_axi_weights_rresp,
    input  wire                       m_axi_weights_rlast,
    input  wire                       m_axi_weights_rvalid,
    output wire                       m_axi_weights_rready,
    // Output
    output wire [AXI_ID_WIDTH-1:0]    m_axi_output_awid,
    output wire [AXI_ADDR_WIDTH-1:0]  m_axi_output_awaddr,
    output wire [7:0]                 m_axi_output_awlen,
    output wire [2:0]                 m_axi_output_awsize,
    output wire [1:0]                 m_axi_output_awburst,
    output wire                       m_axi_output_awlock,
    output wire [3:0]                 m_axi_output_awcache,
    output wire [2:0]                 m_axi_output_awprot,
    output wire                       m_axi_output_awvalid,
    input  wire                       m_axi_output_awready,
    (* mark_debug = "true" *) output wire [AXI_WIDTH   -1:0]  m_axi_output_wdata,
    (* mark_debug = "true" *) output wire [AXI_STRB_WIDTH-1:0]  m_axi_output_wstrb,
    (* mark_debug = "true" *) output wire                       m_axi_output_wlast,
    (* mark_debug = "true" *) output wire                       m_axi_output_wvalid,
    (* mark_debug = "true" *) input  wire                       m_axi_output_wready,
    input  wire [AXI_ID_WIDTH-1:0]    m_axi_output_bid,
    input  wire [1:0]                 m_axi_output_bresp,
    input  wire                       m_axi_output_bvalid,
    output wire                       m_axi_output_bready

);

localparam      OUT_ADDR_WIDTH          = 10,
                OUT_BITS                = 32,
    // Parameters for controller
                SRAM_RD_DATA_WIDTH      = 256,
                SRAM_RD_DEPTH           = `MAX_N_BUNDLES,
                COUNTER_WIDTH           = 16,
                AXI_LEN_WIDTH           = 32,

    // Alex AXI DMA RD                
                AXIS_ID_WIDTH           = 6,
                AXIS_KEEP_ENABLE        = 1,//(AXI_WIDTH>8),
                AXIS_KEEP_WIDTH         = (AXI_WIDTH/8),//(AXI_WIDTH/8),
                AXIS_LAST_ENABLE        = 1,
                AXIS_ID_ENABLE          = 0,
                AXIS_DEST_ENABLE        = 0,
                AXIS_DEST_WIDTH         = 8,
                HEADER_WIDTH            = `HEADER_WIDTH,
                AXIS_USER_WIDTH         = HEADER_WIDTH+1,
                LEN_WIDTH               = 32,
                TAG_WIDTH               = 8,
                ENABLE_SG               = 0,
                ENABLE_UNALIGNED        = 1;
    

// Controller register interface (word addresses from the LLPP slave)
wire [AXIL_WIDTH-1:0] reg_wr_data;
wire reg_wr_en;
wire reg_wr_ack;
wire reg_rd_en;
wire [AXIL_WIDTH-1:0] reg_rd_data;
wire reg_rd_ack;

// Controller with Alex DMAs: desc signals (including od tag) and status signals
wire [AXI_ADDR_WIDTH+AXI_LEN_WIDTH-1:0] m_od_axis_write_desc_tdata;
wire m_od_axis_write_desc_tvalid;
wire m_od_axis_write_desc_tready;
wire [TAG_WIDTH-1:0] m_od_axis_write_desc_tag;
wire [TAG_WIDTH-1:0] m_os_axis_write_desc_status_tag;
wire [3:0] m_os_axis_write_desc_status_error;
wire m_os_axis_write_desc_status_valid;


wire [AXI_ADDR_WIDTH+AXI_LEN_WIDTH-1:0] m_xd_axis_write_desc_tdata;
wire [AXIS_USER_WIDTH-1:0] m_xd_axis_write_desc_tuser;
wire m_xd_axis_write_desc_tvalid;
wire m_xd_axis_write_desc_tready;

wire [AXI_ADDR_WIDTH+AXI_LEN_WIDTH-1:0] m_wd_axis_write_desc_tdata;
wire [AXIS_USER_WIDTH-1:0] m_wd_axis_write_desc_tuser;
wire m_wd_axis_write_desc_tvalid;
wire m_wd_axis_write_desc_tready;

// AXIS input & outputs to DNN engine
// *_skid: DMA side of the stream buffers (see CGRA4ML_AXIS_SKID below)
wire                       s_axis_pixels_tready, s_axis_pixels_skid_tready ;
wire                       s_axis_pixels_tvalid, s_axis_pixels_skid_tvalid ;
wire                       s_axis_pixels_tlast , s_axis_pixels_skid_tlast  ;
wire [AXI_WIDTH  -1:0]     s_axis_pixels_tdata , s_axis_pixels_skid_tdata  ;
wire [AXI_WIDTH/8-1:0]     s_axis_pixels_tkeep , s_axis_pixels_skid_tkeep  ;
wire [AXIS_USER_WIDTH-1:0] s_axis_pixels_tuser , s_axis_pixels_skid_tuser  ;

wire                       s_axis_weights_tready, s_axis_weights_skid_tready;
wire                       s_axis_weights_tvalid, s_axis_weights_skid_tvalid;
wire                       s_axis_weights_tlast , s_axis_weights_skid_tlast ;
wire [AXI_WIDTH  -1:0]     s_axis_weights_tdata , s_axis_weights_skid_tdata ;
wire [AXI_WIDTH/8-1:0]     s_axis_weights_tkeep , s_axis_weights_skid_tkeep ;
wire [AXIS_USER_WIDTH-1:0] s_axis_weights_tuser , s_axis_weights_skid_tuser ;

    // AND, controller monitors the axis output status
wire                    m_axis_output_tready, m_axis_output_skid_tready;
wire                    m_axis_output_tvalid, m_axis_output_skid_tvalid;
wire                    m_axis_output_tlast , m_axis_output_skid_tlast ;
wire [AXI_WIDTH   -1:0] m_axis_output_tdata , m_axis_output_skid_tdata ;
wire [AXI_WIDTH/8 -1:0] m_axis_output_tkeep , m_axis_output_skid_tkeep ;
wire [W_BPT-1:0]        m_bytes_per_transfer, m_bytes_per_transfer_skid;

// Read DMA address handshakes, before the optional masking
wire m_axi_pixel_dma_arvalid,   m_axi_pixel_dma_arready;
wire m_axi_weights_dma_arvalid, m_axi_weights_dma_arready;


wire [AXIL_ADDR_WIDTH-1:0] reg_wr_addr_ctrl = reg_ext_wr_addr;
wire [AXIL_ADDR_WIDTH-1:0] reg_rd_addr_ctrl = reg_ext_rd_addr;
assign reg_wr_en   = reg_ext_wr_en;
assign reg_wr_data = reg_ext_wr_data;
assign reg_rd_en   = reg_ext_rd_en;

assign reg_ext_rd_data = reg_rd_data;

dma_controller #(
    .SRAM_RD_DATA_WIDTH(SRAM_RD_DATA_WIDTH),
    .SRAM_RD_DEPTH(SRAM_RD_DEPTH),
    .COUNTER_WIDTH(COUNTER_WIDTH),
    .AXI_ADDR_WIDTH(AXI_ADDR_WIDTH),
    .AXIS_USER_WIDTH(AXIS_USER_WIDTH),
    .AXI_DATA_WIDTH(AXIL_WIDTH),
    .AXI_LEN_WIDTH(AXI_LEN_WIDTH),
    .AXI_TAG_WIDTH(TAG_WIDTH)
) CONTROLLER (
    .clk(clk),
    .rstn(rstn),
    .reg_wr_en(reg_wr_en),
    .reg_wr_ack(reg_wr_ack),
    .reg_wr_addr(reg_wr_addr_ctrl[AXI_ADDR_WIDTH-1:0]),
    .reg_wr_data(reg_wr_data),
    .reg_rd_en(reg_rd_en),
    .reg_rd_ack(reg_rd_ack),
    .reg_rd_addr(reg_rd_addr_ctrl[AXI_ADDR_WIDTH-1:0]),
    .reg_rd_data(reg_rd_data),
    .o_ready(m_axis_output_skid_tready),
    .o_valid(m_axis_output_skid_tvalid),
    .o_last(m_axis_output_skid_tlast),
    .o_bpt(m_bytes_per_transfer_skid),
    .os_tag(m_os_axis_write_desc_status_tag),
    .os_error(m_os_axis_write_desc_status_error),
    .os_valid(m_os_axis_write_desc_status_valid),
    .m_od_addr(m_od_axis_write_desc_tdata[AXI_ADDR_WIDTH-1:0]),
    .m_od_len(m_od_axis_write_desc_tdata[AXI_ADDR_WIDTH+AXI_LEN_WIDTH-1:AXI_ADDR_WIDTH]),
    .m_od_valid(m_od_axis_write_desc_tvalid),
    .m_od_ready(m_od_axis_write_desc_tready),
    .m_od_tag(m_od_axis_write_desc_tag),
    .m_xd_addr(m_xd_axis_write_desc_tdata[AXI_ADDR_WIDTH-1:0]),
    .m_xd_user(m_xd_axis_write_desc_tuser),
    .m_xd_len(m_xd_axis_write_desc_tdata[AXI_ADDR_WIDTH+AXI_LEN_WIDTH-1:AXI_ADDR_WIDTH]),
    .m_xd_valid(m_xd_axis_write_desc_tvalid),
    .m_xd_ready(m_xd_axis_write_desc_tready),
    .m_wd_addr(m_wd_axis_write_desc_tdata[AXI_ADDR_WIDTH-1:0]),
    .m_wd_user(m_wd_axis_write_desc_tuser),
    .m_wd_len((m_wd_axis_write_desc_tdata[AXI_ADDR_WIDTH+AXI_LEN_WIDTH-1:AXI_ADDR_WIDTH])),
    .m_wd_valid(m_wd_axis_write_desc_tvalid),
    .m_wd_ready(m_wd_axis_write_desc_tready)
);

dnn_engine #(
    .ROWS(ROWS),
    .COLS(COLS),
    .X_BITS(X_BITS),
    .K_BITS(K_BITS),
    .Y_BITS(Y_BITS),
    .Y_OUT_BITS(Y_OUT_BITS),
    .HEADER_WIDTH(HEADER_WIDTH),
    .M_DATA_WIDTH_HF_CONV(M_DATA_WIDTH_HF_CONV),
    .M_DATA_WIDTH_HF_CONV_DW(M_DATA_WIDTH_HF_CONV_DW),
    .AXI_WIDTH(AXI_WIDTH),
    .W_BPT(W_BPT),
    .OUT_ADDR_WIDTH(OUT_ADDR_WIDTH),
    .OUT_BITS(OUT_BITS)
) ENGINE ( 
    .aclk(clk),
    .aresetn(rstn),
    .s_axis_pixels_tready(s_axis_pixels_tready),
    .s_axis_pixels_tvalid(s_axis_pixels_tvalid),
    .s_axis_pixels_tlast(s_axis_pixels_tlast),
    .s_axis_pixels_tdata(s_axis_pixels_tdata),
    .s_axis_pixels_tuser(s_axis_pixels_tuser),
    .s_axis_pixels_tkeep(s_axis_pixels_tkeep),
    .s_axis_weights_tready(s_axis_weights_tready),
    .s_axis_weights_tvalid(s_axis_weights_tvalid),
    .s_axis_weights_tlast(s_axis_weights_tlast),
    .s_axis_weights_tdata(s_axis_weights_tdata),
    .s_axis_weights_tuser(s_axis_weights_tuser),
    .s_axis_weights_tkeep(s_axis_weights_tkeep),
    .m_axis_tready(m_axis_output_tready),
    .m_axis_tvalid(m_axis_output_tvalid),
    .m_axis_tlast(m_axis_output_tlast),
    .m_axis_tdata(m_axis_output_tdata),
    .m_axis_tkeep(m_axis_output_tkeep),
    .m_bytes_per_transfer(m_bytes_per_transfer)
);

// Upstream CGRA4ML (dfa3eb5, c29123b): two-entry skid buffers between the
// DMAs and the engine, and read addresses issued only while the engine-side
// skid buffer can accept data, so read data does not wait on the AXI R channel.
skid_buffer #(
  .WIDTH(AXI_WIDTH + AXI_WIDTH/8 + AXIS_USER_WIDTH + 1)
  ) SKID_X (
  .clk     (clk ),
  .rstn    (rstn),
  .s_ready (s_axis_pixels_skid_tready ),
  .s_valid (s_axis_pixels_skid_tvalid ),
  .s_data  ({s_axis_pixels_skid_tdata, s_axis_pixels_skid_tuser, s_axis_pixels_skid_tkeep, s_axis_pixels_skid_tlast}),
  .m_ready (s_axis_pixels_tready      ),
  .m_valid (s_axis_pixels_tvalid      ),
  .m_data  ({s_axis_pixels_tdata,      s_axis_pixels_tuser,      s_axis_pixels_tkeep,      s_axis_pixels_tlast     })
);

skid_buffer #(
  .WIDTH(AXI_WIDTH + AXI_WIDTH/8 + AXIS_USER_WIDTH + 1)
  ) SKID_W (
  .clk     (clk ),
  .rstn    (rstn),
  .s_ready (s_axis_weights_skid_tready ),
  .s_valid (s_axis_weights_skid_tvalid ),
  .s_data  ({s_axis_weights_skid_tdata, s_axis_weights_skid_tuser, s_axis_weights_skid_tkeep, s_axis_weights_skid_tlast}),
  .m_ready (s_axis_weights_tready      ),
  .m_valid (s_axis_weights_tvalid      ),
  .m_data  ({s_axis_weights_tdata,      s_axis_weights_tuser,      s_axis_weights_tkeep,      s_axis_weights_tlast     })
);

skid_buffer #(
  .WIDTH(AXI_WIDTH + AXI_WIDTH/8 + W_BPT + 1)
  ) SKID_Y (
  .clk     (clk ),
  .rstn    (rstn),
  .s_ready (m_axis_output_tready),
  .s_valid (m_axis_output_tvalid),
  .s_data  ({m_axis_output_tdata,      m_axis_output_tkeep,      m_bytes_per_transfer,        m_axis_output_tlast     }),
  .m_ready (m_axis_output_skid_tready),
  .m_valid (m_axis_output_skid_tvalid),
  .m_data  ({m_axis_output_skid_tdata, m_axis_output_skid_tkeep, m_bytes_per_transfer_skid,   m_axis_output_skid_tlast})
);

// Read-data FIFOs with address reservation: R data from the SoC is always
// accepted, so neither read port can block the SoC's shared in-order
// read-return path.
wire [AXI_ID_WIDTH-1:0] m_axi_pixel_dma_rid,   m_axi_weights_dma_rid;
wire [AXI_WIDTH-1:0]    m_axi_pixel_dma_rdata, m_axi_weights_dma_rdata;
wire [1:0]              m_axi_pixel_dma_rresp, m_axi_weights_dma_rresp;
wire m_axi_pixel_dma_rlast,   m_axi_pixel_dma_rvalid,   m_axi_pixel_dma_rready;
wire m_axi_weights_dma_rlast, m_axi_weights_dma_rvalid, m_axi_weights_dma_rready;

ngultra_read_reserve #(
    .DATA_WIDTH(AXI_WIDTH),
    .ID_WIDTH(AXI_ID_WIDTH),
    .DEPTH_LOG2(5)
) PIXEL_READ_RESERVE (
    .clk(clk),
    .rstn(rstn),
    .dma_arvalid(m_axi_pixel_dma_arvalid),
    .dma_arready(m_axi_pixel_dma_arready),
    .arlen(m_axi_pixel_arlen),
    .soc_arvalid(m_axi_pixel_arvalid),
    .soc_arready(m_axi_pixel_arready),
    .soc_rid(m_axi_pixel_rid),
    .soc_rdata(m_axi_pixel_rdata),
    .soc_rresp(m_axi_pixel_rresp),
    .soc_rlast(m_axi_pixel_rlast),
    .soc_rvalid(m_axi_pixel_rvalid),
    .soc_rready(m_axi_pixel_rready),
    .dma_rid(m_axi_pixel_dma_rid),
    .dma_rdata(m_axi_pixel_dma_rdata),
    .dma_rresp(m_axi_pixel_dma_rresp),
    .dma_rlast(m_axi_pixel_dma_rlast),
    .dma_rvalid(m_axi_pixel_dma_rvalid),
    .dma_rready(m_axi_pixel_dma_rready)
);

ngultra_read_reserve #(
    .DATA_WIDTH(AXI_WIDTH),
    .ID_WIDTH(AXI_ID_WIDTH),
    .DEPTH_LOG2(5)
) WEIGHTS_READ_RESERVE (
    .clk(clk),
    .rstn(rstn),
    .dma_arvalid(m_axi_weights_dma_arvalid),
    .dma_arready(m_axi_weights_dma_arready),
    .arlen(m_axi_weights_arlen),
    .soc_arvalid(m_axi_weights_arvalid),
    .soc_arready(m_axi_weights_arready),
    .soc_rid(m_axi_weights_rid),
    .soc_rdata(m_axi_weights_rdata),
    .soc_rresp(m_axi_weights_rresp),
    .soc_rlast(m_axi_weights_rlast),
    .soc_rvalid(m_axi_weights_rvalid),
    .soc_rready(m_axi_weights_rready),
    .dma_rid(m_axi_weights_dma_rid),
    .dma_rdata(m_axi_weights_dma_rdata),
    .dma_rresp(m_axi_weights_dma_rresp),
    .dma_rlast(m_axi_weights_dma_rlast),
    .dma_rvalid(m_axi_weights_dma_rvalid),
    .dma_rready(m_axi_weights_dma_rready)
);


alex_axi_dma_rd #(
    .AXI_DATA_WIDTH(AXI_WIDTH   ),
    .AXI_ADDR_WIDTH(AXI_ADDR_WIDTH),
    .AXI_STRB_WIDTH(AXI_STRB_WIDTH),
    .AXI_ID_WIDTH(AXI_ID_WIDTH),
    .AXI_MAX_BURST_LEN(AXI_MAX_BURST_LEN),
    .AXIS_DATA_WIDTH(AXI_WIDTH),
    .AXIS_KEEP_ENABLE(AXIS_KEEP_ENABLE),
    .AXIS_KEEP_WIDTH(AXIS_KEEP_WIDTH),
    .AXIS_LAST_ENABLE(AXIS_LAST_ENABLE),
    .AXIS_ID_ENABLE(AXIS_ID_ENABLE),
    .AXIS_ID_WIDTH(AXIS_ID_WIDTH),
    .AXIS_DEST_ENABLE(AXIS_DEST_ENABLE),
    .AXIS_DEST_WIDTH(AXIS_DEST_WIDTH),
    .AXIS_USER_ENABLE(1),
    .AXIS_USER_WIDTH(AXIS_USER_WIDTH),
    .LEN_WIDTH(LEN_WIDTH),
    .TAG_WIDTH(TAG_WIDTH),
    .ENABLE_SG(ENABLE_SG),
    .ENABLE_UNALIGNED(ENABLE_UNALIGNED)
) PIXEL_DMA (
    .clk(clk),
    .rstn(rstn),
    .s_axis_read_desc_tdata(m_xd_axis_write_desc_tdata),
    .s_axis_read_desc_tag({TAG_WIDTH{1'b0}}),
    .s_axis_read_desc_tid({AXI_ID_WIDTH{1'b0}}),
    .s_axis_read_desc_tdest({AXIS_DEST_WIDTH{1'b0}}),
    .s_axis_read_desc_tuser(m_xd_axis_write_desc_tuser),
    .s_axis_read_desc_tvalid(m_xd_axis_write_desc_tvalid),
    .s_axis_read_desc_tready(m_xd_axis_write_desc_tready),
    .m_axis_read_desc_status_tag(),
    .m_axis_read_desc_status_error(),
    .m_axis_read_desc_status_valid(),
    .m_axis_read_data_tdata(s_axis_pixels_skid_tdata),
    .m_axis_read_data_tkeep(s_axis_pixels_skid_tkeep),
    .m_axis_read_data_tvalid(s_axis_pixels_skid_tvalid),
    .m_axis_read_data_tready(s_axis_pixels_skid_tready),
    .m_axis_read_data_tlast(s_axis_pixels_skid_tlast),
    .m_axis_read_data_tid(),
    .m_axis_read_data_tdest(),
    .m_axis_read_data_tuser(s_axis_pixels_skid_tuser),
    .m_axi_arid(m_axi_pixel_arid),
    .m_axi_araddr(m_axi_pixel_araddr),
    .m_axi_arlen(m_axi_pixel_arlen),
    .m_axi_arsize(m_axi_pixel_arsize),
    .m_axi_arburst(m_axi_pixel_arburst),
    .m_axi_arlock(m_axi_pixel_arlock),
    .m_axi_arcache(m_axi_pixel_arcache),
    .m_axi_arprot(m_axi_pixel_arprot),
    .m_axi_arvalid(m_axi_pixel_dma_arvalid),
    .m_axi_arready(m_axi_pixel_dma_arready),
    .m_axi_rid(m_axi_pixel_dma_rid),
    .m_axi_rdata(m_axi_pixel_dma_rdata),
    .m_axi_rresp(m_axi_pixel_dma_rresp),
    .m_axi_rlast(m_axi_pixel_dma_rlast),
    .m_axi_rvalid(m_axi_pixel_dma_rvalid),
    .m_axi_rready(m_axi_pixel_dma_rready),
    .enable(1'b1)
);

alex_axi_dma_rd #(
    .AXI_DATA_WIDTH(AXI_WIDTH   ),
    .AXI_ADDR_WIDTH(AXI_ADDR_WIDTH),
    .AXI_STRB_WIDTH(AXI_STRB_WIDTH),
    .AXI_ID_WIDTH(AXI_ID_WIDTH),
    .AXI_MAX_BURST_LEN(AXI_MAX_BURST_LEN),
    .AXIS_DATA_WIDTH(AXI_WIDTH),
    .AXIS_KEEP_ENABLE(AXIS_KEEP_ENABLE),
    .AXIS_KEEP_WIDTH(AXIS_KEEP_WIDTH),
    .AXIS_LAST_ENABLE(AXIS_LAST_ENABLE),
    .AXIS_ID_ENABLE(AXIS_ID_ENABLE),
    .AXIS_ID_WIDTH(AXIS_ID_WIDTH),
    .AXIS_DEST_ENABLE(AXIS_DEST_ENABLE),
    .AXIS_DEST_WIDTH(AXIS_DEST_WIDTH),
    .AXIS_USER_ENABLE(1),
    .AXIS_USER_WIDTH(AXIS_USER_WIDTH),
    .LEN_WIDTH(LEN_WIDTH),
    .TAG_WIDTH(TAG_WIDTH),
    .ENABLE_SG(ENABLE_SG),
    .ENABLE_UNALIGNED(ENABLE_UNALIGNED)
) WEIGHTS_DMA (
    .clk(clk),
    .rstn(rstn),
    .s_axis_read_desc_tdata(m_wd_axis_write_desc_tdata),
    .s_axis_read_desc_tag({TAG_WIDTH{1'b0}}),
    .s_axis_read_desc_tid({AXI_ID_WIDTH{1'b0}}),
    .s_axis_read_desc_tdest({AXIS_DEST_WIDTH{1'b0}}),
    .s_axis_read_desc_tuser(m_wd_axis_write_desc_tuser),
    .s_axis_read_desc_tvalid(m_wd_axis_write_desc_tvalid),
    .s_axis_read_desc_tready(m_wd_axis_write_desc_tready),
    .m_axis_read_desc_status_tag(),
    .m_axis_read_desc_status_error(),
    .m_axis_read_desc_status_valid(),
    .m_axis_read_data_tdata(s_axis_weights_skid_tdata),
    .m_axis_read_data_tkeep(s_axis_weights_skid_tkeep),
    .m_axis_read_data_tvalid(s_axis_weights_skid_tvalid),
    .m_axis_read_data_tready(s_axis_weights_skid_tready),
    .m_axis_read_data_tlast(s_axis_weights_skid_tlast),
    .m_axis_read_data_tid(),
    .m_axis_read_data_tdest(),
    .m_axis_read_data_tuser(s_axis_weights_skid_tuser),
    .m_axi_arid(m_axi_weights_arid),
    .m_axi_araddr(m_axi_weights_araddr),
    .m_axi_arlen(m_axi_weights_arlen),
    .m_axi_arsize(m_axi_weights_arsize),
    .m_axi_arburst(m_axi_weights_arburst),
    .m_axi_arlock(m_axi_weights_arlock),
    .m_axi_arcache(m_axi_weights_arcache),
    .m_axi_arprot(m_axi_weights_arprot),
    .m_axi_arvalid(m_axi_weights_dma_arvalid),
    .m_axi_arready(m_axi_weights_dma_arready),
    .m_axi_rid(m_axi_weights_dma_rid),
    .m_axi_rdata(m_axi_weights_dma_rdata),
    .m_axi_rresp(m_axi_weights_dma_rresp),
    .m_axi_rlast(m_axi_weights_dma_rlast),
    .m_axi_rvalid(m_axi_weights_dma_rvalid),
    .m_axi_rready(m_axi_weights_dma_rready),
    .enable(1'b1)
);

alex_axi_dma_wr #(
    .AXI_DATA_WIDTH(AXI_WIDTH   ),
    .AXI_ADDR_WIDTH(AXI_ADDR_WIDTH),
    .AXI_STRB_WIDTH(AXI_STRB_WIDTH),
    .AXI_ID_WIDTH(AXI_ID_WIDTH),
    .AXI_MAX_BURST_LEN(AXI_MAX_BURST_LEN),
    .AXIS_DATA_WIDTH(AXI_WIDTH),
    .AXIS_KEEP_ENABLE(AXIS_KEEP_ENABLE),
    .AXIS_KEEP_WIDTH(AXIS_KEEP_WIDTH),
    .AXIS_LAST_ENABLE(AXIS_LAST_ENABLE),
    .AXIS_ID_ENABLE(AXIS_ID_ENABLE),
    .AXIS_ID_WIDTH(AXIS_ID_WIDTH),
    .AXIS_DEST_ENABLE(AXIS_DEST_ENABLE),
    .AXIS_DEST_WIDTH(AXIS_DEST_WIDTH),
    .AXIS_USER_ENABLE(0),
    .AXIS_USER_WIDTH(AXIS_USER_WIDTH),
    .LEN_WIDTH(LEN_WIDTH),
    .TAG_WIDTH(TAG_WIDTH),
    .ENABLE_SG(ENABLE_SG),
    .ENABLE_UNALIGNED(0) // the output banks are beat-aligned (the runtime checks)
) OUT_DMA (
    .clk(clk),
    .rstn(rstn),
    .s_axis_write_desc_tdata(m_od_axis_write_desc_tdata),
    .s_axis_write_desc_tag(m_od_axis_write_desc_tag),
    .s_axis_write_desc_tvalid(m_od_axis_write_desc_tvalid),
    .s_axis_write_desc_tready(m_od_axis_write_desc_tready),
    .m_axis_write_desc_status_len(),
    .m_axis_write_desc_status_tag(m_os_axis_write_desc_status_tag),
    .m_axis_write_desc_status_id(),
    .m_axis_write_desc_status_dest(),
    .m_axis_write_desc_status_user(),
    .m_axis_write_desc_status_error(m_os_axis_write_desc_status_error),
    .m_axis_write_desc_status_valid(m_os_axis_write_desc_status_valid),
    .s_axis_write_data_tdata(m_axis_output_skid_tdata),
    .s_axis_write_data_tkeep(m_axis_output_skid_tkeep),
    .s_axis_write_data_tvalid(m_axis_output_skid_tvalid),
    .s_axis_write_data_tready(m_axis_output_skid_tready),
    .s_axis_write_data_tlast(m_axis_output_skid_tlast),
    .s_axis_write_data_tid({AXIS_ID_WIDTH{1'b0}}),
    .s_axis_write_data_tdest({AXIS_DEST_WIDTH{1'b0}}),
    .s_axis_write_data_tuser({AXIS_USER_WIDTH{1'b0}}),
    .m_axi_awid(m_axi_output_awid),
    .m_axi_awaddr(m_axi_output_awaddr),
    .m_axi_awlen(m_axi_output_awlen),
    .m_axi_awsize(m_axi_output_awsize),
    .m_axi_awburst(m_axi_output_awburst),
    .m_axi_awlock(m_axi_output_awlock),
    .m_axi_awcache(m_axi_output_awcache),
    .m_axi_awprot(m_axi_output_awprot),
    .m_axi_awvalid(m_axi_output_awvalid),
    .m_axi_awready(m_axi_output_awready),
    .m_axi_wdata(m_axi_output_wdata),
    .m_axi_wstrb(m_axi_output_wstrb),
    .m_axi_wlast(m_axi_output_wlast),
    .m_axi_wvalid(m_axi_output_wvalid),
    .m_axi_wready(m_axi_output_wready),
    .m_axi_bid(m_axi_output_bid),
    .m_axi_bresp(m_axi_output_bresp),
    .m_axi_bvalid(m_axi_output_bvalid),
    .m_axi_bready(m_axi_output_bready),
    .enable(1'b1),
    .abort(1'b0)
);

endmodule
