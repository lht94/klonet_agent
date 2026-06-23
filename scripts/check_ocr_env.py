from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def check_import(module_name: str) -> dict:
    try:
        __import__(module_name)
        return {"name": module_name, "available": True, "error": ""}
    except Exception as exc:  # pragma: no cover - environment probe
        return {"name": module_name, "available": False, "error": str(exc)}


def find_tesseract() -> str | None:
    command = os.environ.get("TESSERACT_CMD") or get_windows_user_env("TESSERACT_CMD")
    local_appdata = os.environ.get("LOCALAPPDATA")
    candidates = [
        Path(command) if command else None,
        Path(shutil.which("tesseract")) if shutil.which("tesseract") else None,
        Path(local_appdata) / "Programs" / "Tesseract-OCR" / "tesseract.exe" if local_appdata else None,
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return str(candidate)
    return None


def get_windows_user_env(name: str) -> str | None:
    try:
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"[Environment]::GetEnvironmentVariable('{name}', 'User')",
        ]
        value = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
        return value or None
    except Exception:  # pragma: no cover - Windows-only convenience
        return None


def main() -> int:
    python_packages = [
        check_import("PIL"),
        check_import("pytesseract"),
        check_import("pdf2image"),
        check_import("docx"),
        check_import("pptx"),
        check_import("pdfplumber"),
        check_import("pypdf"),
    ]

    tools = {
        "tesseract": find_tesseract(),
        "pdftoppm": shutil.which("pdftoppm"),
        "pdftocairo": shutil.which("pdftocairo"),
    }

    tesseract_version = ""
    if tools["tesseract"]:
        try:
            import pytesseract

            pytesseract.pytesseract.tesseract_cmd = tools["tesseract"]
            tesseract_version = str(pytesseract.get_tesseract_version())
        except Exception as exc:  # pragma: no cover - environment probe
            tesseract_version = f"unavailable: {exc}"

    result = {
        "python_packages": python_packages,
        "system_tools": tools,
        "tesseract_version": tesseract_version,
        "ready_for_docx_pptx_image_ocr": bool(tools["tesseract"])
        and all(item["available"] for item in python_packages[:2]),
        "ready_for_pdf_ocr": bool(tools["tesseract"])
        and bool(tools["pdftoppm"] or tools["pdftocairo"])
        and all(item["available"] for item in python_packages[:3]),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
