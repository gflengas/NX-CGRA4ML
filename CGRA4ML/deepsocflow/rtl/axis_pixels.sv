`timescale 1ns/1ps
`include "defines.svh"

/* verilator lint_off WIDTHTRUNC */

module axis_pixels #(
  parameter   ROWS               = `ROWS               ,
              KH_MAX             = `KH_MAX             ,
              CI_MAX             = `CI_MAX             ,
              XW_MAX             = `XW_MAX             ,
              XH_MAX             = `XH_MAX             ,
              WORD_WIDTH         = `X_BITS             ,
              RAM_EDGES_DEPTH    = `RAM_EDGES_DEPTH    , 
              AXI_WIDTH          = `AXI_WIDTH          ,
              HEADER_WIDTH       = `HEADER_WIDTH       ,

  parameter  EDGE_WORDS         =  KH_MAX/2              ,
              IM_SHIFT_REGS      =  ROWS + KH_MAX-1       ,
              BITS_KH            = $clog2(KH_MAX         ),
              BITS_KH2           = $clog2((KH_MAX+1)/2   ),
              BITS_CI            = $clog2(CI_MAX)         ,
              BITS_XW            = $clog2(XW_MAX)         ,
              BITS_IM_BLOCKS     = $clog2(XH_MAX/ROWS) 
  )(
    input  aclk,
    input  aresetn,

    output reg s_ready,
    input  s_valid,
    input  s_last,
    input  [AXI_WIDTH-1:0] s_data,
    input  [AXI_WIDTH/WORD_WIDTH-1:0] s_keep,
    input  [HEADER_WIDTH:0] s_user,

    input  m_ready,
    output reg m_valid,
    output [ROWS*WORD_WIDTH-1:0] m_data
  );

  localparam [1:0] SET_S   = 2'd0;
  localparam [1:0] PASS_S  = 2'd1;
  localparam [1:0] BLOCK_S = 2'd2;
  localparam USER_FIRST_P_BIT = 0;
  localparam USER_KH2_LSB     = USER_FIRST_P_BIT + 1;
  localparam USER_W_LSB       = USER_KH2_LSB + BITS_KH2;
  localparam USER_L_LSB       = USER_W_LSB + BITS_XW;
  localparam USER_CI_P0_LSB   = USER_L_LSB + BITS_IM_BLOCKS;
  localparam USER_CI_P_LSB    = USER_CI_P0_LSB + BITS_CI;
  localparam BITS_RAM_ADDR    = $clog2(RAM_EDGES_DEPTH);
  localparam [BITS_KH2-1:0] EDGE_WORDS_CONST = EDGE_WORDS;

  wire dw_ro_s_ready, dw_ro_m_valid, dw_ro_m_last;
  wire dw_re_s_ready, dw_re_m_valid, dw_re_m_last;
  reg  dw_ro_s_valid, dw_ro_m_ready;
  reg  dw_re_s_valid, dw_re_m_ready;
  reg  i_ready, i_valid, i_last;
  reg  dw_m_valid_r, dw_m_last_r;
  wire [ROWS*WORD_WIDTH-1:0] dw_ro_m_data;
  wire [ROWS+EDGE_WORDS-1:0] dw_re_m_keep_unused;
  wire [ROWS-1:0] dw_ro_m_keep_unused;
  wire [7:0] dw_re_m_tid_unused, dw_re_m_tdest_unused;
  wire [7:0] dw_ro_m_tid_unused, dw_ro_m_tdest_unused;
  wire dw_re_m_tuser_unused, dw_ro_m_tuser_unused;
  wire [BITS_KH-1:0] kh_count_unused;
  wire [BITS_CI-1:0] ci_count_unused;
  wire [BITS_XW-1:0] w_count_unused;
  wire [BITS_IM_BLOCKS-1:0] l_count_unused;
  wire ci_last_unused, w_last_unused, l_last_clk_unused;
  wire kh_first_unused, ci_first_unused, w_first_unused;
  reg  [(ROWS+EDGE_WORDS)*WORD_WIDTH-1:0] i_data;
  wire [(ROWS+EDGE_WORDS)*WORD_WIDTH-1:0] dw_re_m_data;
  reg  [(ROWS+EDGE_WORDS)*WORD_WIDTH-1:0] dw_m_data_r;
  reg  [1:0] state;

  reg  en_config, en_shift, en_copy, en_kh;
  reg  en_copy_r, last_kh_r, last_clk_kh_r, last_l_r, m_last_reg, first_l_r;
  wire last_kh, last_clk_kh, last_clk_ci, last_clk_w, last_l, first_l, m_last, first_p;
  reg  [BITS_KH2-1:0] ref_kh2;
  wire [BITS_KH2-1:0] ref_kh2_in, ref_kh2_in_bounded;
  wire [BITS_CI-1:0] ref_ci_in, ref_ci_p0_in, ref_ci_p_in;
  wire [BITS_XW-1:0] ref_w_in;
  wire [BITS_IM_BLOCKS-1:0] ref_l_in;
  wire [BITS_KH-1:0] ref_kh_limit;

  reg  [BITS_RAM_ADDR-1:0] ram_addr, ram_addr_r;
  wire [BITS_RAM_ADDR-1:0] ram_addr_in;
  wire [EDGE_WORDS*WORD_WIDTH-1:0] ram_dout;
  reg  [EDGE_WORDS*WORD_WIDTH-1:0] ram_dout_hold;
  wire [EDGE_WORDS*WORD_WIDTH-1:0] ram_dout_r;
  wire [EDGE_WORDS*WORD_WIDTH-1:0] edge_top_r;
  wire [EDGE_WORDS*WORD_WIDTH-1:0] edge_bot_r;
  reg  ram_ren_reg;

  reg  [IM_SHIFT_REGS*WORD_WIDTH-1:0] shift_reg;
  wire [BITS_KH2-1:0] m_data_shift_words;

  function automatic [ROWS*WORD_WIDTH-1:0] select_shifted_rows;
    input [IM_SHIFT_REGS*WORD_WIDTH-1:0] value;
    input [BITS_KH2-1:0] words;
    begin
      select_shifted_rows = value[WORD_WIDTH * words +: ROWS*WORD_WIDTH];
    end
  endfunction

  alex_axis_adapter_any #(
    .S_DATA_WIDTH  (AXI_WIDTH),
    .M_DATA_WIDTH  (WORD_WIDTH*(ROWS+EDGE_WORDS)),
    .S_KEEP_ENABLE (1),
    .M_KEEP_ENABLE (1),
    .S_KEEP_WIDTH  (AXI_WIDTH/WORD_WIDTH),
    .M_KEEP_WIDTH  ((ROWS+EDGE_WORDS)),
    .ID_ENABLE     (0),
    .DEST_ENABLE   (0),
    .USER_ENABLE   (0)
    ) DW_ROWS_EDGES (
    .clk           (aclk       ),
    .rstn          (aresetn    ),
    .s_axis_tdata  (s_data     ),
    .s_axis_tkeep  (s_keep     ),
    .s_axis_tvalid (dw_re_s_valid ),
    .s_axis_tlast  (s_last     ),
    .s_axis_tready (dw_re_s_ready),
    .s_axis_tid    (8'd0),
    .s_axis_tdest  (8'd0),
    .s_axis_tuser  (1'b0),
    .m_axis_tdata  (dw_re_m_data  ),
    .m_axis_tready (dw_re_m_ready ),
    .m_axis_tvalid (dw_re_m_valid ),
    .m_axis_tlast  (dw_re_m_last  ),
    .m_axis_tid    (dw_re_m_tid_unused),
    .m_axis_tdest  (dw_re_m_tdest_unused),
    .m_axis_tkeep  (dw_re_m_keep_unused),
    .m_axis_tuser  (dw_re_m_tuser_unused)
  );

  alex_axis_adapter_any #(
    .S_DATA_WIDTH  (AXI_WIDTH),
    .M_DATA_WIDTH  (WORD_WIDTH*ROWS),
    .S_KEEP_ENABLE (1),
    .M_KEEP_ENABLE (1),
    .S_KEEP_WIDTH  (AXI_WIDTH/WORD_WIDTH),
    .M_KEEP_WIDTH  (ROWS),
    .ID_ENABLE     (0),
    .DEST_ENABLE   (0),
    .USER_ENABLE   (0)
    ) DW_ROWS (
    .clk           (aclk       ),
    .rstn          (aresetn    ),
    .s_axis_tdata  (s_data     ),
    .s_axis_tkeep  (s_keep     ),
    .s_axis_tvalid (dw_ro_s_valid) ,
    .s_axis_tlast  (s_last     ),
    .s_axis_tready (dw_ro_s_ready),
    .s_axis_tid    (8'd0),
    .s_axis_tdest  (8'd0),
    .s_axis_tuser  (1'b0),
    .m_axis_tdata  (dw_ro_m_data  ),
    .m_axis_tready (dw_ro_m_ready ),
    .m_axis_tvalid (dw_ro_m_valid ),
    .m_axis_tlast  (dw_ro_m_last  ),
    .m_axis_tid    (dw_ro_m_tid_unused),
    .m_axis_tdest  (dw_ro_m_tdest_unused),
    .m_axis_tkeep  (dw_ro_m_keep_unused),
    .m_axis_tuser  (dw_ro_m_tuser_unused)
  );

  assign first_p     = s_user[USER_FIRST_P_BIT];
  assign ref_kh2_in  = s_user[USER_KH2_LSB + BITS_KH2 - 1 : USER_KH2_LSB];
  assign ref_w_in    = s_user[USER_W_LSB + BITS_XW - 1 : USER_W_LSB];
  assign ref_l_in    = s_user[USER_L_LSB + BITS_IM_BLOCKS - 1 : USER_L_LSB];
  assign ref_ci_p0_in = s_user[USER_CI_P0_LSB + BITS_CI - 1 : USER_CI_P0_LSB];
  assign ref_ci_p_in  = s_user[USER_CI_P_LSB + BITS_CI - 1 : USER_CI_P_LSB];
  assign ref_ci_in = first_p ? ref_ci_p0_in : ref_ci_p_in;
  assign ref_kh_limit = {ref_kh2_in, 1'b0};

  wire s_last_beat    = s_valid    && s_ready    && s_last;
  wire m_last_beat    = m_ready    && m_valid    && m_last;

  always @(posedge aclk `OR_NEGEDGE(aresetn))
    if (!aresetn)                      state <= SET_S;
    else case (state)
      SET_S   : if (s_valid)             state <= PASS_S;
      PASS_S  : if (s_last_beat)
                  if (m_last_beat)       state <= SET_S;
                  else                   state <= BLOCK_S;
      BLOCK_S : if (m_last_beat)         state <= SET_S;
      default :                          state <= SET_S;
    endcase

  always @(*) begin
    en_config  = (state == SET_S);
    en_kh      = (state == SET_S) ? 1'b0 : (m_ready && (last_kh ? (i_valid | m_last_reg | dw_m_last_r) : 1'b1));
    en_copy    = (state == SET_S) ? 1'b0 : (i_valid && last_clk_kh && m_ready);
    en_shift   = (state == SET_S) ? 1'b0 : m_ready;
  end

  always @(*) begin
    if (state == SET_S) begin
      s_ready       = 1'b0;
      dw_re_s_valid = 1'b0;
      i_ready       = 1'b0;
      i_data        = {((ROWS+EDGE_WORDS)*WORD_WIDTH){1'b0}};
      i_valid       = 1'b0;
      i_last        = 1'b0;
      dw_re_m_ready = 1'b0;
      dw_ro_m_ready = 1'b0;
      dw_ro_s_valid = 1'b0;
    end else begin
      i_ready       = en_copy;
      i_data        = (ref_kh2 == 0) ? {{(EDGE_WORDS*WORD_WIDTH){1'b0}}, dw_ro_m_data} : dw_re_m_data;
      i_valid       = (ref_kh2 == 0) ? dw_ro_m_valid : dw_re_m_valid;
      i_last        = ref_kh2 == 0 ? dw_ro_m_last : dw_re_m_last;

      dw_ro_m_ready = (ref_kh2 == 0) ? i_ready : 1'b0;
      dw_re_m_ready = (ref_kh2 == 0) ? 1'b0 : i_ready;

      if (state == PASS_S) begin
        s_ready       = ref_kh2 == 0 ? dw_ro_s_ready : dw_re_s_ready;
        dw_ro_s_valid = (ref_kh2 == 0) ? s_valid : 1'b0;
        dw_re_s_valid = (ref_kh2 == 0) ? 1'b0 : s_valid;
      end else begin
        s_ready       = 1'b0;
        dw_re_s_valid = 1'b0;
        dw_ro_s_valid = 1'b0;
      end
    end
  end

  counter #(.W(BITS_KH)       ) C_KH (.clk(aclk), .rstn_g(aresetn), .rst_l(en_config), .en(en_kh      ), .max_in(ref_kh_limit ), .last_clk(last_clk_kh ), .last(last_kh),        .first(kh_first_unused), .count(kh_count_unused));
  counter #(.W(BITS_CI)       ) C_CI (.clk(aclk), .rstn_g(aresetn), .rst_l(en_config), .en(last_clk_kh), .max_in(ref_ci_in    ), .last_clk(last_clk_ci ), .last(ci_last_unused), .first(ci_first_unused), .count(ci_count_unused));
  counter #(.W(BITS_XW)       ) C_W  (.clk(aclk), .rstn_g(aresetn), .rst_l(en_config), .en(last_clk_ci), .max_in(ref_w_in     ), .last_clk(last_clk_w  ), .last(w_last_unused),  .first(w_first_unused),  .count(w_count_unused));
  counter #(.W(BITS_IM_BLOCKS)) C_L  (.clk(aclk), .rstn_g(aresetn), .rst_l(en_config), .en(last_clk_w ), .max_in(ref_l_in     ), .last_clk(l_last_clk_unused), .last(last_l), .first(first_l), .count(l_count_unused));

  always @(posedge aclk `OR_NEGEDGE(aresetn))
    if (!aresetn)                     ram_addr <= 0;
    else if (en_config || last_clk_w) ram_addr <= 0;
    else if (en_copy)                 ram_addr <= ram_addr + 1;

  wire ram_ren = en_copy   && !first_l;
  wire ram_wen = en_copy_r && !last_l_r;
  assign ram_addr_in = ram_wen ? ram_addr_r : ram_addr;

  ram_edges #(
    .DEPTH (RAM_EDGES_DEPTH),
    .WIDTH (EDGE_WORDS*WORD_WIDTH)
  ) RAM (
    .clk  (aclk),    
    .en   (ram_ren || ram_wen),     
    .we  (ram_wen),     
    .addr (ram_addr_in),  
    .di  (edge_bot_r),
    .dout (ram_dout)
  );

  always @(posedge aclk `OR_NEGEDGE(aresetn))
    if (!aresetn) begin
      ram_ren_reg   <= 1'b0;
      ram_dout_hold <= {(EDGE_WORDS*WORD_WIDTH){1'b0}};
    end
    else begin
      ram_ren_reg <= ram_ren;
      if (ram_ren_reg) ram_dout_hold <= ram_dout;
    end

  assign ram_dout_r = ram_ren_reg ? ram_dout : ram_dout_hold;

  always @(posedge aclk `OR_NEGEDGE(aresetn))
    if (!aresetn) begin
      first_l_r     <= 1'b0;
      last_l_r      <= 1'b0;
      last_clk_kh_r <= 1'b0;
      last_kh_r     <= 1'b0;
      en_copy_r     <= 1'b0;
      ram_addr_r    <= {BITS_RAM_ADDR{1'b0}};
      dw_m_data_r   <= {((ROWS+EDGE_WORDS)*WORD_WIDTH){1'b0}};
      dw_m_last_r   <= 1'b0;
      dw_m_valid_r  <= 1'b0;
    end
    else if (en_shift) begin // m_ready
      first_l_r     <= first_l;
      last_l_r      <= last_l;
      last_clk_kh_r <= last_clk_kh;
      last_kh_r     <= last_kh;
      en_copy_r     <= en_copy;
      ram_addr_r    <= ram_addr;
      dw_m_data_r   <= i_data;
      dw_m_last_r   <= i_last;
      dw_m_valid_r  <= i_valid;
    end
  assign edge_top_r = first_l_r ? {(EDGE_WORDS*WORD_WIDTH){1'b0}} : ram_dout_r;
  assign edge_bot_r = dw_m_data_r[WORD_WIDTH*ROWS-1:WORD_WIDTH*(ROWS-EDGE_WORDS)];

  always @(posedge aclk `OR_NEGEDGE(aresetn))
    if (!aresetn) begin
      shift_reg <= {(IM_SHIFT_REGS*WORD_WIDTH){1'b0}};
    end else if (en_copy_r && en_shift) begin
      shift_reg <= {dw_m_data_r, edge_top_r};
    end else if (en_shift) begin
      shift_reg <= {{WORD_WIDTH{1'b0}}, shift_reg[IM_SHIFT_REGS*WORD_WIDTH-1:WORD_WIDTH]};
    end

  assign ref_kh2_in_bounded = (ref_kh2_in < EDGE_WORDS_CONST) ? ref_kh2_in : {BITS_KH2{1'b0}};
  always @(posedge aclk `OR_NEGEDGE(aresetn))
    if (!aresetn)       ref_kh2 <= {BITS_KH2{1'b0}};
    else if (en_config) ref_kh2 <= ref_kh2_in_bounded;

  assign m_data_shift_words = EDGE_WORDS_CONST - ref_kh2;
  assign m_data = select_shifted_rows(shift_reg, m_data_shift_words);

  always @(posedge aclk `OR_NEGEDGE(aresetn))
    if (!aresetn) begin
      m_valid     <= 1'b0;
      m_last_reg  <= 1'b0;
    end else if (m_last_beat) begin
      m_valid     <= 1'b0;
      m_last_reg  <= 1'b0;
    end
    else begin 
      if (en_copy_r && en_shift) m_last_reg <= dw_m_last_r;
      if (last_kh_r && en_shift) m_valid    <= dw_m_valid_r;
    end

  assign m_last = m_last_reg && last_clk_kh_r;

endmodule

/* verilator lint_on WIDTHTRUNC */
