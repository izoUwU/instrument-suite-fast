"""Collect Python + MSVC runtime DLLs for PyInstaller onedir bundles.

Microsoft Store Python builds often bundle python313.dll but omit python3.dll,
which causes "Failed to load Python DLL" on launch (including on the build PC
in some PyInstaller versions). We force-include the full set from sys.base_prefix.
"""
from __future__ import annotations

import sys
from pathlib import Path


def python_runtime_binaries() -> list[tuple[str, str]]:
    names = [
        f"python{sys.version_info.major}{sys.version_info.minor}.dll",
        "python3.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
    ]
    roots: list[Path] = [
        Path(sys.base_prefix).resolve(),
        Path(sys.prefix).resolve(),
        Path(sys.executable).resolve().parent,
    ]

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for name in names:
            if name in seen:
                continue
            path = root / name
            if path.is_file():
                out.append((str(path), "."))
                seen.add(name)
    return out
