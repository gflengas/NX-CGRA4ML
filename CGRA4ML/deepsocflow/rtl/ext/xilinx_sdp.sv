// Asymmetric port RAM
// Read Wider than Write. Read Statement in loop
//asym_ram_sdp_read_wider.v
`timescale 1ns / 1ps

module asym_ram_sdp_read_wider (
    clkA,
    clkB,
    enaA,
    weA,
    enaB,
    addrA,
    addrB,
    diA,
    doB
);
  parameter WIDTHA = 4;
  parameter SIZEA = 1024;
  parameter ADDRWIDTHA = 10;

  parameter WIDTHB = 16;
  parameter SIZEB = 256;
  parameter ADDRWIDTHB = 8;
  input clkA;
  input clkB;
  input weA;
  input enaA, enaB;
  input [ADDRWIDTHA-1:0] addrA;
  input [ADDRWIDTHB-1:0] addrB;
  input [WIDTHA-1:0] diA;
  output [WIDTHB-1:0] doB;

  localparam RATIO = WIDTHB / WIDTHA;
  localparam LOG2RATIO = $clog2(RATIO);

  genvar bank_idx;
  generate
    for (bank_idx = 0; bank_idx < RATIO; bank_idx = bank_idx + 1) begin : bank
      localparam [LOG2RATIO-1:0] BANK_SEL = bank_idx;
      reg [WIDTHA-1:0] RAM[0:SIZEB-1];
      reg [WIDTHA-1:0] readB;

      always @(posedge clkA) begin
        if (enaA && weA && (addrA[LOG2RATIO-1:0] == BANK_SEL)) begin
          RAM[addrA[ADDRWIDTHA-1:LOG2RATIO]] <= diA;
        end
      end

      always @(posedge clkB) begin
        if (enaB) begin
          readB <= RAM[addrB];
        end
      end

      assign doB[(bank_idx+1)*WIDTHA-1:bank_idx*WIDTHA] = readB;
    end
  endgenerate

  function automatic [WIDTHA-1:0] get_word(input integer word_offset);
    integer row;
    begin
      row = word_offset / RATIO;
      case (word_offset % RATIO)
        0: get_word = bank[0].RAM[row];
        1: get_word = bank[1].RAM[row];
        2: get_word = bank[2].RAM[row];
        3: get_word = bank[3].RAM[row];
        4: get_word = bank[4].RAM[row];
        5: get_word = bank[5].RAM[row];
        6: get_word = bank[6].RAM[row];
        default: get_word = bank[7].RAM[row];
      endcase
    end
  endfunction

  task automatic set_word(input integer word_offset, input [WIDTHA-1:0] data);
    integer row;
    begin
      row = word_offset / RATIO;
      case (word_offset % RATIO)
        0: bank[0].RAM[row] = data;
        1: bank[1].RAM[row] = data;
        2: bank[2].RAM[row] = data;
        3: bank[3].RAM[row] = data;
        4: bank[4].RAM[row] = data;
        5: bank[5].RAM[row] = data;
        6: bank[6].RAM[row] = data;
        default: bank[7].RAM[row] = data;
      endcase
    end
  endtask

endmodule
