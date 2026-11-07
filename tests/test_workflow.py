from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

from pynup_gui.integrity import compare_manifest, create_manifest
from pynup_gui.workflow import ExtractConfig, NupService, PackConfig


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app_root = Path(__file__).resolve().parents[1]
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = NupService(self.app_root, self.root)
        self.source = self.root / "Juego"
        (self.source / "exefs").mkdir(parents=True)
        (self.source / "romfs" / "datos").mkdir(parents=True)
        (self.source / "exefs" / "main").write_bytes(b"test executable")
        (self.source / "romfs" / "datos" / "archivo.bin").write_bytes(
            b"contenido de prueba"
        )
        (self.source / "romfs" / ".listing").write_text(
            "listado", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def pack_config(
        self,
        name: str = "Juego",
        level: int = 11,
        source: Path | None = None,
    ) -> PackConfig:
        return PackConfig(
            source=source or self.source,
            container=self.service.container_path_for_name(name),
            manifest=self.service.manifest_path_for_name(name),
            compression_level=level,
        )

    def test_pack_creates_manifest_verifies_and_removes_temporary_extraction(
        self,
    ) -> None:
        config = self.pack_config()
        messages: list[str] = []
        extracted_paths: list[Path] = []
        extract_packages = self.service._extract_all_packages

        def capture_extraction(
            container: Path,
            destination: Path,
            log,
            **kwargs,
        ) -> None:
            extracted_paths.append(destination)
            extract_packages(container, destination, log, **kwargs)

        with patch.object(
            self.service, "_extract_all_packages", side_effect=capture_extraction
        ), patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(config, messages.append)

        self.assertTrue(config.container.is_file())
        self.assertEqual(config.container.parent, self.service.output_directory)
        self.assertTrue(config.manifest.is_file())
        self.assertTrue(
            all(line[64:66] == " *" for line in config.manifest.read_text(
                encoding="utf-8-sig"
            ).splitlines())
        )
        self.assertTrue(
            compare_manifest(
                self.source, config.manifest, "SHA-256", True
            ).matches
        )
        self.assertEqual(len(extracted_paths), 1)
        self.assertFalse(extracted_paths[0].exists())
        self.assertEqual(extracted_paths[0].parent, self.service.temp_directory)
        self.assertTrue(
            any(message.startswith("FLUJO COMPLETO: CORRECTO") for message in messages)
        )
        self.assertIn("Extracción temporal eliminada.", messages)
        self.assertIn(
            "Compresión Zstandard seleccionada: MEDIA, nivel 11.",
            messages,
        )

    def test_pack_preserves_existing_manifest_and_stops_on_mismatch(self) -> None:
        config = self.pack_config()
        config.manifest.parent.mkdir(parents=True)
        config.manifest.write_text(
            "0" * 64 + " *exefs/main\n",
            encoding="utf-8",
        )

        with patch.object(sys, "frozen", True, create=True):
            with self.assertRaisesRegex(RuntimeError, "No se empaquetará"):
                self.service.pack_and_check(config, lambda _message: None)

        self.assertFalse(config.container.exists())

    def test_pack_replaces_empty_manifest_with_automatic_sha256_manifest(self) -> None:
        config = self.pack_config()
        config.manifest.parent.mkdir(parents=True)
        config.manifest.write_bytes(b"")

        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(config, lambda _message: None)

        self.assertTrue(config.manifest.is_file())
        self.assertGreater(config.manifest.stat().st_size, 0)
        self.assertTrue(
            compare_manifest(
                self.source,
                config.manifest,
                "SHA-256",
                True,
            ).matches
        )

    def test_pack_rejects_manifest_outside_executable_crc_directory(self) -> None:
        config = PackConfig(
            source=self.source,
            container=self.service.container_path_for_name("outside"),
            manifest=self.root / "outside.sha256",
        )

        with patch.object(sys, "frozen", True, create=True):
            with self.assertRaisesRegex(ValueError, "crc-sha256 del ejecutable"):
                self.service.pack_and_check(config, lambda _message: None)

        self.assertFalse(config.container.exists())

    def test_pack_rejects_container_outside_executable_output_directory(self) -> None:
        config = PackConfig(
            source=self.source,
            container=self.root / "outside.nup",
            manifest=self.service.manifest_path_for_name("outside"),
        )

        with patch.object(sys, "frozen", True, create=True):
            with self.assertRaisesRegex(ValueError, "guardarse en output"):
                self.service.pack_and_check(config, lambda _message: None)

        self.assertFalse(config.container.exists())

    def test_pack_accepts_only_the_three_gui_compression_levels(self) -> None:
        config = self.pack_config(level=4)

        with patch.object(sys, "frozen", True, create=True):
            with self.assertRaisesRegex(ValueError, "bajo 3, medio 11 o alto 22"):
                self.service.pack_and_check(config, lambda _message: None)

        self.assertFalse(config.container.exists())

    def test_pack_reports_selected_low_and_high_compression_levels(self) -> None:
        for name, level, label in (
            ("JuegoBajo", 3, "BAJA, nivel 3"),
            ("JuegoAlto", 22, "ALTA, nivel 22"),
        ):
            config = self.pack_config(name, level)
            messages: list[str] = []
            with self.subTest(level=level), patch.object(
                sys, "frozen", True, create=True
            ):
                self.service.pack_and_check(config, messages.append)
            self.assertIn(
                f"Compresión Zstandard seleccionada: {label}.",
                messages,
            )

    def test_pack_removes_temporary_extraction_when_hash_check_fails(self) -> None:
        config = self.pack_config()
        extracted_paths: list[Path] = []
        extract_packages = self.service._extract_all_packages

        def corrupt_extraction(
            container: Path,
            destination: Path,
            log,
            **kwargs,
        ) -> None:
            extracted_paths.append(destination)
            extract_packages(container, destination, log, **kwargs)
            (destination / "exefs" / "main").write_bytes(b"corrupted")

        with patch.object(
            self.service, "_extract_all_packages", side_effect=corrupt_extraction
        ), patch.object(sys, "frozen", True, create=True):
            with self.assertRaisesRegex(RuntimeError, "no coincide"):
                self.service.pack_and_check(config, lambda _message: None)

        self.assertEqual(len(extracted_paths), 1)
        self.assertFalse(extracted_paths[0].exists())

    def test_discovers_nested_multi_package_dump_and_skips_invalid_dlc(self) -> None:
        source = self.root / "Coleccion"
        files = {
            "base/exefs/main": b"base program",
            "base/romfs/nested/base.bin": b"base data",
            "base/romfs/.listing": b"listing",
            "updates/655360/romfs/nested/update.bin": b"update one",
            "updates/720896/exefs/main": b"update two",
            "updates/not-a-version/romfs/ignored.bin": b"ignored version",
            "updates/123/other/ignored.bin": b"ignored content",
            "dlc/010074A01BF12000/0/romfs/nested/dlc.bin": b"dlc zero",
            "dlc/010074A01BF12000/1/exefs/sub/main": b"dlc one",
            "dlc/invalid/0/romfs/ignored.bin": b"ignored id",
            "dlc/010074A01BF12000/2/other/ignored.bin": b"ignored content",
        }
        for relative, content in files.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        config = self.pack_config("Coleccion", source=source)
        messages: list[str] = []
        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(config, messages.append)

        self.assertIn(
            "Estructura NUP detectada: 5 paquete(s) (base, actualizaciones y/o DLC).",
            messages,
        )
        entries = config.manifest.read_text(encoding="utf-8-sig")
        self.assertIn("base/romfs/nested/base.bin", entries)
        self.assertIn("updates/655360/romfs/nested/update.bin", entries)
        self.assertIn("dlc/010074A01BF12000/1/exefs/sub/main", entries)
        self.assertNotIn("ignored.bin", entries)

        destination = self.root / "Coleccion_extraida"
        with patch.object(sys, "frozen", True, create=True):
            self.service.extract_and_check(
                ExtractConfig(config.container, destination, config.manifest),
                messages.append,
            )
        for relative, content in files.items():
            path = destination / relative
            if "ignored" in relative:
                self.assertFalse(path.exists())
            else:
                self.assertEqual(path.read_bytes(), content)

    def test_replaces_legacy_full_dump_manifest_with_packaged_file_manifest(
        self,
    ) -> None:
        source = self.root / "LegacyBundle"
        (source / "base/romfs").mkdir(parents=True)
        (source / "base/romfs/data.bin").write_bytes(b"packaged")
        (source / "dlc/not-a-title/0/romfs").mkdir(parents=True)
        (source / "dlc/not-a-title/0/romfs/ignored.bin").write_bytes(
            b"not a recognized package"
        )
        config = self.pack_config("LegacyBundle", source=source)
        config.manifest.parent.mkdir(parents=True)
        create_manifest(source, config.manifest, "SHA-256", True)

        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(config, lambda _message: None)

        manifest_contents = config.manifest.read_text(encoding="utf-8-sig")
        self.assertIn("base/romfs/data.bin", manifest_contents)
        self.assertNotIn("ignored.bin", manifest_contents)

    def test_update_only_dump_is_packed_with_original_version_folders(self) -> None:
        source = self.root / "SoloActualizaciones"
        for relative, content in (
            ("updates/655360/romfs/data/one.bin", b"one"),
            ("updates/720896/exefs/main", b"two"),
        ):
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        config = self.pack_config("SoloActualizaciones", source=source)
        messages: list[str] = []
        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(config, messages.append)

        destination = self.root / "SoloActualizaciones_extraida"
        with patch.object(sys, "frozen", True, create=True):
            self.service.extract_and_check(
                ExtractConfig(config.container, destination, config.manifest),
                messages.append,
            )
        self.assertEqual(
            (destination / "updates/655360/romfs/data/one.bin").read_bytes(),
            b"one",
        )
        self.assertEqual(
            (destination / "updates/720896/exefs/main").read_bytes(),
            b"two",
        )

    def test_single_content_dump_can_contain_only_romfs(self) -> None:
        source = self.root / "RomfsOnly"
        file_path = source / "romfs/nested/data.bin"
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(b"romfs")
        config = self.pack_config("RomfsOnly", source=source)

        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(config, lambda _message: None)

        self.assertTrue(config.container.is_file())

    def test_extract_verifies_persistent_extraction_against_manifest(self) -> None:
        pack_config = self.pack_config()
        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(pack_config, lambda _message: None)

        destination = self.root / "desempaquetado"
        config = ExtractConfig(
            container=pack_config.container,
            extraction=destination,
            manifest=pack_config.manifest,
        )
        messages: list[str] = []
        with patch.object(sys, "frozen", True, create=True):
            self.service.extract_and_check(config, messages.append)

        self.assertTrue((destination / "exefs" / "main").is_file())
        self.assertTrue((destination / "romfs" / ".listing").is_file())
        self.assertIn(
            "EXTRACCIÓN COMPLETA: el manifiesto RapidCRC SHA-256 coincide.",
            messages,
        )

    def test_extract_reports_hash_mismatch(self) -> None:
        pack_config = self.pack_config()
        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(pack_config, lambda _message: None)

        changed_container = self.service.container_path_for_name("cambiado")
        self.service.output_directory.mkdir(parents=True, exist_ok=True)
        (self.source / "exefs" / "main").write_bytes(b"changed")
        with patch.object(sys, "frozen", True, create=True):
            self.service._run_nup(
                [
                    "pack",
                    str(self.source),
                    str(changed_container),
                    "--keep-listings",
                    "--quiet",
                ],
                lambda _message: None,
            )

        with patch.object(sys, "frozen", True, create=True):
            with self.assertRaisesRegex(RuntimeError, "no coincide"):
                self.service.extract_and_check(
                    ExtractConfig(
                        container=changed_container,
                        extraction=self.root / "desempaquetado-cambiado",
                        manifest=pack_config.manifest,
                    ),
                    lambda _message: None,
                )


if __name__ == "__main__":
    unittest.main()
