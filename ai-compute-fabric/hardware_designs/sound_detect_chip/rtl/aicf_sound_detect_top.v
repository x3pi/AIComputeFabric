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
