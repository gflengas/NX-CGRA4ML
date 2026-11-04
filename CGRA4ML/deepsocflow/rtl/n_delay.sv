`timescale 1ns/1ps
`include "defines.svh"

module n_delay #(
  parameter N = 1,
            W = 8
)(
  input  wire     c, e, rng, rnl,
  input  wire [W-1 : 0]  i,
  output wire [W-1 : 0]  o
);

  reg [W-1 : 0] data [0:N];

  always @(*) data[0] = i;
  assign o = data[N];

  genvar n;
  generate 
  for (n=0 ; n < N; n = n + 1) begin : n_dat
    always @(posedge c `OR_NEGEDGE(rng))
      if (!rng)      data[n+1] <= {W{1'b0}};
      else if (!rnl) data[n+1] <= {W{1'b0}};
      else if (e)    data [n+1] <= data [n];
  end
  endgenerate

endmodule
