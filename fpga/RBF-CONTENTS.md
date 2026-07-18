# What `mega-pro.rbf` contains

`fpga/prebuilt/mega-pro.rbf` is a Quartus **Raw Binary File**: the Cyclone IV
configuration bitstream loaded into the Mega EverDrive Pro FPGA. It is not an
archive and does not carry a useful file manifest that can be opened from the
binary. Its contents are established from the Quartus project, HDL sources and
the recorded build.

## Verified artifact

| Property | Value |
| --- | --- |
| File | `fpga/prebuilt/mega-pro.rbf` |
| Size | 168,919 bytes |
| SHA-256 | `13d96ba13c762af61d2ecb56f6f4d9fb59d50df421baa406a53a9410ed31a993` |
| Target | Intel/Altera Cyclone IV E `EP4CE15F23C8` |
| Tool | Quartus Prime 20.1.1 Lite, build 720 |
| Build result | Successful, 2026-07-15 |
| Timing | Passed all reported setup, hold and minimum-pulse checks |
| Utilization | 1,917 / 15,408 logic elements; 49,664 / 516,096 memory bits |

The same SHA-256 is pinned in [`release-integrity.json`](../release-integrity.json)
and verified in CI, so the distributed file is exactly the recorded successful
build output.

## Functional contents

The bitstream configures the cart FPGA with:

- the public Mega EverDrive Pro base design: console/cart bus handling,
  system configuration, ARM/SPI interface, DMA, memory controllers, base I/O
  and audio plumbing;
- the normal Mega Drive mapper path, including standard, Codemasters and 10M
  mapper selection plus SRAM/EEPROM save handling;
- a passive WRAM write counter exposed to the host at mapper-config offsets
  `0xff00..0xff03`;
- the Achievement Box WRAM sniffer: it watches 68K writes in
  `$E00000-$FFFFFF`, queues captured 33-bit records in a 512-entry FPGA-memory
  FIFO, and mirrors the low 64 KiB address space into cart SRAM byte range
  `0x40000..0x4ffff`;
- an SRAM arbiter that preserves mapper access priority and drains captured
  WRAM writes only when the mapper and ARM host are not using SRAM;
- host-readable loss telemetry (`drop_count`) at `0xff04..0xff05`;
- passive Start+Down pad detection, wired into the stock reset-to-menu path,
  with a diagnostic flag at `0xff06`; and
- passive capture of the console version/region register `$A10001`, exposed at
  `0xff08` with a validity flag at `0xff09`, allowing the host to suppress an
  incompatible PAL achievement session instead of presenting it as active; and
- a passive vblank cadence counter: each level-6 autovector fetch from cart
  ROM `$000078` increments a free-wrapping 8-bit counter at `0xff0a`, letting
  the host measure the real frame rate (~60/s NTSC vs ~50/s PAL) and stop an
  achievement session when the console's region switch is flipped mid-game.

The host reads the WRAM shadow through the EverDrive's existing SRAM window at
PI/USB addresses `0x1040000..0x104ffff`. No ROM patch or custom USB command is
part of this mechanism.

## Deliberately absent

The build configuration defines `SST_SMD_OFF`, `SST_SMS_OFF`, `CHEATS_OFF`,
`MDP_OFF`, `MCD_OFF` and `MCD_MASTER_OFF`. As a result, this bitstream does not
provide the factory save-state/in-game-menu engine, cheat engine, optional MDP
subsystem, or Mega-CD path. Quartus may parse shared source containing
conditional modules, but disabled logic is not instantiated into the fitted
bitstream.

The `.rbf` also contains no game ROM, RetroAchievements credentials,
achievement definitions, `rcheevos`, HTTP/network code, Python daemon or web
UI. Those remain host-side. The bitstream observes memory and exposes a shadow;
it neither evaluates nor submits achievements.

## Build inputs

The authoritative input list is in `fpga/fpga_pro/map_smd/mega-pro.qsf`
(assembled by [`assemble.py`](assemble.py) — see [README.md](README.md)).
The project directly assigns:

- `topcfg.sv`, `fpga/fpga_pro/top.sv`, and `fpga/fpga_pro/clocks.sdc`;
- original Achievement Box modules `fpga/wram_sniffer.sv`, `fpga/pad_watch.sv`
  and `fpga/frame_watch.sv`;
- mapper modules `hub.sv`, `map_smd.sv`, `map_cdb.sv`, `map_sys.sv`,
  `map_nom.sv`, `srm_smd.sv`, `ram_cart.sv`, and `eep_24x.sv`; and
- shared EverDrive modules `var.sv`, `sys_cfg.sv`, `structs.sv`, `pi.sv`,
  `pi_map.sv`, `everdrive.sv`, `mdp.sv`, `dma.sv`, `defs.sv`, `base_io.sv`,
  and `audio_out.sv`.

Some of those files include other HDL files with SystemVerilog `` `include ``
directives. For exact reconstruction, treat the full assembled `fpga/` source
tree plus the QSF/SDC configuration as the source input, not the list above as
a standalone bundle.

## Limits of this verification

The reports prove that this source configuration fitted and met its declared
timing constraints, and the hashes prove which output is shipped. They do not
prove runtime correctness on real hardware. Hardcore-readiness evidence still
needs sustained hardware tests showing `drop_count == 0`, WRAM-shadow dumps
matching a trusted reference, correct byte lanes, save compatibility, clean
reset/session boundaries and broad game/mapper coverage.
