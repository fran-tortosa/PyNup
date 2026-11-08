from __future__ import annotations

import hashlib
import ntpath
import posixpath
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable

ManifestProgressFunction = Callable[[int, int, str], None]
ManifestComparisonFunction = Callable[
    [str, str, str | None, str | None], None
]

HASH_ALGORITHMS = {
    "SHA-256": ("sha256", 64, ".sha256"),
    "SHA-512": ("sha512", 128, ".sha512"),
}
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ComparisonResult:
    checked: int
    missing: tuple[str, ...]
    mismatched: tuple[str, ...]
    extra: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not (self.missing or self.mismatched or self.extra)


def _algorithm_details(algorithm: str) -> tuple[str, int, str]:
    try:
        return HASH_ALGORITHMS[algorithm]
    except KeyError as exc:
        raise ValueError(f"Algoritmo no admitido: {algorithm}") from exc


def _files(root: Path, include_listings: bool) -> list[Path]:
    if not root.is_dir():
        raise ValueError(f"No existe la carpeta: {root}")

    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and (include_listings or path.name != ".listing")
    ]
    return sorted(
        files,
        key=lambda path: path.relative_to(root).as_posix().encode(
            "utf-8", "surrogateescape"
        ),
    )


def _hash_file(
    path: Path,
    algorithm: str,
    cancel_check: Callable[[], None] | None = None,
) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            if cancel_check is not None:
                cancel_check()
            digest.update(chunk)
    return digest.hexdigest()


def create_manifest(
    root: Path,
    manifest_path: Path,
    algorithm: str,
    include_listings: bool,
    progress: ManifestProgressFunction | None = None,
) -> int:
    algorithm_name, _, _ = _algorithm_details(algorithm)
    files = _files(root, include_listings)
    if not files:
        raise ValueError("No hay archivos para incluir en el manifiesto.")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open(
        "x", encoding="utf-8-sig", newline="\r\n"
    ) as manifest:
        for index, path in enumerate(files, start=1):
            relative_path = path.relative_to(root).as_posix()
            manifest.write(f"{_hash_file(path, algorithm_name)} *{relative_path}\n")
            if progress is not None:
                progress(index, len(files), relative_path)
    return len(files)


def _scoped_files(
    root: Path,
    source_directories: tuple[Path, ...],
    include_listings: bool,
) -> dict[str, Path]:
    resolved_root = root.resolve()
    files: dict[str, Path] = {}
    for source_directory in source_directories:
        package_root = (root / source_directory).resolve()
        try:
            relative_root = package_root.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(
                f"La carpeta de paquete queda fuera del dump: {source_directory}"
            ) from exc
        package_files = _files(package_root, include_listings)
        for path in package_files:
            relative_path = (
                relative_root / path.relative_to(package_root)
            ).as_posix()
            if relative_path in files:
                raise ValueError(
                    f"Ruta duplicada en los paquetes descubiertos: {relative_path}"
                )
            files[relative_path] = path
    return files


def create_manifest_for_sources(
    root: Path,
    source_directories: tuple[Path, ...],
    manifest_path: Path,
    algorithm: str,
    include_listings: bool,
    cancel_check: Callable[[], None] | None = None,
    progress: ManifestProgressFunction | None = None,
) -> int:
    algorithm_name, _, _ = _algorithm_details(algorithm)
    files = _scoped_files(root, source_directories, include_listings)
    if not files:
        raise ValueError("No hay archivos para incluir en el manifiesto.")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open(
        "x", encoding="utf-8-sig", newline="\r\n"
    ) as manifest:
        ordered_paths = sorted(
            files, key=lambda name: name.encode("utf-8", "surrogateescape")
        )
        for index, relative_path in enumerate(ordered_paths, start=1):
            if cancel_check is not None:
                cancel_check()
            manifest.write(
                f"{_hash_file(files[relative_path], algorithm_name, cancel_check)} "
                f"*{relative_path}\n"
            )
            if progress is not None:
                progress(index, len(ordered_paths), relative_path)
    return len(files)


def _read_manifest(manifest_path: Path, algorithm: str) -> dict[str, str]:
    _, digest_length, _ = _algorithm_details(algorithm)
    if not manifest_path.is_file():
        raise ValueError(f"No existe el manifiesto: {manifest_path}")

    raw_manifest = manifest_path.read_bytes()
    encoding = (
        "utf-16"
        if raw_manifest.startswith((b"\xff\xfe", b"\xfe\xff"))
        else "utf-8-sig"
    )
    text = raw_manifest.decode(encoding)
    raw_entries: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\r\n")
        if not line or line.startswith("#"):
            continue
        if len(line) <= digest_length or line[digest_length] != " ":
            raise ValueError(
                f"Formato inválido en la línea {line_number} del manifiesto."
            )

        digest = line[:digest_length].lower()
        if any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(
                f"Hash inválido en la línea {line_number} del manifiesto."
            )

        remainder = line[digest_length:]
        if remainder.startswith("  "):
            relative_name = remainder[2:]
        elif remainder.startswith(" *"):
            relative_name = remainder[2:]
        else:
            raise ValueError(
                f"Separador inválido en la línea {line_number} del manifiesto."
            )

        if not relative_name:
            raise ValueError(
                f"Ruta no válida en la línea {line_number} del manifiesto."
            )
        raw_entries.append((relative_name, digest))

    absolute_names = [
        name
        for name, _ in raw_entries
        if PureWindowsPath(name).is_absolute() or PurePosixPath(name).is_absolute()
    ]
    if absolute_names and len(absolute_names) != len(raw_entries):
        raise ValueError("El manifiesto no puede mezclar rutas absolutas y relativas.")

    if absolute_names:
        windows_paths = [PureWindowsPath(name) for name in absolute_names]
        if all(path.drive for path in windows_paths):
            common_parent = ntpath.commonpath(
                [str(path.parent) for path in windows_paths]
            )
            normalized_names = [
                ntpath.relpath(name, common_parent).replace("\\", "/")
                for name in absolute_names
            ]
        else:
            posix_paths = [PurePosixPath(name) for name in absolute_names]
            common_parent = posixpath.commonpath(
                [str(path.parent) for path in posix_paths]
            )
            normalized_names = [
                posixpath.relpath(name, common_parent)
                for name in absolute_names
            ]
    else:
        normalized_names = [
            PurePosixPath(name.replace("\\", "/")).as_posix()
            for name, _ in raw_entries
        ]

    entries: dict[str, str] = {}
    for (name, digest), normalized_name in zip(raw_entries, normalized_names):
        relative_path = PurePosixPath(normalized_name)
        if (
            relative_path.is_absolute()
            or not relative_path.parts
            or any(part in ("", ".", "..") for part in relative_path.parts)
            or ":" in relative_path.parts[0]
        ):
            raise ValueError(f"Ruta no válida en el manifiesto: {name}")
        if normalized_name in entries:
            raise ValueError(f"Ruta duplicada en el manifiesto: {normalized_name}")
        entries[normalized_name] = digest

    if not entries:
        raise ValueError("El manifiesto no contiene archivos.")
    return entries


def compare_manifest(
    root: Path,
    manifest_path: Path,
    algorithm: str,
    include_listings: bool,
    cancel_check: Callable[[], None] | None = None,
    progress: ManifestProgressFunction | None = None,
    comparison: ManifestComparisonFunction | None = None,
) -> ComparisonResult:
    algorithm_name, _, _ = _algorithm_details(algorithm)
    expected = _read_manifest(manifest_path, algorithm)
    actual_paths = _files(root, include_listings)
    actual = {
        path.relative_to(root).as_posix(): path
        for path in actual_paths
    }

    return _compare_entries(
        expected, actual, algorithm_name, cancel_check, progress, comparison
    )


def compare_manifest_for_sources(
    root: Path,
    source_directories: tuple[Path, ...],
    manifest_path: Path,
    algorithm: str,
    include_listings: bool,
    cancel_check: Callable[[], None] | None = None,
    progress: ManifestProgressFunction | None = None,
    comparison: ManifestComparisonFunction | None = None,
) -> ComparisonResult:
    algorithm_name, _, _ = _algorithm_details(algorithm)
    expected = _read_manifest(manifest_path, algorithm)
    actual = _scoped_files(root, source_directories, include_listings)
    return _compare_entries(
        expected, actual, algorithm_name, cancel_check, progress, comparison
    )


def read_manifest_entries(
    manifest_path: Path,
    algorithm: str,
) -> dict[str, str]:
    return _read_manifest(manifest_path, algorithm)


def _compare_entries(
    expected: dict[str, str],
    actual: dict[str, Path],
    algorithm_name: str,
    cancel_check: Callable[[], None] | None = None,
    progress: ManifestProgressFunction | None = None,
    comparison: ManifestComparisonFunction | None = None,
) -> ComparisonResult:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatched: list[str] = []
    ordered_names = sorted(expected)
    for index, relative_name in enumerate(ordered_names, start=1):
        if cancel_check is not None:
            cancel_check()
        actual_path = actual.get(relative_name)
        expected_digest = expected[relative_name]
        actual_digest: str | None = None
        if actual_path is not None:
            actual_digest = _hash_file(actual_path, algorithm_name, cancel_check)
            if actual_digest != expected_digest:
                mismatched.append(relative_name)
                status = "HASH_DISTINTO"
            else:
                status = "COINCIDE"
        else:
            status = "FALTA"
        if comparison is not None:
            comparison(
                relative_name, status, expected_digest, actual_digest
            )
        if progress is not None:
            progress(index, len(ordered_names), relative_name)
    if comparison is not None:
        for relative_name in extra:
            comparison(relative_name, "ADICIONAL", None, None)
    return ComparisonResult(
        checked=len(expected) - len(missing),
        missing=tuple(missing),
        mismatched=tuple(mismatched),
        extra=tuple(extra),
    )


def path_is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def display_comparison(result: ComparisonResult) -> list[str]:
    messages = [f"Archivos comprobados: {result.checked}"]
    for label, paths in (
        ("Falta", result.missing),
        ("Hash distinto", result.mismatched),
        ("Adicional", result.extra),
    ):
        messages.extend(f"{label}: {path}" for path in paths)
    messages.append(
        "RESULTADO: COINCIDE" if result.matches else "RESULTADO: NO COINCIDE"
    )
    return messages
