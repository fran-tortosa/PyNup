from __future__ import annotations

import contextlib
import configparser
import importlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from threading import Event
from typing import Callable, Protocol

from .integrity import (
    ComparisonResult,
    ManifestComparisonFunction,
    compare_manifest,
    compare_manifest_for_sources,
    create_manifest_for_sources,
    display_comparison,
    path_is_within,
    read_manifest_entries,
)

LogFunction = Callable[[str], None]
ManifestComparison = Callable[
    [ManifestComparisonFunction], ComparisonResult
]


@dataclass(frozen=True)
class ProgressUpdate:
    step: int
    title: str
    percent: float | None = None
    compression_percent: float | None = None
    detail: str = ""
    phase: str = ""
    bytes_done: int | None = None
    total_bytes: int | None = None
    stored_bytes: int | None = None
    speed_mb_s: float | None = None
    eta_seconds: float | None = None


ProgressFunction = Callable[[ProgressUpdate], None]


class OperationCancelled(Exception):
    pass


@dataclass(frozen=True)
class PackConfig:
    source: Path
    container: Path
    manifest: Path
    compression_level: int = 3
    keep_listings: bool = True


@dataclass(frozen=True)
class ExtractConfig:
    container: Path
    extraction: Path
    manifest: Path | None = None
    keep_listings: bool = True


@dataclass(frozen=True)
class PackageLayout:
    kind: int
    kind_name: str
    title_id: int
    version: int
    relative_path: Path


class PackageRecord(Protocol):
    index: int
    title_id: int
    kind: int
    kind_name: str
    version: int
    root_dir: int


class SourceFile(Protocol):
    src_path: str


class ExtractedFile(Protocol):
    name: bytes


class NupService:
    def __init__(self, app_root: Path, data_root: Path | None = None) -> None:
        self.app_root = app_root.resolve()
        self.data_root = (data_root or app_root).resolve()
        self.output_directory = self.data_root / "output"
        self.manifest_directory = self.data_root / "crc-sha256"
        self.temp_directory = self.data_root / "temp"
        self.log_directory = self.data_root / "log"
        self.nup_python_path = self.app_root / "vendor" / "nup" / "tools" / "nup"
        if not (self.nup_python_path / "nup" / "cli.py").is_file():
            raise FileNotFoundError(
                f"No se encuentra la implementación NUP en {self.nup_python_path}"
            )

    def manifest_path_for_name(self, name: str) -> Path:
        return self.manifest_directory / f"{name}_original.sha256"

    def container_path_for_name(self, name: str) -> Path:
        return self.output_directory / f"{name}.nup"

    def container_name_for_source(self, source: Path) -> str:
        layouts = self._discover_source_packages(source)
        return self._container_name_for_layouts(layouts, source.name)

    def _container_name_for_layouts(
        self, layouts: tuple[PackageLayout, ...], fallback_name: str
    ) -> str:
        base_packages = [layout for layout in layouts if layout.kind_name == "base"]
        update_packages = [
            layout for layout in layouts if layout.kind_name == "update"
        ]
        addon_packages = [
            layout for layout in layouts if layout.kind_name == "addon"
        ]

        if base_packages and base_packages[0].title_id:
            title_id = base_packages[0].title_id
        elif update_packages:
            title_id = next(
                (layout.title_id for layout in update_packages if layout.title_id),
                0,
            )
        elif addon_packages:
            addon_title_ids = {layout.title_id for layout in addon_packages}
            if len(addon_title_ids) != 1:
                raise ValueError(
                    "No se puede crear un nombre único: el dump solo contiene "
                    "DLC de varios TITLE ID."
                )
            title_id = addon_title_ids.pop()
        else:
            raise ValueError(
                "No se encontró un TITLE ID de juego, actualización o DLC."
            )

        if not title_id:
            raise ValueError(
                "NUP no pudo identificar el TITLE ID. Comprueba que el dump "
                "contenga su archivo main.npdm."
            )
        title_name = self._game_name_for_title_id(title_id) or fallback_name
        title_name = re.sub(
            r'[<>:"/\\|?*\x00-\x1f]', "_", title_name
        ).rstrip(" .")
        if not title_name:
            raise ValueError(
                "No se pudo obtener un nombre válido de db-name.ini ni de "
                "la carpeta de origen."
            )
        highest_version = max(layout.version for layout in layouts)
        return f"{title_name} ({title_id:016X}) (v{highest_version})"

    def _game_name_for_title_id(self, title_id: int) -> str | None:
        database_path = self.data_root / "db-name.ini"
        if not database_path.exists():
            template_path = self.app_root / "db-name.ini"
            if template_path.is_file() and template_path != database_path:
                try:
                    shutil.copyfile(template_path, database_path)
                except OSError as exc:
                    raise ValueError(
                        f"No se pudo crear la base de nombres junto a la "
                        f"aplicación ({database_path}): {exc}"
                    ) from exc
        if not database_path.is_file():
            return None

        database = configparser.ConfigParser(interpolation=None)
        try:
            with database_path.open(encoding="utf-8-sig") as database_file:
                database.read_file(database_file)
        except (OSError, configparser.Error) as exc:
            raise ValueError(
                f"No se pudo leer la base de nombres {database_path}: {exc}"
            ) from exc

        if not database.has_section("games"):
            return None
        for configured_title_id, configured_name in database.items("games"):
            normalized_id = configured_title_id.strip().upper()
            if not re.fullmatch(r"[0-9A-F]{16}", normalized_id):
                raise ValueError(
                    f"TITLE ID no válido en {database_path}: "
                    f"{configured_title_id}"
                )
            if int(normalized_id, 16) != title_id:
                continue
            name = configured_name.strip()
            if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'":
                name = name[1:-1].strip()
            return name or None
        return None

    def container_path_for_source(self, source: Path) -> Path:
        return self.container_path_for_name(self.container_name_for_source(source))

    @staticmethod
    def _check_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelled("Operación cancelada por el usuario.")

    def _clear_temp_contents(self) -> None:
        if not self.temp_directory.is_dir():
            return
        root = self.temp_directory.resolve()
        for item in self.temp_directory.iterdir():
            if item.parent.resolve() != root:
                raise OSError(
                    f"No se limpiará un temporal fuera de {self.temp_directory}: "
                    f"{item}"
                )
            if item.is_symlink() or item.is_file():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
            else:
                raise OSError(f"No se reconoce el elemento temporal: {item}")

    @staticmethod
    def _remove_extraction(extraction: Path | None) -> None:
        if extraction is not None and (extraction.is_dir() or extraction.is_symlink()):
            if extraction.is_dir() and not extraction.is_symlink():
                shutil.rmtree(extraction)
            else:
                extraction.unlink()

    def _cancel_cleanup(self, extraction: Path | None = None) -> None:
        self._remove_extraction(extraction)
        self._clear_temp_contents()

    @staticmethod
    def _report_progress(
        progress: ProgressFunction | None,
        step: int,
        title: str,
        percent: float | None = None,
        compression_percent: float | None = None,
        detail: str = "",
        phase: str = "",
        bytes_done: int | None = None,
        total_bytes: int | None = None,
        stored_bytes: int | None = None,
        speed_mb_s: float | None = None,
        eta_seconds: float | None = None,
    ) -> None:
        if progress is not None:
            progress(
                ProgressUpdate(
                    step=step,
                    title=title,
                    percent=percent,
                    compression_percent=compression_percent,
                    detail=detail,
                    phase=phase,
                    bytes_done=bytes_done,
                    total_bytes=total_bytes,
                    stored_bytes=stored_bytes,
                    speed_mb_s=speed_mb_s,
                    eta_seconds=eta_seconds,
                )
            )

    def _pack_nup(
        self,
        config: PackConfig,
        log: LogFunction,
        progress: ProgressFunction | None,
        cancel_event: Event | None,
    ) -> None:
        self._check_cancelled(cancel_event)
        if str(self.nup_python_path) not in sys.path:
            sys.path.insert(0, str(self.nup_python_path))
        pack_module = importlib.import_module("nup.pack")
        if not pack_module.have_zstd():
            raise RuntimeError(
                "No se encontró la dependencia Zstandard; no se puede comprimir."
            )
        log("Creando archivo NUP...")
        pack_started = time.monotonic()
        compression_started: float | None = None
        highest_step_percent = 0.0
        highest_compression_percent = 0.0

        def byte_progress(
            phase: str,
            bytes_done: int,
            total_bytes: int,
            stored_bytes: int,
            file_node: SourceFile,
        ) -> None:
            nonlocal compression_started, highest_step_percent
            nonlocal highest_compression_percent
            self._check_cancelled(cancel_event)
            now = time.monotonic()
            if phase == "comprimiendo":
                if compression_started is None:
                    compression_started = now
                elapsed = max(now - compression_started, 0.001)
            else:
                elapsed = max(now - pack_started, 0.001)
            speed = bytes_done / 1_000_000 / elapsed
            eta = (
                max(total_bytes - bytes_done, 0) / (bytes_done / elapsed)
                if bytes_done > 0
                else None
            )
            current_compression_percent = (
                (bytes_done / total_bytes * 100 if total_bytes else 100.0)
                if phase == "comprimiendo"
                else None
            )
            if current_compression_percent is not None:
                highest_compression_percent = max(
                    highest_compression_percent,
                    current_compression_percent,
                )
            step_percent = bytes_done / total_bytes * 50 if total_bytes else 100.0
            if phase == "comprimiendo":
                step_percent = (
                    50 + highest_compression_percent / 2
                    if total_bytes
                    else 100.0
                )
            highest_step_percent = max(highest_step_percent, step_percent)
            file_name = Path(file_node.src_path).name
            self._report_progress(
                progress,
                3,
                "Creando archivo NUP...",
                highest_step_percent,
                compression_percent=(
                    highest_compression_percent
                    if current_compression_percent is not None
                    else None
                ),
                detail=file_name,
                phase=phase,
                bytes_done=bytes_done,
                total_bytes=total_bytes,
                stored_bytes=stored_bytes,
                speed_mb_s=speed,
                eta_seconds=eta,
            )

        try:
            log(
                "COMANDO INTERNO: nup.pack "
                f"--source {subprocess.list2cmdline([str(config.source)])} "
                f"--output {subprocess.list2cmdline([str(config.container)])} "
                f"--level {config.compression_level} "
                f"--keep-listings {str(config.keep_listings).lower()}"
            )
            packages = pack_module.pack(
                str(config.container),
                str(config.source),
                level=config.compression_level,
                keep_listings=config.keep_listings,
                byte_progress=byte_progress,
                cancel_check=lambda: self._check_cancelled(cancel_event),
            )
        except SystemExit as exc:
            raise RuntimeError(f"NUP terminó con error: {exc}") from exc
        self._check_cancelled(cancel_event)
        format_module = importlib.import_module("nup.format")
        for package in packages:
            kind_name = format_module.KIND_NAMES.get(
                package.kind, f"kind{package.kind}"
            )
            log(
                f"  {kind_name:<6} {package.title_id:016X} "
                f"v{package.version} <- {package.src}"
            )
        log(f"pack: escribió {config.container}")

    def _run_nup(
        self,
        arguments: list[str],
        log: LogFunction,
        cancel_event: Event | None = None,
    ) -> None:
        self._check_cancelled(cancel_event)
        command_text = subprocess.list2cmdline(["nup", *arguments])
        if arguments[0] == "verify":
            log(f"COMANDO INTERNO: {command_text} --deep")
        else:
            log(f"COMANDO NUP: {command_text}")
        if arguments[0] == "verify":
            if str(self.nup_python_path) not in sys.path:
                sys.path.insert(0, str(self.nup_python_path))
            verify_module = importlib.import_module("nup.verify")
            ok, problems = verify_module.verify(
                arguments[1],
                deep=True,
                cancel_check=lambda: self._check_cancelled(cancel_event),
            )
            if not ok:
                log(f"verify: FAILED - {arguments[1]}")
                for problem in problems[:20]:
                    log(f"  - {problem}")
                raise RuntimeError("NUP verify detectó errores.")
            log(f"verify: OK (deep) - {arguments[1]}")
            self._check_cancelled(cancel_event)
            return
        if getattr(sys, "frozen", False):
            output = io.StringIO()
            if str(self.nup_python_path) not in sys.path:
                sys.path.insert(0, str(self.nup_python_path))
            cli = importlib.import_module("nup.cli")
            try:
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(
                    output
                ):
                    result = cli.main(arguments)
            except SystemExit as exc:
                result = exc.code if isinstance(exc.code, int) else 1
            self._log_output(output.getvalue(), log)
            if result:
                raise RuntimeError(f"NUP terminó con código {result}.")
            self._check_cancelled(cancel_event)
            return

        environment = os.environ.copy()
        current_python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            part
            for part in (str(self.nup_python_path), current_python_path)
            if part
        )
        command = [sys.executable, "-m", "nup.cli", *arguments]
        log(f"COMANDO EXTERNO: {subprocess.list2cmdline(command)}")
        try:
            result = subprocess.run(
                command,
                cwd=self.app_root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise RuntimeError(f"No se pudo iniciar NUP: {exc}") from exc
        self._log_output(result.stdout + result.stderr, log)
        if result.returncode:
            raise RuntimeError(f"NUP terminó con código {result.returncode}.")
        self._check_cancelled(cancel_event)

    @staticmethod
    def _log_output(output: str, log: LogFunction) -> None:
        for line in output.splitlines():
            if line:
                log(line)

    def _discover_source_packages(self, source: Path) -> tuple[PackageLayout, ...]:
        if not source.is_dir():
            raise ValueError(f"No existe la carpeta del juego: {source}")
        if str(self.nup_python_path) not in sys.path:
            sys.path.insert(0, str(self.nup_python_path))
        discovery = importlib.import_module("nup.discover")
        format_module = importlib.import_module("nup.format")
        try:
            discovered = discovery.discover(str(source), None)
        except SystemExit as exc:
            raise ValueError(
                f"NUP no reconoce una estructura de dump válida en {source}: "
                f"{exc}"
            ) from exc

        layouts: list[PackageLayout] = []
        resolved_source = source.resolve()
        for package in discovered:
            package_source = Path(package.src).resolve()
            try:
                relative_path = package_source.relative_to(resolved_source)
            except ValueError as exc:
                raise ValueError(
                    f"NUP encontró un paquete fuera de la carpeta seleccionada: "
                    f"{package_source}"
                ) from exc
            layouts.append(
                PackageLayout(
                    kind=int(package.kind),
                    kind_name=format_module.KIND_NAMES.get(
                        package.kind, f"kind{package.kind}"
                    ),
                    title_id=package.title_id,
                    version=package.version,
                    relative_path=relative_path,
                )
            )
        return tuple(layouts)

    def _extract_all_packages(
        self,
        container: Path,
        destination: Path,
        log: LogFunction,
        source_layouts: tuple[PackageLayout, ...] | None = None,
        manifest: Path | None = None,
        progress: ProgressFunction | None = None,
        structure_step: int | None = None,
        extract_step: int | None = None,
        cancel_event: Event | None = None,
    ) -> bool:
        if str(self.nup_python_path) not in sys.path:
            sys.path.insert(0, str(self.nup_python_path))
        reader_module = importlib.import_module("nup.read")
        access_module = importlib.import_module("nup.access")
        format_module = importlib.import_module("nup.format")
        reader_type = reader_module.NupReader

        self._check_cancelled(cancel_event)
        with reader_type(str(container)) as reader:
            packages = reader.packages
            if source_layouts is not None:
                layouts = source_layouts
                if len(packages) != len(layouts):
                    raise ValueError(
                        "El número de paquetes del NUP no coincide con los "
                        "paquetes descubiertos en el dump."
                    )
                for package, layout in zip(packages, layouts):
                    if (
                        int(package.kind) != layout.kind
                        or package.title_id != layout.title_id
                        or package.version != layout.version
                    ):
                        raise ValueError(
                            "Los metadatos de los paquetes NUP no coinciden con "
                            "el orden y la estructura del dump original."
                        )
            elif manifest is not None:
                self._check_cancelled(cancel_event)
                entries = read_manifest_entries(manifest, "SHA-256")
                layouts = self._infer_package_layouts(
                    packages,
                    entries,
                    format_module.KIND_BASE,
                    format_module.KIND_UPDATE,
                    format_module.KIND_ADDON,
                )
            else:
                layouts = self._default_package_layouts(
                    packages,
                    format_module.KIND_BASE,
                    format_module.KIND_UPDATE,
                    format_module.KIND_ADDON,
                )

            if not packages:
                raise ValueError("El contenedor NUP no contiene paquetes.")
            has_addons = any(
                package.kind == format_module.KIND_ADDON
                for package in packages
            )
            if structure_step is not None:
                self._report_progress(
                    progress,
                    structure_step,
                    "Reconstruyendo la estructura de paquetes",
                    100.0,
                )
            total_files = 0
            if extract_step is not None:
                for package in packages:
                    pending = [package.root_dir]
                    while pending:
                        self._check_cancelled(cancel_event)
                        directory_index = pending.pop()
                        for child in access_module.iter_children(
                            reader, directory_index
                        ):
                            if child.is_dir:
                                pending.append(child.index)
                            else:
                                total_files += 1
            completed_files = 0
            for package, layout in zip(packages, layouts):
                self._check_cancelled(cancel_event)
                package_destination = destination.joinpath(
                    *layout.relative_path.parts
                )
                log(
                    f"Extrayendo {package.kind_name} "
                    f"({package.index + 1}/{len(packages)}) a "
                    f"{package_destination}..."
                )
                if extract_step is not None:
                    self._report_progress(
                        progress,
                        extract_step,
                        "Extrayendo archivos del contenedor",
                        completed_files / total_files * 100
                        if total_files
                        else 100.0,
                        detail=f"Paquete {package.index + 1}/{len(packages)}",
                    )

                package_file_offset = completed_files

                def file_extracted(count: int, node: ExtractedFile) -> None:
                    nonlocal completed_files
                    self._check_cancelled(cancel_event)
                    completed_files = package_file_offset + count
                    self._report_progress(
                        progress,
                        extract_step or 1,
                        "Extrayendo archivos del contenedor",
                        completed_files / total_files * 100
                        if total_files
                        else 100.0,
                        detail=node.name.decode("utf-8", "surrogateescape"),
                    )

                access_module.extract_tree(
                    reader,
                    str(package_destination),
                    package.root_dir,
                    progress=file_extracted if extract_step is not None else None,
                    cancel_check=lambda: self._check_cancelled(cancel_event),
                )
                self._check_cancelled(cancel_event)
                if extract_step is not None:
                    self._report_progress(
                        progress,
                        extract_step,
                        "Extrayendo archivos del contenedor",
                        completed_files / total_files * 100
                        if total_files
                        else 100.0,
                        detail=f"Paquete {package.index + 1}/{len(packages)}",
                    )
        return has_addons

    @staticmethod
    def _default_package_layouts(
        packages: list[PackageRecord],
        kind_base: int,
        kind_update: int,
        kind_addon: int,
    ) -> tuple[PackageLayout, ...]:
        layouts: list[PackageLayout] = []
        addon_indices: dict[int, int] = {}
        for package in packages:
            if package.kind == kind_base:
                relative_path = (
                    Path(".") if len(packages) == 1 else Path("base")
                )
            elif package.kind == kind_update:
                relative_path = Path("updates", str(package.version))
            elif package.kind == kind_addon:
                addon_index = addon_indices.get(package.title_id, 0)
                addon_indices[package.title_id] = addon_index + 1
                relative_path = Path(
                    "dlc",
                    f"{package.title_id:016x}",
                    str(addon_index),
                )
            else:
                raise ValueError(
                    f"Tipo de paquete NUP no compatible: {package.kind_name}."
                )
            layouts.append(
                PackageLayout(
                    kind=int(package.kind),
                    kind_name=package.kind_name,
                    title_id=package.title_id,
                    version=package.version,
                    relative_path=relative_path,
                )
            )
        return tuple(layouts)

    @staticmethod
    def _infer_package_layouts(
        packages: list[PackageRecord],
        entries: dict[str, str],
        kind_base: int,
        kind_update: int,
        kind_addon: int,
    ) -> tuple[PackageLayout, ...]:
        paths = tuple(PurePosixPath(name).parts for name in entries)

        base_has_wrapper = any(
            len(parts) > 1 and parts[0].casefold() == "base"
            for parts in paths
        )
        base_count = sum(package.kind == kind_base for package in packages)
        if base_count > 1:
            raise ValueError("El contenedor contiene más de un paquete base.")
        layouts: list[PackageLayout] = []
        for package in packages:
            kind = int(package.kind)
            if package.kind == kind_base:
                if base_has_wrapper:
                    relative_path = Path("base")
                elif len(packages) == 1:
                    relative_path = Path(".")
                else:
                    raise ValueError(
                        "El manifiesto no conserva la carpeta base/ del "
                        "contenedor con varios paquetes."
                    )
            elif package.kind == kind_update:
                candidates = sorted({
                    parts[1]
                    for parts in paths
                    if (
                        len(parts) > 2
                        and parts[0].casefold() == "updates"
                        and parts[1].isdecimal()
                        and int(parts[1], 10) == package.version
                    )
                })
                same_version_count = sum(
                    other.kind == kind_update
                    and other.version == package.version
                    for other in packages
                )
                if len(candidates) != same_version_count:
                    raise ValueError(
                        "No se puede determinar la carpeta de la actualización "
                        f"versión {package.version} desde el manifiesto."
                    )
                version_candidates = [
                    candidate for candidate in candidates
                ]
                package_position = sum(
                    other.kind == kind_update
                    and other.version == package.version
                    for other in packages[:package.index]
                )
                relative_path = Path(
                    "updates", version_candidates[package_position]
                )
            elif package.kind == kind_addon:
                title_id = f"{package.title_id:016x}"
                candidates = sorted({
                    (parts[1], parts[2])
                    for parts in paths
                    if (
                        len(parts) > 3
                        and parts[0].casefold() == "dlc"
                        and len(parts[1]) == 16
                        and parts[1].casefold() == title_id
                    )
                })
                same_title_count = sum(
                    other.kind == kind_addon
                    and other.title_id == package.title_id
                    for other in packages
                )
                if len(candidates) != same_title_count:
                    raise ValueError(
                        "No se puede reconstruir el índice del DLC "
                        f"{title_id} desde el manifiesto original."
                    )
                package_position = sum(
                    other.kind == kind_addon
                    and other.title_id == package.title_id
                    for other in packages[:package.index]
                )
                relative_path = Path(
                    "dlc",
                    *candidates[package_position],
                )
            else:
                raise ValueError(
                    f"Tipo de paquete NUP no compatible: {package.kind_name}."
                )
            layouts.append(
                PackageLayout(
                    kind=kind,
                    kind_name=package.kind_name,
                    title_id=package.title_id,
                    version=package.version,
                    relative_path=relative_path,
                )
            )
        return tuple(layouts)

    @staticmethod
    def _validate_new_paths(
        source: Path,
        container: Path,
        manifest: Path,
        manifest_directory: Path,
        output_directory: Path,
    ) -> None:
        if not source.is_dir():
            raise ValueError(f"No existe la carpeta de origen: {source}")
        resolved_source = source.resolve()
        for path, label in (
            (container, "El contenedor"),
            (manifest, "El manifiesto"),
        ):
            if path_is_within(path, resolved_source):
                raise ValueError(
                    f"{label} debe guardarse fuera de la carpeta de origen."
                )
        if container.resolve() == manifest.resolve():
            raise ValueError("El contenedor y el manifiesto deben ser distintos.")
        if manifest.parent.resolve() != manifest_directory.resolve():
            raise ValueError(
                "El manifiesto SHA-256 debe guardarse en la carpeta "
                f"crc-sha256 del ejecutable: {manifest_directory}"
            )
        if container.parent.resolve() != output_directory.resolve():
            raise ValueError(
                f"El contenedor NUP debe guardarse en output junto al ejecutable: "
                f"{output_directory}"
            )
        if container.exists():
            raise FileExistsError(
                f"El contenedor ya existe y no se sobrescribirá: {container}"
            )
        if not container.parent.is_dir():
            raise FileNotFoundError(
                f"No existe la carpeta de destino del contenedor: {container.parent}"
            )

    @staticmethod
    def _ensure_directory(directory: Path, purpose: str) -> None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(
                f"No se pudo crear la carpeta {purpose} {directory}: {exc}"
            ) from exc

    def _ensure_source_manifest(
        self,
        source: Path,
        source_layouts: tuple[PackageLayout, ...],
        manifest: Path,
        include_listings: bool,
        log: LogFunction,
        cancel_event: Event | None,
        progress: ProgressFunction | None,
    ) -> None:
        self._check_cancelled(cancel_event)
        cancel_check = lambda: self._check_cancelled(cancel_event)
        source_directories = tuple(
            layout.relative_path for layout in source_layouts
        )

        def report_manifest_check(
            completed: int, total: int, relative_name: str
        ) -> None:
            self._report_progress(
                progress,
                2,
                "Validando o creando el manifiesto SHA-256",
                completed / total * 100 if total else 100.0,
                detail=(
                    f"Revisando {completed} de {total} archivos del "
                    f"SHA-256 original: {relative_name}"
                ),
            )

        def report_manifest_creation(
            completed: int, total: int, relative_name: str
        ) -> None:
            self._report_progress(
                progress,
                2,
                "Validando o creando el manifiesto SHA-256",
                completed / total * 100 if total else 100.0,
                detail=(
                    f"Creando SHA-256 para verificación: archivo {completed} "
                    f"de {total} — {relative_name}"
                ),
            )

        if manifest.is_file():
            log("Revisando manifiesto SHA-256 compatible con RapidCRC...")
            self._archive_manifest_log(
                manifest, log, action="revisado"
            )
            try:
                result = self._compare_manifest_and_log(
                    manifest,
                    "empaqueta",
                    log,
                    lambda comparison: compare_manifest_for_sources(
                        source,
                        source_directories,
                        manifest,
                        "SHA-256",
                        include_listings,
                        cancel_check,
                        report_manifest_check,
                        comparison,
                    ),
                )
            except (UnicodeError, ValueError, OSError) as exc:
                log(
                    f"El manifiesto existente está vacío o no se puede leer "
                    f"({exc}); se volverá a generar."
                )
            else:
                for message in display_comparison(result):
                    log(message)
                if result.mismatched or result.extra:
                    raise RuntimeError(
                        "El manifiesto SHA-256 existente no coincide con el "
                        "contenido de los paquetes descubiertos. No se "
                        "empaquetará ni se reemplazará."
                    )
                if not result.missing:
                    log(f"Manifiesto original comprobado: {manifest}")
                    return

                try:
                    full_tree_result = self._compare_manifest_and_log(
                        manifest,
                        "empaqueta",
                        log,
                        lambda comparison: compare_manifest(
                            source,
                            manifest,
                            "SHA-256",
                            include_listings,
                            cancel_check,
                            report_manifest_check,
                            comparison,
                        ),
                    )
                except (UnicodeError, ValueError, OSError) as exc:
                    log(
                        f"El manifiesto existente no se puede validar "
                        f"({exc}); se volverá a generar."
                    )
                else:
                    if full_tree_result.mismatched or full_tree_result.missing:
                        raise RuntimeError(
                            "El manifiesto SHA-256 existente no coincide con "
                            "el dump original. No se empaquetará ni se "
                            "reemplazará."
                        )
                    log(
                        "El manifiesto anterior incluye archivos que NUP omite "
                        "según su estructura de paquetes; se regenerará con "
                        "las rutas que realmente se empaquetarán."
                    )

        self._ensure_directory(manifest.parent, "de manifiestos")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".pynup-",
            suffix=".sha256",
            dir=manifest.parent,
        )
        os.close(descriptor)
        temporary_manifest = Path(temporary_name)
        try:
            temporary_manifest.unlink()
            log("Creando manifiesto SHA-256 compatible con RapidCRC...")
            count = create_manifest_for_sources(
                source,
                source_directories,
                temporary_manifest,
                "SHA-256",
                include_listings,
                cancel_check,
                report_manifest_creation,
            )
            self._check_cancelled(cancel_event)
            temporary_manifest.replace(manifest)
            log(f"Manifiesto creado: {count} archivos -> {manifest}")
            self._archive_manifest_log(manifest, log, action="creado")
        finally:
            temporary_manifest.unlink(missing_ok=True)

    def _archive_manifest_log(
        self,
        manifest: Path,
        log: LogFunction,
        action: str = "creado",
    ) -> None:
        archive_directory = self.log_directory / "crc-sha256"
        self._ensure_directory(archive_directory, "de control de manifiestos")
        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        snapshot = archive_directory / (
            f"{timestamp}_manifiesto_{manifest.stem}.log"
        )
        counter = 1
        while snapshot.exists():
            snapshot = archive_directory / (
                f"{timestamp}_{counter}_manifiesto_{manifest.stem}.log"
            )
            counter += 1
        with (
            manifest.open("r", encoding="utf-8-sig", newline="") as source,
            snapshot.open("x", encoding="utf-8", newline="") as archived_log,
        ):
            archived_log.write("Registro de control del manifiesto SHA-256\n")
            archived_log.write(
                f"Fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            archived_log.write(f"Acción: manifiesto {action}\n")
            archived_log.write(f"Manifiesto: {manifest.name}\n")
            archived_log.write(
                "Contenido del manifiesto (línea por línea):\n"
            )
            shutil.copyfileobj(source, archived_log)
        snapshots = sorted(
            archive_directory.glob("*_manifiesto_*.log"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        for old_snapshot in snapshots[3:]:
            old_snapshot.unlink()
        for legacy_manifest in archive_directory.glob("*_original.sha256"):
            legacy_manifest.unlink()
        log(f"Log del manifiesto guardado: {snapshot}")

    def _compare_manifest_and_log(
        self,
        manifest: Path,
        operation: str,
        log: LogFunction,
        compare: ManifestComparison,
    ) -> ComparisonResult:
        if operation not in ("empaqueta", "extrae"):
            raise ValueError(f"Operación de comparación no válida: {operation}")
        archive_directory = self.log_directory / "crc-sha256"
        self._ensure_directory(archive_directory, "de control de manifiestos")
        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        log_path = archive_directory / (
            f"{timestamp}_comparacion-{operation}_{manifest.stem}.log"
        )
        counter = 1
        while log_path.exists():
            log_path = archive_directory / (
                f"{timestamp}_{counter}_comparacion-{operation}_"
                f"{manifest.stem}.log"
            )
            counter += 1

        try:
            with log_path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write("Comparación SHA-256 por archivo\n")
                stream.write(
                    f"Fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                )
                stream.write(f"Tipo: comparación-{operation}\n")
                stream.write(f"Manifiesto: {manifest.name}\n")
                stream.write("Resultado por archivo:\n")
                last_flush = time.monotonic()

                def report_comparison(
                    relative_name: str,
                    status: str,
                    expected_digest: str | None,
                    actual_digest: str | None,
                ) -> None:
                    nonlocal last_flush
                    safe_name = relative_name.replace("\r", "\\r").replace(
                        "\n", "\\n"
                    )
                    stream.write(
                        f"{status} | {safe_name} | "
                        f"esperado={expected_digest or '—'} | "
                        f"obtenido={actual_digest or '—'}\n"
                    )
                    now = time.monotonic()
                    if now - last_flush >= 10:
                        stream.flush()
                        last_flush = now

                result = compare(report_comparison)
                stream.write(f"Archivos comprobados: {result.checked}\n")
                stream.write(
                    f"Archivos ausentes: {len(result.missing)}\n"
                )
                stream.write(
                    f"Hashes distintos: {len(result.mismatched)}\n"
                )
                stream.write(f"Archivos adicionales: {len(result.extra)}\n")
                stream.write(
                    "Resultado final: "
                    f"{'COINCIDE' if result.matches else 'NO COINCIDE'}\n"
                )
                stream.flush()
        finally:
            if log_path.is_file():
                pattern = f"*_comparacion-{operation}_*.log"
                snapshots = sorted(
                    archive_directory.glob(pattern),
                    key=lambda path: (path.stat().st_mtime_ns, path.name),
                    reverse=True,
                )
                for old_snapshot in snapshots[3:]:
                    old_snapshot.unlink()
                log(f"Log de comparación guardado: {log_path}")
        return result

    def retain_recent_zstandard_logs(
        self, current_log: Path, log: LogFunction
    ) -> None:
        archive_directory = self.log_directory / "zstandard"
        snapshots = sorted(
            archive_directory.glob("*.log"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        for old_snapshot in snapshots[3:]:
            old_snapshot.unlink()
        log(
            f"Registro Zstandard guardado: {current_log} "
            "(se conservan los 3 más recientes)."
        )

    @staticmethod
    def _validate_extract_paths(container: Path, extraction: Path) -> None:
        if not container.is_file():
            raise FileNotFoundError(f"No existe el contenedor NUP: {container}")
        if extraction.exists():
            raise FileExistsError(
                f"La carpeta de extracción ya existe y no se sobrescribirá: "
                f"{extraction}"
            )
        if not extraction.parent.is_dir():
            raise FileNotFoundError(
                f"No existe la carpeta padre de extracción: {extraction.parent}"
            )
        if path_is_within(container, extraction):
            raise ValueError("La extracción debe estar fuera de la carpeta del NUP.")

    def pack_and_check(
        self,
        config: PackConfig,
        log: LogFunction,
        progress: ProgressFunction | None = None,
        cancel_event: Event | None = None,
    ) -> None:
        try:
            self._pack_and_check(
                config, log, progress, cancel_event
            )
        except OperationCancelled:
            self._cancel_cleanup()
            raise

    def _pack_and_check(
        self,
        config: PackConfig,
        log: LogFunction,
        progress: ProgressFunction | None,
        cancel_event: Event | None,
    ) -> None:
        self._check_cancelled(cancel_event)
        self._report_progress(
            progress, 1, "Analizando la estructura del dump", 0.0
        )
        source_layouts = self._discover_source_packages(config.source)
        expected_container = self.container_path_for_name(
            self._container_name_for_layouts(
                source_layouts, config.source.name
            )
        )
        expected_manifest = self.manifest_path_for_name(expected_container.stem)
        self._check_cancelled(cancel_event)
        self._report_progress(
            progress, 1, "Analizando la estructura del dump", 100.0
        )
        if config.compression_level not in (3, 11, 22):
            raise ValueError(
                "Elige un nivel disponible: bajo 3, medio 11 o alto 22."
            )
        self._ensure_directory(self.output_directory, "de salida")
        self._validate_new_paths(
            config.source,
            config.container,
            config.manifest,
            self.manifest_directory,
            self.output_directory,
        )
        if config.container != expected_container:
            raise ValueError(
                "El nombre del contenedor no coincide con el TITLE ID y la "
                f"versión máxima detectados: {expected_container.name}"
            )
        if config.manifest != expected_manifest:
            raise ValueError(
                "El manifiesto debe usar el mismo nombre base que el NUP: "
                f"{expected_manifest.name}"
            )
        if config.manifest.suffix.lower() != ".sha256":
            raise ValueError("El manifiesto debe terminar en .sha256 (SHA-256).")
        self._ensure_directory(self.manifest_directory, "de manifiestos")
        self._report_progress(
            progress, 2, "Validando o creando el manifiesto SHA-256", 0.0
        )
        self._ensure_source_manifest(
            config.source,
            source_layouts,
            config.manifest,
            config.keep_listings,
            log,
            cancel_event,
            progress,
        )
        self._report_progress(
            progress, 2, "Validando o creando el manifiesto SHA-256", 100.0
        )
        log(
            f"Estructura NUP detectada: {len(source_layouts)} paquete(s) "
            "(base, actualizaciones y/o DLC)."
        )
        for index, layout in enumerate(source_layouts, start=1):
            relative = layout.relative_path.as_posix()
            log(
                f"Paquete {index}: {layout.kind_name}, origen "
                f"{relative if relative != '.' else '(contenido raíz)'}."
            )

        compression_name = {3: "BAJA", 11: "MEDIA", 22: "ALTA"}[
            config.compression_level
        ]
        log(
            f"Compresión Zstandard seleccionada: {compression_name}, "
            f"nivel {config.compression_level}."
        )
        self._report_progress(
            progress, 3, "Creando archivo NUP...", 0.0, phase="iniciando"
        )
        try:
            self._pack_nup(config, log, progress, cancel_event)
        except OperationCancelled:
            config.container.unlink(missing_ok=True)
            raise
        self._check_cancelled(cancel_event)
        self._report_progress(
            progress, 3, "Creando archivo NUP...", 100.0, phase="completo"
        )

        self._report_progress(
            progress, 4, "Verificando el contenedor NUP", 0.0
        )
        log("Verificación NUP profunda (incluye SHA-256 interno)...")
        self._run_nup(
            ["verify", str(config.container)], log, cancel_event
        )
        self._report_progress(
            progress, 4, "Verificando el contenedor NUP", 100.0
        )
        log("Verificando la extracción temporal frente al manifiesto SHA-256...")
        self._ensure_directory(self.temp_directory, "temporal")
        self._report_progress(
            progress, 5, "Extrayendo para la comprobación temporal", 0.0
        )
        with tempfile.TemporaryDirectory(
            prefix="PyNup-verificacion-",
            dir=self.temp_directory,
        ) as temporary_directory:
            temporary_extraction = Path(temporary_directory)
            self._extract_all_packages(
                config.container,
                temporary_extraction,
                log,
                source_layouts=source_layouts,
                progress=progress,
                extract_step=5,
                cancel_event=cancel_event,
            )
            self._check_cancelled(cancel_event)
            self._report_progress(
                progress, 5, "Extracción temporal completada", 100.0
            )
            self._report_progress(
                progress, 6, "Comparando SHA-256 y limpiando temporales", 0.0
            )
            result = self._compare_manifest_and_log(
                config.manifest,
                "empaqueta",
                log,
                lambda comparison: compare_manifest_for_sources(
                    temporary_extraction,
                    tuple(layout.relative_path for layout in source_layouts),
                    config.manifest,
                    "SHA-256",
                    config.keep_listings,
                    lambda: self._check_cancelled(cancel_event),
                    lambda completed, total, relative_name: self._report_progress(
                        progress,
                        6,
                        "Comparando SHA-256 y limpiando temporales",
                        completed / total * 100 if total else 100.0,
                        detail=(
                            f"Revisando {completed} de {total} archivos del "
                            f"SHA-256 original: {relative_name}"
                        ),
                    ),
                    comparison,
                ),
            )
            for message in display_comparison(result):
                log(message)
            if not result.matches:
                raise RuntimeError(
                    "La extracción no coincide con el manifiesto SHA-256. "
                    "Revisa los archivos ausentes, adicionales o modificados."
                )
        log("Extracción temporal eliminada.")
        self._report_progress(
            progress, 6, "Comparando SHA-256 y limpiando temporales", 100.0
        )
        log(
            "FLUJO COMPLETO: CORRECTO. El manifiesto RapidCRC SHA-256, "
            "NUP Verify y la extracción coinciden."
        )
        self._check_cancelled(cancel_event)

    def extract_and_check(
        self,
        config: ExtractConfig,
        log: LogFunction,
        progress: ProgressFunction | None = None,
        cancel_event: Event | None = None,
    ) -> None:
        extraction_existed = config.extraction.exists()
        try:
            self._extract_and_check_impl(
                config, log, progress, cancel_event
            )
        except OperationCancelled:
            self._cancel_cleanup(
                config.extraction if not extraction_existed else None
            )
            raise
        except Exception:
            if not extraction_existed:
                self._remove_extraction(config.extraction)
            raise

    def _extract_and_check_impl(
        self,
        config: ExtractConfig,
        log: LogFunction,
        progress: ProgressFunction | None,
        cancel_event: Event | None,
    ) -> None:
        self._check_cancelled(cancel_event)
        self._report_progress(
            progress, 1, "Validando archivos y destinos", 0.0
        )
        self._validate_extract_paths(config.container, config.extraction)
        if config.manifest is not None:
            if not config.manifest.is_file():
                raise FileNotFoundError(
                    f"No existe el manifiesto SHA-256: {config.manifest}"
                )
            if config.manifest.suffix.lower() != ".sha256":
                raise ValueError("El manifiesto debe terminar en .sha256 (SHA-256).")
            if (
                config.manifest.parent.resolve()
                != self.manifest_directory.resolve()
            ):
                raise ValueError(
                    "Selecciona el manifiesto original de la carpeta crc-sha256 "
                    f"del ejecutable: {self.manifest_directory}"
                )
        self._report_progress(
            progress, 1, "Validando archivos y destinos", 100.0
        )

        self._report_progress(
            progress, 2, "Verificando el contenedor NUP", 0.0
        )
        log("Verificando el contenedor NUP antes de extraer...")
        self._run_nup(
            ["verify", str(config.container)], log, cancel_event
        )
        self._report_progress(
            progress, 2, "Verificando el contenedor NUP", 100.0
        )
        self._check_cancelled(cancel_event)
        log("Extrayendo el paquete...")
        self._report_progress(
            progress, 3, "Reconstruyendo la estructura de paquetes", 0.0
        )
        self._report_progress(
            progress, 4, "Extrayendo archivos del contenedor", 0.0
        )
        has_addons = self._extract_all_packages(
            config.container,
            config.extraction,
            log,
            manifest=config.manifest,
            progress=progress,
            structure_step=3,
            extract_step=4,
            cancel_event=cancel_event,
        )
        self._report_progress(
            progress, 4, "Extrayendo archivos del contenedor", 100.0
        )
        self._check_cancelled(cancel_event)
        if config.manifest is None:
            message = (
                "No se seleccionó manifiesto: NUP Verify terminó correctamente; "
                "se omite la comparación SHA-256."
            )
            if has_addons:
                message += (
                    " Los índices DLC se asignaron por orden y no se pueden "
                    "reconstruir exactamente sin manifiesto."
                )
            log(message)
            self._report_progress(
                progress, 5, "Verificación SHA-256 omitida (sin manifiesto)", 100.0
            )
            self._report_progress(
                progress, 6, "Extracción finalizada", 100.0
            )
            return
        manifest_path = config.manifest
        if manifest_path is None:
            raise RuntimeError(
                "Se perdió la selección del manifiesto SHA-256."
            )
        self._report_progress(
            progress, 5, "Comparando archivos con SHA-256", 0.0
        )
        log("Revisando manifiesto SHA-256 compatible con RapidCRC...")
        result = self._compare_manifest_and_log(
            manifest_path,
            "extrae",
            log,
            lambda comparison: compare_manifest(
                config.extraction,
                manifest_path,
                "SHA-256",
                config.keep_listings,
                lambda: self._check_cancelled(cancel_event),
                lambda completed, total, relative_name: self._report_progress(
                    progress,
                    5,
                    "Comparando archivos con SHA-256",
                    completed / total * 100 if total else 100.0,
                    detail=(
                        f"Revisando {completed} de {total} archivos del "
                        f"SHA-256 original: {relative_name}"
                    ),
                ),
                comparison,
            ),
        )
        for message in display_comparison(result):
            log(message)
        if not result.matches:
            raise RuntimeError(
                "La extracción no coincide con el manifiesto original. "
                "Revisa los archivos ausentes, adicionales o modificados."
            )
        log("EXTRACCIÓN COMPLETA: el manifiesto RapidCRC SHA-256 coincide.")
        self._report_progress(
            progress, 5, "Comparando archivos con SHA-256", 100.0
        )
        self._report_progress(
            progress, 6, "Extracción y comprobación finalizadas", 100.0
        )
        self._check_cancelled(cancel_event)
