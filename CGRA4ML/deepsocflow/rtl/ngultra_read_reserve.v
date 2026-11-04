// Read-data FIFO with address reservation for one AXI read master. The
// NG-ULTRA SoC appears to return read data for its FPGA master ports through
// a shared in-order path with finite buffering: a
// port that stops accepting R data blocks read data for the other port, and the
// accelerator deadlocks (pixel data waiting for weights that are stuck behind
// it). Here a read address is offered to the SoC only when the FIFO has room for
// the whole burst; the room is reserved when the address is accepted and freed
// when the DMA takes a beat, so R data from the SoC is always accepted.
// Accounting is in AXI beats inside this FIFO, so the DMA's realignment of
// unaligned transfers does not affect it. An offered address is held until its
// handshake (AXI rule).
// FIFO: write process plus registered read of a memory (maps to RAM), one
// output register; first word fall-through towards the DMA.
module ngultra_read_reserve #(
  parameter DATA_WIDTH = 128,
  parameter ID_WIDTH   = 6,
  parameter DEPTH_LOG2 = 5          // 32 beats: two 16-beat bursts in flight
)(
  input                       clk,
  input                       rstn,

  // DMA address side
  input                       dma_arvalid,
  output                      dma_arready,
  input  [7:0]                arlen,        // the DMA's arlen, also on the SoC port

  // SoC address side
  output                      soc_arvalid,
  input                       soc_arready,

  // SoC read data
  input  [ID_WIDTH-1:0]       soc_rid,
  input  [DATA_WIDTH-1:0]     soc_rdata,
  input  [1:0]                soc_rresp,
  input                       soc_rlast,
  input                       soc_rvalid,
  output                      soc_rready,

  // DMA read data
  output [ID_WIDTH-1:0]       dma_rid,
  output [DATA_WIDTH-1:0]     dma_rdata,
  output [1:0]                dma_rresp,
  output                      dma_rlast,
  output                      dma_rvalid,
  input                       dma_rready
);
  localparam DEPTH = 1 << DEPTH_LOG2;
  localparam W = ID_WIDTH + DATA_WIDTH + 3;

  // ------------------------------------------------------------ reservation
  reg  [DEPTH_LOG2:0] reserved;   // beats requested and not yet taken by the DMA
  reg                 ar_held;
  wire [8:0]          burst = {1'b0, arlen} + 9'd1;
  wire [9:0]          need  = {{(9 - DEPTH_LOG2){1'b0}}, reserved} + {1'b0, burst};
  wire                room  = need <= DEPTH;  // DEPTH_LOG2 <= 9

  assign soc_arvalid = dma_arvalid && (room || ar_held);
  assign dma_arready = soc_arready && soc_arvalid;

  wire ar_fire  = soc_arvalid && soc_arready;
  wire pop      = dma_rvalid && dma_rready;

  always @(posedge clk)
    if (!rstn) begin
      reserved <= {(DEPTH_LOG2+1){1'b0}};
      ar_held  <= 1'b0;
    end else begin
      ar_held  <= soc_arvalid && !soc_arready;
      reserved <= reserved + (ar_fire ? burst[DEPTH_LOG2:0] : {(DEPTH_LOG2+1){1'b0}})
                           - (pop ? {{DEPTH_LOG2{1'b0}}, 1'b1} : {(DEPTH_LOG2+1){1'b0}});
    end

  // -------------------------------------------------------------- FIFO
  reg [W-1:0]          mem [0:DEPTH-1];
  reg [DEPTH_LOG2-1:0] wptr, rptr;
  reg [DEPTH_LOG2:0]   stored;      // beats in mem (excluding the output register)
  reg                  out_valid;
  reg [W-1:0]          out_data;

  wire push = soc_rvalid && soc_rready;
  wire load = (stored != 0) && (!out_valid || pop);

  // Space is reserved for every beat, so the FIFO cannot be full when R
  // arrives; rready still guards it.
  assign soc_rready = stored != DEPTH;

  always @(posedge clk)
    if (push) mem[wptr] <= {soc_rid, soc_rresp, soc_rlast, soc_rdata};

  always @(posedge clk)
    if (load) out_data <= mem[rptr];

  always @(posedge clk)
    if (!rstn) begin
      wptr      <= {DEPTH_LOG2{1'b0}};
      rptr      <= {DEPTH_LOG2{1'b0}};
      stored    <= {(DEPTH_LOG2+1){1'b0}};
      out_valid <= 1'b0;
    end else begin
      if (push) wptr <= wptr + 1'b1;
      if (load) rptr <= rptr + 1'b1;
      stored <= stored + {{DEPTH_LOG2{1'b0}}, push} - {{DEPTH_LOG2{1'b0}}, load};
      if (load)     out_valid <= 1'b1;
      else if (pop) out_valid <= 1'b0;
    end

  assign dma_rvalid = out_valid;
  assign {dma_rid, dma_rresp, dma_rlast, dma_rdata} = out_data;
endmodule
