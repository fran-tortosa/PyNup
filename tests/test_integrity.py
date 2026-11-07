from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from pynup_gui.integrity import (
    compare_manifest,
    create_manifest,
)


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "exefs").mkdir()
        (self.source / "romfs").mkdir()
        (self.source / "exefs" / "main").write_bytes(b"program")
        (self.source / "romfs" / "archivo con espacios.bin").write_bytes(b"data")
        (self.source / "romfs" / ".listing").write_text("listing", encoding="utf-8")
        self.manifest = self.root / "original.sha256"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_round_trip_with_sha256_and_listings(self) -> None:
        count = create_manifest(self.source, self.manifest, "SHA-256", True)
        result = compare_manifest(self.source, self.manifest, "SHA-256", True)
        lines = self.manifest.read_text(encoding="utf-8-sig").splitlines()

        self.assertEqual(count, 3)
        self.assertTrue(all(line[64:66] == " *" for line in lines))
        self.assertTrue(result.matches)
        self.assertEqual(result.checked, 3)

    def test_sha512_and_listing_exclusion(self) -> None:
        manifest = self.root / "original.sha512"
        count = create_manifest(self.source, manifest, "SHA-512", False)
        result = compare_manifest(self.source, manifest, "SHA-512", False)
        full_tree_result = compare_manifest(
            self.source, manifest, "SHA-512", True
        )

        self.assertEqual(count, 2)
        self.assertTrue(result.matches)
        self.assertEqual(full_tree_result.extra, ("romfs/.listing",))

    def test_detects_missing_changed_and_additional_files(self) -> None:
        create_manifest(self.source, self.manifest, "SHA-256", True)
        (self.source / "exefs" / "main").write_bytes(b"changed")
        (self.source / "romfs" / "archivo con espacios.bin").unlink()
        (self.source / "romfs" / "nuevo.bin").write_bytes(b"extra")

        result = compare_manifest(self.source, self.manifest, "SHA-256", True)

        self.assertEqual(result.mismatched, ("exefs/main",))
        self.assertEqual(result.missing, ("romfs/archivo con espacios.bin",))
        self.assertEqual(result.extra, ("romfs/nuevo.bin",))
        self.assertFalse(result.matches)

    def test_reads_rapidcrc_style_utf16le_manifest(self) -> None:
        digest = hashlib.sha256(b"program").hexdigest()
        self.manifest.write_bytes(
            f"{digest} *exefs\\main\r\n".encode("utf-16")
        )

        result = compare_manifest(self.source, self.manifest, "SHA-256", False)

        self.assertEqual(result.checked, 1)
        self.assertEqual(result.missing, ())
        self.assertEqual(result.mismatched, ())
        self.assertEqual(
            result.extra,
            ("romfs/archivo con espacios.bin",),
        )

    def test_reads_rapidcrc_manifest_with_absolute_windows_paths(self) -> None:
        files = (
            self.source / "exefs" / "main",
            self.source / "romfs" / "archivo con espacios.bin",
        )
        self.manifest.write_text(
            "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()} *{path}\n"
                for path in files
            ),
            encoding="utf-8",
        )

        result = compare_manifest(
            self.source, self.manifest, "SHA-256", include_listings=False
        )

        self.assertEqual(result.checked, 2)
        self.assertEqual(result.missing, ())
        self.assertEqual(result.mismatched, ())
        self.assertEqual(result.extra, ())


if __name__ == "__main__":
    unittest.main()
