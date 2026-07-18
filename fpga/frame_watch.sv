//==============================================================================
// frame_watch.sv — passive vblank cadence counter for the RA sniffer core
//
// The boot-time region latch in pad_watch only updates when the game reads
// $A10001, so a console whose NTSC/PAL switch is flipped mid-game is
// invisible to the daemon. This module gives it a live signal: every vblank
// the 68000 fetches the level-6 autovector from cart ROM $000078, a normal
// word read the mapper serves itself. Counting those fetches yields the real
// frame rate — ~60/s NTSC vs ~50/s PAL — regardless of what the game latched
// at boot.
//
// Kept deliberately dumb: a free-wrapping 8-bit counter, same shape as the
// WRAM write counter. The daemon polls it over USB every ~0.5s and computes
// the rate from the delta (mod 256; at 60/s the counter wraps every ~4s).
// V-interrupts disabled (loading screens) → delta 0 → the daemon draws no
// verdict. There is no oe_ck, so reads are latched during the cycle and
// counted once when /OE deasserts — the pend pattern from pad_watch.
//==============================================================================

module frame_watch (
    input  bit    clk,        // mai.clk
    input  bit    rst,        // mai.map_rst (active in menu)
    input  CpuBus cpu,

    output bit [7:0] vint_count // free-wrapping vblank vector-fetch counter
);

    // Level-6 autovector fetch: word read of $000078 (first word of the
    // longword vector). Cart ROM space, so /CE_LO asserts — unlike the
    // $A10xxx taps in pad_watch we can gate on it. Full 23-bit compare;
    // no mirrors in this range. Only $78 counts → one event per vblank.
    wire vec_sel = (cpu.addr[23:1] == 23'h00003C) &
                   !cpu.ce_lo & !cpu.as & !cpu.oe;

    bit vec_pend;

    always_ff @(posedge clk) begin
        if (rst) begin
            vint_count <= 0;
            vec_pend   <= 0;
        end else if (vec_sel) begin
            vec_pend <= 1'b1;          // track until cycle end
        end else if (vec_pend) begin   // /OE deasserted: one completed fetch
            vec_pend   <= 1'b0;
            vint_count <= vint_count + 1'b1;
        end
    end

endmodule
