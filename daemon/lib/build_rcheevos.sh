#!/usr/bin/env bash
# Build librcheevos.so — the rc_client shared library the daemon loads by
# ctypes on non-Windows hosts (see daemon/achievementbox/rcbridge.py).
#
# Windows ships a prebuilt MSVC rcheevos.dll whose hash is pinned in
# release-integrity.json. There is no equivalent prebuilt for Linux: you
# build it here from the same pinned upstream tag, so the .so is a local
# artifact rather than something this repository vouches for.
#
# Usage:
#   daemon/lib/build_rcheevos.sh              # clone the pinned tag and build
#   RCHEEVOS_SRC=/path/to/rcheevos ...        # build from an existing checkout
#   KEEP_BUILD=1 ...                          # keep the scratch clone
#
# Requires: gcc, git, make-less (plain gcc invocation), a C toolchain.

set -euo pipefail

# Pinned to match "source" in release-integrity.json. Bump both together.
RCHEEVOS_TAG="${RCHEEVOS_TAG:-v12.3.0}"
RCHEEVOS_URL="${RCHEEVOS_URL:-https://github.com/RetroAchievements/rcheevos.git}"

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${OUT:-$LIB_DIR/librcheevos.so}"
BUILD_DIR="${BUILD_DIR:-$LIB_DIR/build}"

log() { printf '>> %s\n' "$*"; }

# --- obtain sources ---------------------------------------------------
if [ -n "${RCHEEVOS_SRC:-}" ]; then
    # An explicit checkout is taken as given -- the caller owns which
    # revision it is -- but say so, since it need not be RCHEEVOS_TAG.
    SRC_DIR="$RCHEEVOS_SRC"
    log "using existing rcheevos checkout: $SRC_DIR"
    log "  at: $(git -C "$SRC_DIR" describe --tags --always --dirty 2>/dev/null \
                 || echo 'unknown revision')"
else
    SRC_DIR="$BUILD_DIR/rcheevos"
    # KEEP_BUILD leaves this clone behind, so a later run -- or a bumped
    # RCHEEVOS_TAG -- would otherwise reuse it and silently build the
    # wrong version. Reuse it only when it is already at the wanted tag.
    if [ -d "$SRC_DIR/.git" ] &&
       [ "$(git -C "$SRC_DIR" describe --tags --exact-match 2>/dev/null)" \
         = "$RCHEEVOS_TAG" ]; then
        log "reusing clone at $SRC_DIR ($RCHEEVOS_TAG)"
    else
        if [ -d "$SRC_DIR" ]; then
            log "clone at $SRC_DIR is not $RCHEEVOS_TAG -- discarding it"
            rm -rf "$SRC_DIR"
        fi
        log "cloning rcheevos $RCHEEVOS_TAG"
        mkdir -p "$BUILD_DIR"
        git clone --depth 1 --branch "$RCHEEVOS_TAG" "$RCHEEVOS_URL" "$SRC_DIR"
    fi
fi

if [ ! -f "$SRC_DIR/include/rc_client.h" ]; then
    echo "error: $SRC_DIR does not look like an rcheevos checkout" >&2
    exit 1
fi

# --- source set -------------------------------------------------------
# Mirrors the shipped Windows DLL's export surface (252 symbols): the
# rc_client core, rcheevos evaluation, the rapi request/response layer and
# rhash. Deliberately excluded, as they are absent from that DLL too:
#   rc_client_external.c      (RC_CLIENT_SUPPORTS_EXTERNAL)
#   rc_client_raintegration.c (Windows RAIntegration overlay)
#   rc_libretro.c             (libretro frontend glue)
SOURCES=(
    "$SRC_DIR/src/rc_compat.c"
    "$SRC_DIR/src/rc_client.c"
    "$SRC_DIR/src/rc_util.c"
    "$SRC_DIR/src/rc_version.c"
)
while IFS= read -r -d '' f; do SOURCES+=("$f"); done \
    < <(find "$SRC_DIR/src/rcheevos" "$SRC_DIR/src/rapi" "$SRC_DIR/src/rhash" \
             -name '*.c' -print0 | sort -z)

log "compiling ${#SOURCES[@]} source files"

# -DRC_SHARED makes RC_EXPORT resolve to visibility("default") on gcc, and
# -fvisibility=hidden keeps everything else internal — so the .so exports
# exactly the public API, matching the DLL.
mkdir -p "$(dirname "$OUT")"
"${CC:-gcc}" -shared -fPIC -fvisibility=hidden -O2 \
    -DRC_SHARED -DRC_CLIENT_SUPPORTS_HASH \
    -D_LARGEFILE64_SOURCE -D_FILE_OFFSET_BITS=64 \
    -I"$SRC_DIR/include" -I"$SRC_DIR/src/rcheevos" \
    "${SOURCES[@]}" -o "$OUT" -lm

# --- sanity check -----------------------------------------------------
# The bridge resolves these by name at runtime; a silent ABI or feature
# regression here would surface as an AttributeError mid-session instead.
# nm runs once into a variable: piping it per-symbol into `grep -q` makes
# grep exit on first match, and the resulting SIGPIPE trips `pipefail`.
exported="$(nm -D --defined-only "$OUT")"
missing=()
for sym in rc_client_create rc_client_destroy rc_client_do_frame \
           rc_client_begin_login_with_password rc_client_begin_load_game \
           rc_client_set_hardcore_enabled rc_client_get_hardcore_enabled \
           rc_client_create_achievement_list rc_client_get_game_info \
           rc_client_get_rich_presence_message rc_client_set_event_handler \
           rc_client_enable_logging rc_client_idle rc_client_unload_game \
           rc_client_get_user_game_summary rc_client_destroy_achievement_list \
           rc_hash_generate_from_buffer; do
    grep -qw "$sym" <<<"$exported" || missing+=("$sym")
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "error: built library is missing symbols: ${missing[*]}" >&2
    exit 1
fi

if [ -z "${KEEP_BUILD:-}" ] && [ -z "${RCHEEVOS_SRC:-}" ]; then
    rm -rf "$BUILD_DIR"
fi

log "built $OUT ($(grep -cw 'T' <<<"$exported") exported symbols)"
log "sha256: $(sha256sum "$OUT" | cut -d' ' -f1)"
