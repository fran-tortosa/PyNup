"""NUP tree access: traversal, metadata accessors, and extraction.

Convenience layer over a parsed NupReader: directory traversal and path
resolution, per-package meta/localized accessors, and subtree extraction.
Functions take the reader as their first argument.
"""

from __future__ import annotations

import ctypes
import os
import re
from pathlib import Path, PureWindowsPath
from typing import BinaryIO, TYPE_CHECKING

from . import format as F
from . import meta as M
from .read import NodeRef

if TYPE_CHECKING:
    from .read import NupReader, PackageInfo

    from collections.abc import Callable


_EXTRACTION_WRITE_BUFFER_SIZE = 16 << 20


def _open_extracted_file(path: Path) -> BinaryIO:
    if os.name != "nt":
        return path.open("wb")

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
        0x40000000,
        0x00000007,
        None,
        2,
        0x80000000 | 0x08000000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(
            handle, os.O_WRONLY | os.O_BINARY
        )
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(
        descriptor, "wb", buffering=_EXTRACTION_WRITE_BUFFER_SIZE
    )


def find_package(
    r: NupReader,
    kind: int | None = None,
    title_id: int | None = None,
    version: int | None = None,
) -> PackageInfo | None:
    for p in r.packages:
        if kind is not None and p.kind != kind:
            continue
        if title_id is not None and p.title_id != title_id:
            continue
        if version is not None and p.version != version:
            continue
        return p
    return None


def iter_children(r: NupReader, dir_index: int) -> list[NodeRef]:
    _, first, count = r.dirs[dir_index]
    out = []
    for slot in range(first, first + count):
        cid = r.children[slot]
        if cid >= r.dir_count:
            fi = cid - r.dir_count
            out.append(NodeRef(False, fi, r.files[fi][0]))
        else:
            out.append(NodeRef(True, cid, r.dirs[cid][0]))
    return out


def find_child(r: NupReader, dir_index: int, name: bytes) -> NodeRef | None:
    children = iter_children(r, dir_index)
    lo, hi = 0, len(children)
    while lo < hi:
        mid = (lo + hi) // 2
        if children[mid].name < name:
            lo = mid + 1
        else:
            hi = mid
    if lo < len(children) and children[lo].name == name:
        return children[lo]
    return None


def meta_records(r: NupReader, pkg: PackageInfo) -> list[tuple[int, bytes]]:
    out = []
    pos = 0
    block = r.read_at(pkg.meta_offset, pkg.meta_size)
    while pos + 8 <= len(block):
        t, _r, size = F._tlv_s.unpack_from(block, pos)
        if pos + 8 + size > len(block):
            raise F.NupError("meta TLV overruns block")
        out.append((t, bytes(block[pos + 8:pos + 8 + size])))
        pos += 8 + size
    return out


def _localized(r: NupReader, pkg: PackageInfo, tlv_type: int) -> dict[bytes, bytes]:
    out: dict[bytes, bytes] = {}
    for t, payload in meta_records(r, pkg):
        if t == tlv_type:
            out.update(M.decode_localized(payload))
    return out


def localized_titles(r: NupReader, pkg: PackageInfo) -> dict[bytes, bytes]:
    """BCP-47 tag (lowercase bytes) -> UTF-8 title name. SPEC §5.1, TLV 0x0009."""
    return _localized(r, pkg, F.TLV_LOCALIZED_TITLES)


def localized_icons(r: NupReader, pkg: PackageInfo) -> dict[bytes, bytes]:
    """BCP-47 tag (lowercase bytes) -> image bytes. SPEC §5.1, TLV 0x000A."""
    return _localized(r, pkg, F.TLV_LOCALIZED_ICONS)


def app_meta(r: NupReader, pkg: PackageInfo) -> dict[int, bytes]:
    """TLV type -> payload, preferring the base (or first) package's block."""
    return dict(meta_records(r, pkg))


def build_id(r: NupReader, pkg: PackageInfo) -> bytes | None:
    """32-byte ModuleId from meta TLV 0x0007, or None if absent (SPEC §5).

    Bytes 0x40..0x60 of the package's `main` (NSO0/NRO0 header); only the
    first 0x14 are significant in SDK-built titles."""
    payload = app_meta(r, pkg).get(F.TLV_BUILD_ID)
    if payload is not None and len(payload) != 32:
        raise F.NupError(f"build ID TLV payload is {len(payload)} bytes, expected 32")
    return payload


def resolve_path(r: NupReader, pkg: PackageInfo, relpath: str) -> NodeRef | None:
    """Resolve a '/'-separated path from the package root."""
    cur = NodeRef(True, pkg.root_dir, b"")
    for part in relpath.split("/"):
        if not part:
            continue
        if not cur.is_dir:
            return None
        nxt = find_child(r, cur.index, part.encode("utf-8"))
        if nxt is None:
            return None
        cur = nxt
    return cur


def extract_tree(
    r: NupReader,
    dest: str,
    dir_index: int,
    progress: Callable[[int, NodeRef], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> int:
    """Extract a directory subtree to dest. Returns number of files written."""
    n = 0
    destination = Path(dest)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    stack = [(dir_index, root)]
    while stack:
        if cancel_check:
            cancel_check()
        di, dpath = stack.pop()
        dpath.mkdir(parents=True, exist_ok=True)
        for child in iter_children(r, di):
            if cancel_check:
                cancel_check()
            name = child.name.decode("utf-8", "surrogateescape")
            if (
                not name
                or name in (".", "..")
                or "/" in name
                or "\\" in name
                or any(ord(char) < 32 or char in '<>:"|?*' for char in name)
                or name.endswith((" ", "."))
                or PureWindowsPath(name).is_absolute()
                or PureWindowsPath(name).drive
                or re.match(
                    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)",
                    name,
                    re.I,
                )
            ):
                raise F.NupError(f"unsafe path component in NUP: {child.name!r}")
            cpath = dpath / name
            try:
                cpath.resolve().relative_to(root)
            except ValueError as exc:
                raise F.NupError(
                    f"extraction path escapes destination: {child.name!r}"
                ) from exc
            if child.is_dir:
                stack.append((child.index, cpath))
            else:
                with _open_extracted_file(cpath) as fh:
                    for chunk in r.iter_file_chunks(child.index):
                        if cancel_check:
                            cancel_check()
                        fh.write(chunk)
                n += 1
                if progress:
                    progress(n, child)
    return n
