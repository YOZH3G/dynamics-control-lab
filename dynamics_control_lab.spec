# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path
from zipfile import ZipFile

from PyInstaller.utils.hooks.tcl_tk import tcltk_info


data_files = [("data/builtin_scenarios.json", "data"), ("data/identification_step.csv", "data"), ("data/heater_object.json", "data")]
if sys.platform == "win32" and tcltk_info.tcl_data_dir.startswith("//zipfs:"):
    # Tcl/Tk 9 keeps its scripts in external archives, missed by the hook.
    for library, target in (("tcl", "_tcl_data"), ("tk", "_tk_data")):
        archives = list((Path(sys.base_prefix) / "tcl").glob(f"lib{library}*.zip"))
        if len(archives) != 1:
            raise RuntimeError(f"Expected one {library} script archive: {archives}")
        destination = Path("build") / "tk_scripts" / library
        with ZipFile(archives[0]) as archive:
            archive.extractall(destination)
        data_files.append((str(destination / f"{library}_library"), target))

analysis = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=data_files,
    # Object pages are imported by name from the model registry, which the analysis cannot see.
    hiddenimports=["matplotlib.backends.backend_tkagg", *(f"ui.{p.stem}" for p in Path("ui").glob("*.py") if p.stem != "__init__")],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

# One folder starts about twice as fast as a one-file EXE, which unpacks itself on every launch.
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="DynamicsControlLab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collected = COLLECT(executable, analysis.binaries, analysis.datas, strip=False, upx=True, upx_exclude=[],
                    name="DynamicsControlLab")
