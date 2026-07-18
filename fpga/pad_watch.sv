//==============================================================================
// pad_watch.sv — passive joypad combo detector for the RA sniffer core
//
// Our core (public sources) lacks krikzz's private SST engine, so the
// in-game menu is unavailable during achievement sessions. This module
// restores the user-facing half: it passively watches the console bus for
// pad-port 1 traffic ($A10003) and raises a sticky flag when the in-game
// menu combo (Start+Down) is held. The daemon polls the flag over USB and
// performs the action (v1: reset to the EverDrive menu).
//
// Pad protocol: games strobe TH (bit 6, written to $A10003) and read two
// button rows. TH=0 row: bit5=Start, bit4=A, bit1=Down, bit0=Up, active
// low. $A10xxx cycles are visible on the cart bus (krikzz's own MegaKey
// decodes them — lib_base/var.sv); /CE does not assert there, so gate on
// /AS + strobes only. Read data is latched continuously during the cycle
// and evaluated once when /OE deasserts (end-of-cycle value = valid).
//==============================================================================

module pad_watch #(
    // Consecutive TH=0 samples the combo must be held: pads are polled
    // ~once per frame, so 8 samples = ~130ms hold — enough to reject
    // transients without feeling laggy.
    parameter HOLD_SAMPLES = 8,
    // Completed pad-row samples to ignore after boot (rst falling edge)
    // before the combo can fire at all. combo_hit feeds krikzz's own
    // rst_ctrl directly (hardware reset-to-menu, no daemon involved) --
    // hardware-confirmed 2026-07-17 (Phil) that holding Start+Down through
    // a game's very first boot-time pad polls can trigger that reset before
    // the game has set up whatever state its own boot sequence assumes is
    // safe, wedging the console. ~180 samples at the ~once-per-frame poll
    // rate above is roughly 3s -- long enough to clear early BIOS/boot
    // polls, short enough not to feel unresponsive for a genuine early quit.
    parameter BOOT_GUARD_SAMPLES = 180
)(
    input  bit    clk,        // mai.clk
    input  bit    rst,        // mai.map_rst (active in menu)
    input  CpuBus cpu,

    output bit    combo_hit,  // sticky until rst
    output bit [7:0] last_sample, // most recent completed pad read (diagnostic)
    output bit [7:0] region_sample, // $A10001 version/region register
    output bit    region_valid
);

    // Controller data ports: bytes $A10003/$A10005 (word addresses
    // $A10002/$A10004). Support either port so the safety combo follows the
    // controller actually used by the player.
    wire pad_sel = (cpu.addr[23:1] == 23'h508001) ||
                   (cpu.addr[23:1] == 23'h508002);
    wire region_sel = (cpu.addr[23:1] == 23'h508000);

    // Recognise the TH=0 row from its data: Left/Right (bits 2/3) are forced
    // low by a standard Mega Drive pad in that phase. This is more robust
    // than relying on seeing the game's preceding TH write on the cart bus.
    function automatic bit is_th0_row(input bit [7:0] d);
        return d[3:2] == 2'b00;
    endfunction

    // Start and Down are bits 5/1, active-low. A and Up are ignored.
    function automatic bit is_combo(input bit [7:0] d);
        return !d[5] && !d[1];
    endfunction

    bit [7:0] dat_l;
    bit rd_pend;
    bit [7:0] region_dat_l;
    bit region_rd_pend;
    bit [3:0] held;
    bit [7:0] boot_guard;
    wire boot_settled = (boot_guard == 8'(BOOT_GUARD_SAMPLES));

    always_ff @(posedge clk) begin
        if (rst) begin
            combo_hit <= 0;
            last_sample <= 8'hff;
            held      <= 0;
            rd_pend   <= 0;
            region_sample <= 0;
            region_valid <= 0;
            region_rd_pend <= 0;
            boot_guard <= 0;
        end else if (pad_sel & !cpu.as & !cpu.oe) begin
            dat_l   <= cpu.data[7:0];  // track until cycle end
            rd_pend <= 1'b1;
        end else if (rd_pend) begin    // /OE deasserted: evaluate sample
            rd_pend <= 1'b0;
            last_sample <= dat_l;
            // Every completed pad-port read counts toward the boot guard,
            // regardless of row, so it advances during a game's earliest
            // boot-time polling and saturates instead of wrapping.
            if (!boot_settled)
                boot_guard <= boot_guard + 1'b1;
            // Games alternate TH-high and TH-low controller rows. Ignore the
            // high row entirely: resetting `held` there made it impossible to
            // accumulate consecutive per-frame combo samples.
            if (is_th0_row(dat_l)) begin
                if (is_combo(dat_l) && boot_settled) begin
                    if (held == HOLD_SAMPLES - 1)
                        combo_hit <= 1'b1;
                    else
                        held <= held + 1'b1;
                end else
                    held <= 0;
            end
        end else if (region_sel & !cpu.as & !cpu.oe) begin
            // Games read the hardware version byte at $A10001 during boot.
            // Capture the actual bus value, including any MegaKey override.
            region_dat_l <= cpu.data[7:0];
            region_rd_pend <= 1'b1;
        end else if (region_rd_pend) begin
            region_rd_pend <= 1'b0;
            region_sample <= region_dat_l;
            region_valid <= 1'b1;
        end
    end

endmodule
