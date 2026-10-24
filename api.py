"""
recipang — agent-app API (FastAPI): Docker 의 frontend 컨테이너가 부르는 HTTP API
==================================================================================
backend.LocalBackend 의 메서드를 그대로 HTTP 로 연다 (day5 의 agent-app/main.py 와 같은 역할).

  메서드  경로                          LocalBackend           요청 본문 → 응답
  ─────────────────────────────────────────────────────────────────────────────────────────────────────
  GET     /health                       —                      → {"status": "ok"}  (compose healthcheck)
  GET     /catalog                      catalog()              → {"채소": ["감자", ...], ...}
  GET     /items                        items()                → {"cart": [...], "fridge": [...]}
  POST    /items/{place}                add_items()            {"ingredients": [...]} → {"added", "already", "in_other"}
  DELETE  /items/{place}/{ingredient}   remove_item()          → {"removed": 재료}
  POST    /recipes/recommend            recommend()            {"owned", "limit"} → {"recipes": [카드], "total"}
  POST    /recipes/describe             describe()             {"recipe_ids", "owned"} → [카드]
  POST    /chat                         chat()                 {"query", "thread_id", "page_size"} → GraphResult
  POST    /chat/{thread_id}/resume      resume()               {"chosen": [[품목]] 또는 null} → GraphResult

  place = "cart" | "fridge".  카드 = recipe_db.Recommendation, GraphResult = graph.GraphResult 의 필드 그대로.
  오류: errors.NoPendingError → 409, errors.BackendError → 502 (본문 {"detail": 화면에 보여 줄 문장}).

실행: uvicorn api:app --host 0.0.0.0 --port 8000   (Docker 에서는 agent-app 컨테이너가 실행)
      http://localhost:8000/docs 에서 API 를 직접 시험해 볼 수 있다.

엔드포인트는 일반 함수(def)로 둔다 — FastAPI 가 스레드 풀에서 실행하므로, LocalBackend 가 자기 이벤트 루프로
그래프를 돌리는 동안 서버의 이벤트 루프를 막지 않는다.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend import LocalBackend
from errors import BackendError, NoPendingError

backend = LocalBackend()
Place = Literal["cart", "fridge"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """시작할 때 에이전트를 준비한다 (검색 색인 확인 + MCP 연결) → 준비가 끝나야 /health 가 응답한다."""
    await run_in_threadpool(backend.start)
    yield
    await run_in_threadpool(backend.close)


app = FastAPI(title="recipang agent-app", lifespan=lifespan)


@app.exception_handler(NoPendingError)
async def _no_pending(request: Request, exc: NoPendingError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(BackendError)
async def _backend_error(request: Request, exc: BackendError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


# ── 요청 본문 ───────────────────────────────────────────────────────────
class IngredientsBody(BaseModel):
    ingredients: list[str] = Field(min_length=1)


class RecommendBody(BaseModel):
    owned: list[str]
    limit: int = Field(default=3, ge=1)


class DescribeBody(BaseModel):
    recipe_ids: list[str]
    owned: list[str]


class ChatBody(BaseModel):
    query: str = Field(min_length=1)
    thread_id: str = "default"  # 대화방 id — frontend 가 브라우저 세션마다 하나씩 만들어 보낸다
    page_size: int = Field(default=3, ge=1)


class ResumeBody(BaseModel):
    chosen: list[list[str]] | None = None  # 작업마다 실행할 품목. null 이면 취소


# ── 엔드포인트 ─────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/catalog")
def get_catalog() -> dict[str, list[str]]:
    return backend.catalog()


@app.get("/items")
def get_items() -> dict[str, list[str]]:
    return backend.items()


@app.post("/items/{place}")
def add_items(place: Place, body: IngredientsBody) -> dict:
    return backend.add_items(place, body.ingredients)


@app.delete("/items/{place}/{ingredient}")
def remove_item(place: Place, ingredient: str) -> dict:
    backend.remove_item(place, ingredient)
    return {"removed": ingredient}


@app.post("/recipes/recommend")
def recommend(body: RecommendBody) -> dict:
    return backend.recommend(body.owned, body.limit)


@app.post("/recipes/describe")
def describe(body: DescribeBody) -> list[dict]:
    return backend.describe(body.recipe_ids, body.owned)


@app.post("/chat")
def chat(body: ChatBody) -> dict:
    return backend.chat(body.query, body.thread_id, body.page_size)


@app.post("/chat/{thread_id}/resume")
def resume(thread_id: str, body: ResumeBody) -> dict:
    return backend.resume(thread_id, body.chosen)
