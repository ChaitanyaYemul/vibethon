from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
import logging
from app.config import settings
from app.errors import ApiError
from app.security import limiter
from app.routers import auth, batches, assignments, riders, internal, shelters, events, admin


def create_app():
    app = FastAPI(title="Last-Mile Spoilage Trap")
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=settings.origins,
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})
        return response

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return JSONResponse(status_code=exc.status, content=jsonable_encoder({"code": exc.code, "message": exc.message, **exc.details}))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={"code": "VALIDATION_ERROR", "message": "Invalid request fields"})

    @app.exception_handler(RateLimitExceeded)
    async def rate_error(request, exc):
        return JSONResponse(status_code=429, content={"code": "RATE_LIMITED", "message": "Too many requests"})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger(__name__).exception("Request failed", exc_info=exc)
        return JSONResponse(status_code=500, content={"code": "INTERNAL", "message": "Internal server error"})

    app.include_router(auth.router, prefix="/api")
    app.include_router(batches.router, prefix="/api")
    app.include_router(assignments.router, prefix="/api")
    app.include_router(riders.router, prefix="/api")
    app.include_router(internal.router, prefix="/api")
    app.include_router(shelters.router, prefix="/api")
    app.include_router(events.router, prefix="/api")
    app.include_router(admin.router, prefix="/api")
    if settings.demo_mode:
        from app.routers import demo
        app.include_router(demo.router, prefix="/api")
    app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[1] / "public", html=True), name="ui")
    return app


app = create_app()
