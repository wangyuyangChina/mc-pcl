# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys


project_root = Path(SPECPATH)
data_files = [
    (str(project_root / filename), ".")
    for filename in ("index.html", "app.js", "styles.css")
]

a = Analysis(
    [str(project_root / "launcher.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=data_files,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
if sys.platform == "darwin":
    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="MCPCL",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    collected = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="MCPCL",
    )
    app = BUNDLE(
        collected,
        name="方块启动器.app",
        icon=None,
        bundle_identifier="com.mc-pcl.launcher",
        info_plist={
            "CFBundleDisplayName": "方块启动器",
            "CFBundleName": "方块启动器",
            "CFBundleShortVersionString": "0.1.0",
            "LSMinimumSystemVersion": "10.15",
        },
    )
else:
    pyz = PYZ(a.pure)
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="MCPCL",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
