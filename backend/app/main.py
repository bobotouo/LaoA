from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .data_service import MarketDataError, MarketDataService


app = FastAPI(title="A股全景 API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_origin_regex=r"https://.*\.(vercel\.app|dpdns\.org)",
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

service = MarketDataService(
    # Cold starts on Vercel benefit from a slightly longer request timeout.
    timeout=8.0 if os.getenv("VERCEL") else 5.0,
)


@app.get("/api/health")
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/market/dashboard")
@app.get("/market/dashboard")
def dashboard(response: Response, refresh: bool = Query(default=False)) -> dict:
    try:
        result = service.get_dashboard(refresh=refresh)
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if os.getenv("VERCEL"):
        if refresh:
            response.headers["Cache-Control"] = "no-store"
        else:
            ttl = 8 if result.get("meta", {}).get("isTrading") else 120
            response.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
            response.headers["Vercel-CDN-Cache-Control"] = (
                f"s-maxage={ttl}, stale-while-revalidate=60"
            )
    return result


# Local `make run` still serves the Vite build. On Vercel, static files come from /public.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if FRONTEND_DIST.exists() and not os.getenv("VERCEL"):
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def frontend(full_path: str) -> FileResponse:
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
elif not os.getenv("VERCEL"):

    @app.get("/")
    def root() -> dict[str, str]:
        return {
            "message": "A股全景 API 正在运行。前端开发服务器请访问 http://localhost:5173",
            "docs": "/docs",
        }
