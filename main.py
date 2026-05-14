"""
Gemini live preview entrypoint.

Quickstart:
    pip install -r requirements.txt
    python main.py --mode camera --audio off
"""

from __future__ import annotations

import asyncio
import os

from app.bootstrap import configure_qt_fonts


def build_client():
    from google import genai

    return genai.Client(
        http_options={"api_version": "v1beta"},
        api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
    )


def main() -> None:
    configure_qt_fonts()
    from app.cli import parse_args

    config = parse_args()
    from app.live_loop import LiveApp

    app = LiveApp(client=build_client(), config=config)
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
