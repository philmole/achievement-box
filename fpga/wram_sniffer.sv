//==============================================================================
// wram_sniffer.sv — passive 68K work-RAM shadow for Mega EverDrive Pro
//
// Route B of the RA-on-MD plan. Watches the console bus (MapIn.cpu) for
// writes into work RAM ($E00000-$FFFFFF mirror region), and replays them
// into a reserved region of the cart's 512KB SRAM chip. The ARM I/O
// coprocessor can already read SRAM directly over the pi bus
// (pi address 0x1000000 + offset), so the PC/Pi reads the live shadow
// with EXISTING USB memory-read commands. No custom read path needed.
//
// Written against krikzz/mega-ed-pub structs (CpuBus, MemCtrl, MapIn/MapOut).
// UNVERIFIED-ON-HARDWARE assumptions are marked ASSUMPTION — check each
// during bring-up (see integration notes).
//==============================================================================

module wram_sniffer #(
    // Byte offset inside SRAM where the 64KB shadow lives.
    // ASSUMPTION: srm_smd save handling stays below this. 0x40000 leaves
    // the lower 256KB untouched.
    parameter SHADOW_BASE_BYTES = 23'h40000
)(
    input  bit        clk,          // mai.clk system clock
    input  bit        rst,          // mai.map_rst (active in menu)

    input  CpuBus     cpu,          // mai.cpu — raw console bus
    input  bit        pi_act,       // mai.pi.act — ARM window preempts SRAM

    // Request interface into the SRAM write arbiter (see hub integration).
    // Held until 'grant' is pulsed by the arbiter.
    output bit        req,
    output bit [22:0] req_addr,     // SRAM word address
    output bit [15:0] req_data,
    output bit        req_we_hi,
    output bit        req_we_lo,
    input  bit        grant,

    // Debug/status (optional: expose via a spare mcfg register)
    output bit [15:0] drop_count    // FIFO overflows == shadow divergence!
);

    //--------------------------------------------------------------
    // 1. Write-cycle detection
    //--------------------------------------------------------------
    // VERIFIED from lib_base/everdrive.sv: CpuBus strobes are raw
    // active-low pins, and krikzz already provides cpu.we_ck — a
    // synchronised one-clock pulse at the start of every write cycle
    // (3FF shift register on we_lo&we_hi). Use it directly.
    //
    // WRAM decode: $E00000-$FFFFFF (top-3 address bits set covers the
    // full mirror space; games conventionally use $FF0000+).
    wire is_wram = (cpu.addr[23:21] == 3'b111);
    wire capture = cpu.we_ck & is_wram & !cpu.as;

    //--------------------------------------------------------------
    // 2. Capture FIFO (BRAM, 512 deep)
    //--------------------------------------------------------------
    // 68K writes arrive at most ~1 per 4 bus clocks (~1.9M/s worst case);
    // the drain side runs at system clock and only stalls while the CPU
    // itself owns SRAM, so 512 entries is generous. drop_count telemetry
    // will prove it.
    //
    // VDP DMA is not itself a hazard: DMA transfers are bus READS (never
    // asserting we_ck) and the 68K is halted for the burst, so the write
    // stream pauses. "DMA-heavy" games stress this FIFO through the dense
    // write bursts packed around DMA windows (per-frame buffer rebuilds)
    // and drain contention with the mapper/pi — exactly what drop_count
    // measures.
    typedef struct packed {
        bit [14:0] waddr;   // cpu.addr[15:1] — word address within 64KB
        bit [15:0] wdata;
        bit        whi;
        bit        wlo;
    } cap_t;

    cap_t fifo [0:511];
    bit [8:0] wr_ptr, rd_ptr;
    wire fifo_empty = (wr_ptr == rd_ptr);
    wire fifo_full  = (wr_ptr + 9'd1 == rd_ptr);

    always_ff @(posedge clk) begin
        if (rst) begin
            wr_ptr <= 0; drop_count <= 0;
        end else if (capture) begin
            if (!fifo_full) begin
                fifo[wr_ptr] <= '{cpu.addr[15:1], cpu.data,
                                  !cpu.we_hi, !cpu.we_lo};
                wr_ptr <= wr_ptr + 9'd1;
            end else
                drop_count <= drop_count + 16'd1;
        end
    end

    //--------------------------------------------------------------
    // 3. Drain into SRAM via arbiter
    //--------------------------------------------------------------
    // VERIFIED from lib_bram/srm_smd.sv: MemCtrl.addr is a BYTE address
    // (bit 0 held 0 in 16-bit configs, byte lanes via we_hi/we_lo).
    // VERIFIED from map_smd: the stock Genesis mapper never touches the
    // SRAM chip (saves live on BRAM) — the 512KB is free for the shadow.
    // VERIFIED from everdrive.sv sram mux: the ARM window (pi) preempts
    // mao.sram, so pause draining while pi is active (mai.pi.act) and
    // let the FIFO absorb — pass pi_act in from the hub.
    cap_t head;
    assign head = fifo[rd_ptr];

    assign req      = !fifo_empty & !rst & !pi_act;
    assign req_addr = SHADOW_BASE_BYTES | {7'd0, head.waddr, 1'b0};
    assign req_data = head.wdata;
    assign req_we_hi= head.whi;
    assign req_we_lo= head.wlo;

    always_ff @(posedge clk)
        if (rst)            rd_ptr <= 0;
        else if (grant && !fifo_empty) rd_ptr <= rd_ptr + 9'd1;

endmodule

//==============================================================================
// sram_arbiter — one-hot priority mux for the SRAM write port.
// CPU/mapper access (saves) always wins; sniffer drains in idle cycles.
// Instantiate in map_hub between map_smd's MapOut and the real mao.
//==============================================================================
module sram_arbiter(
    input  MemCtrl    map_sram,     // from map_smd (saves etc.) — priority
    input  bit        snif_req,
    input  bit [22:0] snif_addr,
    input  bit [15:0] snif_data,
    input  bit        snif_we_hi,
    input  bit        snif_we_lo,
    output bit        snif_grant,
    output MemCtrl    sram_out      // drive mao.sram with this
);
    wire map_active = map_sram.oe | map_sram.we_lo | map_sram.we_hi;

    always_comb begin
        sram_out   = map_sram;          // default: pass-through
        snif_grant = 0;
        if (!map_active && snif_req) begin
            sram_out.addr  = snif_addr;
            sram_out.dati  = snif_data;
            sram_out.we_hi = snif_we_hi;
            sram_out.we_lo = snif_we_lo;
            sram_out.oe    = 0;
            snif_grant     = 1;
        end
    end
endmodule
