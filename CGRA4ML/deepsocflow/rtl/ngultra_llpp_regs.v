// NG-ULTRA LLPP slave straight to the dma_controller register interface.
//
// - One transaction at a time per channel; read and write channels independent.
// - The register word is addr[REG_ADDR_BITS-1:2]: the 4 KB CPU window
//   (0x0D000000) aliases nothing else on this port.
// - Writes: AW first, then W (a slave may wait for AW before WREADY). A single
//   beat with all strobes writes the register; reg_wr_en is a registered pulse.
// - Reads: reg_rd_en is a registered pulse; the controller registers
//   cfg[addr] on it, so RDATA is the controller's reg_rd_data one cycle later,
//   held until RREADY (nothing else reads the controller meanwhile).
// - Bursts (len != 0) and partial strobes are answered with SLVERR and touch no
//   register: the runtime only issues single 32-bit accesses, so either means a
//   bus or software fault that must not pass silently.
// Instantiated in axi_cgra4ml_2m.v. Verilog-2001 subset accepted by Impulse.
module ngultra_llpp_regs #(
  parameter ID_WIDTH      = 12,
  parameter ADDR_WIDTH    = 40,
  parameter DATA_WIDTH    = 32,
  parameter REG_ADDR_BITS = 12,
  parameter REG_WIDTH     = 32
)(
  input  clk, rstn,

  input  [ID_WIDTH-1:0]     s_awid,
  input  [ADDR_WIDTH-1:0]   s_awaddr,
  input  [7:0]              s_awlen,
  input                     s_awvalid,
  output                    s_awready,
  input  [DATA_WIDTH-1:0]   s_wdata,
  input  [DATA_WIDTH/8-1:0] s_wstrb,
  input                     s_wlast,
  input                     s_wvalid,
  output                    s_wready,
  output     [ID_WIDTH-1:0] s_bid,
  output reg [1:0]          s_bresp,
  output reg                s_bvalid,
  input                     s_bready,
  input  [ID_WIDTH-1:0]     s_arid,
  input  [ADDR_WIDTH-1:0]   s_araddr,
  input  [7:0]              s_arlen,
  input                     s_arvalid,
  output                    s_arready,
  output [DATA_WIDTH-1:0]   s_rdata,
  output     [ID_WIDTH-1:0] s_rid,
  output reg [1:0]          s_rresp,
  output                    s_rlast,
  output reg                s_rvalid,
  input                     s_rready,

  output reg                reg_wr_en,
  output reg [REG_WIDTH-1:0] reg_wr_addr,
  output reg [DATA_WIDTH-1:0] reg_wr_data,
  output reg                reg_rd_en,
  output reg [REG_WIDTH-1:0] reg_rd_addr,
  input  [DATA_WIDTH-1:0]   reg_rd_data
);
  localparam [1:0] OKAY = 2'b00, SLVERR = 2'b10;

  // The NG-ULTRA shell has no pins. Impulse keeps the SoC block, the PLL and
  // with them the whole design only through kept registers fed by the SoC, as
  // a bus bridge's ID registers would; without these the design is pruned
  // to nothing. Syntax from the vendor's SynKeep example.
  (*synthesis,syn_noprune="true",syn_preserve="true"*) reg [ID_WIDTH-1:0] bid_r;
  (*synthesis,syn_noprune="true",syn_preserve="true"*) reg [ID_WIDTH-1:0] rid_r;
  assign s_bid = bid_r;
  assign s_rid = rid_r;

  // ------------------------------ write ------------------------------------
  reg                     aw_held, w_error;
  reg [REG_ADDR_BITS-3:0] aw_word;
  reg                     aw_burst;

  assign s_awready = !aw_held && !s_bvalid;
  assign s_wready  =  aw_held;

  wire aw_fire = s_awvalid && s_awready;
  wire w_fire  = s_wvalid  && s_wready;
  wire w_ok    = !aw_burst && !w_error && (&s_wstrb) && s_wlast;

  always @(posedge clk)
    if (!rstn) begin
      aw_held     <= 1'b0;
      aw_word     <= {(REG_ADDR_BITS-2){1'b0}};
      aw_burst    <= 1'b0;
      w_error     <= 1'b0;
      bid_r       <= {ID_WIDTH{1'b0}};
      s_bresp     <= OKAY;
      s_bvalid    <= 1'b0;
      reg_wr_en   <= 1'b0;
      reg_wr_addr <= {REG_WIDTH{1'b0}};
      reg_wr_data <= {DATA_WIDTH{1'b0}};
    end else begin
      reg_wr_en <= 1'b0;
      if (aw_fire) begin
        aw_held  <= 1'b1;
        aw_word  <= s_awaddr[REG_ADDR_BITS-1:2];
        aw_burst <= s_awlen != 8'd0;
        w_error  <= 1'b0;
        bid_r    <= s_awid;
      end
      if (w_fire) begin
        if (s_wlast) begin
          aw_held  <= 1'b0;
          s_bvalid <= 1'b1;
          s_bresp  <= w_ok ? OKAY : SLVERR;
          if (w_ok) begin
            reg_wr_en   <= 1'b1;
            reg_wr_addr <= {{(REG_WIDTH-REG_ADDR_BITS+2){1'b0}}, aw_word};
            reg_wr_data <= s_wdata;
          end
        end else begin
          w_error <= 1'b1;  // a burst beat: the whole write is rejected
        end
      end
      if (s_bvalid && s_bready)
        s_bvalid <= 1'b0;
    end

  // ------------------------------ read -------------------------------------
  // ar_state: 0 idle, 1 reg_rd_en issued, 2 data valid; bursts answer
  // ar_beats + 1 error beats from state 2.
  reg [1:0] ar_state;
  reg       ar_burst;
  reg [7:0] ar_beats;

  assign s_arready = ar_state == 2'd0;
  assign s_rdata   = ar_burst ? {DATA_WIDTH{1'b0}} : reg_rd_data;
  assign s_rlast   = s_rvalid && ar_beats == 8'd0;

  always @(posedge clk)
    if (!rstn) begin
      ar_state    <= 2'd0;
      ar_burst    <= 1'b0;
      ar_beats    <= 8'd0;
      rid_r       <= {ID_WIDTH{1'b0}};
      s_rresp     <= OKAY;
      s_rvalid    <= 1'b0;
      reg_rd_en   <= 1'b0;
      reg_rd_addr <= {REG_WIDTH{1'b0}};
    end else begin
      reg_rd_en <= 1'b0;
      case (ar_state)
        2'd0: if (s_arvalid) begin
                rid_r    <= s_arid;
                ar_beats <= s_arlen;
                ar_burst <= s_arlen != 8'd0;
                if (s_arlen == 8'd0) begin
                  reg_rd_en   <= 1'b1;
                  reg_rd_addr <= {{(REG_WIDTH-REG_ADDR_BITS+2){1'b0}}, s_araddr[REG_ADDR_BITS-1:2]};
                  ar_state    <= 2'd1;
                end else begin
                  ar_state <= 2'd2;
                  s_rvalid <= 1'b1;
                  s_rresp  <= SLVERR;
                end
              end
        2'd1: begin  // controller registers reg_rd_data at the end of this cycle
                ar_state <= 2'd2;
                s_rvalid <= 1'b1;
                s_rresp  <= OKAY;
              end
        2'd2: if (s_rready) begin
                if (ar_beats == 8'd0) begin
                  s_rvalid <= 1'b0;
                  ar_state <= 2'd0;
                end else begin
                  ar_beats <= ar_beats - 8'd1;
                end
              end
        default: ar_state <= 2'd0;
      endcase
    end
endmodule
