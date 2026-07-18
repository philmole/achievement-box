"""Release component inventory and SHA-256 verification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "release-integrity.json"
VERSION_SOURCE = "daemon/achievementbox/version.py"


@dataclass(frozen=True)
class IntegrityResult:
    name: str
    path: Path
    expected: str
    actual: str | None
    shipped: bool

    @property
    def ok(self) -> bool:
        return self.actual == self.expected

    @property
    def status(self) -> str:
        if self.actual is None:
            return "missing"
        return "ok" if self.ok else "mismatch"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1:
        raise ValueError("unsupported integrity manifest schema")
    if data.get("version_source") != VERSION_SOURCE:
        raise ValueError("integrity manifest does not use the canonical "
                         "application version source")
    components = data.get("components")
    if not isinstance(components, dict) or not components:
        raise ValueError("integrity manifest has no components")
    return data


def verify_manifest(path: Path = DEFAULT_MANIFEST, *,
                    root: Path = ROOT,
                    shipped_only: bool = False) -> list[IntegrityResult]:
    data = load_manifest(path)
    results = []
    for name, component in data["components"].items():
        shipped = component.get("shipped") is True
        if shipped_only and not shipped:
            continue
        component_path = root / component["path"]
        expected = component["sha256"].casefold()
        if (len(expected) != 64 or
                any(character not in "0123456789abcdef"
                    for character in expected)):
            raise ValueError(f"invalid SHA-256 for component {name}")
        actual = (sha256_file(component_path)
                  if component_path.is_file() else None)
        results.append(IntegrityResult(
            name=name,
            path=component_path,
            expected=expected,
            actual=actual,
            shipped=shipped,
        ))
    return results
