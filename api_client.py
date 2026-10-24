"""
recipang — agent-app API 클라이언트: Docker 의 frontend 컨테이너에서 app.py 가 쓰는 백엔드
==========================================================================================
backend.LocalBackend 와 같은 메서드를 HTTP 로 구현한다 → app.py 는 둘 중 무엇을 쓰든 같은 코드로 동작한다.

    app.py ── ApiBackend.chat(...) ──HTTP──▶ agent-app (api.py)  POST /chat ──▶ LocalBackend.chat(...)

실패는 LocalBackend 와 같은 예외로 바꿔 던진다:
  연결 실패·시간 초과·5xx → errors.BackendError (FastAPI 오류 응답의 detail 을 메시지로)
  409                    → errors.NoPendingError (확인 대기 없음)

이 파일은 requests 와 errors.py 만 쓴다 (LangGraph·임베딩 같은 무거운 모듈이 frontend 이미지에 들어가지 않도록).
"""

from __future__ import annotations

from urllib.parse import quote

import requests

from errors import BackendError, NoPendingError

# /chat 은 라우터 LLM + 해석 LLM + 검색 + 답변 LLM 을 거치므로 오래 걸릴 수 있다
CHAT_TIMEOUT_SECONDS = 120
DEFAULT_TIMEOUT_SECONDS = 15


class ApiBackend:
    """agent-app API 를 부르는 백엔드 (메서드 목록은 backend.py 참고).

    Args:
        base_url: agent-app 주소 (Docker: http://agent-app:8000 — 컨테이너 서비스 이름으로 접속).
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._catalog: dict[str, list[str]] | None = None  # 카탈로그는 바뀌지 않으므로 한 번만 받아 둔다

    def _request(self, method: str, path: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, **kwargs):
        """HTTP 요청을 보내고 JSON 응답을 돌려준다. 실패하면 원인별 메시지로 BackendError 를 던진다."""
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
        except requests.exceptions.ConnectionError as e:
            raise BackendError(f"agent-app({self.base_url})에 연결할 수 없어요. 컨테이너가 떠 있는지 확인해 주세요.") from e
        except requests.exceptions.Timeout as e:
            raise BackendError(f"agent-app 응답이 {timeout:g}초 안에 오지 않았어요. 다시 시도해 주세요.") from e
        if resp.ok:
            return resp.json()
        # FastAPI 오류 응답은 {"detail": ...} 형식이다. JSON 이 아니면 본문 텍스트를 쓴다.
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        if resp.status_code == 409:
            raise NoPendingError(detail)
        raise BackendError(detail if resp.status_code == 502 else f"agent-app 오류 {resp.status_code}: {detail}")

    def catalog(self) -> dict[str, list[str]]:
        if self._catalog is None:
            self._catalog = self._request("GET", "/catalog")
        return self._catalog

    def items(self) -> dict[str, list[str]]:
        return self._request("GET", "/items")

    def add_items(self, place: str, ingredients: list[str]) -> dict:
        return self._request("POST", f"/items/{place}", json={"ingredients": ingredients})

    def remove_item(self, place: str, ingredient: str) -> None:
        self._request("DELETE", f"/items/{place}/{quote(ingredient, safe='')}")

    def recommend(self, owned: list[str], limit: int) -> dict:
        return self._request("POST", "/recipes/recommend", json={"owned": owned, "limit": limit})

    def describe(self, recipe_ids: list[str], owned: list[str]) -> list[dict]:
        return self._request("POST", "/recipes/describe", json={"recipe_ids": recipe_ids, "owned": owned})

    def chat(self, query: str, thread_id: str, page_size: int) -> dict:
        payload = {"query": query, "thread_id": thread_id, "page_size": page_size}
        return self._request("POST", "/chat", json=payload, timeout=CHAT_TIMEOUT_SECONDS)

    def resume(self, thread_id: str, chosen: list[list[str]] | None) -> dict:
        return self._request("POST", f"/chat/{quote(thread_id, safe='')}/resume", json={"chosen": chosen},
                             timeout=CHAT_TIMEOUT_SECONDS)
