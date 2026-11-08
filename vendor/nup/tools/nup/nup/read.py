"""NUP reader: validated parsing and integrity checking.

Reader conformance (SPEC §6.1): validate superblock, verify TOC SHA-256
before trusting TOC-derived values, bounds-check every offset/size, treat
title_id/version/NPDM/NACP as opaque, reject unknown codecs and flags.
Tree traversal, metadata access, and extraction live in .access.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
import ctypes
import hashlib
import os
import struct
from typing import BinaryIO, TYPE_CHECKING

from . import format as F

if TYPE_CHECKING:
    from types import TracebackType


def _open_sequential(path: str) -> BinaryIO:
    if os.name != "nt":
        return open(path, "rb")

    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        os.path.abspath(path),
        0x80000000,
        0x00000007,
        None,
        3,
        0x08000000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_RDONLY | os.O_BINARY
        )
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "rb")


class _FileExtentReader:
    __slots__ = ("_fh", "_offset", "_size", "_position")

    def __init__(self, fh: BinaryIO, offset: int, size: int) -> None:
        self._fh = fh
        self._offset = offset
        self._size = size
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = self._size - self._position
        size = min(size, self._size - self._position)
        self._fh.seek(self._offset + self._position)
        data = self._fh.read(size)
        size = len(data)
        self._position += size
        return data


@dataclass(frozen=True, slots=True)
class PackageInfo:
    index: int
    title_id: int
    kind: F.Kind
    version: int
    flags: int
    root_dir: int
    exefs_dir: int
    romfs_dir: int
    meta_offset: int
    meta_size: int

    @property
    def kind_name(self) -> str:
        return F.KIND_NAMES.get(self.kind, f"kind{self.kind}")

    @property
    def version_str(self) -> str:
        return f"{self.version >> 16}.{(self.version >> 8) & 0xFF}.{self.version & 0xFF}"


@dataclass(frozen=True, slots=True)
class NodeRef:
    """A child reference: is_dir + index into the respective array."""
    is_dir: bool
    index: int
    name: bytes


class NupReader:
    __slots__ = ("path", "_fh", "super", "dir_count", "file_count", "pool", "packages", "files", "dirs", "children")
    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = _open_sequential(path)
        try:
            self._fh.seek(0)
            self.super = F.parse_super(self._fh.read(F.HEADER_SIZE))
            real_size = os.fstat(self._fh.fileno()).st_size
            if self.super["file_size"] != real_size:
                raise F.NupError(
                    f"file_size {self.super['file_size']} != actual {real_size}")
            if self.super["toc_offset"] < F.HEADER_SIZE:
                raise F.NupError("toc_offset before header end")
            if self.super["blob_offset"] < self.super["toc_offset"] + self.super["toc_size"]:
                raise F.NupError("blob region overlaps TOC")
            if self.super["blob_offset"] % F.EXTENT_ALIGN:
                raise F.NupError("blob_offset not 16-byte aligned")

            toc_start = self.super["toc_offset"]
            raw_toc = self.read_at(toc_start, self.super["toc_size"])
            if hashlib.sha256(raw_toc).digest() != self.super["toc_sha256"]:
                raise F.NupError("TOC SHA-256 mismatch - file corrupt or tampered")
            self._parse_toc(raw_toc)
            self._check_bounds()
        except BaseException:
            # Never leak a file handle during partially completed parsing.
            self.close()
            raise

    def close(self) -> None:
        self._fh.close()

    def read_at(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0:
            raise ValueError("offset and size must be non-negative")
        self._fh.seek(offset)
        data = self._fh.read(size)
        if len(data) != size:
            raise F.NupError(
                f"short read at {offset:#x}: {len(data)} != {size}"
            )
        return data

    def __enter__(self) -> "NupReader":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # ---- TOC parsing ----

    def _parse_toc(self, toc: bytes) -> None:
        magic, pkg_count, dir_count, file_count, pool_off, pool_size, _r = \
            F._toc_head_s.unpack_from(toc, 0)
        if magic != F.TOC_MAGIC:
            raise F.NupError(f"bad TOC magic {magic!r}")
        if pkg_count != self.super["package_count"]:
            raise F.NupError("package_count mismatch between superblock and TOC")
        need = (F._toc_head_s.size + pkg_count * F.PKG_SIZE
                + dir_count * F.DIR_SIZE + file_count * F.FILE_SIZE)
        if len(toc) < need:
            raise F.NupError("TOC truncated")
        if pool_off + pool_size > len(toc) or pool_off < need:
            raise F.NupError("string pool out of TOC bounds")

        self.dir_count = dir_count
        self.file_count = file_count
        self.pool = toc[pool_off:pool_off + pool_size]

        pos = F._toc_head_s.size
        self.packages: list[PackageInfo] = []
        for i in range(pkg_count):
            p = F.parse_pkg(toc, pos)
            pos += F.PKG_SIZE
            self.packages.append(PackageInfo(
                index=i, title_id=p.title_id, kind=F.Kind(p.kind), version=p.version,
                flags=p.flags, root_dir=p.root_dir, exefs_dir=p.exefs_dir,
                romfs_dir=p.romfs_dir, meta_offset=p.meta_offset,
                meta_size=p.meta_size))

        self.dirs: list[tuple[bytes, int, int]] = []  # (name, first_child, child_count)
        for _ in range(dir_count):
            off, ln, first, count, _r2 = F._dir_s.unpack_from(toc, pos)
            pos += F.DIR_SIZE
            self.dirs.append((self._pool_str(off, ln), first, count))

        # (name, size, sha256, ext_off, ext_size, codec)
        self.files: list[tuple[bytes, int, bytes, int, int, F.Codec]] = []  
        for _ in range(file_count):
            (off, ln), size, sha, eoff, esz, codec = F.parse_file(toc, pos)
            pos += F.FILE_SIZE
            self.files.append((self._pool_str(off, ln), size, sha, eoff, esz, codec))

        slot_end = pool_off
        if (slot_end - pos) % 4:
            raise F.NupError("children array not 4-byte aligned")
        self.children = list(struct.unpack_from(f"<{(slot_end - pos) // 4}I", toc, pos))

    def _pool_str(self, off: int, ln: int) -> bytes:
        if off + ln > len(self.pool):
            raise F.NupError("string reference out of pool bounds")
        return self.pool[off:off + ln]

    def _check_bounds(self) -> None:
        file_size = self.super["file_size"]
        blob_start = self.super["blob_offset"]
        for name, size, sha, eoff, esz, codec in self.files:
            if size == 0:
                continue
            if eoff < blob_start or eoff + esz > file_size:
                raise F.NupError(f"extent out of bounds for file {name!r}")
            if eoff % F.EXTENT_ALIGN:
                raise F.NupError(f"extent not {F.EXTENT_ALIGN}-aligned for {name!r}")
        slot_total = len(self.children)
        used = 0
        for name, first, count in self.dirs:
            if first + count > slot_total:
                raise F.NupError(f"child range out of bounds for dir {name!r}")
            used += count
        if used != slot_total:
            raise F.NupError(f"child slots used {used} != total {slot_total}")
        for p in self.packages:
            if p.root_dir >= self.dir_count:
                raise F.NupError(f"package {p.index}: root_dir out of bounds")
            for d in (p.exefs_dir, p.romfs_dir):
                if d != F.NO_DIR and d >= self.dir_count:
                    raise F.NupError(f"package {p.index}: exefs/romfs dir out of bounds")
            if p.meta_size and (p.meta_offset + p.meta_size > file_size):
                raise F.NupError(f"package {p.index}: meta block out of bounds")
            if p.flags & ~F.PKG_FLAG_STANDALONE_UPDATE:
                raise F.NupError(f"package {p.index}: unknown package flags {p.flags:#x}")
        # SPEC §3.1.2: update packages need a base in the same container, or
        # the standalone_update flag (record flags, offset 0x19, bit 0).
        has_base = any(p.kind == F.KIND_BASE for p in self.packages)
        for p in self.packages:
            if (p.kind == F.KIND_UPDATE and not has_base
                    and not (p.flags & F.PKG_FLAG_STANDALONE_UPDATE)):
                raise F.NupError(
                    f"package {p.index}: update without base in container and "
                    f"standalone_update flag not set")

    # ---- member access ----

    def node_name(self, ref: NodeRef) -> bytes:
        if ref.is_dir:
            return self.dirs[ref.index][0]
        return self.files[ref.index][0]

    def file_info(self, file_index: int) -> tuple[bytes, int, bytes, int, int, F.Codec]:
        return self.files[file_index]

    def iter_file_chunks(
        self, file_index: int, chunk_size: int = 64 << 20
    ) -> Iterator[bytes]:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        name, size, _sha, eoff, esz, codec = self.files[file_index]
        if size == 0:
            return
        total = 0
        if codec == F.CODEC_RAW:
            for offset in range(0, esz, chunk_size):
                data = self.read_at(
                    eoff + offset, min(chunk_size, esz - offset)
                )
                if not data:
                    raise F.NupError(f"short read for {name!r}")
                total += len(data)
                if total > size:
                    raise F.NupError(
                        f"size mismatch for {name!r}: more than {size} bytes"
                    )
                yield data
        elif codec == F.CODEC_ZSTD:
            import zstandard

            source = _FileExtentReader(self._fh, eoff, esz)
            dctx = zstandard.ZstdDecompressor()
            try:
                with dctx.stream_reader(
                    source, read_size=chunk_size, closefd=False
                ) as stream:
                    while data := stream.read(chunk_size):
                        total += len(data)
                        if total > size:
                            raise F.NupError(
                                f"size mismatch for {name!r}: "
                                f"more than {size} bytes"
                            )
                        yield data
            except zstandard.ZstdError as exc:
                raise F.NupError(
                    f"decompression failed for {name!r}: {exc}"
                ) from exc
        else:
            raise F.NupError(f"unknown codec {codec}")
        if total != size:
            raise F.NupError(f"size mismatch for {name!r}: {total} != {size}")

    def read_file(self, file_index: int) -> bytes:
        return b"".join(self.iter_file_chunks(file_index))
