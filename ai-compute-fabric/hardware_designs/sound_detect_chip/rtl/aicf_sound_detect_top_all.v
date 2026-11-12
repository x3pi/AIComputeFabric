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

module sound_detect_fsm (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        enable_i,
    input  wire        frame_valid_i,
    input  wire [23:0] energy_total_i,
    input  wire [23:0] energy_hf_i,
    input  wire [7:0]  zcr_count_i,
    input  wire [23:0] thresh_energy_i,
    input  wire [7:0]  thresh_zcr_i,
    input  wire [3:0]  hangover_frames_i,
    output reg         frame_done_o,
    output reg  [23:0] noise_floor_o,
    output reg  [1:0]  state_o,
    output reg  [1:0]  detected_class_o,
    output reg         irq_wakeup_o
);
    reg [3:0] hangover_cnt;

    wire is_loud = (energy_total_i > (noise_floor_o + thresh_energy_i)) && (zcr_count_i >= 8'd2);
    wire [1:0] frame_class = !is_loud ? 2'd0 :
        ((energy_hf_i > (energy_total_i + (energy_total_i >> 2))) && (zcr_count_i >= thresh_zcr_i)) ? 2'd2 :
        (zcr_count_i >= thresh_zcr_i) ? 2'd3 : 2'd1;

    always @(posedge clk) begin
        if (!rst_n || !enable_i) begin
            noise_floor_o    <= 24'd1024;
            state_o          <= 2'd0;
            detected_class_o <= 2'd0;
            irq_wakeup_o     <= 1'b0;
            hangover_cnt     <= 4'd0;
            frame_done_o     <= 1'b0;
        end else if (frame_valid_i) begin
            frame_done_o <= 1'b1;
            if (state_o == 2'd0 && !is_loud) begin
                noise_floor_o <= noise_floor_o - (noise_floor_o >> 4) + (energy_total_i >> 4);
            end
            case (state_o)
                2'd0: begin
                    if (is_loud) begin
                        if (frame_class == 2'd2) begin
                            state_o          <= 2'd2;
                            irq_wakeup_o     <= 1'b1;
                            detected_class_o <= frame_class;
                            hangover_cnt     <= hangover_frames_i;
                        end else begin
                            state_o          <= 2'd1;
                            irq_wakeup_o     <= 1'b0;
                            detected_class_o <= frame_class;
                        end
                    end else begin
                        irq_wakeup_o     <= 1'b0;
                        detected_class_o <= 2'd0;
                    end
                end
                2'd1: begin
                    if (is_loud) begin
                        state_o          <= 2'd2;
                        irq_wakeup_o     <= 1'b1;
                        detected_class_o <= frame_class;
                        hangover_cnt     <= hangover_frames_i;
                    end else begin
                        state_o          <= 2'd0;
                        irq_wakeup_o     <= 1'b0;
                        detected_class_o <= 2'd0;
                    end
                end
                2'd2: begin
                    if (is_loud) begin
                        state_o          <= 2'd2;
                        irq_wakeup_o     <= 1'b1;
                        detected_class_o <= frame_class;
                        hangover_cnt     <= hangover_frames_i;
                    end else if (hangover_frames_i > 4'd0) begin
                        state_o      <= 2'd3;
                        hangover_cnt <= hangover_frames_i - 4'd1;
                        irq_wakeup_o <= 1'b1;
                    end else begin
                        state_o          <= 2'd0;
                        irq_wakeup_o     <= 1'b0;
                        detected_class_o <= 2'd0;
                    end
                end
                2'd3: begin
                    if (is_loud) begin
                        state_o          <= 2'd2;
                        irq_wakeup_o     <= 1'b1;
                        detected_class_o <= frame_class;
                        hangover_cnt     <= hangover_frames_i;
                    end else if (hangover_cnt == 4'd0) begin
                        state_o          <= 2'd0;
                        irq_wakeup_o     <= 1'b0;
                        detected_class_o <= 2'd0;
                    end else begin
                        hangover_cnt <= hangover_cnt - 4'd1;
                        irq_wakeup_o <= 1'b1;
                    end
                end
            endcase
        end else begin
            frame_done_o <= 1'b0;
        end
    end
endmodule

module aicf_sound_detect_top (
    input  wire               clk,
    input  wire               rst_n,
    input  wire               cfg_enable,
    input  wire [23:0]        cfg_thresh_energy,
    input  wire [7:0]         cfg_thresh_zcr,
    input  wire [3:0]         cfg_hangover_frames,
    input  wire               pcm_valid_i,
    input  wire signed [15:0] pcm_data_i,
    output wire               frame_done_o,
    output wire [23:0]        frame_energy_o,
    output wire [23:0]        frame_hf_energy_o,
    output wire [7:0]         frame_zcr_o,
    output wire [23:0]        noise_floor_o,
    output wire [1:0]         fsm_state_o,
    output wire [1:0]         detected_class_o,
    output wire               irq_wakeup_o
);
    wire               dc_valid;
    wire signed [15:0] dc_pcm;
    wire               feat_frame_valid;

    dc_blocker_iir u_dc (
        .clk     (clk),
        .rst_n   (rst_n),
        .enable_i(cfg_enable),
        .valid_i (pcm_valid_i),
        .pcm_in  (pcm_data_i),
        .valid_o (dc_valid),
        .pcm_out (dc_pcm)
    );

    frame_feature_unit u_feat (
        .clk           (clk),
        .rst_n         (rst_n),
        .enable_i      (cfg_enable),
        .sample_valid_i(dc_valid),
        .sample_i      (dc_pcm),
        .frame_valid_o (feat_frame_valid),
        .energy_total_o(frame_energy_o),
        .energy_hf_o   (frame_hf_energy_o),
        .zcr_count_o   (frame_zcr_o)
    );

    sound_detect_fsm u_fsm (
        .clk              (clk),
        .rst_n            (rst_n),
        .enable_i         (cfg_enable),
        .frame_valid_i    (feat_frame_valid),
        .energy_total_i   (frame_energy_o),
        .energy_hf_i      (frame_hf_energy_o),
        .zcr_count_i      (frame_zcr_o),
        .thresh_energy_i  (cfg_thresh_energy),
        .thresh_zcr_i     (cfg_thresh_zcr),
        .hangover_frames_i(cfg_hangover_frames),
        .frame_done_o     (frame_done_o),
        .noise_floor_o    (noise_floor_o),
        .state_o          (fsm_state_o),
        .detected_class_o (detected_class_o),
        .irq_wakeup_o     (irq_wakeup_o)
    );
endmodule
