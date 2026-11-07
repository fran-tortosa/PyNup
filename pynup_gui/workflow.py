from __future__ import annotations

import contextlib
import importlib
import io
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Protocol

from .integrity import (
    compare_manifest,
    compare_manifest_for_sources,
    create_manifest_for_sources,
    display_comparison,
    path_is_within,
    read_manifest_entries,
)

LogFunction = Callable[[str], None]


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
    manifest: Path
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


class NupService:
    def __init__(self, app_root: Path, data_root: Path | None = None) -> None:
        self.app_root = app_root.resolve()
        self.data_root = (data_root or app_root).resolve()
        self.output_directory = self.data_root / "output"
        self.manifest_directory = self.data_root / "crc-sha256"
        self.temp_directory = self.data_root / "temp"
        self.nup_python_path = self.app_root / "vendor" / "nup" / "tools" / "nup"
        if not (self.nup_python_path / "nup" / "cli.py").is_file():
            raise FileNotFoundError(
                f"No se encuentra la implementación NUP en {self.nup_python_path}"
            )

    def manifest_path_for_name(self, name: str) -> Path:
        return self.manifest_directory / f"{name}_original.sha256"

    def container_path_for_name(self, name: str) -> Path:
        return self.output_directory / f"{name}.nup"

    def _run_nup(self, arguments: list[str], log: LogFunction) -> None:
        log(f"NUP {arguments[0]}...")
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
            return

        environment = os.environ.copy()
        current_python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            part
            for part in (str(self.nup_python_path), current_python_path)
            if part
        )
        command = [sys.executable, "-m", "nup.cli", *arguments]
        log(f"> {subprocess.list2cmdline(command)}")
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
    ) -> None:
        if str(self.nup_python_path) not in sys.path:
            sys.path.insert(0, str(self.nup_python_path))
        reader_module = importlib.import_module("nup.read")
        access_module = importlib.import_module("nup.access")
        format_module = importlib.import_module("nup.format")
        if source_layouts is None and manifest is None:
            raise ValueError(
                "Se necesita el manifiesto original para reconstruir las rutas "
                "del dump durante la extracción."
            )
        reader_type = reader_module.NupReader

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
            else:
                assert manifest is not None
                entries = read_manifest_entries(manifest, "SHA-256")
                layouts = self._infer_package_layouts(
                    packages,
                    entries,
                    format_module.KIND_BASE,
                    format_module.KIND_UPDATE,
                    format_module.KIND_ADDON,
                )

            if not packages:
                raise ValueError("El contenedor NUP no contiene paquetes.")
            for package, layout in zip(packages, layouts):
                package_destination = destination.joinpath(
                    *layout.relative_path.parts
                )
                log(
                    f"Extrayendo {package.kind_name} "
                    f"({package.index + 1}/{len(packages)}) a "
                    f"{package_destination}..."
                )
                access_module.extract_tree(
                    reader,
                    str(package_destination),
                    package.root_dir,
                )

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
    ) -> None:
        source_directories = tuple(
            layout.relative_path for layout in source_layouts
        )
        if manifest.is_file():
            try:
                result = compare_manifest_for_sources(
                    source,
                    source_directories,
                    manifest,
                    "SHA-256",
                    include_listings,
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
                    full_tree_result = compare_manifest(
                        source,
                        manifest,
                        "SHA-256",
                        include_listings,
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
            )
            temporary_manifest.replace(manifest)
            log(f"Manifiesto creado: {count} archivos -> {manifest}")
        finally:
            temporary_manifest.unlink(missing_ok=True)

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
    ) -> None:
        source_layouts = self._discover_source_packages(config.source)
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
        if config.manifest.suffix.lower() != ".sha256":
            raise ValueError("El manifiesto debe terminar en .sha256 (SHA-256).")
        self._ensure_directory(self.manifest_directory, "de manifiestos")
        self._ensure_source_manifest(
            config.source,
            source_layouts,
            config.manifest,
            config.keep_listings,
            log,
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
        pack_arguments = [
            "pack",
            str(config.source),
            str(config.container),
            "--level",
            str(config.compression_level),
            "--quiet",
        ]
        if config.keep_listings:
            pack_arguments.append("--keep-listings")
        self._run_nup(pack_arguments, log)

        log("Verificación NUP profunda (incluye SHA-256 interno)...")
        self._run_nup(["verify", str(config.container)], log)
        log("Verificando la extracción temporal frente al manifiesto SHA-256...")
        self._ensure_directory(self.temp_directory, "temporal")
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
            )
            result = compare_manifest_for_sources(
                temporary_extraction,
                tuple(layout.relative_path for layout in source_layouts),
                config.manifest,
                "SHA-256",
                config.keep_listings,
            )
            for message in display_comparison(result):
                log(message)
            if not result.matches:
                raise RuntimeError(
                    "La extracción no coincide con el manifiesto SHA-256. "
                    "Revisa los archivos ausentes, adicionales o modificados."
                )
        log("Extracción temporal eliminada.")
        log(
            "FLUJO COMPLETO: CORRECTO. El manifiesto RapidCRC SHA-256, "
            "NUP Verify y la extracción coinciden."
        )

    def extract_and_check(
        self,
        config: ExtractConfig,
        log: LogFunction,
    ) -> None:
        self._validate_extract_paths(config.container, config.extraction)
        if not config.manifest.is_file():
            raise FileNotFoundError(
                "Para comprobar la extracción, selecciona el manifiesto SHA-256 "
                f"original del juego: {config.manifest}"
            )
        if config.manifest.suffix.lower() != ".sha256":
            raise ValueError("El manifiesto debe terminar en .sha256 (SHA-256).")
        if config.manifest.parent.resolve() != self.manifest_directory.resolve():
            raise ValueError(
                "Selecciona el manifiesto original de la carpeta crc-sha256 "
                f"del ejecutable: {self.manifest_directory}"
            )

        log("Verificando el contenedor NUP antes de extraer...")
        self._run_nup(["verify", str(config.container)], log)
        log("Extrayendo el paquete...")
        self._extract_all_packages(
            config.container,
            config.extraction,
            log,
            manifest=config.manifest,
        )
        result = compare_manifest(
            config.extraction,
            config.manifest,
            "SHA-256",
            config.keep_listings,
        )
        for message in display_comparison(result):
            log(message)
        if not result.matches:
            raise RuntimeError(
                "La extracción no coincide con el manifiesto original. "
                "Revisa los archivos ausentes, adicionales o modificados."
            )
        log("EXTRACCIÓN COMPLETA: el manifiesto RapidCRC SHA-256 coincide.")
