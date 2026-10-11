"""HTTP API v1. Същият engine се ползва от формата, чата и ERP."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .. import auth
from ..db.models import User
from .auth_routes import router as auth_router
from .quotations import router as quotations_router
from .registry import router as registry_router
from .deps import CALCULATORS, require

from .. import ENGINE_VERSION
from ..engine.calculate import calculate
from ..db.repository import build_config
from ..db.session import database_url, session_scope
from ..engine.config import Config, get_config
from ..engine.construction import UnsupportedConstruction
from ..engine.models import CalculateRequest, CalculationResult, TraceStep
from ..parser.quick import ParseResult, parse
from .price_lists import router as price_list_router

app = FastAPI(title="Калкулатор велпапе", version=ENGINE_VERSION,
              description="Детерминиран калкулационен engine. Decimal стойностите са низове.")

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
WEB = WEB_DIR / "index.html"
IMPORTS_WEB = WEB_DIR / "imports.html"
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
app.include_router(auth_router)
app.include_router(registry_router)
app.include_router(quotations_router)
app.include_router(price_list_router)
calculator = require(*CALCULATORS)


@app.middleware("http")
async def security_and_rate_limit(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        principal = (request.headers.get("authorization") or request.cookies.get(auth.SESSION_COOKIE)
                     or (request.client.host if request.client else "?"))
        key = "api:" + hashlib.sha256(principal.encode()).hexdigest()[:32]
        if not auth.limiter.allow(key, auth.settings()["rate_per_minute"]):
            return JSONResponse({"detail": "Твърде много заявки; опитайте след малко."}, status_code=429)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def current_config() -> Config:
    """С DATABASE_URL: регистрите и ценоразписите от PostgreSQL; иначе JSON (dev/тестове без база)."""
    if database_url():
        with session_scope() as s:
            return build_config(s)
    return get_config()


def _run(req: CalculateRequest) -> CalculationResult:
    try:
        return calculate(req, current_config())
    except (UnsupportedConstruction, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB)


@app.get("/imports", include_in_schema=False)
def imports_page():
    return FileResponse(IMPORTS_WEB)


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(WEB_DIR / "login.html")


@app.get("/quotations", include_in_schema=False)
def quotations_page():
    return FileResponse(WEB_DIR / "quotations.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "engine_version": ENGINE_VERSION}


@app.get("/api/v1/engine-info")
def engine_info(_: User = Depends(calculator)):
    cfg = current_config()
    return {"engine_version": ENGINE_VERSION, "rule_version": cfg.rule_version, "test_data_only": cfg.test_data_only}


@app.post("/api/v1/calculate", response_model=CalculationResult)
def calculate_endpoint(req: CalculateRequest, _: User = Depends(calculator)):
    started = time.perf_counter()
    res = _run(req)
    res.trace.append(TraceStep(step="timing", result=f"{(time.perf_counter() - started) * 1000:.1f} ms"))
    return res


@app.post("/api/v1/recommend-material")
def recommend_material(req: CalculateRequest, _: User = Depends(calculator)):
    res = _run(req)
    return {"recommendation": res.recommendation, "material": res.material, "warnings": res.warnings}


@app.get("/api/v1/material-codes")
def materials(_: User = Depends(calculator)):
    cfg = current_config()
    return {code: {"display_code": m.display_code, "flute": m.flute, "tags": m.tags} for code, m in
            cfg.materials.items()}


class ChatRequest(BaseModel):
    text: str
    state: Optional[dict[str, Any]] = None


class ChatResponse(BaseModel):
    parse: ParseResult
    state: dict[str, Any]
    result: Optional[CalculationResult] = None
    error: Optional[str] = None
    explanation: list[str] = []


@app.post("/api/v1/chat/parse", response_model=ParseResult)
def chat_parse(body: ChatRequest, _: User = Depends(calculator)):
    return parse(body.text, body.state)


@app.post("/api/v1/chat/message", response_model=ChatResponse)
def chat_message(body: ChatRequest, _: User = Depends(calculator)):
    """Парсване → валидиране → engine. При грешка няма измислен резервен резултат."""
    p = parse(body.text, body.state)
    state = {k: v for k, v in p.request.items() if not k.startswith("_")}
    if not p.calculable:
        return ChatResponse(parse=p, state=p.request, explanation=["Липсва: " + ", ".join(p.missing_inputs)])
    try:
        cfg = current_config()
        req = CalculateRequest.model_validate(state)
        res = calculate(req, cfg)
        explanation = []
        if p.intent == "select_economy":
            econ = res.recommendation.economy if res.recommendation else None
            if econ:
                state["material_code"] = econ
                res = calculate(CalculateRequest.model_validate(state), cfg)
                explanation.append(f"Избран economy материал {econ} (техническата еквивалентност не е потвърдена).")
            else:
                explanation.append("Няма по-евтина допустима алтернатива с налична цена.")
            p.request = state
    except (ValidationError, ValueError, UnsupportedConstruction) as e:
        return ChatResponse(parse=p, state=p.request, error=str(e))
    return ChatResponse(parse=p, state=p.request, result=res, explanation=explanation)
