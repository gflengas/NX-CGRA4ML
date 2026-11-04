// NG-ULTRA replacement for ext/xilinx_spwf.v, cycle-equivalent to it.
// Impulse cannot map the Xilinx write-first template (write and registered read
// in one process) natively: it infers an asynchronous read port and emulates it
// with an inverted pseudo read clock. Separate write and registered-read
// processes map to NX_RAM with a synchronous read. A one-cycle bypass restores
// write-first output: in a write cycle, dout shows the written data.
// Written in the Verilog subset accepted by Impulse, like the other ported RTL.
`timescale 1ns/1ps
module rams_sp_wf #(
  parameter WIDTH = 16,
  parameter DEPTH = 1024,
  parameter ADDR_WIDTH = 10
)(
  input                   clk,
  input                   we,
  input                   en,
  input  [ADDR_WIDTH-1:0] addr,
  input  [WIDTH-1:0]      di,
  output [WIDTH-1:0]      dout
);
  reg [WIDTH-1:0] RAM [DEPTH-1:0];
  reg [WIDTH-1:0] read_data;
  reg [WIDTH-1:0] write_data;
  reg             wrote;

  always @(posedge clk)
    if (en && we) RAM[addr] <= di;

  always @(posedge clk)
    if (en) read_data <= RAM[addr];

  always @(posedge clk)
    if (en) begin
      wrote      <= we;
      write_data <= di;
    end

  assign dout = wrote ? write_data : read_data;
endmodule
