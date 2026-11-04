`timescale 1ns/1ps

module axis_weight_rotator_tb;
  localparam int COLS              = 2;
  localparam int WORD_WIDTH        = 4;
  localparam int KW_MAX            = 3;
  localparam int CI_MAX            = 2;
  localparam int XW_MAX            = 2;
  localparam int XH_MAX            = 8;
  localparam int XN_MAX            = 2;
  localparam int AXI_WIDTH         = 8;
  localparam int HEADER_WIDTH      = 16;
  localparam int DELAY_W_RAM       = 1;
  localparam int RAM_WEIGHTS_DEPTH = 4;
  localparam int CONFIG_BEATS      = 0;
  localparam int TUSER_WIDTH       = 6;

  localparam int SCI_IS_FIRST_P_BIT = 0;
  localparam int SCI_KW2_LSB        = 1;
  localparam int SCI_COLS_LSB       = 2;
  localparam int SCI_BLOCKS_LSB     = 3;
  localparam int SCI_CIN_P0_LSB     = 4;
  localparam int SCI_CIN_P_LSB      = 5;
  localparam int SCI_XN_LSB         = 6;
  localparam int SCI_ADDR_P0_LSB    = 7;
  localparam int SCI_ADDR_P_LSB     = 9;

  reg aclk = 1'b0;
  reg aresetn = 1'b0;
  wire s_axis_tready;
  reg s_axis_tvalid = 1'b0;
  reg s_axis_tlast = 1'b0;
  reg [AXI_WIDTH-1:0] s_axis_tdata = {AXI_WIDTH{1'b0}};
  reg [AXI_WIDTH/WORD_WIDTH-1:0] s_axis_tkeep = {AXI_WIDTH/WORD_WIDTH{1'b0}};
  reg [HEADER_WIDTH:0] s_axis_tuser = {(HEADER_WIDTH+1){1'b0}};
  reg [COLS-1:0] m_axis_tready = {COLS{1'b1}};
  wire [COLS-1:0] m_axis_tvalid;
  wire [COLS-1:0] m_axis_tlast;
  wire [COLS*TUSER_WIDTH-1:0] m_axis_tuser;
  wire [COLS*WORD_WIDTH-1:0] m_axis_tdata;

  axis_weight_rotator #(
    .COLS              (COLS),
    .WORD_WIDTH        (WORD_WIDTH),
    .KW_MAX            (KW_MAX),
    .CI_MAX            (CI_MAX),
    .XW_MAX            (XW_MAX),
    .XH_MAX            (XH_MAX),
    .XN_MAX            (XN_MAX),
    .AXI_WIDTH         (AXI_WIDTH),
    .HEADER_WIDTH      (HEADER_WIDTH),
    .DELAY_W_RAM       (DELAY_W_RAM),
    .RAM_WEIGHTS_DEPTH (RAM_WEIGHTS_DEPTH),
    .CONFIG_BEATS      (CONFIG_BEATS),
    .TUSER_WIDTH       (TUSER_WIDTH)
  ) dut (
    .aclk          (aclk),
    .aresetn       (aresetn),
    .s_axis_tready (s_axis_tready),
    .s_axis_tvalid (s_axis_tvalid),
    .s_axis_tlast  (s_axis_tlast),
    .s_axis_tdata  (s_axis_tdata),
    .s_axis_tkeep  (s_axis_tkeep),
    .s_axis_tuser  (s_axis_tuser),
    .m_axis_tready (m_axis_tready),
    .m_axis_tvalid (m_axis_tvalid),
    .m_axis_tlast  (m_axis_tlast),
    .m_axis_tuser  (m_axis_tuser),
    .m_axis_tdata  (m_axis_tdata)
  );

  always #5 aclk = ~aclk;

  task automatic set_one_word_user;
    begin
      s_axis_tuser = {(HEADER_WIDTH+1){1'b0}};
      s_axis_tuser[SCI_IS_FIRST_P_BIT] = 1'b1;
      s_axis_tuser[SCI_KW2_LSB] = 1'b0;
      s_axis_tuser[SCI_COLS_LSB] = 1'b0;
      s_axis_tuser[SCI_BLOCKS_LSB] = 1'b0;
      s_axis_tuser[SCI_CIN_P0_LSB] = 1'b0;
      s_axis_tuser[SCI_CIN_P_LSB] = 1'b0;
      s_axis_tuser[SCI_XN_LSB] = 1'b0;
      s_axis_tuser[SCI_ADDR_P0_LSB +: 2] = 2'd0;
      s_axis_tuser[SCI_ADDR_P_LSB +: 2] = 2'd0;
    end
  endtask

  task automatic send_weight_word(input [AXI_WIDTH-1:0] data);
    begin
      @(posedge aclk);
      s_axis_tdata = data;
      s_axis_tkeep = {AXI_WIDTH/WORD_WIDTH{1'b1}};
      s_axis_tlast = 1'b1;
      s_axis_tvalid = 1'b1;
      while (!s_axis_tready) @(posedge aclk);
      @(posedge aclk);
      s_axis_tvalid = 1'b0;
      s_axis_tlast = 1'b0;
      s_axis_tdata = {AXI_WIDTH{1'b0}};
      s_axis_tkeep = {AXI_WIDTH/WORD_WIDTH{1'b0}};
    end
  endtask

  task automatic expect_rotated_word(input [COLS*WORD_WIDTH-1:0] expected);
    integer cycles;
    begin
      cycles = 0;
      while ((m_axis_tvalid !== {COLS{1'b1}}) && cycles < 64) begin
        cycles = cycles + 1;
        @(posedge aclk);
      end
      if (m_axis_tvalid !== {COLS{1'b1}}) $fatal(1, "axis_weight_rotator_tb: timed out waiting for valid columns");
      if (m_axis_tdata !== expected) $fatal(1, "axis_weight_rotator_tb: data mismatch expected=0x%0h got=0x%0h", expected, m_axis_tdata);
      if (m_axis_tlast !== {COLS{1'b1}}) $fatal(1, "axis_weight_rotator_tb: expected last on one-word transfer");
      @(posedge aclk);
    end
  endtask

  initial begin
    repeat (4) @(posedge aclk);
    aresetn = 1'b1;
    repeat (2) @(posedge aclk);

    set_one_word_user();
    send_weight_word(8'hba);
    expect_rotated_word(8'hba);

    $display("axis_weight_rotator_tb: PASS");
    $finish;
  end
endmodule
