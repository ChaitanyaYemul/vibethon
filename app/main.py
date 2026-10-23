from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.config import settings


def create_app():
    app = FastAPI(title="Last-Mile Spoilage Trap")
    app.add_middleware(CORSMiddleware, allow_origins=settings.origins,
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[1] / "public", html=True), name="ui")
    return app


app = create_app()
