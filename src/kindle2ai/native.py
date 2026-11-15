"""Thin wrapper around the compiled Swift helper (`build/kindle-ocr`)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "build" / "kindle-ocr"
SOURCE = ROOT / "swift" / "KindleOCR.swift"


class NativeError(RuntimeError):
    pass


def ensure_binary() -> Path:
    """Build the helper on first use so a fresh checkout just works."""
    if BINARY.exists() and BINARY.stat().st_mtime >= SOURCE.stat().st_mtime:
        return BINARY
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["swiftc", "-O", str(SOURCE), "-o", str(BINARY)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise NativeError(
            "failed to build kindle-ocr (Xcode Command Line Tools required):\n"
            + proc.stderr.strip()
        )
    return BINARY


def run(*args: str, timeout: float = 120.0) -> str:
    proc = subprocess.run(
        [str(ensure_binary()), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise NativeError(proc.stderr.strip() or f"kindle-ocr {args[0]} failed")
    return proc.stdout


def run_json(*args: str, timeout: float = 120.0):
    return json.loads(run(*args, timeout=timeout))


def permissions() -> dict:
    return run_json("perms")


def request_permissions() -> dict:
    return run_json("perms", "--request")


def windows(bundle: str | None = None) -> list[dict]:
    args = ["windows"]
    if bundle:
        args += ["--bundle", bundle]
    return run_json(*args)


def ocr(image: Path, langs: str = "ja-JP,en-US", min_conf: float = 0.0,
        fast: bool = False) -> dict:
    args = ["ocr", str(image), "--langs", langs, "--min-conf", str(min_conf)]
    if fast:
        args.append("--fast")
    return run_json(*args)


def livetext(image: Path, langs: str = "ja-JP") -> dict:
    """Live Text transcript: {'width', 'height', 'text'}. Reads vertical text."""
    return run_json("livetext", str(image), "--langs", langs)


def livetext_available() -> bool:
    """Live Text (VisionKit ImageAnalyzer) shipped in macOS 13."""
    import platform
    version = platform.mac_ver()[0]
    return bool(version) and int(version.split(".")[0]) >= 13


def dhash(image: Path) -> int:
    return int(run("dhash", str(image)).strip(), 16)


def ink(image: Path, rect: tuple[float, float, float, float]) -> float:
    """Share of dark pixels in rect = (x, y, w, h) as fractions of the image."""
    return run_json("ink", str(image), "--rect", ",".join(map(str, rect)))["ink"]


def compare(a: Path, b: Path, size: int = 256) -> dict:
    """{'mean_abs', 'changed_frac'} - how different two captures look."""
    return run_json("compare", str(a), str(b), "--size", str(size))


def prep(src: Path, dest: Path, crop: tuple[float, float, float, float] = (0, 0, 0, 0),
         scale: float = 1.0, contrast: bool = False, binarize: bool = False,
         strip_ruby: bool = False) -> dict:
    """Crop chrome, scale up and stretch contrast before OCR."""
    top, bottom, left, right = crop
    args = ["prep", str(src), str(dest)]
    if scale != 1.0:
        args += ["--scale", str(scale)]
    for name, value in (("crop-top", top), ("crop-bottom", bottom),
                        ("crop-left", left), ("crop-right", right)):
        if value:
            args += [f"--{name}", str(value)]
    if contrast:
        args.append("--contrast")
    if binarize:
        args.append("--binarize")
    if strip_ruby:
        args.append("--strip-ruby")
    return run_json(*args)


def pdf_text(path: Path) -> dict:
    return run_json("pdftext", str(path), timeout=600)


def pdf_render(path: Path, outdir: Path, dpi: int = 200) -> dict:
    return run_json("pdfrender", str(path), str(outdir), "--dpi", str(dpi), timeout=1800)


def supported_languages() -> list[str]:
    return run_json("langs")


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def screencapture_window(window_id: int, dest: Path) -> None:
    """Capture a single window. Requires Screen Recording permission."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["screencapture", "-x", "-o", f"-l{window_id}", str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not dest.exists():
        raise NativeError(proc.stderr.strip() or "screencapture failed")


def osascript(script: str) -> str:
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        raise NativeError(proc.stderr.strip() or "osascript failed")
    return proc.stdout.strip()
