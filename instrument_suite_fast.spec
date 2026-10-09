# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for instrument_suite_fast.py (multi-instrument, tk.Canvas plots).
#
# Build:    pyinstaller --noconfirm instrument_suite_fast.spec
#           OR: powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -Target fast
# Output:   dist\InstrumentSuiteFast\InstrumentSuiteFast.exe  (double-click to run)
#
# One-folder build. Distribute the entire dist\InstrumentSuiteFast folder (or zip).
# Do NOT copy only InstrumentSuiteFast.exe -- _internal\ must stay beside it.
# VISA hardware still requires an IVI/VISA driver (NI-VISA / vendor).
#
# Leaner than instrument_suite.spec: no matplotlib / pymeasure / numpy / pandas.

import os

from PyInstaller.utils.hooks import collect_all

from pyinstaller_runtime_dlls import python_runtime_binaries

block_cipher = None

visa_datas, visa_binaries, visa_imports = collect_all('pyvisa')
_runtime_bins = python_runtime_binaries()

a = Analysis(
    ['instrument_suite_fast.py'],
    pathex=[os.path.dirname(os.path.abspath(SPEC))],
    binaries=visa_binaries + _runtime_bins,
    datas=visa_datas,
    hiddenimports=visa_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'pymeasure',
        'pandas',
        'scipy',
        'numpy',
        'PIL',
        'IPython',
        'pytest',
        'tornado',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'tkinter.test',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='InstrumentSuiteFast',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='InstrumentSuiteFast',
)
