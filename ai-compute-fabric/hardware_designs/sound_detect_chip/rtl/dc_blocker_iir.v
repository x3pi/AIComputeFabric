module dc_blocker_iir (
    input  wire               clk,
    input  wire               rst_n,
    input  wire               enable_i,
    input  wire               valid_i,
    input  wire signed [15:0] pcm_in,
    output reg                valid_o,
    output reg  signed [15:0] pcm_out
);
    reg signed [15:0] x_prev;
    reg signed [15:0] y_prev;

    wire signed [17:0] x_ext  = {{2{pcm_in[15]}}, pcm_in};
    wire signed [17:0] xp_ext = {{2{x_prev[15]}}, x_prev};
    wire signed [17:0] yp_ext = {{2{y_prev[15]}}, y_prev};
    wire signed [17:0] diff_x = x_ext - xp_ext;
    wire signed [17:0] decay  = yp_ext >>> 4;
    wire signed [17:0] acc    = diff_x + yp_ext - decay;
    wire signed [15:0] sat_y  = (acc > 18'sd32767)  ? 16'sd32767 :
                                (acc < -18'sd32768) ? -16'sd32768 : acc[15:0];

    always @(posedge clk) begin
        if (!rst_n || !enable_i) begin
            x_prev  <= 16'sd0;
            y_prev  <= 16'sd0;
            pcm_out <= 16'sd0;
            valid_o <= 1'b0;
        end else if (valid_i) begin
            x_prev  <= pcm_in;
            y_prev  <= sat_y;
            pcm_out <= sat_y;
            valid_o <= 1'b1;
        end else begin
            valid_o <= 1'b0;
        end
    end
endmodule
