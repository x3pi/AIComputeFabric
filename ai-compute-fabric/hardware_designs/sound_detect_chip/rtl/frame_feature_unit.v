module frame_feature_unit (
    input  wire               clk,
    input  wire               rst_n,
    input  wire               enable_i,
    input  wire               sample_valid_i,
    input  wire signed [15:0] sample_i,
    output reg                frame_valid_o,
    output reg  [23:0]        energy_total_o,
    output reg  [23:0]        energy_hf_o,
    output reg  [7:0]         zcr_count_o
);
    reg [5:0]         sample_cnt;
    reg signed [15:0] prev_y;
    reg [23:0]        acc_energy;
    reg [23:0]        acc_hf;
    reg [7:0]         acc_zcr;

    wire [15:0] abs_y = (sample_i == -16'sd32768) ? 16'd32767 :
                        (sample_i[15] ? (-sample_i) : sample_i);
    wire signed [16:0] s_ext  = {sample_i[15], sample_i};
    wire signed [16:0] py_ext = {prev_y[15], prev_y};
    wire signed [16:0] diff_y = s_ext - py_ext;
    wire [16:0] abs_diff = diff_y[16] ? (-diff_y) : diff_y;
    wire [15:0] clamped_diff = (abs_diff > 17'd65535) ? 16'd65535 : abs_diff[15:0];
    wire        zc = (sample_cnt != 6'd0) && (sample_i[15] != prev_y[15]);

    wire [23:0] next_energy = acc_energy + {8'd0, abs_y};
    wire [23:0] next_hf     = acc_hf + {8'd0, clamped_diff};
    wire [7:0]  next_zcr    = acc_zcr + {7'd0, zc};

    always @(posedge clk) begin
        if (!rst_n || !enable_i) begin
            sample_cnt     <= 6'd0;
            prev_y         <= 16'sd0;
            acc_energy     <= 24'd0;
            acc_hf         <= 24'd0;
            acc_zcr        <= 8'd0;
            frame_valid_o  <= 1'b0;
            energy_total_o <= 24'd0;
            energy_hf_o    <= 24'd0;
            zcr_count_o    <= 8'd0;
        end else if (sample_valid_i) begin
            prev_y <= sample_i;
            if (sample_cnt == 6'd63) begin
                sample_cnt     <= 6'd0;
                acc_energy     <= 24'd0;
                acc_hf         <= 24'd0;
                acc_zcr        <= 8'd0;
                energy_total_o <= next_energy;
                energy_hf_o    <= next_hf;
                zcr_count_o    <= next_zcr;
                frame_valid_o  <= 1'b1;
            end else begin
                sample_cnt    <= sample_cnt + 6'd1;
                acc_energy    <= next_energy;
                acc_hf        <= next_hf;
                acc_zcr       <= next_zcr;
                frame_valid_o <= 1'b0;
            end
        end else begin
            frame_valid_o <= 1'b0;
        end
    end
endmodule
