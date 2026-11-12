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
