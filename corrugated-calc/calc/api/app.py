"""HTTP API v1. Същият engine се ползва от формата, чата и ERP."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

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

WEB = Path(__file__).resolve().parents[1] / "web" / "index.html"
IMPORTS_WEB = Path(__file__).resolve().parents[1] / "web" / "imports.html"
app.include_router(price_list_router)


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


@app.get("/api/v1/health")
def health():
    cfg = current_config()
    return {"status": "ok", "engine_version": ENGINE_VERSION, "rule_version": cfg.rule_version,
            "test_data_only": cfg.test_data_only}


@app.post("/api/v1/calculate", response_model=CalculationResult)
def calculate_endpoint(req: CalculateRequest):
    started = time.perf_counter()
    res = _run(req)
    res.trace.append(TraceStep(step="timing", result=f"{(time.perf_counter() - started) * 1000:.1f} ms"))
    return res


@app.post("/api/v1/recommend-material")
def recommend_material(req: CalculateRequest):
    res = _run(req)
    return {"recommendation": res.recommendation, "material": res.material, "warnings": res.warnings}


@app.get("/api/v1/materials")
def materials():
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
def chat_parse(body: ChatRequest):
    return parse(body.text, body.state)


@app.post("/api/v1/chat/message", response_model=ChatResponse)
def chat_message(body: ChatRequest):
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
