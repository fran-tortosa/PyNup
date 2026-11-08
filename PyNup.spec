# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

project_root = Path(SPECPATH)
nup_sources = project_root / "vendor" / "nup" / "tools" / "nup"

a = Analysis(
    [str(project_root / "app.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(nup_sources), "vendor/nup/tools/nup"),
        (str(project_root / "vendor" / "nup" / "LICENSE"), "vendor/nup"),
        (str(project_root / "db-name.ini"), "."),
        *[
            (str(project_root / "assets" / f"pynup-{size}.png"), "assets")
            for size in (16, 24, 32, 48, 64, 128, 256)
        ],
        (str(project_root / "assets" / "pynup.ico"), "assets"),
    ],
    hiddenimports=[
        *collect_submodules("zstandard"),
        "ctypes.wintypes",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PyNup",
    icon=str(project_root / "assets" / "pynup.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
