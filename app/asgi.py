"""Entry point for the web server.

    uv run uvicorn app.asgi:app --port 8000

uvicorn (the web server) imports this module and serves `app`. Keeping the entry point
separate from create_app() lets tests build their own app with test settings.
"""

from app.main import create_app

app = create_app()
