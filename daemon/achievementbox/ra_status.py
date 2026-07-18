"""Authoritative RetroAchievements mode and availability state."""

from __future__ import annotations


VALID_RA_MODES = frozenset({"casual", "hardcore"})

_UNAVAILABLE_REASONS = {
    "cd-session": "mega_cd",
    "no-set": "no_set",
    "unsupported-region": "unsupported_region",
    "region-changed": "region_changed",
    "core-inactive": "core_inactive",
    "capture-invalid": "capture_invalid",
    "offline": "offline",
    "ra-disabled": "ra_disabled",
    "login-failed": "login_failed",
}


def mode_from_native(value: int) -> str:
    """Translate rc_client's exact boolean result, rejecting bad ABI data."""
    if value == 0:
        return "casual"
    if value == 1:
        return "hardcore"
    raise RuntimeError(f"invalid native Hardcore state: {value}")


def validate_mode(mode: str | None) -> str | None:
    if mode is not None and mode not in VALID_RA_MODES:
        raise ValueError(f"invalid RA mode: {mode}")
    return mode


def availability_for_connection(connection: str) -> tuple[str, str | None]:
    """Map transport/session state to generic RA content availability."""
    if connection == "playing":
        return "available", None
    return "unavailable", _UNAVAILABLE_REASONS.get(
        connection, "not_in_game")
