// NG-ULTRA top level:
// fabric PLL, SoC interface, reset release, the SoC's FPGA AXI masters m1/m2
// for the two-port core, and LLPP0 straight to the core's register slave
// (ngultra_llpp_regs.v).
// Hard cells: NX_PLL_U, NX_SOC_INTERFACE_WRAP (vendor library; instantiated
// from Verilog as in the training package's Design/DelayIo/src/dly.v).
// create_nx_cgra4ml_project.py patches the localparams and PLL parameters
// in the staged copy; never hand-edit a staged copy.
`timescale 1ns/1ps
module ngultra_cgra4ml_top;
  localparam integer C_ROWS                     = 4;
  localparam integer C_COLS                     = 8;
  localparam integer C_X_BITS                   = 4;
  localparam integer C_K_BITS                   = 4;
  localparam integer C_Y_BITS                   = 16;
  localparam integer C_Y_OUT_BITS               = 16;
  localparam integer C_M_DATA_WIDTH_HF_CONV     = C_COLS * C_ROWS * C_Y_BITS;
  localparam integer C_M_DATA_WIDTH_HF_CONV_DW  = C_ROWS * C_Y_BITS;
  localparam integer C_AXI_WIDTH                = 128;
  localparam integer C_AXI_STRB_WIDTH           = 16;
  localparam integer C_AXI_ID_WIDTH             = 5;
  localparam integer C_AXI_MAX_BURST_LEN        = 16;
  localparam integer C_AXI_ADDR_WIDTH           = 32;
  localparam integer C_AXIL_WIDTH               = 32;
  localparam integer C_AXIL_ADDR_WIDTH          = 40;
  localparam integer C_AXIL_STRB_WIDTH          = 4;
  localparam integer C_W_BPT                    = 32;

  wire [1:0] fabric_lowskew;
  wire clk_50_0mhz;
  wire pll_locked;
  wire fabric_clk;
  wire fabric_rstn;
  wire core_rstn;
  reg  [7:0] reset_shift;
  wire [16:0] clk_nic_fabric;
  wire [9:0] rstn_nic_fabric;
  wire [31:0] llpp0_awaddr;
  wire [2:0] llpp0_awprot;
  wire [11:0] llpp0_awid;
  wire llpp0_awvalid;
  wire llpp0_awready;
  wire [31:0] llpp0_wdata;
  wire [3:0] llpp0_wstrb;
  wire llpp0_wvalid;
  wire llpp0_wready;
  wire [11:0] llpp0_bid;
  wire [1:0] llpp0_bresp;
  wire llpp0_bvalid;
  wire llpp0_bready;
  wire [31:0] llpp0_araddr;
  wire [2:0] llpp0_arprot;
  wire [11:0] llpp0_arid;
  wire llpp0_arvalid;
  wire llpp0_arready;
  wire [31:0] llpp0_rdata;
  wire [11:0] llpp0_rid;
  wire llpp0_rlast;
  wire [1:0] llpp0_rresp;
  wire llpp0_rvalid;
  wire llpp0_rready;
  wire [7:0] llpp0_awlen;
  wire [7:0] llpp0_arlen;
  wire llpp0_wlast;
  wire [C_AXI_ID_WIDTH-1:0] shared_arid;
  wire [C_AXI_ADDR_WIDTH-1:0] shared_araddr;
  wire [7:0] shared_arlen;
  wire [2:0] shared_arsize;
  wire [1:0] shared_arburst;
  wire shared_arlock;
  wire [3:0] shared_arcache;
  wire [2:0] shared_arprot;
  wire shared_arvalid;
  wire shared_arready;
  wire [C_AXI_ID_WIDTH-1:0] shared_rid;
  wire [C_AXI_WIDTH-1:0] shared_rdata;
  wire [1:0] shared_rresp;
  wire shared_rlast;
  wire shared_rvalid;
  wire shared_rready;
  wire [C_AXI_ID_WIDTH-1:0] shared_awid;
  wire [C_AXI_ADDR_WIDTH-1:0] shared_awaddr;
  wire [7:0] shared_awlen;
  wire [2:0] shared_awsize;
  wire [1:0] shared_awburst;
  wire shared_awlock;
  wire [3:0] shared_awcache;
  wire [2:0] shared_awprot;
  wire shared_awvalid;
  wire shared_awready;
  wire [C_AXI_WIDTH-1:0] shared_wdata;
  wire [C_AXI_STRB_WIDTH-1:0] shared_wstrb;
  wire shared_wlast;
  wire shared_wvalid;
  wire shared_wready;
  wire [C_AXI_ID_WIDTH-1:0] shared_bid;
  wire [1:0] shared_bresp;
  wire shared_bvalid;
  wire shared_bready;
  wire [C_AXI_ID_WIDTH-1:0] weights_arid;
  wire [C_AXI_ADDR_WIDTH-1:0] weights_araddr;
  wire [7:0] weights_arlen;
  wire [2:0] weights_arsize;
  wire [1:0] weights_arburst;
  wire weights_arlock;
  wire [3:0] weights_arcache;
  wire [2:0] weights_arprot;
  wire weights_arvalid;
  wire weights_arready;
  wire [C_AXI_ID_WIDTH-1:0] weights_rid;
  wire [C_AXI_WIDTH-1:0] weights_rdata;
  wire [1:0] weights_rresp;
  wire weights_rlast;
  wire weights_rvalid;
  wire weights_rready;
  wire m1_arready;
  wire m1_awready;
  wire [C_AXI_ID_WIDTH-1:0] m1_bid;
  wire [1:0] m1_bresp;
  wire m1_bvalid;
  wire [C_AXI_WIDTH-1:0] m1_rdata;
  wire [C_AXI_ID_WIDTH-1:0] m1_rid;
  wire m1_rlast;
  wire [1:0] m1_rresp;
  wire m1_rvalid;
  wire m1_wready;
  wire m2_arready;
  wire m2_awready;
  wire [C_AXI_ID_WIDTH-1:0] m2_bid;
  wire [1:0] m2_bresp;
  wire m2_bvalid;
  wire [C_AXI_WIDTH-1:0] m2_rdata;
  wire [C_AXI_ID_WIDTH-1:0] m2_rid;
  wire m2_rlast;
  wire [1:0] m2_rresp;
  wire m2_rvalid;
  wire m2_wready;

  assign fabric_clk      = clk_50_0mhz;
  assign fabric_rstn     = reset_shift[7];
  assign core_rstn       = fabric_rstn;
  assign clk_nic_fabric  = {17{fabric_clk}};
  assign rstn_nic_fabric = {10{core_rstn}};

  // Release reset after PLL lock is stable (as the VHDL shell).
  always @(posedge clk_50_0mhz or negedge pll_locked)
    if (!pll_locked) reset_shift <= 8'd0;
    else             reset_shift <= {reset_shift[6:0], 1'b1};

  NX_PLL_U
    #(
      .location     (""),
      .ref_osc_on   (1'b1),
      .use_pll      (1'b1),
      .ext_fbk_on   (1'b0),
      .fbk_delay_on (1'b0),
      .fbk_delay    (6'd0),
      .ref_intdiv   (5'd15),
      .fbk_intdiv   (7'd14),
      .clk_outdiv1  (3'd6),
      .clk_outdiv2  (3'd0),
      .clk_outdiv3  (3'd0),
      .clk_outdiv4  (3'd0),
      .clk_outdivd1 (4'd0),
      .clk_outdivd2 (4'd0),
      .clk_outdivd3 (4'd0),
      .clk_outdivd4 (4'd0),
      .clk_outdivd5 (4'd0)
    )
    i_fabric_pll (
      .REF        (1'b0),
      .FBK        (1'b0),
      .R          (1'b0),
      .VCO        (),
      .LDFO       (),
      .REFO       (),
      .OSC        (),
      .CAL_LOCKED (),
      .PLL_LOCKED (pll_locked),
      .CLK_DIV1   (clk_50_0mhz),
      .CLK_DIV2   (),
      .CLK_DIV3   (),
      .CLK_DIV4   (),
      .CLK_DIVD1  (),
      .CLK_DIVD2  (),
      .CLK_DIVD3  (),
      .CLK_DIVD4  (),
      .CLK_DIVD5  (),
      // Calibration inputs: the VHDL library defaults ('0'), which the VHDL
      // shell relied on; Verilog has no port defaults.
      .ARST_CAL       (1'b0),
      .CLK_CAL        (1'b0),
      .EXT_CAL_LOCKED (1'b0),
      .EXT_CAL1       (1'b0),
      .EXT_CAL2       (1'b0),
      .EXT_CAL3       (1'b0),
      .EXT_CAL4       (1'b0),
      .EXT_CAL5       (1'b0)
    );

  assign shared_arready  = m1_arready;
  assign shared_awready  = m1_awready;
  assign shared_bid      = m1_bid;
  assign shared_bresp    = m1_bresp;
  assign shared_bvalid   = m1_bvalid;
  assign shared_rdata    = m1_rdata;
  assign shared_rid      = m1_rid;
  assign shared_rlast    = m1_rlast;
  assign shared_rresp    = m1_rresp;
  assign shared_rvalid   = m1_rvalid;
  assign shared_wready   = m1_wready;
  assign weights_arready = m2_arready;
  assign weights_rdata   = m2_rdata;
  assign weights_rid     = m2_rid;
  assign weights_rlast   = m2_rlast;
  assign weights_rresp   = m2_rresp;
  assign weights_rvalid  = m2_rvalid;

  NX_SOC_INTERFACE_WRAP
    i_soc (
      .fabric_lowskew_o                         (fabric_lowskew),
      .fabric_lowskew_i                         (clk_nic_fabric),
      .fabric_fpga_nic_rstn_i                   (rstn_nic_fabric),
      .fabric_fpga_pmrstn_i                     (1'b1),
      .fabric_fpga_sysrstn_i                    (1'b1),
      .fabric_fpga_trigger_in_o                 (),
      .fabric_fpga_trigger_out_i                (8'd0),
      .fabric_fpga_interrupt_in_i               (120'd0),
      .fabric_sysc_hold_on_debug_i              (1'b0),
      .fabric_fpga_events60_i                   (60'd0),
      .fabric_fpga_araddr_axi_s1_o              (),
      .fabric_fpga_arburst_axi_s1_o             (),
      .fabric_fpga_arcache_axi_s1_o             (),
      .fabric_fpga_arid_axi_s1_o                (),
      .fabric_fpga_arlen_axi_s1_o               (),
      .fabric_fpga_arlock_axi_s1_o              (),
      .fabric_fpga_arprot_axi_s1_o              (),
      .fabric_fpga_arqos_axi_s1_o               (),
      .fabric_fpga_arregion_axi_s1_o            (),
      .fabric_fpga_arsize_axi_s1_o              (),
      .fabric_fpga_arvalid_axi_s1_o             (),
      .fabric_fpga_awaddr_axi_s1_o              (),
      .fabric_fpga_awburst_axi_s1_o             (),
      .fabric_fpga_awcache_axi_s1_o             (),
      .fabric_fpga_awid_axi_s1_o                (),
      .fabric_fpga_awlen_axi_s1_o               (),
      .fabric_fpga_awlock_axi_s1_o              (),
      .fabric_fpga_awprot_axi_s1_o              (),
      .fabric_fpga_awqos_axi_s1_o               (),
      .fabric_fpga_awregion_axi_s1_o            (),
      .fabric_fpga_awsize_axi_s1_o              (),
      .fabric_fpga_bready_axi_s1_o              (),
      .fabric_fpga_rready_axi_s1_o              (),
      .fabric_fpga_wdata_axi_s1_o               (),
      .fabric_fpga_wlast_axi_s1_o               (),
      .fabric_fpga_wstrb_axi_s1_o               (),
      .fabric_fpga_wvalid_axi_s1_o              (),
      .fabric_fpga_awvalid_axi_s1_o             (),
      .fabric_fpga_arready_axi_s1_i             (1'b0),
      .fabric_fpga_awready_axi_s1_i             (1'b0),
      .fabric_fpga_bid_axi_s1_i                 (12'd0),
      .fabric_fpga_bresp_axi_s1_i               (2'd0),
      .fabric_fpga_bvalid_axi_s1_i              (1'b0),
      .fabric_fpga_rdata_axi_s1_i               (128'd0),
      .fabric_fpga_rid_axi_s1_i                 (12'd0),
      .fabric_fpga_rlast_axi_s1_i               (1'b0),
      .fabric_fpga_rresp_axi_s1_i               (2'd0),
      .fabric_fpga_rvalid_axi_s1_i              (1'b0),
      .fabric_fpga_wready_axi_s1_i              (1'b0),
      .fabric_fpga_araddr_axi_s2_o              (),
      .fabric_fpga_arburst_axi_s2_o             (),
      .fabric_fpga_arcache_axi_s2_o             (),
      .fabric_fpga_arid_axi_s2_o                (),
      .fabric_fpga_arlen_axi_s2_o               (),
      .fabric_fpga_arlock_axi_s2_o              (),
      .fabric_fpga_arprot_axi_s2_o              (),
      .fabric_fpga_arqos_axi_s2_o               (),
      .fabric_fpga_arregion_axi_s2_o            (),
      .fabric_fpga_arsize_axi_s2_o              (),
      .fabric_fpga_arvalid_axi_s2_o             (),
      .fabric_fpga_awaddr_axi_s2_o              (),
      .fabric_fpga_awburst_axi_s2_o             (),
      .fabric_fpga_awcache_axi_s2_o             (),
      .fabric_fpga_awid_axi_s2_o                (),
      .fabric_fpga_awlen_axi_s2_o               (),
      .fabric_fpga_awlock_axi_s2_o              (),
      .fabric_fpga_awprot_axi_s2_o              (),
      .fabric_fpga_awqos_axi_s2_o               (),
      .fabric_fpga_awregion_axi_s2_o            (),
      .fabric_fpga_awsize_axi_s2_o              (),
      .fabric_fpga_bready_axi_s2_o              (),
      .fabric_fpga_rready_axi_s2_o              (),
      .fabric_fpga_wdata_axi_s2_o               (),
      .fabric_fpga_wlast_axi_s2_o               (),
      .fabric_fpga_wstrb_axi_s2_o               (),
      .fabric_fpga_wvalid_axi_s2_o              (),
      .fabric_fpga_awvalid_axi_s2_o             (),
      .fabric_fpga_arready_axi_s2_i             (1'b0),
      .fabric_fpga_awready_axi_s2_i             (1'b0),
      .fabric_fpga_bid_axi_s2_i                 (12'd0),
      .fabric_fpga_bresp_axi_s2_i               (2'd0),
      .fabric_fpga_bvalid_axi_s2_i              (1'b0),
      .fabric_fpga_rdata_axi_s2_i               (128'd0),
      .fabric_fpga_rid_axi_s2_i                 (12'd0),
      .fabric_fpga_rlast_axi_s2_i               (1'b0),
      .fabric_fpga_rresp_axi_s2_i               (2'd0),
      .fabric_fpga_rvalid_axi_s2_i              (1'b0),
      .fabric_fpga_wready_axi_s2_i              (1'b0),
      .fabric_fpga_arready_axi_m1_o             (m1_arready),
      .fabric_fpga_awready_axi_m1_o             (m1_awready),
      .fabric_fpga_bid_axi_m1_o                 (m1_bid),
      .fabric_fpga_bresp_axi_m1_o               (m1_bresp),
      .fabric_fpga_bvalid_axi_m1_o              (m1_bvalid),
      .fabric_fpga_dma_ack_m1_o                 (),
      .fabric_fpga_dma_finish_m1_o              (),
      .fabric_fpga_rdata_axi_m1_o               (m1_rdata),
      .fabric_fpga_rid_axi_m1_o                 (m1_rid),
      .fabric_fpga_rlast_axi_m1_o               (m1_rlast),
      .fabric_fpga_rresp_axi_m1_o               (m1_rresp),
      .fabric_fpga_rvalid_axi_m1_o              (m1_rvalid),
      .fabric_fpga_wready_axi_m1_o              (m1_wready),
      .fabric_fpga_araddr_axi_m1_i              ({8'h00, shared_araddr}),
      .fabric_fpga_arburst_axi_m1_i             (shared_arburst),
      .fabric_fpga_arcache_axi_m1_i             (shared_arcache),
      .fabric_fpga_arid_axi_m1_i                (shared_arid),
      .fabric_fpga_arlen_axi_m1_i               (shared_arlen),
      .fabric_fpga_arlock_axi_m1_i              (shared_arlock),
      .fabric_fpga_arprot_axi_m1_i              (shared_arprot),
      .fabric_fpga_arqos_axi_m1_i               (4'd0),
      .fabric_fpga_arsize_axi_m1_i              (shared_arsize),
      .fabric_fpga_arvalid_axi_m1_i             (shared_arvalid),
      .fabric_fpga_awaddr_axi_m1_i              ({8'h00, shared_awaddr}),
      .fabric_fpga_awburst_axi_m1_i             (shared_awburst),
      .fabric_fpga_awcache_axi_m1_i             (shared_awcache),
      .fabric_fpga_awid_axi_m1_i                (shared_awid),
      .fabric_fpga_awlen_axi_m1_i               (shared_awlen),
      .fabric_fpga_awlock_axi_m1_i              (shared_awlock),
      .fabric_fpga_awprot_axi_m1_i              (shared_awprot),
      .fabric_fpga_awqos_axi_m1_i               (4'd0),
      .fabric_fpga_awsize_axi_m1_i              (shared_awsize),
      .fabric_fpga_awvalid_axi_m1_i             (shared_awvalid),
      .fabric_fpga_bready_axi_m1_i              (shared_bready),
      .fabric_fpga_dma_last_m1_i                (6'd0),
      .fabric_fpga_dma_req_m1_i                 (6'd0),
      .fabric_fpga_dma_single_m1_i              (6'd0),
      .fabric_fpga_rready_axi_m1_i              (shared_rready),
      .fabric_fpga_wdata_axi_m1_i               (shared_wdata),
      .fabric_fpga_wlast_axi_m1_i               (shared_wlast),
      .fabric_fpga_wstrb_axi_m1_i               (shared_wstrb),
      .fabric_fpga_wvalid_axi_m1_i              (shared_wvalid),
      .fabric_fpga_arready_axi_m2_o             (m2_arready),
      .fabric_fpga_awready_axi_m2_o             (m2_awready),
      .fabric_fpga_bid_axi_m2_o                 (m2_bid),
      .fabric_fpga_bresp_axi_m2_o               (m2_bresp),
      .fabric_fpga_bvalid_axi_m2_o              (m2_bvalid),
      .fabric_fpga_dma_ack_m2_o                 (),
      .fabric_fpga_dma_finish_m2_o              (),
      .fabric_fpga_rdata_axi_m2_o               (m2_rdata),
      .fabric_fpga_rid_axi_m2_o                 (m2_rid),
      .fabric_fpga_rlast_axi_m2_o               (m2_rlast),
      .fabric_fpga_rresp_axi_m2_o               (m2_rresp),
      .fabric_fpga_rvalid_axi_m2_o              (m2_rvalid),
      .fabric_fpga_wready_axi_m2_o              (m2_wready),
      .fabric_fpga_araddr_axi_m2_i              ({8'h00, weights_araddr}),
      .fabric_fpga_arburst_axi_m2_i             (weights_arburst),
      .fabric_fpga_arcache_axi_m2_i             (weights_arcache),
      .fabric_fpga_arid_axi_m2_i                (weights_arid),
      .fabric_fpga_arlen_axi_m2_i               (weights_arlen),
      .fabric_fpga_arlock_axi_m2_i              (weights_arlock),
      .fabric_fpga_arprot_axi_m2_i              (weights_arprot),
      .fabric_fpga_arqos_axi_m2_i               (4'd0),
      .fabric_fpga_arsize_axi_m2_i              (weights_arsize),
      .fabric_fpga_arvalid_axi_m2_i             (weights_arvalid),
      .fabric_fpga_awaddr_axi_m2_i              (40'd0),
      .fabric_fpga_awburst_axi_m2_i             (2'd0),
      .fabric_fpga_awcache_axi_m2_i             (4'd0),
      .fabric_fpga_awid_axi_m2_i                (5'd0),
      .fabric_fpga_awlen_axi_m2_i               (8'd0),
      .fabric_fpga_awlock_axi_m2_i              (1'b0),
      .fabric_fpga_awprot_axi_m2_i              (3'd0),
      .fabric_fpga_awqos_axi_m2_i               (4'd0),
      .fabric_fpga_awsize_axi_m2_i              (3'd0),
      .fabric_fpga_awvalid_axi_m2_i             (1'b0),
      .fabric_fpga_bready_axi_m2_i              (1'b0),
      .fabric_fpga_dma_last_m2_i                (6'd0),
      .fabric_fpga_dma_req_m2_i                 (6'd0),
      .fabric_fpga_dma_single_m2_i              (6'd0),
      .fabric_fpga_rready_axi_m2_i              (weights_rready),
      .fabric_fpga_wdata_axi_m2_i               (128'd0),
      .fabric_fpga_wlast_axi_m2_i               (1'b0),
      .fabric_fpga_wstrb_axi_m2_i               (16'd0),
      .fabric_fpga_wvalid_axi_m2_i              (1'b0),
      .fabric_fpga_ddr0_arready_o               (),
      .fabric_fpga_ddr0_awready_o               (),
      .fabric_fpga_ddr0_bid_o                   (),
      .fabric_fpga_ddr0_bresp_o                 (),
      .fabric_fpga_ddr0_bvalid_o                (),
      .fabric_fpga_ddr0_rdata_o                 (),
      .fabric_fpga_ddr0_rid_o                   (),
      .fabric_fpga_ddr0_rlast_o                 (),
      .fabric_fpga_ddr0_rresp_o                 (),
      .fabric_fpga_ddr0_rvalid_o                (),
      .fabric_fpga_ddr0_wready_o                (),
      .fabric_fpga_ddr0_araddr_i                (40'd0),
      .fabric_fpga_ddr0_arburst_i               (2'd0),
      .fabric_fpga_ddr0_arcache_i               (4'd0),
      .fabric_fpga_ddr0_arid_i                  (5'd0),
      .fabric_fpga_ddr0_arlen_i                 (8'd0),
      .fabric_fpga_ddr0_arlock_i                (1'b0),
      .fabric_fpga_ddr0_arprot_i                (3'd0),
      .fabric_fpga_ddr0_arqos_i                 (4'd0),
      .fabric_fpga_ddr0_arsize_i                (3'd0),
      .fabric_fpga_ddr0_arvalid_i               (1'b0),
      .fabric_fpga_ddr0_awaddr_i                (40'd0),
      .fabric_fpga_ddr0_awburst_i               (2'd0),
      .fabric_fpga_ddr0_awcache_i               (4'd0),
      .fabric_fpga_ddr0_awid_i                  (5'd0),
      .fabric_fpga_ddr0_awlen_i                 (8'd0),
      .fabric_fpga_ddr0_awlock_i                (1'b0),
      .fabric_fpga_ddr0_awprot_i                (3'd0),
      .fabric_fpga_ddr0_awqos_i                 (4'd0),
      .fabric_fpga_ddr0_awsize_i                (3'd0),
      .fabric_fpga_ddr0_awvalid_i               (1'b0),
      .fabric_fpga_ddr0_bready_i                (1'b0),
      .fabric_fpga_ddr0_rready_i                (1'b0),
      .fabric_fpga_ddr0_wdata_i                 (128'd0),
      .fabric_fpga_ddr0_wlast_i                 (1'b0),
      .fabric_fpga_ddr0_wstrb_i                 (16'd0),
      .fabric_fpga_ddr0_wvalid_i                (1'b0),
      .fabric_fpga_paddr_apb_o                  (),
      .fabric_fpga_penable_apb_o                (),
      .fabric_fpga_psel_apb_o                   (),
      .fabric_fpga_pwdata_apb_o                 (),
      .fabric_fpga_pwrite_apb_o                 (),
      .fabric_fpga_prdata_apb_i                 (32'd0),
      .fabric_fpga_pready_apb_i                 (1'b0),
      .fabric_fpga_pslverr_apb_i                (1'b0),
      .fabric_llpp0_araddr_s_o                  (llpp0_araddr),
      .fabric_llpp0_arburst_s_o                 (),
      .fabric_llpp0_arcache_s_o                 (),
      .fabric_llpp0_arid_s_o                    (llpp0_arid),
      .fabric_llpp0_arlen_s_o                   (llpp0_arlen),
      .fabric_llpp0_arlock_s_o                  (),
      .fabric_llpp0_arprot_s_o                  (llpp0_arprot),
      .fabric_llpp0_arqos_s_o                   (),
      .fabric_llpp0_arsize_s_o                  (),
      .fabric_llpp0_arvalid_s_o                 (llpp0_arvalid),
      .fabric_llpp0_awaddr_s_o                  (llpp0_awaddr),
      .fabric_llpp0_awburst_s_o                 (),
      .fabric_llpp0_awcache_s_o                 (),
      .fabric_llpp0_awid_s_o                    (llpp0_awid),
      .fabric_llpp0_awlen_s_o                   (llpp0_awlen),
      .fabric_llpp0_awlock_s_o                  (),
      .fabric_llpp0_awprot_s_o                  (llpp0_awprot),
      .fabric_llpp0_awqos_s_o                   (),
      .fabric_llpp0_awsize_s_o                  (),
      .fabric_llpp0_awvalid_s_o                 (llpp0_awvalid),
      .fabric_llpp0_bready_s_o                  (llpp0_bready),
      .fabric_llpp0_rready_s_o                  (llpp0_rready),
      .fabric_llpp0_wdata_s_o                   (llpp0_wdata),
      .fabric_llpp0_wlast_s_o                   (llpp0_wlast),
      .fabric_llpp0_wstrb_s_o                   (llpp0_wstrb),
      .fabric_llpp0_wvalid_s_o                  (llpp0_wvalid),
      .fabric_llpp0_arready_s_i                 (llpp0_arready),
      .fabric_llpp0_awready_s_i                 (llpp0_awready),
      .fabric_llpp0_bid_s_i                     (llpp0_bid),
      .fabric_llpp0_bresp_s_i                   (llpp0_bresp),
      .fabric_llpp0_bvalid_s_i                  (llpp0_bvalid),
      .fabric_llpp0_rdata_s_i                   (llpp0_rdata),
      .fabric_llpp0_rid_s_i                     (llpp0_rid),
      .fabric_llpp0_rlast_s_i                   (llpp0_rlast),
      .fabric_llpp0_rresp_s_i                   (llpp0_rresp),
      .fabric_llpp0_rvalid_s_i                  (llpp0_rvalid),
      .fabric_llpp0_wready_s_i                  (llpp0_wready),
      .fabric_llpp1_araddr_s_o                  (),
      .fabric_llpp1_arburst_s_o                 (),
      .fabric_llpp1_arcache_s_o                 (),
      .fabric_llpp1_arid_s_o                    (),
      .fabric_llpp1_arlen_s_o                   (),
      .fabric_llpp1_arlock_s_o                  (),
      .fabric_llpp1_arprot_s_o                  (),
      .fabric_llpp1_arqos_s_o                   (),
      .fabric_llpp1_arsize_s_o                  (),
      .fabric_llpp1_arvalid_s_o                 (),
      .fabric_llpp1_awaddr_s_o                  (),
      .fabric_llpp1_awburst_s_o                 (),
      .fabric_llpp1_awcache_s_o                 (),
      .fabric_llpp1_awid_s_o                    (),
      .fabric_llpp1_awlen_s_o                   (),
      .fabric_llpp1_awlock_s_o                  (),
      .fabric_llpp1_awprot_s_o                  (),
      .fabric_llpp1_awqos_s_o                   (),
      .fabric_llpp1_awsize_s_o                  (),
      .fabric_llpp1_awvalid_s_o                 (),
      .fabric_llpp1_bready_s_o                  (),
      .fabric_llpp1_rready_s_o                  (),
      .fabric_llpp1_wdata_s_o                   (),
      .fabric_llpp1_wlast_s_o                   (),
      .fabric_llpp1_wstrb_s_o                   (),
      .fabric_llpp1_wvalid_s_o                  (),
      .fabric_llpp1_arready_s_i                 (1'b0),
      .fabric_llpp1_awready_s_i                 (1'b0),
      .fabric_llpp1_bid_s_i                     (12'd0),
      .fabric_llpp1_bresp_s_i                   (2'd0),
      .fabric_llpp1_bvalid_s_i                  (1'b0),
      .fabric_llpp1_rdata_s_i                   (32'd0),
      .fabric_llpp1_rid_s_i                     (12'd0),
      .fabric_llpp1_rlast_s_i                   (1'b0),
      .fabric_llpp1_rresp_s_i                   (2'd0),
      .fabric_llpp1_rvalid_s_i                  (1'b0),
      .fabric_llpp1_wready_s_i                  (1'b0),
      .fabric_llpp2_araddr_s_o                  (),
      .fabric_llpp2_arburst_s_o                 (),
      .fabric_llpp2_arcache_s_o                 (),
      .fabric_llpp2_arid_s_o                    (),
      .fabric_llpp2_arlen_s_o                   (),
      .fabric_llpp2_arlock_s_o                  (),
      .fabric_llpp2_arprot_s_o                  (),
      .fabric_llpp2_arqos_s_o                   (),
      .fabric_llpp2_arsize_s_o                  (),
      .fabric_llpp2_arvalid_s_o                 (),
      .fabric_llpp2_awaddr_s_o                  (),
      .fabric_llpp2_awburst_s_o                 (),
      .fabric_llpp2_awcache_s_o                 (),
      .fabric_llpp2_awid_s_o                    (),
      .fabric_llpp2_awlen_s_o                   (),
      .fabric_llpp2_awlock_s_o                  (),
      .fabric_llpp2_awprot_s_o                  (),
      .fabric_llpp2_awqos_s_o                   (),
      .fabric_llpp2_awsize_s_o                  (),
      .fabric_llpp2_awvalid_s_o                 (),
      .fabric_llpp2_bready_s_o                  (),
      .fabric_llpp2_rready_s_o                  (),
      .fabric_llpp2_wdata_s_o                   (),
      .fabric_llpp2_wlast_s_o                   (),
      .fabric_llpp2_wstrb_s_o                   (),
      .fabric_llpp2_wvalid_s_o                  (),
      .fabric_llpp2_arready_s_i                 (1'b0),
      .fabric_llpp2_awready_s_i                 (1'b0),
      .fabric_llpp2_bid_s_i                     (12'd0),
      .fabric_llpp2_bresp_s_i                   (2'd0),
      .fabric_llpp2_bvalid_s_i                  (1'b0),
      .fabric_llpp2_rdata_s_i                   (32'd0),
      .fabric_llpp2_rid_s_i                     (12'd0),
      .fabric_llpp2_rlast_s_i                   (1'b0),
      .fabric_llpp2_rresp_s_i                   (2'd0),
      .fabric_llpp2_rvalid_s_i                  (1'b0),
      .fabric_llpp2_wready_s_i                  (1'b0),
      .fabric_llpp3_araddr_s_o                  (),
      .fabric_llpp3_arburst_s_o                 (),
      .fabric_llpp3_arcache_s_o                 (),
      .fabric_llpp3_arid_s_o                    (),
      .fabric_llpp3_arlen_s_o                   (),
      .fabric_llpp3_arlock_s_o                  (),
      .fabric_llpp3_arprot_s_o                  (),
      .fabric_llpp3_arqos_s_o                   (),
      .fabric_llpp3_arsize_s_o                  (),
      .fabric_llpp3_arvalid_s_o                 (),
      .fabric_llpp3_awaddr_s_o                  (),
      .fabric_llpp3_awburst_s_o                 (),
      .fabric_llpp3_awcache_s_o                 (),
      .fabric_llpp3_awid_s_o                    (),
      .fabric_llpp3_awlen_s_o                   (),
      .fabric_llpp3_awlock_s_o                  (),
      .fabric_llpp3_awprot_s_o                  (),
      .fabric_llpp3_awqos_s_o                   (),
      .fabric_llpp3_awsize_s_o                  (),
      .fabric_llpp3_awvalid_s_o                 (),
      .fabric_llpp3_bready_s_o                  (),
      .fabric_llpp3_rready_s_o                  (),
      .fabric_llpp3_wdata_s_o                   (),
      .fabric_llpp3_wlast_s_o                   (),
      .fabric_llpp3_wstrb_s_o                   (),
      .fabric_llpp3_wvalid_s_o                  (),
      .fabric_llpp3_arready_s_i                 (1'b0),
      .fabric_llpp3_awready_s_i                 (1'b0),
      .fabric_llpp3_bid_s_i                     (12'd0),
      .fabric_llpp3_bresp_s_i                   (2'd0),
      .fabric_llpp3_bvalid_s_i                  (1'b0),
      .fabric_llpp3_rdata_s_i                   (32'd0),
      .fabric_llpp3_rid_s_i                     (12'd0),
      .fabric_llpp3_rlast_s_i                   (1'b0),
      .fabric_llpp3_rresp_s_i                   (2'd0),
      .fabric_llpp3_rvalid_s_i                  (1'b0),
      .fabric_llpp3_wready_s_i                  (1'b0),
      .fabric_qos_pprdata_o                     (),
      .fabric_qos_ppready_o                     (),
      .fabric_qos_ppslverr_o                    (),
      .fabric_qos_ppaddr_i                      (32'd0),
      .fabric_qos_ppenable_i                    (1'b0),
      .fabric_qos_ppwdata_i                     (32'd0),
      .fabric_qos_ppwrite_i                     (1'b0),
      .fabric_qos_presetn_i                     (1'b0),
      .fabric_qos_psel_i                        (1'b0),
      .fabric_tnd_hssl_flushin_o                (),
      .fabric_tnd_hssl_trigin_o                 (),
      .fabric_tnd_fpga_apb_master_paddr_o       (),
      .fabric_tnd_fpga_apb_master_penable_o     (),
      .fabric_tnd_fpga_apb_master_psel_o        (),
      .fabric_tnd_fpga_apb_master_pwdata_o      (),
      .fabric_tnd_fpga_apb_master_pwrite_o      (),
      .fabric_tnd_fpga_atb_master_afvalid_o     (),
      .fabric_tnd_fpga_atb_master_atready_o     (),
      .fabric_tnd_fpga_atb_master_syncreq_o     (),
      .fabric_tnd_hssl_apb_master_paddr_o       (),
      .fabric_tnd_hssl_apb_master_penable_o     (),
      .fabric_tnd_hssl_apb_master_psel_o        (),
      .fabric_tnd_hssl_apb_master_pwdata_o      (),
      .fabric_tnd_hssl_apb_master_pwrite_o      (),
      .fabric_tnd_hssl_atb_master_afready_o     (),
      .fabric_tnd_hssl_atb_master_atbytes_o     (),
      .fabric_tnd_hssl_atb_master_atdata_o      (),
      .fabric_tnd_hssl_atb_master_atid_o        (),
      .fabric_tnd_hssl_atb_master_atvalid_o     (),
      .fabric_tnd_trace_clk_traceoutportintf_o  (),
      .fabric_tnd_trace_ctl_traceoutportintf_o  (),
      .fabric_tnd_trace_data_traceoutportintf_o (),
      .fabric_tsvalue_tsgen_fpga_o              (),
      .fabric_tnd_fpga_apb_master_prdata_i      (32'd0),
      .fabric_tnd_fpga_apb_master_pready_i      (1'b0),
      .fabric_tnd_fpga_apb_master_pslverr_i     (1'b0),
      .fabric_tnd_fpga_atb_master_afready_i     (1'b0),
      .fabric_tnd_fpga_atb_master_atbytes_i     (4'd0),
      .fabric_tnd_fpga_atb_master_atdata_i      (128'd0),
      .fabric_tnd_fpga_atb_master_atid_i        (7'd0),
      .fabric_tnd_fpga_atb_master_atvalid_i     (1'b0),
      .fabric_tnd_hssl_apb_master_prdata_i      (32'd0),
      .fabric_tnd_hssl_apb_master_pready_i      (1'b0),
      .fabric_tnd_hssl_apb_master_pslverr_i     (1'b0),
      .fabric_tnd_hssl_atb_master_afvalid_i     (1'b0),
      .fabric_tnd_hssl_atb_master_atready_i     (1'b0),
      .fabric_tnd_hssl_atb_master_syncreq_i     (1'b0),
      .fabric_watchdog0_signal_0_o              (),
      .fabric_watchdog0_signal_1_o              (),
      .fabric_watchdog1_signal_0_o              (),
      .fabric_watchdog1_signal_1_o              (),
      .fabric_watchdog2_signal_0_o              (),
      .fabric_watchdog2_signal_1_o              (),
      .fabric_watchdog3_signal_0_o              (),
      .fabric_watchdog3_signal_1_o              (),
      .fabric_tst_pll_lock_o                    (),
      .fabric_soc_mon_sensor_alarm_o            (),
      .fabric_erom_fpga_cpu0_dbgen_i            (1'b0),
      .fabric_erom_fpga_cpu0_hiden_i            (1'b0),
      .fabric_erom_fpga_cpu0_hniden_i           (1'b0),
      .fabric_erom_fpga_cpu0_niden_i            (1'b0),
      .fabric_erom_fpga_cpu1_dbgen_i            (1'b0),
      .fabric_erom_fpga_cpu1_hiden_i            (1'b0),
      .fabric_erom_fpga_cpu1_hniden_i           (1'b0),
      .fabric_erom_fpga_cpu1_niden_i            (1'b0),
      .fabric_erom_fpga_cpu2_dbgen_i            (1'b0),
      .fabric_erom_fpga_cpu2_hiden_i            (1'b0),
      .fabric_erom_fpga_cpu2_hniden_i           (1'b0),
      .fabric_erom_fpga_cpu2_niden_i            (1'b0),
      .fabric_erom_fpga_cpu3_dbgen_i            (1'b0),
      .fabric_erom_fpga_cpu3_hiden_i            (1'b0),
      .fabric_erom_fpga_cpu3_hniden_i           (1'b0),
      .fabric_erom_fpga_cpu3_niden_i            (1'b0),
      .fabric_erom_fpga_cs_dbgen_i              (1'b0),
      .fabric_erom_fpga_cs_niden_i              (1'b0),
      .fabric_erom_fpga_cs_deviceen_i           (1'b0),
      .fabric_erom_fpga_cs_rst_n_i              (1'b0),
      .fabric_erom_fpga_debug_en_i              (1'b0),
      .fabric_enable_TMR_i                      ({3{1'b1}}),
      .fabric_spw_interrupts_toggle_o           (),
      .fabric_spw_interrupts_o                  (),
      .fabric_flash_irq_toggle_o                (),
      .fabric_flash_irq_o                       (),
      .fabric_fpga_dma_hs_rstn_i                ({6{1'b1}})
    );

  axi_cgra4ml_2m
    #(
      .ROWS         (C_ROWS),
      .COLS         (C_COLS),
      .X_BITS       (C_X_BITS),
      .K_BITS       (C_K_BITS),
      .Y_BITS       (C_Y_BITS),
      .Y_OUT_BITS   (C_Y_OUT_BITS),
      .M_DATA_WIDTH_HF_CONV (C_M_DATA_WIDTH_HF_CONV),
      .M_DATA_WIDTH_HF_CONV_DW (C_M_DATA_WIDTH_HF_CONV_DW),
      .AXI_WIDTH    (C_AXI_WIDTH),
      .AXI_ID_WIDTH (C_AXI_ID_WIDTH),
      .AXI_STRB_WIDTH (C_AXI_STRB_WIDTH),
      .AXI_MAX_BURST_LEN (C_AXI_MAX_BURST_LEN),
      .AXI_ADDR_WIDTH (C_AXI_ADDR_WIDTH),
      .AXIL_WIDTH   (C_AXIL_WIDTH),
      .AXIL_ADDR_WIDTH (C_AXIL_ADDR_WIDTH),
      .STRB_WIDTH   (C_AXIL_STRB_WIDTH),
      .W_BPT        (C_W_BPT)
    )
    i_core (
      .clk                   (fabric_clk),
      .rstn                  (core_rstn),
      .s_llpp_awaddr         (llpp0_awaddr),
      .s_llpp_awvalid        (llpp0_awvalid),
      .s_llpp_awready        (llpp0_awready),
      .s_llpp_wdata          (llpp0_wdata),
      .s_llpp_wstrb          (llpp0_wstrb),
      .s_llpp_wvalid         (llpp0_wvalid),
      .s_llpp_wready         (llpp0_wready),
      .s_llpp_bresp          (llpp0_bresp),
      .s_llpp_bvalid         (llpp0_bvalid),
      .s_llpp_bready         (llpp0_bready),
      .s_llpp_araddr         (llpp0_araddr),
      .s_llpp_arvalid        (llpp0_arvalid),
      .s_llpp_arready        (llpp0_arready),
      .s_llpp_rdata          (llpp0_rdata),
      .s_llpp_rresp          (llpp0_rresp),
      .s_llpp_rvalid         (llpp0_rvalid),
      .s_llpp_rready         (llpp0_rready),
      .s_llpp_awid           (llpp0_awid),
      .s_llpp_awlen          (llpp0_awlen),
      .s_llpp_wlast          (llpp0_wlast),
      .s_llpp_bid            (llpp0_bid),
      .s_llpp_arid           (llpp0_arid),
      .s_llpp_arlen          (llpp0_arlen),
      .s_llpp_rid            (llpp0_rid),
      .s_llpp_rlast          (llpp0_rlast),
      .m_axi_shared_arid     (shared_arid),
      .m_axi_shared_araddr   (shared_araddr),
      .m_axi_shared_arlen    (shared_arlen),
      .m_axi_shared_arsize   (shared_arsize),
      .m_axi_shared_arburst  (shared_arburst),
      .m_axi_shared_arlock   (shared_arlock),
      .m_axi_shared_arcache  (shared_arcache),
      .m_axi_shared_arprot   (shared_arprot),
      .m_axi_shared_arvalid  (shared_arvalid),
      .m_axi_shared_arready  (shared_arready),
      .m_axi_shared_rid      (shared_rid),
      .m_axi_shared_rdata    (shared_rdata),
      .m_axi_shared_rresp    (shared_rresp),
      .m_axi_shared_rlast    (shared_rlast),
      .m_axi_shared_rvalid   (shared_rvalid),
      .m_axi_shared_rready   (shared_rready),
      .m_axi_shared_awid     (shared_awid),
      .m_axi_shared_awaddr   (shared_awaddr),
      .m_axi_shared_awlen    (shared_awlen),
      .m_axi_shared_awsize   (shared_awsize),
      .m_axi_shared_awburst  (shared_awburst),
      .m_axi_shared_awlock   (shared_awlock),
      .m_axi_shared_awcache  (shared_awcache),
      .m_axi_shared_awprot   (shared_awprot),
      .m_axi_shared_awvalid  (shared_awvalid),
      .m_axi_shared_awready  (shared_awready),
      .m_axi_shared_wdata    (shared_wdata),
      .m_axi_shared_wstrb    (shared_wstrb),
      .m_axi_shared_wlast    (shared_wlast),
      .m_axi_shared_wvalid   (shared_wvalid),
      .m_axi_shared_wready   (shared_wready),
      .m_axi_shared_bid      (shared_bid),
      .m_axi_shared_bresp    (shared_bresp),
      .m_axi_shared_bvalid   (shared_bvalid),
      .m_axi_shared_bready   (shared_bready),
      .m_axi_weights_arid    (weights_arid),
      .m_axi_weights_araddr  (weights_araddr),
      .m_axi_weights_arlen   (weights_arlen),
      .m_axi_weights_arsize  (weights_arsize),
      .m_axi_weights_arburst (weights_arburst),
      .m_axi_weights_arlock  (weights_arlock),
      .m_axi_weights_arcache (weights_arcache),
      .m_axi_weights_arprot  (weights_arprot),
      .m_axi_weights_arvalid (weights_arvalid),
      .m_axi_weights_arready (weights_arready),
      .m_axi_weights_rid     (weights_rid),
      .m_axi_weights_rdata   (weights_rdata),
      .m_axi_weights_rresp   (weights_rresp),
      .m_axi_weights_rlast   (weights_rlast),
      .m_axi_weights_rvalid  (weights_rvalid),
      .m_axi_weights_rready  (weights_rready)
    );
endmodule
