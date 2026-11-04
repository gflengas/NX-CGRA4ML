`timescale 1ns/1ps

module axis_pixels_tb;
  localparam int ROWS            = 4;
  localparam int KH_MAX          = 3;
  localparam int CI_MAX          = 2;
  localparam int XW_MAX          = 2;
  localparam int XH_MAX          = 8;
  localparam int WORD_WIDTH      = 4;
  localparam int RAM_EDGES_DEPTH = 4;
  localparam int AXI_WIDTH       = 16;
  localparam int HEADER_WIDTH    = 16;

  localparam int USER_FIRST_P_BIT = 0;
  localparam int USER_KH2_LSB     = 1;
  localparam int USER_W_LSB       = 2;
  localparam int USER_L_LSB       = 3;
  localparam int USER_CI_P0_LSB   = 4;
  localparam int USER_CI_P_LSB    = 5;

  reg aclk = 1'b0;
  reg aresetn = 1'b0;
  wire s_ready;
  reg s_valid = 1'b0;
  reg s_last = 1'b0;
  reg [AXI_WIDTH-1:0] s_data = {AXI_WIDTH{1'b0}};
  reg [AXI_WIDTH/WORD_WIDTH-1:0] s_keep = {AXI_WIDTH/WORD_WIDTH{1'b0}};
  reg [HEADER_WIDTH:0] s_user = {(HEADER_WIDTH+1){1'b0}};
  reg m_ready = 1'b1;
  wire m_valid;
  wire [ROWS*WORD_WIDTH-1:0] m_data;

  axis_pixels #(
    .ROWS            (ROWS),
    .KH_MAX          (KH_MAX),
    .CI_MAX          (CI_MAX),
    .XW_MAX          (XW_MAX),
    .XH_MAX          (XH_MAX),
    .WORD_WIDTH      (WORD_WIDTH),
    .RAM_EDGES_DEPTH (RAM_EDGES_DEPTH),
    .AXI_WIDTH       (AXI_WIDTH),
    .HEADER_WIDTH    (HEADER_WIDTH)
  ) dut (
    .aclk    (aclk),
    .aresetn (aresetn),
    .s_ready (s_ready),
    .s_valid (s_valid),
    .s_last  (s_last),
    .s_data  (s_data),
    .s_keep  (s_keep),
    .s_user  (s_user),
    .m_ready (m_ready),
    .m_valid (m_valid),
    .m_data  (m_data)
  );

  always #5 aclk <= ~aclk;

  task automatic set_user_single_pixel;
    begin
      s_user = {(HEADER_WIDTH+1){1'b0}};
      s_user[USER_FIRST_P_BIT] = 1'b1;
      s_user[USER_KH2_LSB]     = 1'b0;
      s_user[USER_W_LSB]       = 1'b0;
      s_user[USER_L_LSB]       = 1'b0;
      s_user[USER_CI_P0_LSB]   = 1'b0;
      s_user[USER_CI_P_LSB]    = 1'b0;
    end
  endtask

  task automatic send_single_beat(input [AXI_WIDTH-1:0] data);
    begin
      @(posedge aclk);
      s_data  = data;
      s_keep  = {AXI_WIDTH/WORD_WIDTH{1'b1}};
      s_last  = 1'b1;
      s_valid = 1'b1;

      while (!s_ready) @(posedge aclk);
      @(posedge aclk);
      s_valid = 1'b0;
      s_last  = 1'b0;
      s_data  = {AXI_WIDTH{1'b0}};
      s_keep  = {AXI_WIDTH/WORD_WIDTH{1'b0}};
    end
  endtask

  task automatic expect_output(input [ROWS*WORD_WIDTH-1:0] expected);
    integer cycles;
    begin
      cycles = 0;
      while (!m_valid && cycles < 32) begin
        cycles = cycles + 1;
        @(posedge aclk);
      end

      if (!m_valid) begin
        $fatal(1, "axis_pixels_tb: timed out waiting for m_valid");
      end

      if (m_data !== expected) begin
        $fatal(1, "axis_pixels_tb: m_data mismatch expected=0x%0h got=0x%0h", expected, m_data);
      end

      @(posedge aclk);
    end
  endtask

  initial begin
    repeat (4) @(posedge aclk);
    aresetn = 1'b1;
    repeat (2) @(posedge aclk);

    set_user_single_pixel();
    send_single_beat(16'h4321);
    expect_output(16'h4321);

    $display("axis_pixels_tb: PASS");
    $finish;
  end
endmodule
