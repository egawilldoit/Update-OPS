"""FastAPI application: route wiring + static-asset serving only.

No contract changes here: schemas/auth/jobs/config semantics are untouched.
CORS stays disabled (no CORSMiddleware). Authenticated /api/v1 responses
carry Cache-Control: no-store (set per-response in api/routes.py and enforced
here as a safety net).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router as v1_router
from .config import settings
from .db import connect, validate_schema
from .readiness import ReadinessError, validate_startup


@asynccontextmanager
async def lifespan(app: FastAPI):
    # type: (FastAPI) -> AsyncIterator[None]
    # R31: startup VALIDATES ONLY — the controlled deploy procedure owns
    # migration. A pending/incompatible/newer schema, or failed readiness
    # (placeholders, unreadable secrets), fails startup so an unsafe API
    # never serves traffic.
    conn = connect(settings.db_path)
    try:
        validate_schema(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    try:
        validate_startup("api", settings)
    except ReadinessError:
        raise
    yield


app = FastAPI(title="EGA Update Console", version="1.0.0", lifespan=lifespan)
app.include_router(v1_router)


@app.middleware("http")
async def _no_store_for_api(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/v1"):
        response.headers["Cache-Control"] = "no-store"
    return response


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
ASSETS_DIR = os.path.join(STATIC_DIR, "assets")

if os.path.isdir(ASSETS_DIR):
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

if os.path.isdir(STATIC_DIR):

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse(
                status_code=404,
                content={"code": "not_found", "message": "unknown api route",
                         "details": "", "request_id": ""},
                headers={"Cache-Control": "no-store"},
            )
        index = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(index):
            return FileResponse(index)
        return JSONResponse(
            status_code=404,
            content={"code": "not_found", "message": "frontend not built",
                     "details": "", "request_id": ""},
            headers={"Cache-Control": "no-store"},
        )
else:

    @app.get("/", include_in_schema=False)
    def _no_frontend():
        return JSONResponse(
            {"status": "api-only; frontend not built"},
            headers={"Cache-Control": "no-store"},
        )
