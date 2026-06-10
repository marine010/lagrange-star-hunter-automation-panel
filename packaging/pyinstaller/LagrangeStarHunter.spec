# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH).resolve().parents[1]
entry_script = project_root / "packaging" / "entrypoints" / "main_gui.py"

datas = []
binaries = []
hiddenimports = [
    "cv2",
    "mss",
    "PIL.ImageGrab",
]
hiddenimports += collect_submodules("pyautogui")

windows_capture = collect_all("windows_capture")
datas += windows_capture[0]
binaries += windows_capture[1]
hiddenimports += windows_capture[2]


a = Analysis(
    [str(entry_script)],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name="LagrangeStarHunter",
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
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LagrangeStarHunter",
)
