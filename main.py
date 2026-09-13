"""Railpack-compatible ASGI entrypoint for the Chusky voice bridge.

Railway's Python detector may invoke ``uvicorn main:app`` even when the
service's explicit start command is ``python app.py``.  Re-export the existing
application so both launch paths serve the exact same FastAPI instance.
"""

from app import app

