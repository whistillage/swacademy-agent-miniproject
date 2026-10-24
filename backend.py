"""
recipang — 백엔드: 화면(app.py)이 쓰는 기능을 한곳에 모은 인터페이스 (같은 프로세스에서 직접 실행)
==================================================================================================
화면이 쓰는 기능 8가지를 메서드로 제공한다. 반환값은 모두 JSON 으로 바꿀 수 있는 dict·list·str 이다.

  메서드                            하는 일                                       API (api.py)
  ───────────────────────────────────────────────────────────────────────────────────────────────────
  catalog()                         대분류 → 식자재 목록                            GET    /catalog
  items()                           장바구니·냉장고 목록                             GET    /items
  add_items(place, ingredients)     담기 (장보기·[+ 재료] — 직접 누르는 버튼)          POST   /items/{place}
  remove_item(place, ingredient)    빼기 (사이드바 [빼기])                           DELETE /items/{place}/{ingredient}
  recommend(owned, limit)           [내 재료로 추천] — 더 필요한 재료가 적은 순          POST   /recipes/recommend
  describe(recipe_ids, owned)       레시피 카드 정보 (검색 결과·[더보기])               POST   /recipes/describe
  chat(query, thread_id, page_size) 자연어 요청 → 그래프 실행                         POST   /chat
  resume(thread_id, chosen)         확인 대기 작업 실행(체크한 품목) / 취소(None)       POST   /chat/{thread_id}/resume

두 가지 방식으로 쓰인다:
  - 로컬 실행: app.py 가 LocalBackend 를 직접 만들어 부른다 (streamlit run app.py 하나로 실행).
  - Docker  : agent-app 컨테이너의 api.py 가 LocalBackend 를 HTTP API 로 열고, frontend 컨테이너의 app.py 는
              같은 메서드를 가진 api_client.ApiBackend 로 그 API 를 부른다 (day5 방식).

실패는 errors.BackendError(화면에 보여 줄 문장) / errors.NoPendingError(확인 대기 없음)로 알린다.

그래프(에이전트)는 전용 이벤트 루프 스레드에서 돌린다 — MCP 연결은 그 연결을 연 이벤트 루프에서만 쓸 수 있으므로
모든 요청을 이 루프로 보낸다. 로컬 실행은 처음 chat() 을 부를 때, API 서버는 시작할 때 start() 로
검색 색인 확인과 MCP 도구 서버 연결을 한 번 한다 (약 10초).
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict

import anthropic

import catalog
import recipe_db
import storage
from errors import BackendError
from graph import RecipangAgent


class LocalBackend:
    """화면이 쓰는 기능 8가지를 같은 프로세스에서 실행한다 (메서드 목록은 모듈 설명 참고).

    핵심 변수:
        _loop:  그래프 전용 이벤트 루프 (별도 스레드에서 계속 돈다).
        _agent: graph.RecipangAgent — 그래프 + MCP 연결 + 체크포인터(대화 기억). start() 전에는 None.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._agent: RecipangAgent | None = None
        self._lock = threading.Lock()  # 여러 요청이 동시에 start() 를 불러도 에이전트를 하나만 만든다

    # ── 에이전트 준비 ─────────────────────────────────────────────────
    def start(self) -> None:
        """이벤트 루프 스레드를 띄우고 에이전트를 준비한다 (검색 색인 확인 + MCP 연결). 이미 준비됐으면 그대로 둔다."""
        with self._lock:
            if self._agent is not None:
                return
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, daemon=True, name="recipang-agent-loop").start()
            agent = RecipangAgent()
            self._run(agent.start())
            self._agent = agent

    def close(self) -> None:
        """MCP 연결을 닫는다."""
        agent, self._agent = self._agent, None
        if agent is not None:
            self._run(agent.stop())

    def _run(self, coro):
        """코루틴을 그래프 전용 이벤트 루프에서 실행하고 결과를 기다린다."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    # ── 장바구니·냉장고 (직접 누르는 버튼 — 확인 없이 바로 실행) ──────────
    def catalog(self) -> dict[str, list[str]]:
        """대분류 → 식자재 목록 (카탈로그 순서)."""
        return {category: catalog.list_ingredients(category) for category in catalog.list_categories()}

    def items(self) -> dict[str, list[str]]:
        """{"cart": [...], "fridge": [...]} — 담은 순서대로."""
        return {place: storage.list_items(place) for place in (storage.CART, storage.FRIDGE)}

    def add_items(self, place: str, ingredients: list[str]) -> dict:
        """담기. {"added": [...], "already": [...], "in_other": [...]} ('한쪽에만' 규칙은 storage.add_items 참고)."""
        return asdict(storage.add_items(place, ingredients))

    def remove_item(self, place: str, ingredient: str) -> None:
        """빼기 (없으면 아무 일도 하지 않음)."""
        storage.remove_item(place, ingredient)

    # ── 레시피 카드 ───────────────────────────────────────────────────
    def recommend(self, owned: list[str], limit: int) -> dict:
        """[내 재료로 추천] — {"recipes": [카드, ...], "total": 전체 레시피 수}. 카드 = recipe_db.Recommendation 의 dict."""
        return {"recipes": [asdict(r) for r in recipe_db.recommend(owned, limit)], "total": recipe_db.count_recipes()}

    def describe(self, recipe_ids: list[str], owned: list[str]) -> list[dict]:
        """recipe_ids 순서대로 카드 정보 (missing 은 owned 기준)."""
        return [asdict(r) for r in recipe_db.describe(recipe_ids, owned)]

    # ── 대화 (그래프) ─────────────────────────────────────────────────
    def chat(self, query: str, thread_id: str, page_size: int) -> dict:
        """자연어 요청을 그래프로 처리한다. 반환값은 graph.GraphResult 의 dict.

        Raises:
            BackendError: AI 호출 실패 등 — 메시지는 화면에 그대로 보여 줄 문장.
        """
        try:
            self.start()
            return asdict(self._run(self._agent.handle_query(query, thread_id=thread_id, page_size=page_size)))
        except anthropic.APIConnectionError as e:
            raise BackendError("AI 서버에 연결하지 못했어요. 인터넷 연결을 확인하고 다시 시도해 주세요.") from e
        except anthropic.RateLimitError as e:
            raise BackendError("요청이 많아 지금은 처리하지 못했어요. 잠시 뒤에 다시 시도해 주세요.") from e
        except anthropic.APIStatusError as e:
            raise BackendError(f"AI 요청이 실패했어요 (오류 코드 {e.status_code}). API 키 설정을 확인해 주세요.") from e
        except anthropic.AnthropicError as e:  # API 키가 없을 때 등
            raise BackendError(f"AI를 호출하지 못했어요: {e}") from e
        except Exception as e:  # MCP 도구 서버 연결이 끊긴 경우 등 — 다음 요청에서 새로 연결하도록 에이전트를 버린다
            self._agent = None  # (대화 기억도 함께 사라진다)
            raise BackendError(f"요청을 처리하지 못했어요 ({type(e).__name__}: {e}). 다시 시도해 주세요.") from e

    def resume(self, thread_id: str, chosen: list[list[str]] | None) -> dict:
        """확인 대기로 멈춘 그래프를 이어서 실행한다 (chosen: 작업마다 체크한 품목, None 이면 취소).

        Raises:
            NoPendingError: 확인을 기다리는 작업이 없을 때 (graph.RecipangAgent.resume 이 던짐).
            BackendError:   실행 실패 (MCP 도구 서버 연결이 끊긴 경우 등 — 같은 선택으로 다시 시도할 수 있다).
        """
        try:
            self.start()
            return asdict(self._run(self._agent.resume(thread_id, chosen)))
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(f"실행하지 못했어요 ({type(e).__name__}: {e}). 다시 눌러 주세요.") from e
