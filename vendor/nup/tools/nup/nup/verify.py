"""NUP verifier: structural conformance + deep content verification."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from . import format as F
from .read import NupReader

if TYPE_CHECKING:
    from collections.abc import Callable


def verify(
    path: str,
    deep: bool = False,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[bool, list[str]]:
    """Returns (ok, problems). `deep` recomputes every file's SHA-256."""
    problems: list[str] = []
    try:
        r = NupReader(path)
    except F.NupError as e:
        return False, [f"open: {e}"]

    def fail(msg: str) -> None:
        problems.append(msg)

    def check_cancelled() -> None:
        if cancel_check is not None:
            try:
                cancel_check()
            except Exception:
                r.close()
                raise

    s = r.super
    if s["toc_offset"] != F.HEADER_SIZE:
        fail(f"toc_offset {s['toc_offset']:#x} != {F.HEADER_SIZE:#x} (v1)")
    if s["file_size"] < s["blob_offset"]:
        fail("file_size smaller than blob_offset")

    # slot overlap check: ranges must be disjoint
    ranges = sorted((first, first + count, i)
                    for i, (_, first, count) in enumerate(r.dirs))
    for (a1, a2, ai), (b1, b2, bi) in zip(ranges, ranges[1:]):
        check_cancelled()
        if a2 > b1:
            fail(f"child slot ranges overlap: dirs {ai} and {bi}")

    # sorted + unique children per dir (producer conformance §6.2)
    for di, (name, first, count) in enumerate(r.dirs):
        check_cancelled()
        kids = r.children[first:first + count]
        names = []
        for cid in kids:
            if cid >= r.dir_count:
                names.append(r.files[cid - r.dir_count][0])
            else:
                names.append(r.dirs[cid][0])
        if names != sorted(names):
            fail(f"dir {di} ({name!r}): children not sorted")
        if len(set(names)) != len(names):
            fail(f"dir {di} ({name!r}): duplicate child names")

    # one global traversal from package roots: cycle + orphan detection, O(n)
    visited: set[int] = set()
    for p in r.packages:
        stack: list[tuple[int, tuple[int, ...]]] = [(p.root_dir, (p.root_dir,))]
        while stack:
            check_cancelled()
            di, path_ids = stack.pop()
            if di in path_ids[:-1]:
                fail(f"cycle detected through dir {di}")
                continue
            if di in visited:
                continue
            visited.add(di)
            _, first, count = r.dirs[di]
            for cid in r.children[first:first + count]:
                if cid < r.dir_count:
                    stack.append((cid, path_ids + (cid,)))
    orphans = set(range(r.dir_count)) - visited
    if orphans:
        fail(f"{len(orphans)} unreachable dir nodes (orphans)")

    referenced: set[int] = {cid - r.dir_count for cid in r.children if cid >= r.dir_count}
    unreferenced = set(range(r.file_count)) - referenced
    if unreferenced:
        names = [r.files[fi][0] for fi in sorted(unreferenced)[:8]]
        more = "..." if len(unreferenced) > len(names) else ""
        fail(f"{len(unreferenced)} file node(s) not referenced by any child slot "
             f"(unextractable): {[n.decode('utf-8', 'replace') for n in names]}{more}")

    if deep:
        seen_extents: dict[tuple[int, int, int], bytes] = {}
        for fi, (name, size, sha, eoff, esz, codec) in enumerate(r.files):
            check_cancelled()
            digest = hashlib.sha256()
            try:
                for chunk in r.iter_file_chunks(fi):
                    check_cancelled()
                    digest.update(chunk)
            except F.NupError as e:
                fail(f"file {fi} ({name!r}): read failed: {e}")
                continue
            if digest.digest() != sha:
                fail(f"file {fi} ({name!r}): SHA-256 mismatch")
            if codec == F.CODEC_RAW and esz != size:
                fail(f"file {fi} ({name!r}): raw extent size {esz} != {size}")
            key = (eoff, esz, codec)
            if key in seen_extents:
                if seen_extents[key] != sha:
                    fail(f"file {fi} ({name!r}): shares extent with different hash")
            else:
                seen_extents[key] = sha

    r.close()
    return not problems, problems
