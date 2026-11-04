`timescale 1ns/1ps

module axis_adapter_any_tb;
  localparam integer S_DATA_WIDTH = 128;
  localparam integer S_KEEP_WIDTH = 32;
  localparam integer M_DATA_WIDTH = 96;
  localparam integer M_KEEP_WIDTH = 24;
  localparam integer INPUT_BEATS = 6;
  localparam integer OUTPUT_BEATS = 8;

  reg clk = 1'b0;
  reg rstn = 1'b0;

  reg [S_DATA_WIDTH-1:0] s_axis_tdata = {S_DATA_WIDTH{1'b0}};
  reg [S_KEEP_WIDTH-1:0] s_axis_tkeep = {S_KEEP_WIDTH{1'b0}};
  reg s_axis_tvalid = 1'b0;
  wire s_axis_tready;
  reg s_axis_tlast = 1'b0;
  reg [3:0] s_axis_tid = 4'ha;
  reg [3:0] s_axis_tdest = 4'h5;
  reg [3:0] s_axis_tuser = 4'h3;

  wire [M_DATA_WIDTH-1:0] m_axis_tdata;
  wire [M_KEEP_WIDTH-1:0] m_axis_tkeep;
  wire m_axis_tvalid;
  reg m_axis_tready = 1'b0;
  wire m_axis_tlast;
  wire [3:0] m_axis_tid;
  wire [3:0] m_axis_tdest;
  wire [3:0] m_axis_tuser;

  reg [S_DATA_WIDTH-1:0] input_data [0:INPUT_BEATS-1];
  reg [M_DATA_WIDTH-1:0] expected_data [0:OUTPUT_BEATS-1];
  reg [INPUT_BEATS*S_DATA_WIDTH-1:0] packed_data;
  integer output_index = 0;
  integer ready_cycle = 0;
  integer accepted_inputs = 0;
  integer stall_mode = 0;
  integer i;

  alex_axis_adapter_any #(
    .S_DATA_WIDTH  (S_DATA_WIDTH),
    .S_KEEP_ENABLE (1),
    .S_KEEP_WIDTH  (S_KEEP_WIDTH),
    .M_DATA_WIDTH  (M_DATA_WIDTH),
    .M_KEEP_ENABLE (1),
    .M_KEEP_WIDTH  (M_KEEP_WIDTH),
    .ID_ENABLE     (1),
    .ID_WIDTH      (4),
    .DEST_ENABLE   (1),
    .DEST_WIDTH    (4),
    .USER_ENABLE   (1),
    .USER_WIDTH    (4)
  ) dut (
    .clk           (clk),
    .rstn          (rstn),
    .s_axis_tdata  (s_axis_tdata),
    .s_axis_tkeep  (s_axis_tkeep),
    .s_axis_tvalid (s_axis_tvalid),
    .s_axis_tready (s_axis_tready),
    .s_axis_tlast  (s_axis_tlast),
    .s_axis_tid    (s_axis_tid),
    .s_axis_tdest  (s_axis_tdest),
    .s_axis_tuser  (s_axis_tuser),
    .m_axis_tdata  (m_axis_tdata),
    .m_axis_tkeep  (m_axis_tkeep),
    .m_axis_tvalid (m_axis_tvalid),
    .m_axis_tready (m_axis_tready),
    .m_axis_tlast  (m_axis_tlast),
    .m_axis_tid    (m_axis_tid),
    .m_axis_tdest  (m_axis_tdest),
    .m_axis_tuser  (m_axis_tuser)
  );

  always #5 clk = ~clk;

  always @(negedge clk) begin
    if (!rstn) begin
      ready_cycle = 0;
      m_axis_tready = 1'b0;
    end else begin
      ready_cycle = ready_cycle + 1;
      if (stall_mode == 0) begin
        m_axis_tready = 1'b1;
      end else begin
        m_axis_tready = ((ready_cycle % 7) != 2) &&
                        ((ready_cycle % 7) != 3) &&
                        ((ready_cycle % 11) != 6);
      end
    end
  end

  always @(posedge clk) begin
    if (rstn && s_axis_tvalid && s_axis_tready) begin
      accepted_inputs = accepted_inputs + 1;
    end

    if (rstn && m_axis_tvalid && m_axis_tready) begin
      if (output_index >= OUTPUT_BEATS) begin
        $fatal(1, "axis_adapter_any_tb: unexpected extra output");
      end
      if (m_axis_tdata !== expected_data[output_index]) begin
        $fatal(1,
          "axis_adapter_any_tb: beat %0d data expected=%024h got=%024h",
          output_index, expected_data[output_index], m_axis_tdata);
      end
      if (m_axis_tkeep !== {M_KEEP_WIDTH{1'b1}}) begin
        $fatal(1, "axis_adapter_any_tb: beat %0d keep=%06h",
          output_index, m_axis_tkeep);
      end
      if (m_axis_tlast !== (output_index == OUTPUT_BEATS-1)) begin
        $fatal(1, "axis_adapter_any_tb: beat %0d last=%0b",
          output_index, m_axis_tlast);
      end
      if (m_axis_tid !== 4'ha || m_axis_tdest !== 4'h5 ||
          m_axis_tuser !== 4'h3) begin
        $fatal(1, "axis_adapter_any_tb: beat %0d metadata mismatch",
          output_index);
      end
      output_index = output_index + 1;
    end
  end

  task send_frame;
    integer beat;
    begin
      @(negedge clk);
      for (beat = 0; beat < INPUT_BEATS; beat = beat + 1) begin
        s_axis_tdata = input_data[beat];
        s_axis_tkeep = {S_KEEP_WIDTH{1'b1}};
        s_axis_tlast = (beat == INPUT_BEATS-1);
        s_axis_tvalid = 1'b1;
        @(posedge clk);
        while (!s_axis_tready) @(posedge clk);
        @(negedge clk);
      end
      s_axis_tvalid = 1'b0;
      s_axis_tlast = 1'b0;
      s_axis_tdata = {S_DATA_WIDTH{1'b0}};
      s_axis_tkeep = {S_KEEP_WIDTH{1'b0}};
    end
  endtask

  task run_case;
    input integer use_stalls;
    integer watchdog;
    begin
      rstn = 1'b0;
      stall_mode = use_stalls;
      output_index = 0;
      accepted_inputs = 0;
      repeat (4) @(posedge clk);
      @(negedge clk);
      rstn = 1'b1;

      send_frame();

      watchdog = 0;
      while (output_index != OUTPUT_BEATS && watchdog < 256) begin
        watchdog = watchdog + 1;
        @(posedge clk);
      end
      if (accepted_inputs != INPUT_BEATS) begin
        $fatal(1, "axis_adapter_any_tb: accepted %0d of %0d inputs",
          accepted_inputs, INPUT_BEATS);
      end
      if (output_index != OUTPUT_BEATS) begin
        $fatal(1, "axis_adapter_any_tb: timed out after %0d outputs",
          output_index);
      end
      repeat (3) @(posedge clk);
    end
  endtask

  initial begin
    input_data[0] = 128'h0123456789abcdef_fedcba9876543210;
    input_data[1] = 128'h13579bdf02468ace_eca86420fdb97531;
    input_data[2] = 128'h89abcdef01234567_76543210fedcba98;
    input_data[3] = 128'h55aa33cc0ff09669_a55ac33cf00f6996;
    input_data[4] = 128'hdeadbeefcafef00d_123456789abcdef0;
    input_data[5] = 128'h0badc0de5eedface_1029384756abcdef;

    packed_data = {INPUT_BEATS*S_DATA_WIDTH{1'b0}};
    for (i = 0; i < INPUT_BEATS; i = i + 1) begin
      packed_data[i*S_DATA_WIDTH +: S_DATA_WIDTH] = input_data[i];
    end
    for (i = 0; i < OUTPUT_BEATS; i = i + 1) begin
      expected_data[i] = packed_data[i*M_DATA_WIDTH +: M_DATA_WIDTH];
    end

    run_case(0);
    run_case(1);

    $display("axis_adapter_any_tb: PASS");
    $finish;
  end
endmodule
