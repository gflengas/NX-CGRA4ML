`timescale 1ns/1ps
`include "defines.svh"

module axis_out_shift #(
  parameter ROWS        = `ROWS,
            COLS        = `COLS,
            KW_MAX      = `KW_MAX,
            WORD_WIDTH  = `Y_BITS,
            Y_OUT_BITS  = `Y_OUT_BITS,
            W_BPT       = `W_BPT,
            TUSER_WIDTH = `TUSER_WIDTH
)(
  input  aclk,
  input  aresetn,

  input  s_valid,
  input  s_last,
  output reg s_ready,
  input  [TUSER_WIDTH-1:0] s_user,
  input  [COLS*ROWS*WORD_WIDTH-1:0] s_data,

  input  m_ready,
  output [ROWS*WORD_WIDTH-1:0] m_data,
  output m_valid,
  output m_last,
  output m_last_pkt,
  output reg [W_BPT-1:0] m_bytes_per_transfer
);

  localparam BITS_KW2          = TUSER_WIDTH - 5;
  localparam USER_W_LAST_BIT   = 0;
  localparam USER_W_FIRST_KW2  = 1;
  localparam USER_W_FIRST_CLK  = 2;
  localparam USER_CIN_LAST_BIT = 3;
  localparam USER_CONFIG_BIT   = 4;
  localparam USER_KW2_LSB      = 5;

  localparam [0:0] IDLE_S  = 1'b0;
  localparam [0:0] SHIFT_S = 1'b1;
  localparam SHIFT_DATA_WIDTH = COLS * ROWS * WORD_WIDTH;

  reg [SHIFT_DATA_WIDTH-1:0] shift_data;
  reg [COLS-1:0] shift_last;
  reg [COLS-1:0] shift_last_pkt;
  reg [COLS-1:0] shift_valid;
  reg [$clog2(COLS+1)-1:0] counter;
  reg state;

  wire [COLS-1:0] lut_valid [0:KW_MAX/2];
  wire [COLS-1:0] lut_valid_last [0:KW_MAX/2];
  wire [COLS-1:0] lut_last_pkt [0:KW_MAX/2];
  wire [COLS-1:0] lut_last [0:KW_MAX/2];
  wire [W_BPT-1:0] lut_bpt_0 [0:KW_MAX/2];
  wire [W_BPT-1:0] lut_bpt_1 [0:KW_MAX/2];

  wire valid_mask;
  wire [BITS_KW2-1:0] kw2_sel;
  wire is_w_last;
  wire [COLS-1:0] s_valid_cols_sel;
  wire [COLS-1:0] s_last_cols_sel;

  assign kw2_sel = s_user[USER_KW2_LSB + BITS_KW2 - 1 : USER_KW2_LSB];
  assign is_w_last = s_user[USER_W_LAST_BIT];
  assign valid_mask = !s_user[USER_W_FIRST_KW2] && !s_user[USER_CONFIG_BIT];
  assign s_valid_cols_sel = is_w_last ? lut_valid_last[kw2_sel] : lut_valid[kw2_sel];
  assign s_last_cols_sel  = is_w_last ? lut_last_pkt[kw2_sel] : lut_last[kw2_sel];

  genvar k2, c_1;
  generate
    for (k2 = 0; k2 <= KW_MAX/2; k2 = k2 + 1) begin : lutk
      localparam integer K = k2*2 + 1;
      for (c_1 = 0; c_1 < COLS; c_1 = c_1 + 1) begin : lutc
        localparam integer C = c_1 + 1;
        assign lut_valid[k2][c_1]      = (C % K == 0);
        assign lut_valid_last[k2][c_1] = (((C % K) > k2) || (C % K == 0)) && (C <= (COLS/K)*K);
        assign lut_last[k2][c_1]       = (C == K);
        assign lut_last_pkt[k2][c_1]   = (C == k2 + 1);
      end
      assign lut_bpt_0[k2] = (ROWS * (COLS/K) * Y_OUT_BITS) / 8;
      assign lut_bpt_1[k2] = (ROWS * (COLS/K) * (k2+1) * Y_OUT_BITS) / 8;
    end
  endgenerate

  always @(posedge aclk `OR_NEGEDGE(aresetn)) begin
    if (!aresetn) begin
      state <= IDLE_S;
      s_ready <= 1'b1;
      m_bytes_per_transfer <= {W_BPT{1'b0}};
      shift_data <= 0;
      shift_valid <= {COLS{1'b0}};
      shift_last <= {COLS{1'b0}};
      shift_last_pkt <= {COLS{1'b0}};
    end else begin
      case (state)
        IDLE_S: begin
          if (s_valid && valid_mask) begin
            state <= SHIFT_S;
            s_ready <= 1'b0;
            shift_data <= s_data;
            shift_valid <= s_valid_cols_sel & {COLS{valid_mask}};
            shift_last <= s_last_cols_sel;
            shift_last_pkt <= {COLS{s_last}} & lut_last_pkt[kw2_sel];
            if (is_w_last) begin
              m_bytes_per_transfer <= lut_bpt_1[kw2_sel];
            end else begin
              m_bytes_per_transfer <= lut_bpt_0[kw2_sel];
            end
          end
        end
        SHIFT_S: begin
          if (m_ready) begin
            shift_data <= shift_data << (ROWS * WORD_WIDTH);
            shift_valid <= shift_valid << 1;
            shift_last <= shift_last << 1;
            shift_last_pkt <= shift_last_pkt << 1;

            if (counter == 1) begin
              state <= IDLE_S;
              s_ready <= 1'b1;
            end
          end
        end
      endcase
    end
  end

  always @(posedge aclk `OR_NEGEDGE(aresetn)) begin
    if (!aresetn) begin
      counter <= COLS;
    end else if (state == SHIFT_S && m_ready) begin
      if (counter == 1) begin
        counter <= COLS;
      end else begin
        counter <= counter - 1'b1;
      end
    end
  end

  assign m_data = shift_data[SHIFT_DATA_WIDTH-1 : SHIFT_DATA_WIDTH-ROWS*WORD_WIDTH];
  assign m_valid = shift_valid[COLS-1];
  assign m_last = shift_last[COLS-1];
  assign m_last_pkt = shift_last_pkt[COLS-1];

endmodule
