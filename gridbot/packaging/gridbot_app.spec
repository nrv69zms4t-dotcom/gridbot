# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for GridBot (onefile, windowed).

Build (from repo root, any cwd works — paths are resolved from SPECPATH):

    python -m PyInstaller gridbot/packaging/gridbot_app.spec --noconfirm \
        --distpath gridbot/packaging/dist --workpath gridbot/packaging/build

Notes on pywebview 6.x / Windows:
  * pywebview ships its own PyInstaller hook (webview/__pyinstaller/hook-webview.py,
    auto-discovered via the `pyinstaller40` entry point). It collects webview/lib
    (WebView2 DLLs, WebBrowserInterop) and webview/js. No manual collect_* needed.
  * pythonnet 3.x and pyinstaller-hooks-contrib provide hook-clr.py /
    hook-clr_loader.py (also auto-discovered) for the .NET runtime bits used by
    the winforms/edgechromium backend.
  * The GUI backends are imported dynamically by webview.guilib, so they are
    listed in hiddenimports explicitly.
"""

import os

# SPECPATH is provided by PyInstaller: directory containing this spec file
# (<repo>/gridbot/packaging). Repo root is two levels up.
SPEC_DIR = SPECPATH
REPO_ROOT = os.path.abspath(os.path.join(SPEC_DIR, os.pardir, os.pardir))

# NOTE: gridbot/app/main.py cannot be the entry script directly — it is a
# package module with relative imports, which fail when PyInstaller runs it
# as a top-level script.  launch_gridbot.py imports it absolutely.
ENTRY_SCRIPT = os.path.join(SPEC_DIR, 'launch_gridbot.py')
WEB_DIR = os.path.join(REPO_ROOT, 'gridbot', 'app', 'web')
ICON_FILE = os.path.join(SPEC_DIR, 'gridbot.ico')
VERSION_FILE = os.path.join(SPEC_DIR, 'version_info.txt')

a = Analysis(
    [ENTRY_SCRIPT],
    pathex=[REPO_ROOT],
    binaries=[],
    # Frontend assets: resolved at runtime via sys._MEIPASS + 'gridbot/app/web'
    datas=[(WEB_DIR, 'gridbot/app/web')],
    hiddenimports=[
        # pywebview Windows backends (loaded dynamically by webview.guilib)
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
        # .NET bridge used by the winforms/edgechromium backend
        'clr',
        'clr_loader',
        'pythonnet',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # keep the onefile exe lean; none of these are used by the app
        'tkinter',
        'matplotlib',
        'scipy',
        'IPython',
        'jupyter',
        'pytest',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
        'qtpy',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GridBot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,               # windowed app: no console, no print()
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_FILE,
    version=VERSION_FILE,
)
