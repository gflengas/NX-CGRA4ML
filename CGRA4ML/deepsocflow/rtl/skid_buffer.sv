// Two-entry AXI-Stream skid buffer, from upstream CGRA4ML (dfa3eb5): registered
// s_ready, m_valid and m_data. Enum and unique case replaced by localparams,
// as in the other NanoXplore ports of the core.
module skid_buffer #(parameter WIDTH = 8)(
  input  clk, rstn, s_valid, m_ready,
  input  [WIDTH-1:0] s_data,
  output reg [WIDTH-1:0] m_data,
  output reg m_valid, s_ready
);
  localparam [1:0] EMPTY = 2'd0, PARTIAL = 2'd1, FULL = 2'd2;
  reg [1:0] state, state_next;

  always @(*) begin
    state_next = state;
    case (state)
      EMPTY  : if      ( s_valid)             state_next = PARTIAL;
      PARTIAL: if      (!m_ready &&  s_valid) state_next = FULL;
               else if ( m_ready && !s_valid) state_next = EMPTY;
      FULL   : if      ( m_ready)             state_next = PARTIAL;
      default:                                state_next = EMPTY;
    endcase
  end

  always @(posedge clk)
    if (!rstn) state <= EMPTY;
    else       state <= state_next;

  reg [WIDTH-1:0] buffer;
  always @(posedge clk)
    if (!rstn) begin
      m_valid <= 1'b0;
      s_ready <= 1'b0;
      buffer  <= {WIDTH{1'b0}};
      m_data  <= {WIDTH{1'b0}};
    end else begin
      m_valid <= state_next != EMPTY;
      s_ready <= state_next != FULL;

      if (state == PARTIAL && state_next == FULL)
        buffer <= s_data;

      case (state)
        EMPTY  : if (state_next == PARTIAL) m_data <= s_data;
        PARTIAL: if (m_ready && s_valid)    m_data <= s_data;
        FULL   : if (m_ready)               m_data <= buffer;
        default: ;
      endcase
    end
endmodule
