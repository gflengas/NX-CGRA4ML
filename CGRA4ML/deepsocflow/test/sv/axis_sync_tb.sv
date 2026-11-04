`timescale 1ns/1ps

`include "../../rtl/defines.svh"

module axis_sync_tb;
  localparam int COLS = 2;
  localparam int TUSER_WIDTH = `TUSER_WIDTH;
  localparam int USER_CONFIG_BIT = 4;

  reg aclk = 1'b0;
  reg [COLS-1:0] weights_m_valid = {COLS{1'b0}};
  reg pixels_m_valid = 1'b0;
  reg [COLS-1:0] m_axis_tready = {COLS{1'b0}};
  reg [COLS*TUSER_WIDTH-1:0] weights_m_user = {(COLS*TUSER_WIDTH){1'b0}};
  reg [COLS-1:0] pixels_m_valid_pipe = {COLS{1'b0}};
  wire [COLS-1:0] m_axis_tvalid;
  wire [COLS-1:0] weights_m_ready;
  wire pixels_m_ready;

  axis_sync #(.COLS(COLS)) dut (
    .aclk                (aclk),
    .weights_m_valid     (weights_m_valid),
    .pixels_m_valid      (pixels_m_valid),
    .m_axis_tready       (m_axis_tready),
    .weights_m_user      (weights_m_user),
    .pixels_m_valid_pipe (pixels_m_valid_pipe),
    .m_axis_tvalid       (m_axis_tvalid),
    .weights_m_ready     (weights_m_ready),
    .pixels_m_ready      (pixels_m_ready)
  );

  always #5 aclk = ~aclk;

  initial begin
    weights_m_valid = 2'b11;
    m_axis_tready = 2'b11;

    weights_m_user[USER_CONFIG_BIT] = 1'b1;
    #1;
    if (weights_m_ready[0] !== 1'b1) $fatal(1, "axis_sync_tb: config beat should not need pixels");
    if (pixels_m_ready !== 1'b0) $fatal(1, "axis_sync_tb: config beat must not consume pixels");

    weights_m_user = {(COLS*TUSER_WIDTH){1'b0}};
    pixels_m_valid_pipe = 2'b00;
    #1;
    if (weights_m_ready[0] !== 1'b0) $fatal(1, "axis_sync_tb: data beat accepted without delayed pixels");

    pixels_m_valid_pipe = 2'b11;
    #1;
    if (weights_m_ready !== 2'b11) $fatal(1, "axis_sync_tb: data beat not accepted with delayed pixels");
    if (pixels_m_ready !== 1'b1) $fatal(1, "axis_sync_tb: pixels should be consumed for data beat");

    $display("axis_sync_tb: PASS");
    $finish;
  end
endmodule
