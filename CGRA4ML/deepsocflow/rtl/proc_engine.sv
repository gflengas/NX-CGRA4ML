`timescale 1ns/1ps
`include "defines.svh"

module proc_engine #(
  parameter   COLS                = `COLS                ,
              ROWS                = `ROWS                ,
              X_BITS              = `X_BITS              ,
              K_BITS              = `K_BITS              ,
              Y_BITS              = `Y_BITS              ,
              DELAY_MUL           = `DELAY_MUL           ,
              KW_MAX              = `KW_MAX              ,
              TUSER_WIDTH         = `TUSER_WIDTH         ,
              M_BITS              = X_BITS + K_BITS      ,
              WORD_WIDTH           = `Y_BITS             ,
              Y_OUT_BITS           = `Y_OUT_BITS         ,
              W_BPT                = `W_BPT              ,
              BITS_COLS           = $clog2(COLS) 
)(
  input  clk, resetn,

  output [COLS-1:0] s_ready,
  input  [COLS-1:0] s_valid, s_last,
  input  [ROWS-1:0][X_BITS-1:0] s_data_pixels,
  input  [COLS-1:0][K_BITS-1:0] s_data_weights,
  input  [COLS*TUSER_WIDTH-1:0] s_user,
  input  pixels_m_valid,
  output [COLS-1:0] pixels_m_valid_pipe,

  //input  logic m_ready,
  //output logic m_valid, m_last,
  //output logic [COLS-1:0][ROWS-1:0][Y_BITS-1:0] m_data,
  //output tuser_st m_user,
 
  input  m_ready,
  output [ROWS -1:0][WORD_WIDTH  -1:0] m_data,
  output m_valid, m_last, m_last_pkt,
  output reg [W_BPT-1:0] m_bytes_per_transfer
);

  reg  [COLS-1:0] pixels_m_valid_pipe_reg;
  wire [COLS-1:0] pixels_m_valid_pipe_int;
  wire [COLS-1:0] en;
  wire [COLS-1:0] acc_m_valid_next;
  reg  [COLS-1:0] acc_m_valid;
  wire [COLS-1:0] mac_freeze;
  wire [COLS-1:0] clken_mul;
  wire [COLS-1:0] sel_shift_next, mul_m_valid, mul_m_last;
  reg  [COLS-1:0] sel_shift;
  reg  [COLS-1:0] acc_m_last;
  wire [COLS*TUSER_WIDTH-1:0] mul_m_user;
  reg  [COLS*TUSER_WIDTH-1:0] acc_m_user;
  wire [COLS-1:0] clken_acc, bypass_sum_next, bypass, acc_m_sum_start, acc_s_valid;
  reg  [COLS-1:0] bypass_sum;
  wire [COLS-1:0] lut_sum_start [0:KW_MAX/2];
  wire [ROWS*M_BITS-1:0] mul_m_data [0:COLS-1];
  reg  [ROWS*Y_BITS-1:0] acc_m_data [0:COLS-1];

  reg  [COLS-1:0] shift_out_ready;
  
  reg  [COLS*ROWS*WORD_WIDTH-1:0] shift_data_out;
  wire [W_BPT-1:0] lut_bpt_0 [0:KW_MAX/2];
  wire [W_BPT-1:0] lut_bpt_1 [0:KW_MAX/2];
  wire [COLS-1:0] lut_valid [0:KW_MAX/2];
  wire [COLS-1:0] lut_valid_last [0:KW_MAX/2];
  wire [COLS-1:0] lut_last_pkt [0:KW_MAX/2];
  wire [COLS-1:0] lut_last [0:KW_MAX/2];
  reg  [COLS-1:0] shift_last, shift_last_pkt, shift_valid;

  wire [COLS-1:0] valid_mask; 
  wire [COLS-1:0] s_valid_cols_sel; 
  wire [COLS-1:0] s_last_cols_sel;  

  wire [COLS-1:0] en_outshift, sel_outshift;
  reg  [COLS-1:0] outshift_flag;
  reg  shift_out_ready_last_col_prev;

  wire [COLS-1:0] s_axis_tvalid;

  localparam BITS_KW2_L         = TUSER_WIDTH - 5;
  localparam USER_W_LAST_BIT    = 0;
  localparam USER_W_FIRST_KW2   = 1;
  localparam USER_W_FIRST_CLK   = 2;
  localparam USER_CIN_LAST_BIT  = 3;
  localparam USER_CONFIG_BIT    = 4;
  localparam USER_KW2_LSB       = 5;

  genvar k2, c_1;
  genvar co;
  generate
  for (k2=0; k2 <= KW_MAX/2; k2 = k2 + 1) begin : lut_k
    for (c_1=0; c_1 <  COLS; c_1 = c_1 + 1) begin : lut_c
      assign lut_valid      [k2][c_1] = (((c_1 + 1) % (k2*2 + 1)) == 0);
      assign lut_valid_last [k2][c_1] = ((((c_1 + 1) % (k2*2 + 1)) > k2) || (((c_1 + 1) % (k2*2 + 1)) == 0)) && ((c_1 + 1) <= (COLS/(k2*2 + 1))*(k2*2 + 1));
      assign lut_last       [k2][c_1] = ((c_1 + 1) == (k2*2 + 1));
      assign lut_last_pkt   [k2][c_1] = ((c_1 + 1) == (k2 + 1));
    end
    assign lut_bpt_0[k2] = (ROWS * (COLS/(k2*2 + 1)) * 1      * Y_OUT_BITS) / 8;
    assign lut_bpt_1[k2] = (ROWS * (COLS/(k2*2 + 1)) * (k2+1) * Y_OUT_BITS) / 8;
  end
  for (c_1=0; c_1 < COLS; c_1 = c_1 + 1) begin : val_mask
    assign valid_mask[c_1] = !acc_m_user[c_1*TUSER_WIDTH + USER_W_FIRST_KW2] && !acc_m_user[c_1*TUSER_WIDTH + USER_CONFIG_BIT];
    assign s_valid_cols_sel[c_1] = acc_m_user[c_1*TUSER_WIDTH + USER_W_LAST_BIT] ?
        lut_valid_last[acc_m_user[c_1*TUSER_WIDTH + USER_KW2_LSB + BITS_KW2_L - 1 : c_1*TUSER_WIDTH + USER_KW2_LSB]][c_1] :
        lut_valid     [acc_m_user[c_1*TUSER_WIDTH + USER_KW2_LSB + BITS_KW2_L - 1 : c_1*TUSER_WIDTH + USER_KW2_LSB]][c_1];
    assign s_last_cols_sel[c_1]  = acc_m_user[c_1*TUSER_WIDTH + USER_W_LAST_BIT] ?
        lut_last_pkt [acc_m_user[c_1*TUSER_WIDTH + USER_KW2_LSB + BITS_KW2_L - 1 : c_1*TUSER_WIDTH + USER_KW2_LSB]][c_1] :
        lut_last     [acc_m_user[c_1*TUSER_WIDTH + USER_KW2_LSB + BITS_KW2_L - 1 : c_1*TUSER_WIDTH + USER_KW2_LSB]][c_1];
  end
  endgenerate
  
  assign s_ready = clken_mul;

  // pixel_valid_pipe_int[i] indicates whether column i has a valid pixel or not.
  // Keep this internal to avoid reading from an output port in generated logic.
  assign pixels_m_valid_pipe = pixels_m_valid_pipe_int;
  assign pixels_m_valid_pipe_int[0] = pixels_m_valid;

  generate
  genvar i;
  for (i=1; i<COLS; i = i + 1) begin : pixel_valid_pipe
    // if prev column is not ready, current column pixel valid will either i) be set to 0 or ii) hold its current value, depending on s_ready[i].
    always @(posedge clk) begin
      pixels_m_valid_pipe_reg[i] <= (s_ready[i-1]) ? pixels_m_valid_pipe_int[i-1] : (s_ready[i]) ? 1'b0 : pixels_m_valid_pipe_int[i];
    end
    assign pixels_m_valid_pipe_int[i] = pixels_m_valid_pipe_reg[i];
  end
  endgenerate

  generate
  for (i=0; i<COLS; i = i + 1) begin : valid_for_mac
    //assign weights_m_ready[i] = s_ready[i]   && (pixels_m_valid_pipe_int[i] || s_user[i].is_config);
    // s_valid is valid from weights_rotator.  it is ANDed with pixels_valid to get the combined valid signal to send to the MAC.
    assign s_axis_tvalid[i]   = s_valid[i] && (pixels_m_valid_pipe_int[i] || s_user[i*TUSER_WIDTH + USER_CONFIG_BIT]);
  end
  endgenerate

  generate
    genvar r,c,kw2,d;
    for(c=0; c<COLS; c = c + 1) begin : selshift
      n_delay #(.N(DELAY_MUL), .W(TUSER_WIDTH+2)) MUL_CONTROL (.c(clk), .rng(resetn), .rnl(1'b1), .e(clken_mul[c]), .i({s_axis_tvalid[c], s_last[c], s_user[c*TUSER_WIDTH + TUSER_WIDTH - 1 : c*TUSER_WIDTH]}), .o ({mul_m_valid[c], mul_m_last[c], mul_m_user[c*TUSER_WIDTH + TUSER_WIDTH - 1 : c*TUSER_WIDTH]}));
      
      assign sel_shift_next[c] = mul_m_valid[c] && mul_m_user[c*TUSER_WIDTH + USER_CIN_LAST_BIT] && (mul_m_user[c*TUSER_WIDTH + USER_KW2_LSB + BITS_KW2_L - 1 : c*TUSER_WIDTH + USER_KW2_LSB] != 0);

      always @(posedge clk `OR_NEGEDGE(resetn))
        if (!resetn) sel_shift[c] <= 0;
        else if (en[c]) sel_shift[c] <= sel_shift_next[c];
      //assign sel_shift[c] = sel_shift_next[c];

      assign clken_mul[c] = en[c]  && !sel_shift[c];
    end        

    for (c=0; c < COLS; c = c + 1) begin: Cg

      // Lookup table
      for (kw2=0; kw2 <= KW_MAX/2; kw2 = kw2 + 1) begin : lutsumst
        assign lut_sum_start[kw2][c] = c % (kw2*2+1) == 0; // c % 3 < 1 : 0,1
      end
      
      assign acc_m_sum_start [c] = lut_sum_start[acc_m_user[c*TUSER_WIDTH + USER_KW2_LSB + BITS_KW2_L - 1 : c*TUSER_WIDTH + USER_KW2_LSB]][c];
      assign acc_s_valid     [c] = sel_shift[c] ? ~acc_m_sum_start [c] : mul_m_valid[c];
      assign clken_acc       [c] = en[c]    && acc_s_valid [c];

      assign bypass_sum_next [c] = mul_m_user[c*TUSER_WIDTH + USER_CIN_LAST_BIT] || mul_m_user[c*TUSER_WIDTH + USER_CONFIG_BIT];

      always @(posedge clk `OR_NEGEDGE(resetn))
        if (!resetn)            bypass_sum [c] <= 0;
        else if (clken_acc [c]) bypass_sum [c] <= bypass_sum_next [c];

      assign bypass    [c] = bypass_sum [c] || mul_m_user[c*TUSER_WIDTH + USER_W_FIRST_CLK]; // clears all partial sums for every first col

    end

    // PE ARRAY: ROWS * COLS
    for (r=0; r < ROWS ; r = r + 1) begin: Rg
      // Kept in the fabric: absorbed into the DSPs they make DSP-to-DSP
      // paths Impulse 24.3 cannot characterize.
      (*synthesis,syn_preserve="true"*) reg [COLS*X_BITS-1:0] pixels_reg;

      for (c=0; c < COLS   ; c = c + 1) begin: Cg
        // --------------- PROCESSING ELEMENT ------------------
        localparam integer PIX_MSB   = ((c + 1) * X_BITS) - 1;
        localparam integer PIX_LSB   = c * X_BITS;
        localparam integer MUL_MSB   = ((r + 1) * M_BITS) - 1;
        localparam integer MUL_LSB   = r * M_BITS;
        localparam integer Y_MSB     = ((r + 1) * Y_BITS) - 1;
        localparam integer Y_LSB     = r * Y_BITS;

        // Pipeline DSP input
        //logic [X_BITS-1:0] pixels_reg; changed to pipeline
        (*synthesis,syn_preserve="true"*) reg [K_BITS-1:0] weights_reg;
        reg [Y_BITS-1:0] shift_data_reg;

        always @ (posedge clk `OR_NEGEDGE(resetn))
          if (!resetn) begin
            pixels_reg[PIX_MSB:PIX_LSB] <= 0;
          end
          else if (clken_mul[c]) begin
            if (c == 0) pixels_reg[PIX_MSB:PIX_LSB] <= s_data_pixels[r];
            else        pixels_reg[PIX_MSB:PIX_LSB] <= pixels_reg[(c*X_BITS)-1:(c-1)*X_BITS];
          end

        always @ (posedge clk `OR_NEGEDGE(resetn))
          if (!resetn) begin        
            weights_reg <= 0;
          end
          else if (clken_mul[c]) begin
            //{pixels_reg[0], weights_reg} <= {s_data_pixels[r], s_data_weights[c]}; // move this to outside the for loop?
            weights_reg <= s_data_weights[c];  
          end
        // Multiplier
        wire [M_BITS-1:0] mul_comb = $signed(pixels_reg[PIX_MSB:PIX_LSB]) * $signed(weights_reg);

        n_delay #(.N(DELAY_MUL-1), .W(M_BITS)) MUL_PIPE (.c(clk), .rng(resetn), .rnl(1'b1), .e(clken_mul[c]), .i(mul_comb), .o (mul_m_data[c][MUL_MSB:MUL_LSB]));
        
        // changed shift_data to FF instead of wire, so that it has previous cycle data. 
        // shift_data[i] is loaded with acc_m_data[i-1] when acc_m_valid[i-1] is high. 
        // This is because the column i will get sel_shift one cycle after column i-1.
        always @ (posedge clk `OR_NEGEDGE(resetn)) begin
          if(!resetn) shift_data_reg <= 0;
          else if (c == 0) shift_data_reg <= 0;
          else if (acc_m_valid[c-1]) shift_data_reg <= acc_m_data[c-1][Y_MSB:Y_LSB];
        end
        //if (c == 0) assign shift_data [c][r] = '0;
        //else        assign shift_data [c][r] = acc_m_data [c-1][r];

        // Two muxes
        wire signed [Y_BITS -1:0] mul_m_data_ext = $signed({{(Y_BITS-M_BITS){mul_m_data[c][MUL_MSB]}}, mul_m_data[c][MUL_MSB:MUL_LSB]});
        wire signed [Y_BITS -1:0] add_in_1 = sel_shift[c] ? shift_data_reg : mul_m_data_ext;
        wire signed [Y_BITS -1:0] add_in_2 = bypass[c] ? 0 : acc_m_data[c][Y_MSB:Y_LSB];

        // Accumulator
        always @(posedge clk `OR_NEGEDGE(resetn))
          if (!resetn)           acc_m_data[c][Y_MSB:Y_LSB] <= 0;
          else if (clken_acc[c]) acc_m_data[c][Y_MSB:Y_LSB] <= add_in_1 + add_in_2;
        
        // --------------- PROCESSING ELEMENT ------------------
      end 
    end
    
    // -------------- OUTPUT SHIFTER ----------------
    
    // counter value is used as pointer to which is the last column to be shifted out
    //counter #(.W(BITS_COLS)) C_OUTSHIFT (.clk(clk), .rstn_g(1'b1), .rst_l(~resetn), .en(cnt_en), .max_in(COLS-1), .last_clk(), .last(), .first(), .count(count_outshift));
    
    //assign cnt_en = m_ready & ~shift_out_ready[COLS-1] & (outshift_flag[COLS-1-count_outshift] == 1);

    always @(posedge clk `OR_NEGEDGE(resetn)) begin
        if (!resetn) begin 
          outshift_flag <= 0;
        end
        else begin
          shift_out_ready_last_col_prev <= shift_out_ready[COLS-1];

          if(shift_out_ready_last_col_prev & ~shift_out_ready[COLS-1]) begin // when last col of out shifter gets data from acc, it is ready to start shifting.
            outshift_flag <= {COLS{1'b1}};
          end
          else begin
          //else if ((count_outshift == (COLS-1)-co) && en_outshift[co]) begin
          if (m_ready && ~shift_out_ready[COLS-1]) outshift_flag <= (outshift_flag << 1);
          end
        end
      end
    
    for (co=0; co<COLS-1; co=co+1) begin : outshift_sel
      assign sel_outshift[co] = ~(acc_m_valid[co] & valid_mask[co] & shift_out_ready[co] & shift_out_ready[co+1]);
    end
    assign sel_outshift[COLS-1] = ~(acc_m_valid[COLS-1] & valid_mask[COLS-1] & shift_out_ready[COLS-1]);

    for (co=0; co<COLS; co=co+1) begin : outshift_en
      assign en_outshift[co] = (m_ready & outshift_flag[co]) | ~sel_outshift[co];
    end

    begin : outshift_col0
      localparam integer SHIFT_DATA_LSB = 0;
      localparam integer SHIFT_DATA_MSB = ROWS * WORD_WIDTH - 1;
      localparam integer SHIFT_DATA_W   = ROWS * WORD_WIDTH;

      always @(posedge clk `OR_NEGEDGE(resetn)) begin
        if (!resetn) begin
          shift_out_ready[0] <= 1'b1;
          shift_data_out[SHIFT_DATA_MSB:SHIFT_DATA_LSB] <= {SHIFT_DATA_W{1'b0}};
          {shift_valid[0], shift_last[0], shift_last_pkt[0]} <= 0;
        end else if (en_outshift[0]) begin
          shift_data_out[SHIFT_DATA_MSB:SHIFT_DATA_LSB] <= (sel_outshift[0]) ? shift_data_out[SHIFT_DATA_MSB:SHIFT_DATA_LSB] : acc_m_data[0];
          shift_last_pkt[0]  <= (sel_outshift[0]) ? shift_last_pkt[0] : {acc_m_last[0]} & lut_last_pkt[acc_m_user[USER_KW2_LSB + BITS_KW2_L - 1 : USER_KW2_LSB]][0];
          shift_valid[0]     <= (sel_outshift[0]) ? shift_valid[0] : s_valid_cols_sel[0] & valid_mask[0];
          shift_last[0]      <= (sel_outshift[0]) ? shift_last[0] : s_last_cols_sel[0];
          shift_out_ready[0] <= (sel_outshift[0]) ? 1'b1 : 1'b0;
        end
      end
    end

    for (co=1; co<COLS; co=co+1) begin : outshift_cols
      localparam integer SHIFT_DATA_LSB = co * ROWS * WORD_WIDTH;
      localparam integer SHIFT_DATA_MSB = ((co + 1) * ROWS * WORD_WIDTH) - 1;
      localparam integer SHIFT_DATA_W   = ROWS * WORD_WIDTH;

      // outshift_flag is used to decide whether the column should be shifted out or not
      // outshift_flag is set to 1 for all cols when the last col of out shifter gets valid data
      // it is set to 0 when shift_out_ready[co] = 0 i.e. the data from that col has been shifted out.
      // always_ff@(posedge clk `OR_NEGEDGE(resetn)) begin
      //   if (!resetn) begin 
      //     outshift_flag[co] <= 0;
      //   end
      //   else begin
      //     if (co == COLS-1) shift_out_ready_last_col_prev <= shift_out_ready[COLS-1];

      //     if(shift_out_ready_last_col_prev & ~shift_out_ready[COLS-1]) begin // when last col of out shifter gets data from acc, it is ready to start shifting.
      //       outshift_flag[co] <= 1;
      //     end
      //     else begin
      //     //else if ((count_outshift == (COLS-1)-co) && en_outshift[co]) begin
      //       if (co < COLS-1) begin
      //         if (shift_out_ready[co]) begin
      //           outshift_flag[co] <= 0;
      //         end
      //       end
      //       else if (shift_out_ready[co]) outshift_flag[co] <= 0; // for last column

      //     end
      //   end
      // end
      
      always @(posedge clk `OR_NEGEDGE(resetn)) begin
        if (!resetn) begin
          shift_out_ready[co] <= 1'b1;
          shift_data_out[SHIFT_DATA_MSB:SHIFT_DATA_LSB] <= {SHIFT_DATA_W{1'b0}};
          {shift_valid[co], shift_last[co], shift_last_pkt[co]} <= 0;
        end else if (en_outshift[co]) begin
          shift_data_out[SHIFT_DATA_MSB:SHIFT_DATA_LSB] <= (sel_outshift[co]) ? shift_data_out[SHIFT_DATA_LSB-1 -: SHIFT_DATA_W] : acc_m_data[co];
          shift_last_pkt[co]  <= (sel_outshift[co]) ? shift_last_pkt[co-1] : {acc_m_last[co]} & lut_last_pkt[acc_m_user[co*TUSER_WIDTH + USER_KW2_LSB + BITS_KW2_L - 1 : co*TUSER_WIDTH + USER_KW2_LSB]][co];
          shift_valid[co]     <= (sel_outshift[co]) ? shift_valid[co-1] : s_valid_cols_sel[co] & valid_mask[co];
          shift_last[co]      <= (sel_outshift[co]) ? shift_last[co-1] : s_last_cols_sel[co];
          shift_out_ready[co] <= (sel_outshift[co]) ? shift_out_ready[co-1] : 1'b0;
        end
      end
    end

    always @(posedge clk `OR_NEGEDGE(resetn)) begin
      if (!resetn) begin
          m_bytes_per_transfer <= 0;
      end
      else begin
        if (en_outshift[COLS-1] && ~sel_outshift[COLS-1]) begin
          if (acc_m_user[(COLS-1)*TUSER_WIDTH + USER_W_LAST_BIT]) m_bytes_per_transfer <= lut_bpt_1[acc_m_user[(COLS-1)*TUSER_WIDTH + USER_KW2_LSB + BITS_KW2_L - 1 : (COLS-1)*TUSER_WIDTH + USER_KW2_LSB]];
          else m_bytes_per_transfer <= lut_bpt_0[acc_m_user[(COLS-1)*TUSER_WIDTH + USER_KW2_LSB + BITS_KW2_L - 1 : (COLS-1)*TUSER_WIDTH + USER_KW2_LSB]];
        end
      end
    end

    assign m_data   = shift_data_out [COLS*ROWS*WORD_WIDTH-1 -: ROWS*WORD_WIDTH];
    assign m_valid  = shift_valid[COLS-1] & outshift_flag[COLS-1];
    assign m_last   = shift_last [COLS-1];
    assign m_last_pkt = shift_last_pkt [COLS-1];

    // -------------- OUTPUT SHIFTER ----------------      

    //assign en_mac = &(~acc_m_valid | shift_out_ready);
    //assign en[0] = ~acc_m_valid[0] | shift_out_ready[0];
    for(c=0; c<COLS-1; c = c + 1) begin : mac_ready
      // If current column and next column output shifter regs both have valid data, and accumulator has valid data column gets frozen
      assign mac_freeze[c] = (acc_m_valid[c] & ~(shift_out_ready[c] & shift_out_ready[c+1]));
      always @(posedge clk `OR_NEGEDGE(resetn))
        if (!resetn) begin
          acc_m_valid[c] <= 0;
        end
        else begin
          if (en[c]) acc_m_valid[c] <= acc_m_valid_next[c];
          else if (~en[c] & acc_m_valid[c] & shift_out_ready[c] & shift_out_ready[c+1]) acc_m_valid[c] <= acc_m_valid_next[c]; // if handshake happens when en is 0, acc_m_valid should become low
        end
    end

    begin : mac_ready_last
      assign mac_freeze[COLS-1] = (acc_m_valid[COLS-1] & ~shift_out_ready[COLS-1]);
      always @(posedge clk `OR_NEGEDGE(resetn))
        if (!resetn) begin
          acc_m_valid[COLS-1] <= 0;
        end
        else begin
          if (en[COLS-1]) acc_m_valid[COLS-1] <= acc_m_valid_next[COLS-1];
          else if (~en[COLS-1] & acc_m_valid[COLS-1] & shift_out_ready[COLS-1]) acc_m_valid[COLS-1] <= acc_m_valid_next[COLS-1]; // if handshake happens when en is 0, acc_m_valid should become low
        end
    end

    for(c=0; c<COLS; c = c + 1) begin : C
      //assign en[c] = &(~mac_freeze);
      assign en[c] = &(~mac_freeze[COLS-1:c]);  // all cols to the left of frozen column should freeze.

      assign acc_m_valid_next[c] = !sel_shift[c] & mul_m_valid[c] & (mul_m_user[c*TUSER_WIDTH + USER_CONFIG_BIT] | mul_m_user[c*TUSER_WIDTH + USER_CIN_LAST_BIT]);

      always @(posedge clk `OR_NEGEDGE(resetn))
        if (!resetn)            {acc_m_user[c*TUSER_WIDTH + TUSER_WIDTH - 1 : c*TUSER_WIDTH], acc_m_last[c]} <= 0;
        else begin
          if (en[c] & mul_m_valid[c]) acc_m_user[c*TUSER_WIDTH + TUSER_WIDTH - 1 : c*TUSER_WIDTH] <= mul_m_user[c*TUSER_WIDTH + TUSER_WIDTH - 1 : c*TUSER_WIDTH];
          if (en[c])               acc_m_last[c] <= mul_m_last[c];
        end
    
    end

  endgenerate
endmodule
