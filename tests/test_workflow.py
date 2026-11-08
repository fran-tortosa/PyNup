from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from pynup_gui.integrity import (
    compare_manifest,
    create_manifest,
    create_manifest_for_sources,
)
from pynup_gui.workflow import (
    ExtractConfig,
    NupService,
    OperationCancelled,
    PackConfig,
    ProgressUpdate,
)


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
        self._add_test_title_id(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def pack_config(
        self,
        name: str = "Juego",
        level: int = 11,
        source: Path | None = None,
    ) -> PackConfig:
        source = source or self.source
        self._add_test_title_id(source)
        container = self.service.container_path_for_source(source)
        return PackConfig(
            source=source,
            container=container,
            manifest=self.service.manifest_path_for_name(container.stem),
            compression_level=level,
        )

    @staticmethod
    def _add_test_title_id(source: Path) -> None:
        if (source / "base").is_dir():
            package_root = source / "base"
        elif (source / "updates").is_dir():
            package_roots = sorted(
                path
                for path in (source / "updates").iterdir()
                if path.is_dir() and path.name.isdecimal()
            )
            package_root = package_roots[0] if package_roots else source
        else:
            package_root = source

        npdm_path = package_root / "exefs" / "main.npdm"
        if npdm_path.is_file():
            return
        npdm_path.parent.mkdir(parents=True, exist_ok=True)
        npdm = bytearray(0x18)
        npdm[:4] = b"ACI0"
        npdm[0x10:0x18] = (0x0100A3A01EE4C000).to_bytes(8, "little")
        npdm_path.write_bytes(npdm)

    def test_pack_creates_manifest_verifies_and_removes_temporary_extraction(
        self,
    ) -> None:
        config = self.pack_config()
        messages: list[str] = []
        updates: list[ProgressUpdate] = []
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
            self.service.pack_and_check(
                config, messages.append, updates.append
            )

        self.assertTrue(config.container.is_file())
        self.assertEqual(config.container.parent, self.service.output_directory)
        self.assertEqual(
            config.container.name, "Juego (0100A3A01EE4C000) (v0).nup"
        )
        self.assertEqual(
            config.manifest.name,
            "Juego (0100A3A01EE4C000) (v0)_original.sha256",
        )
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
        self.assertIn("Creando archivo NUP...", messages)
        archived_logs = sorted(
            (self.service.log_directory / "crc-sha256").glob(
                "*_manifiesto_*.log"
            )
        )
        self.assertEqual(len(archived_logs), 1)
        archived_log = archived_logs[0].read_text(encoding="utf-8")
        self.assertIn("Acción: manifiesto creado", archived_log)
        self.assertIn(f"Manifiesto: {config.manifest.name}", archived_log)
        archived_entries = archived_log.split(
            "Contenido del manifiesto (línea por línea):\n", 1
        )[1].splitlines()
        self.assertEqual(
            archived_entries,
            config.manifest.read_text(encoding="utf-8-sig").splitlines(),
        )
        self.assertFalse(
            list((self.service.log_directory / "crc-sha256").glob("*.sha256"))
        )
        self.assertIn(
            "Creando manifiesto SHA-256 compatible con RapidCRC...", messages
        )
        self.assertIn(
            f"Log del manifiesto guardado: {archived_logs[0]}", messages
        )
        self.assertFalse(
            any("se conservan las 3 más recientes" in message for message in messages)
        )
        comparison_logs = list(
            (self.service.log_directory / "crc-sha256").glob(
                "*_comparacion-empaqueta_*.log"
            )
        )
        self.assertEqual(len(comparison_logs), 1)
        comparison_text = comparison_logs[0].read_text(encoding="utf-8")
        self.assertIn("Tipo: comparación-empaqueta", comparison_text)
        self.assertIn("COINCIDE | exefs/main | esperado=", comparison_text)
        self.assertIn("Resultado final: COINCIDE", comparison_text)
        manifest_creation = [
            update.detail
            for update in updates
            if update.step == 2 and "Creando SHA-256 para verificación" in update.detail
        ]
        manifest_review = [
            update.detail
            for update in updates
            if update.step == 6 and "Revisando " in update.detail
        ]
        self.assertTrue(manifest_creation)
        self.assertTrue(manifest_review)
        self.assertTrue(
            all(
                "de " in detail
                and "archivos del SHA-256 original:" in detail
                for detail in manifest_review
            )
        )

    def test_manifest_logs_keep_only_the_latest_three_and_remove_legacy_copies(
        self,
    ) -> None:
        log_messages: list[str] = []
        archive_directory = self.service.log_directory / "crc-sha256"
        manifests: list[Path] = []
        for index in range(4):
            manifest = self.root / f"juego-{index}_original.sha256"
            manifest.write_text(
                f"{index:064x} *romfs/archivo-{index}.bin\n",
                encoding="utf-8",
            )
            manifests.append(manifest)
            self.service._archive_manifest_log(
                manifest, log_messages.append
            )
            archived_log = next(
                archive_directory.glob(f"*_manifiesto_{manifest.stem}.log")
            )
            os.utime(archived_log, (index + 100, index + 100))

        legacy_copy = archive_directory / "old_original.sha256"
        legacy_copy.write_text("legacy manifest\n", encoding="utf-8")
        newest_manifest = self.root / "juego-4_original.sha256"
        newest_manifest.write_text(
            f"{4:064x} *romfs/archivo-4.bin\n",
            encoding="utf-8",
        )
        self.service._archive_manifest_log(
            newest_manifest, log_messages.append
        )
        snapshots = sorted(archive_directory.glob("*.log"))

        self.assertEqual(len(snapshots), 3)
        self.assertEqual(
            {
                line.split("Manifiesto: ", 1)[1]
                for path in snapshots
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("Manifiesto: ")
            },
            {manifests[2].name, manifests[3].name, newest_manifest.name},
        )
        self.assertFalse(legacy_copy.exists())
        self.assertEqual(len(log_messages), 5)

    def test_existing_manifest_review_is_archived_line_by_line(self) -> None:
        config = self.pack_config()
        layouts = self.service._discover_source_packages(config.source)
        source_directories = tuple(
            layout.relative_path for layout in layouts
        )
        create_manifest_for_sources(
            config.source,
            source_directories,
            config.manifest,
            "SHA-256",
            True,
        )
        messages: list[str] = []
        updates: list[ProgressUpdate] = []

        self.service._ensure_source_manifest(
            config.source,
            layouts,
            config.manifest,
            True,
            messages.append,
            None,
            updates.append,
        )

        archived_logs = list(
            (self.service.log_directory / "crc-sha256").glob(
                "*_manifiesto_*.log"
            )
        )
        self.assertEqual(len(archived_logs), 1)
        archived_text = archived_logs[0].read_text(encoding="utf-8")
        self.assertIn("Acción: manifiesto revisado", archived_text)
        archived_entries = archived_text.split(
            "Contenido del manifiesto (línea por línea):\n", 1
        )[1].splitlines()
        self.assertEqual(
            archived_entries,
            config.manifest.read_text(encoding="utf-8-sig").splitlines(),
        )
        self.assertTrue(
            any("Manifiesto original comprobado" in message for message in messages)
        )
        self.assertIn(
            "Revisando manifiesto SHA-256 compatible con RapidCRC...", messages
        )
        comparison_logs = list(
            (self.service.log_directory / "crc-sha256").glob(
                "*_comparacion-empaqueta_*.log"
            )
        )
        self.assertEqual(len(comparison_logs), 1)
        comparison_text = comparison_logs[0].read_text(encoding="utf-8")
        self.assertIn("Tipo: comparación-empaqueta", comparison_text)
        self.assertIn("COINCIDE | exefs/main | esperado=", comparison_text)
        self.assertIn("Resultado final: COINCIDE", comparison_text)

    def test_zstandard_logs_keep_only_the_latest_three_operations(self) -> None:
        archive_directory = self.service.log_directory / "zstandard"
        archive_directory.mkdir(parents=True)
        logs: list[Path] = []
        for index in range(4):
            log_path = archive_directory / f"operation-{index}.log"
            log_path.write_text(f"operation {index}\n", encoding="utf-8")
            os.utime(log_path, (index + 100, index + 100))
            logs.append(log_path)

        messages: list[str] = []
        self.service.retain_recent_zstandard_logs(logs[-1], messages.append)

        self.assertEqual(
            {path.name for path in archive_directory.glob("*.log")},
            {"operation-1.log", "operation-2.log", "operation-3.log"},
        )
        self.assertEqual(len(messages), 1)

    def test_container_name_uses_title_id_name_database(self) -> None:
        (self.root / "db-name.ini").write_text(
            '[games]\n0100A3A01EE4C000 = "Nombre del juego"\n',
            encoding="utf-8",
        )

        container = self.service.container_path_for_source(self.source)

        self.assertEqual(
            container.name,
            "Nombre del juego (0100A3A01EE4C000) (v0).nup",
        )

    def test_container_name_falls_back_to_source_folder_without_database_entry(
        self,
    ) -> None:
        (self.root / "db-name.ini").write_text(
            '[games]\n0100FFFFFFFFFFFF = "Otro juego"\n',
            encoding="utf-8",
        )

        container = self.service.container_path_for_source(self.source)

        self.assertEqual(
            container.name,
            "Juego (0100A3A01EE4C000) (v0).nup",
        )

    def test_missing_database_template_is_copied_beside_executable(self) -> None:
        database_path = self.root / "db-name.ini"
        self.assertFalse(database_path.exists())

        self.service.container_path_for_source(self.source)

        self.assertEqual(
            database_path.read_text(encoding="utf-8"),
            (self.app_root / "db-name.ini").read_text(encoding="utf-8"),
        )

    def test_extract_rejects_unsafe_nup_path_components(self) -> None:
        if str(self.service.nup_python_path) not in sys.path:
            sys.path.insert(0, str(self.service.nup_python_path))
        from nup import access, format as nup_format
        from nup.read import NodeRef

        class FakeReader:
            @staticmethod
            def read_file(_index: int) -> bytes:
                return b"untrusted"

        for unsafe_name in (
            b"..",
            b"..\\escaped.txt",
            b"C:escaped.txt",
            b"CON",
            b"CON.txt",
            b"safe:stream",
        ):
            destination = self.root / f"Extracted_{len(unsafe_name)}"
            with self.subTest(name=unsafe_name), patch.object(
                access, "iter_children",
                return_value=[NodeRef(False, 0, unsafe_name)],
            ), self.assertRaises(nup_format.NupError):
                access.extract_tree(FakeReader(), str(destination), 0)

        self.assertFalse((self.root.parent / "escaped.txt").exists())

    def test_failed_unsafe_extraction_removes_partial_destination(self) -> None:
        if str(self.service.nup_python_path) not in sys.path:
            sys.path.insert(0, str(self.service.nup_python_path))
        from nup import access, format as nup_format
        from nup.read import NodeRef

        class FakeReader:
            @staticmethod
            def read_file(_index: int) -> bytes:
                return b"untrusted"

        container = self.root / "hostile.nup"
        container.touch()
        destination = self.root / "UnsafeExtraction"

        def extract_unsafe_path(*_args, **_kwargs) -> None:
            access.extract_tree(
                FakeReader(),
                str(destination),
                0,
            )

        with patch.object(
            access, "iter_children",
            return_value=[NodeRef(False, 0, b"..\\escaped.txt")],
        ), patch.object(
            self.service, "_run_nup"
        ), patch.object(
            self.service, "_extract_all_packages",
            side_effect=extract_unsafe_path,
        ), self.assertRaises(nup_format.NupError):
            self.service.extract_and_check(
                ExtractConfig(container, destination),
                lambda _message: None,
            )

        self.assertFalse(destination.exists())

    def test_pack_reports_six_steps_and_compression_progress_data(self) -> None:
        config = self.pack_config()
        updates: list[ProgressUpdate] = []

        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(
                config,
                lambda _message: None,
                updates.append,
            )

        self.assertEqual({update.step for update in updates}, {1, 2, 3, 4, 5, 6})
        compression_updates = [
            update
            for update in updates
            if update.step == 3 and update.phase == "comprimiendo"
        ]
        self.assertTrue(compression_updates)
        final_compression = compression_updates[-1]
        self.assertEqual(final_compression.bytes_done, final_compression.total_bytes)
        self.assertGreater(final_compression.stored_bytes or 0, 0)
        self.assertGreaterEqual(final_compression.speed_mb_s or 0, 0)
        self.assertEqual(updates[-1].step, 6)
        self.assertEqual(updates[-1].percent, 100.0)

    def test_pack_streams_large_files_in_64_mib_blocks(
        self,
    ) -> None:
        source = self.root / "JuegoGrande"
        large_file = source / "romfs" / "large.bin"
        large_file.parent.mkdir(parents=True)
        with large_file.open("wb") as stream:
            stream.truncate(130 * 1024 * 1024)
        config = self.pack_config("JuegoGrande", source=source)
        updates: list[ProgressUpdate] = []

        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(
                config,
                lambda _message: None,
                updates.append,
            )

        compression_updates = [
            update
            for update in updates
            if update.step == 3 and update.phase == "comprimiendo"
        ]
        self.assertGreater(len(compression_updates), 2)
        self.assertEqual(
            compression_updates[-1].bytes_done,
            130 * 1024 * 1024 + 24,
        )
        self.assertLess(
            compression_updates[-1].stored_bytes or 0,
            compression_updates[-1].total_bytes or 0,
        )

    def test_pack_processes_files_under_16_mib_in_one_piece(self) -> None:
        source = self.root / "JuegoMediano"
        large_file = source / "romfs" / "medium.bin"
        large_file.parent.mkdir(parents=True)
        with large_file.open("wb") as stream:
            stream.truncate(12 * 1024 * 1024)
        config = self.pack_config("JuegoMediano", source=source)
        updates: list[ProgressUpdate] = []

        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(
                config,
                lambda _message: None,
                updates.append,
            )

        compression_updates = [
            update
            for update in updates
            if update.step == 3 and update.phase == "comprimiendo"
        ]
        self.assertEqual(len(compression_updates), 2)
        self.assertEqual(
            compression_updates[-1].bytes_done,
            12 * 1024 * 1024 + 24,
        )

    def test_cancel_pack_removes_partial_output_and_temp_but_keeps_manifest(
        self,
    ) -> None:
        source = self.root / "JuegoCancelado"
        large_file = source / "romfs" / "large.bin"
        large_file.parent.mkdir(parents=True)
        with large_file.open("wb") as stream:
            stream.truncate(20 * 1024 * 1024)
        config = self.pack_config("JuegoCancelado", source=source)
        cancel_event = Event()
        self.service.temp_directory.mkdir(parents=True)
        (self.service.temp_directory / "temporal-anterior.tmp").write_bytes(b"tmp")

        def cancel_during_compression(update: ProgressUpdate) -> None:
            if update.phase == "comprimiendo" and update.bytes_done:
                cancel_event.set()

        with patch.object(sys, "frozen", True, create=True):
            with self.assertRaises(OperationCancelled):
                self.service.pack_and_check(
                    config,
                    lambda _message: None,
                    cancel_during_compression,
                    cancel_event,
                )

        self.assertTrue(config.manifest.is_file())
        self.assertFalse(config.container.exists())
        self.assertEqual(list(self.service.temp_directory.iterdir()), [])

    def test_cancel_extract_removes_partial_destination(self) -> None:
        pack_config = self.pack_config()
        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(pack_config, lambda _message: None)
        destination = self.root / "extraccion-cancelada"
        config = ExtractConfig(
            container=pack_config.container,
            extraction=destination,
            manifest=pack_config.manifest,
        )
        cancel_event = Event()

        def cancel_during_extraction(update: ProgressUpdate) -> None:
            if update.step == 4 and update.percent:
                cancel_event.set()

        with self.assertRaises(OperationCancelled):
            self.service.extract_and_check(
                config,
                lambda _message: None,
                cancel_during_extraction,
                cancel_event,
            )

        self.assertFalse(destination.exists())
        self.assertTrue(pack_config.manifest.is_file())

    def test_extract_without_manifest_verifies_and_skips_hash_comparison(self) -> None:
        pack_config = self.pack_config()
        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(pack_config, lambda _message: None)
        pack_config.manifest.unlink()

        destination = self.root / "sin-manifiesto"
        messages: list[str] = []
        with patch.object(sys, "frozen", True, create=True):
            self.service.extract_and_check(
                ExtractConfig(pack_config.container, destination),
                messages.append,
            )

        self.assertTrue((destination / "exefs" / "main").is_file())
        self.assertIn("verify: OK (deep)", "\n".join(messages))
        self.assertIn("se omite la comparación SHA-256", "\n".join(messages))
        self.assertNotIn("índices DLC", "\n".join(messages))

    def test_extract_without_manifest_infers_update_and_dlc_destinations(self) -> None:
        source = self.root / "JuegoConPaquetes"
        files = {
            "base/romfs/base.bin": b"base",
            "updates/655360/romfs/update.bin": b"update",
            "dlc/010074A01BF12000/0/romfs/dlc-zero.bin": b"dlc zero",
            "dlc/010074A01BF12000/1/romfs/dlc-one.bin": b"dlc one",
        }
        for relative, content in files.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        config = self.pack_config("JuegoConPaquetes", source=source)
        with patch.object(sys, "frozen", True, create=True):
            self.service.pack_and_check(config, lambda _message: None)
        config.manifest.unlink()

        destination = self.root / "juego-sin-manifiesto"
        messages: list[str] = []
        with patch.object(sys, "frozen", True, create=True):
            self.service.extract_and_check(
                ExtractConfig(config.container, destination),
                messages.append,
            )

        for relative, content in files.items():
            self.assertEqual((destination / relative).read_bytes(), content)
        self.assertIn("índices DLC se asignaron por orden", "\n".join(messages))

    def test_large_files_are_verified_and_extracted_in_bounded_chunks(self) -> None:
        source = self.root / "JuegoGrande"
        large_file = source / "romfs" / "large.bin"
        large_file.parent.mkdir(parents=True)
        payload = b"0123456789abcdef" * (1 << 18)
        large_file.write_bytes(payload)
        config = self.pack_config("JuegoGrande", source=source)
        if str(self.service.nup_python_path) not in sys.path:
            sys.path.insert(0, str(self.service.nup_python_path))
        from nup.read import NupReader
        from nup.verify import verify

        with patch.object(
            sys, "frozen", True, create=True
        ), patch.object(
            NupReader,
            "read_file",
            side_effect=AssertionError("large files must not be read at once"),
        ), patch(
            "mmap.mmap",
            side_effect=AssertionError("the container must not be memory-mapped"),
        ):
            self.service.pack_and_check(config, lambda _message: None)
            self.assertEqual(verify(str(config.container), deep=True), (True, []))

        with NupReader(str(config.container)) as reader:
            file_index = next(
                index
                for index, file_info in enumerate(reader.files)
                if file_info[0] == b"large.bin"
            )
            chunks = list(
                reader.iter_file_chunks(file_index, chunk_size=64 * 1024)
            )
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 64 * 1024 for chunk in chunks))
        self.assertEqual(b"".join(chunks), payload)

        config.manifest.unlink()
        destination = self.root / "JuegoGrande-extraido"
        with patch.object(
            sys, "frozen", True, create=True
        ), patch.object(
            NupReader,
            "read_file",
            side_effect=AssertionError("large files must not be read at once"),
        ), patch(
            "mmap.mmap",
            side_effect=AssertionError("the container must not be memory-mapped"),
        ):
            self.service.extract_and_check(
                ExtractConfig(config.container, destination),
                lambda _message: None,
            )
        self.assertEqual((destination / "romfs" / "large.bin").read_bytes(), payload)

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
            config.container.unlink()

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
        self.assertEqual(
            config.container.name,
            "Coleccion (0100A3A01EE4C000) (v720896).nup",
        )
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

        destination = self.root / config.container.stem
        self.assertEqual(destination.name, config.container.stem)
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
        self.assertEqual(
            config.container.name,
            "SoloActualizaciones (0100A3A01EE4C000) (v720896).nup",
        )
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
        updates: list[ProgressUpdate] = []
        with patch.object(sys, "frozen", True, create=True):
            self.service.extract_and_check(
                config, messages.append, updates.append
            )

        self.assertTrue((destination / "exefs" / "main").is_file())
        self.assertTrue((destination / "romfs" / ".listing").is_file())
        self.assertIn(
            "EXTRACCIÓN COMPLETA: el manifiesto RapidCRC SHA-256 coincide.",
            messages,
        )
        self.assertIn(
            "Revisando manifiesto SHA-256 compatible con RapidCRC...", messages
        )
        comparison_logs = list(
            (self.service.log_directory / "crc-sha256").glob(
                "*_comparacion-extrae_*.log"
            )
        )
        self.assertEqual(len(comparison_logs), 1)
        comparison_text = comparison_logs[0].read_text(encoding="utf-8")
        self.assertIn("Tipo: comparación-extrae", comparison_text)
        self.assertIn("COINCIDE | exefs/main | esperado=", comparison_text)
        self.assertIn("Resultado final: COINCIDE", comparison_text)
        manifest_review = [
            update.detail
            for update in updates
            if update.step == 5 and update.detail.startswith("Revisando ")
        ]
        self.assertTrue(manifest_review)
        self.assertIn("archivos del SHA-256 original", manifest_review[0])

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

        messages: list[str] = []
        with patch.object(sys, "frozen", True, create=True):
            with self.assertRaisesRegex(RuntimeError, "no coincide"):
                self.service.extract_and_check(
                    ExtractConfig(
                        container=changed_container,
                        extraction=self.root / "desempaquetado-cambiado",
                        manifest=pack_config.manifest,
                    ),
                    messages.append,
                )
        comparison_logs = list(
            (self.service.log_directory / "crc-sha256").glob(
                "*_comparacion-extrae_*.log"
            )
        )
        self.assertEqual(len(comparison_logs), 1)
        comparison_text = comparison_logs[0].read_text(encoding="utf-8")
        self.assertIn("HASH_DISTINTO | exefs/main | esperado=", comparison_text)
        self.assertIn("Resultado final: NO COINCIDE", comparison_text)
        self.assertIn(
            "Revisando manifiesto SHA-256 compatible con RapidCRC...", messages
        )


if __name__ == "__main__":
    unittest.main()
