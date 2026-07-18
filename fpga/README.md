# Achievement Box FPGA mapper

A custom Mega EverDrive Pro mapper core that adds **passive work-RAM
observation** to the stock Mega Drive mapper: a sniffer watches every 68K
write to `$E00000-$FFFFFF` on the console bus and shadows the low 64 KiB of
work-RAM into the cart's otherwise-unused SRAM chip, where the host reads it
over USB with krikzz's existing memory commands. No ROM patching, no custom
USB protocol — the console and game are never modified. What's in the
bitstream (and deliberately not in it) is documented in
[RBF-CONTENTS.md](RBF-CONTENTS.md).

## Using the prebuilt core

Nothing to build: [`prebuilt/mega-pro.rbf`](prebuilt/mega-pro.rbf) ships in
this repository and the daemon stages it automatically when the library's
**RA** switch is on. Its SHA-256 is pinned in
[`release-integrity.json`](../release-integrity.json) and verified by
`daemon/check_integrity.py` in CI and at publish time.

## Rebuilding from source

Don't want to trust a prebuilt bitstream? Build it yourself.

This repository ships Achievement Box's original gateware
(`wram_sniffer.sv`, `pad_watch.sv`, `frame_watch.sv`), plus
[`patches/achievement-box.patch`](patches/achievement-box.patch) containing
our modifications to four upstream files. The full Quartus source tree is
assembled locally from the pinned
[`references/mega-ed-pub`](https://github.com/krikzz/mega-ed-pub) submodule:

```powershell
git submodule update --init
python fpga/assemble.py
```

The assembled upstream files land in their expected places under `fpga/` and
are listed in `fpga/.gitignore` — they are krikzz's code and must not be
committed here.

Then build with **Intel Quartus Prime Lite 20.1.1** (the free edition; the
target is a Cyclone IV E `EP4CE15F23C8`):

1. Open `fpga/fpga_pro/map_smd/mega-pro.qpf`.
2. Run a full compilation (Processing → Start Compilation).
3. The bitstream is written to
   `fpga/fpga_pro/map_smd/output_files/mega-pro.rbf`.

Compare your build against the shipped core with the SHA-256 in
`release-integrity.json`. A caveat on reproducibility: Quartus builds are not
guaranteed bit-identical across machines or tool versions, so a differing
hash does not by itself indicate tampering — the meaningful check is that
your own build behaves identically. The exact recorded build (tool version,
fitter results, timing) is documented in [RBF-CONTENTS.md](RBF-CONTENTS.md).

## Layout

    wram_sniffer.sv     Original: WRAM write capture, FIFO, SRAM shadow,
                        drop-count telemetry (GPLv3)
    pad_watch.sv        Original: passive Start+Down pad detection (GPLv3)
    frame_watch.sv      Original: passive vblank cadence counter for live
                        NTSC/PAL detection (GPLv3)
    assemble.py         Assembles the buildable tree from the submodule
    patches/            Achievement Box modifications to upstream files
    prebuilt/           The shipped, hash-pinned mega-pro.rbf
    mapper/, fpga_pro/  Assembled locally by assemble.py — not committed

Licensing and attribution for the upstream sources are covered in
[NOTICE](../NOTICE).
