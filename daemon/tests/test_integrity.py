"""Release-integrity manifest regression tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

DAEMON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAEMON_DIR))

from achievementbox.integrity import sha256_file, verify_manifest  # noqa: E402


class IntegrityManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.component = self.root / "component.bin"
        self.component.write_bytes(b"approved artifact")
        self.manifest = self.root / "release-integrity.json"
        self._write_manifest(sha256_file(self.component))

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_manifest(self, digest: str,
                        version_source="daemon/achievementbox/version.py"):
        self.manifest.write_text(json.dumps({
            "schema": 1,
            "version_source": version_source,
            "components": {
                "test": {
                    "path": "component.bin",
                    "sha256": digest,
                    "shipped": True,
                },
                "external": {
                    "path": "external.exe",
                    "sha256": "0" * 64,
                    "shipped": False,
                },
            },
        }), encoding="utf-8")

    def test_approved_component_passes(self):
        result = verify_manifest(self.manifest, root=self.root,
                                 shipped_only=True)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].ok)
        self.assertEqual(result[0].status, "ok")

    def test_modified_and_missing_components_fail(self):
        self.component.write_bytes(b"modified")
        results = verify_manifest(self.manifest, root=self.root)
        self.assertEqual([result.status for result in results],
                         ["mismatch", "missing"])
        self.assertFalse(any(result.ok for result in results))

    def test_manifest_must_use_canonical_version_source(self):
        self._write_manifest(sha256_file(self.component), "another-version")
        with self.assertRaisesRegex(ValueError, "version source"):
            verify_manifest(self.manifest, root=self.root)


if __name__ == "__main__":
    unittest.main()
