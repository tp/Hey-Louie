"""Modal entry point.

Dev:    `modal serve backend/deploy.py`
Deploy: `modal deploy backend/deploy.py`

The FastAPI app itself lives in `backend/main.py` so it can be developed
locally without a Modal install. This module only wraps it.
"""

from __future__ import annotations

import modal
from fastapi import FastAPI

from backend.main import app as fastapi_app

app = modal.App("hey-louie-agent")

image = (
    modal.Image.debian_slim(python_version="3.14")
    .pip_install_from_pyproject("pyproject.toml")
    .add_local_python_source("backend")
)


@app.function(image=image, secrets=[modal.Secret.from_dotenv()])
@modal.asgi_app()
def web() -> FastAPI:
    return fastapi_app
