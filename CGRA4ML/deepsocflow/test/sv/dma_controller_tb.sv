`timescale 1ns/1ps

module dma_controller_tb;
  localparam int SRAM_RD_DATA_WIDTH = 256;
  localparam int SRAM_RD_DEPTH      = 2;
  localparam int COUNTER_WIDTH      = 4;
  localparam int AXI_ADDR_WIDTH     = 32;
  localparam int AXI_DATA_WIDTH     = 32;
  localparam int AXIS_USER_WIDTH    = 65;
  localparam int AXI_LEN_WIDTH      = 32;
  localparam int AXI_TAG_WIDTH      = 8;

  localparam int A_START        = 'h0;
  localparam int A_DONE_READ    = 'h1;
  localparam int A_DONE_WRITE   = 'h3;
  localparam int A_OCM_BASE     = 'h5;
  localparam int A_WEIGHTS_BASE = 'h7;
  localparam int A_BUNDLE_DONE  = 'h8;
  localparam int A_N_BUNDLES_1  = 'h9;

  reg clk = 1'b0;
  reg rstn = 1'b0;

  reg reg_wr_en = 1'b0;
  wire reg_wr_ack;
  reg [AXI_ADDR_WIDTH-1:0] reg_wr_addr = {AXI_ADDR_WIDTH{1'b0}};
  reg [AXI_DATA_WIDTH-1:0] reg_wr_data = {AXI_DATA_WIDTH{1'b0}};
  reg reg_rd_en = 1'b0;
  wire reg_rd_ack;
  reg [AXI_ADDR_WIDTH-1:0] reg_rd_addr = {AXI_ADDR_WIDTH{1'b0}};
  wire [AXI_DATA_WIDTH-1:0] reg_rd_data;

  reg o_ready = 1'b1;
  reg o_valid = 1'b0;
  reg o_last = 1'b0;
  reg [AXI_LEN_WIDTH-1:0] o_bpt = {AXI_LEN_WIDTH{1'b0}};
  reg [AXI_TAG_WIDTH-1:0] os_tag = {AXI_TAG_WIDTH{1'b0}};
  reg [3:0] os_error = 4'd0;
  reg os_valid = 1'b0;

  wire [AXI_ADDR_WIDTH-1:0] m_od_addr;
  wire [AXI_LEN_WIDTH-1:0] m_od_len;
  wire m_od_valid;
  wire [AXI_TAG_WIDTH-1:0] m_od_tag;
  reg m_od_ready = 1'b1;

  wire [AXI_ADDR_WIDTH-1:0] m_xd_addr;
  wire [AXIS_USER_WIDTH-1:0] m_xd_user;
  wire [AXI_LEN_WIDTH-1:0] m_xd_len;
  wire m_xd_valid;
  reg m_xd_ready = 1'b1;

  wire [AXI_ADDR_WIDTH-1:0] m_wd_addr;
  wire [AXIS_USER_WIDTH-1:0] m_wd_user;
  wire [AXI_LEN_WIDTH-1:0] m_wd_len;
  wire m_wd_valid;
  reg m_wd_ready = 1'b1;

  dma_controller #(
    .SRAM_RD_DATA_WIDTH (SRAM_RD_DATA_WIDTH),
    .SRAM_RD_DEPTH      (SRAM_RD_DEPTH),
    .COUNTER_WIDTH      (COUNTER_WIDTH),
    .AXI_ADDR_WIDTH     (AXI_ADDR_WIDTH),
    .AXI_DATA_WIDTH     (AXI_DATA_WIDTH),
    .AXIS_USER_WIDTH    (AXIS_USER_WIDTH),
    .AXI_LEN_WIDTH      (AXI_LEN_WIDTH),
    .AXI_TAG_WIDTH      (AXI_TAG_WIDTH)
  ) dut (
    .clk         (clk),
    .rstn        (rstn),
    .reg_wr_en   (reg_wr_en),
    .reg_wr_ack  (reg_wr_ack),
    .reg_wr_addr (reg_wr_addr),
    .reg_wr_data (reg_wr_data),
    .reg_rd_en   (reg_rd_en),
    .reg_rd_ack  (reg_rd_ack),
    .reg_rd_addr (reg_rd_addr),
    .reg_rd_data (reg_rd_data),
    .o_ready     (o_ready),
    .o_valid     (o_valid),
    .o_last      (o_last),
    .o_bpt       (o_bpt),
    .os_tag      (os_tag),
    .os_error    (os_error),
    .os_valid    (os_valid),
    .m_od_addr   (m_od_addr),
    .m_od_len    (m_od_len),
    .m_od_valid  (m_od_valid),
    .m_od_tag    (m_od_tag),
    .m_od_ready  (m_od_ready),
    .m_xd_addr   (m_xd_addr),
    .m_xd_user   (m_xd_user),
    .m_xd_len    (m_xd_len),
    .m_xd_valid  (m_xd_valid),
    .m_xd_ready  (m_xd_ready),
    .m_wd_addr   (m_wd_addr),
    .m_wd_user   (m_wd_user),
    .m_wd_len    (m_wd_len),
    .m_wd_valid  (m_wd_valid),
    .m_wd_ready  (m_wd_ready)
  );

  always #5 clk = ~clk;

  task automatic write_reg(input int addr, input [31:0] data);
    begin
      @(posedge clk);
      reg_wr_addr = addr[AXI_ADDR_WIDTH-1:0];
      reg_wr_data = data;
      reg_wr_en = 1'b1;
      @(posedge clk);
      reg_wr_en = 1'b0;
      reg_wr_addr = {AXI_ADDR_WIDTH{1'b0}};
      reg_wr_data = {AXI_DATA_WIDTH{1'b0}};
    end
  endtask

  task automatic wait_cycles(input int count);
    integer i;
    begin
      for (i = 0; i < count; i = i + 1) @(posedge clk);
    end
  endtask

  task automatic wait_weight_descriptor;
    integer cycles;
    begin
      cycles = 0;
      while (!m_wd_valid && cycles < 32) begin
        cycles = cycles + 1;
        @(posedge clk);
      end
      if (!m_wd_valid) $fatal(1, "dma_controller_tb: timed out waiting for weight descriptor");
      if (m_wd_addr !== 32'h00002000) $fatal(1, "dma_controller_tb: bad weight addr 0x%0h", m_wd_addr);
      if (m_wd_len !== 32'd4) $fatal(1, "dma_controller_tb: bad weight len %0d", m_wd_len);
      if (m_wd_user[0] !== 1'b1) $fatal(1, "dma_controller_tb: first weight pass flag not set");
      @(posedge clk);
    end
  endtask

  task automatic wait_pixel_descriptor;
    integer cycles;
    begin
      cycles = 0;
      while (!m_xd_valid && cycles < 32) begin
        cycles = cycles + 1;
        @(posedge clk);
      end
      if (!m_xd_valid) $fatal(1, "dma_controller_tb: timed out waiting for pixel descriptor");
      if (m_xd_addr !== 32'h00001000) $fatal(1, "dma_controller_tb: bad pixel addr 0x%0h", m_xd_addr);
      if (m_xd_len !== 32'd8) $fatal(1, "dma_controller_tb: bad pixel len %0d", m_xd_len);
      if (m_xd_user[0] !== 1'b1) $fatal(1, "dma_controller_tb: first pixel pass flag not set");
      @(posedge clk);
    end
  endtask

  task automatic check_output_descriptor;
    begin
      @(posedge clk);
      o_bpt = 32'd12;
      o_valid = 1'b1;
      o_last = 1'b0;
      @(posedge clk);
      if (!m_od_valid) $fatal(1, "dma_controller_tb: output descriptor was not issued");
      if (m_od_addr !== 32'h00003000) $fatal(1, "dma_controller_tb: bad output addr 0x%0h", m_od_addr);
      if (m_od_len !== 32'd12) $fatal(1, "dma_controller_tb: bad output len %0d", m_od_len);
      if (m_od_tag !== 8'd0) $fatal(1, "dma_controller_tb: bad output tag %0d", m_od_tag);
      o_valid = 1'b0;
      @(posedge clk);
      os_tag = 8'd0;
      os_error = 4'd0;
      os_valid = 1'b1;
      @(posedge clk);
      os_valid = 1'b0;
    end
  endtask

  initial begin
    repeat (4) @(posedge clk);
    rstn = 1'b1;
    repeat (2) @(posedge clk);

    write_reg(A_OCM_BASE + 0, 32'h00003000);
    write_reg(A_OCM_BASE + 1, 32'h00004000);
    write_reg(A_WEIGHTS_BASE, 32'h00002000);
    write_reg(A_BUNDLE_DONE, 32'd1);
    write_reg(A_N_BUNDLES_1, 32'd1);

    write_reg(16, 32'h00001000); // pixel base
    write_reg(17, 32'd8);        // pixel bytes, first pass
    write_reg(18, 32'd8);        // pixel bytes
    write_reg(19, 32'd4);        // weight bytes, first pass
    write_reg(20, 32'd4);        // weight bytes
    write_reg(21, 32'd1);        // max passes
    write_reg(22, 32'd1);        // max transfers
    write_reg(23, 32'h00000000); // low header bits

    write_reg(A_START, 32'd1);

    wait_weight_descriptor();
    wait_pixel_descriptor();
    check_output_descriptor();
    wait_cycles(4);

    $display("dma_controller_tb: PASS");
    $finish;
  end
endmodule
