"""Environment bootstrap helpers."""

from __future__ import annotations

import os
from pathlib import Path


def configure_qt_fonts() -> None:
    """Point OpenCV's Qt backend at a system font directory if needed."""
    selected_font_dir = None
    for font_dir in (
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/truetype/liberation2",
        "/usr/share/fonts/truetype/liberation",
    ):
        if os.path.isdir(font_dir):
            selected_font_dir = font_dir
            break

    if selected_font_dir and "QT_QPA_FONTDIR" not in os.environ:
        os.environ["QT_QPA_FONTDIR"] = selected_font_dir

    if not selected_font_dir:
        return

    # OpenCV's bundled Qt plugin may still look for cv2/qt/fonts first.
    project_root = Path(__file__).resolve().parents[1]
    qt_dirs = list(project_root.glob(".venv/lib/python*/site-packages/cv2/qt"))
    for qt_dir in qt_dirs:
        fonts_dir = qt_dir / "fonts"
        if fonts_dir.exists():
            continue
        try:
            fonts_dir.symlink_to(selected_font_dir)
        except Exception:
            try:
                fonts_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
