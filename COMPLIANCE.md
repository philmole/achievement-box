# RetroAchievements compliance & good-faith design

Achievement Box earns real RetroAchievements unlocks on real console hardware.
That is a position of trust, so the design is deliberately conservative. This
document states the principles the project holds to, and invites scrutiny.

For the exact scope of the shipped FPGA bitstream, see
[fpga/RBF-CONTENTS.md](fpga/RBF-CONTENTS.md).

## Principles

### 1. Casual only
Unlocks are submitted in **Casual mode** (formerly called softcore) unless and
until the RetroAchievements community explicitly blesses a hardcore path for
this kind of hardware tooling.
Casual is the honest default for a new, community-unreviewed memory source.

### 2. Passive observation — no ROM patching, ever
The project **never modifies the game**. The Mega Drive path reads live RAM
through a custom FPGA mapper that *passively shadows* work-RAM writes off the
console bus into the cart's spare SRAM. The game's own code and data are
untouched; the sniffer only observes.

An earlier, rejected approach (injecting a VBlank stub into a patched ROM) was
explicitly abandoned in favour of purely passive observation. Any future console
backend must meet the same bar — passive reads only (e.g. an SNES backend would
use the SD2SNES's existing usb2snes memory interface, not a patched ROM).

### 3. Games are identified by the pristine ROM hash
Games are identified exactly the way RetroAchievements does it — by hashing the
**original, unmodified ROM**. Even on the Mega Drive path, where a custom mapper
core is loaded onto the cart, the *game* bytes are pristine and hash to the same
value RA expects. No modified ROM ever reports a fake identity.

### 4. Correctness is verifiable, not assumed
The Mega Drive sniffer exposes a FIFO `drop_count` in its config window. A drop
count of **zero** across heavy, DMA-intensive gameplay is a checkable proof that
the RAM shadow never diverged from real work-RAM. The intended validation is an
emulator-vs-shadow diff over hours of play — divergence is a bug, and treated as
one, not tolerated.

### 5. The browser is feedback, not authority
The web UI is the visual feedback layer — library, live progress, Rich
Presence, unlock celebrations (see the [README](README.md)). It holds no
credentials and cannot award or submit unlocks; those originate solely from
the daemon evaluating observed console memory.

### 6. Frame cadence is honest
On real hardware there is no emulator frame callback, so evaluation is driven by
a time-based heuristic tuned to the console's frame rate — the same problem the
[nes-ra-adapter](https://github.com/odelot/nes-ra-adapter) solved, whose
refinements this project follows.

## Precedent

Hardware RetroAchievements is not unprecedented, and this project follows the
paths the community has already accepted:

- **[RA2Snes](https://github.com/Factor-64/RA2Snes)** does SD2SNES → rcheevos on
  a PC today and is community-blessed; it is bundled with usb2snes. It is the
  model a future SNES backend here would follow.
- **[nes-ra-adapter](https://github.com/odelot/nes-ra-adapter)** is an
  interposer that runs openly, with hardcore enabled, and tracks compatibility
  publicly. It is the architecture cousin for the Mega Drive path.

## Engagement

The memory source is open so it can be inspected rather than taken on trust.
Questions and review are welcome.
