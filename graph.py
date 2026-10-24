"""
recipang — LangGraph 그래프: 요청 하나를 어떤 순서·조건으로 처리할지
=====================================================================
흐름 (노드 = 한 단계, 갈림길 = 조건부 엣지):

  START → route ─┬─(냉장고·장바구니)→ storage ─┬─(레시피도)→ recipe ─┐
                 ├─(레시피만)──────────────────┼────────────→ recipe ─┤
                 └─(둘 다 아님)────────────────┴──────────────────────┤
                                                                      ▼
                                                                   respond   ← 답변 정리 + 이번 턴을 turns 에 기록
                                                                      │
                                   (확인 대기 작업 있음) → approval ⏸ → execute → END
                                   (없음) ──────────────────────────────────────→ END

  노드        하는 일                                              실제 처리
  ─────────────────────────────────────────────────────────────────────────────────────
  route       요청 분류 + 지난 턴 작업 메모 초기화                  router.route_query()
  storage     냉장고·장바구니 조회 / 담기·빼기·옮기기 계획           Handlers.handle_storage_request()
  recipe      레시피 검색 + 설명 문장                               Handlers.handle_recipe_query()
  respond     답변 정리 (어느 분기도 아니면 라우터의 reply) + 기억  —
  approval    interrupt() 로 멈추고 사용자 확인 받기                —
  execute     확인한 품목만 MCP 도구로 실행 + 기억에 결과 반영        Handlers.execute_actions()

냉장고·장바구니를 레시피보다 먼저 하는 이유: 레시피 설명 문장이 "확인 대기 중인 담기"와
어긋나지 않도록 storage 결과를 recipe 노드에 함께 넘긴다.

LLM 호출·검색·MCP 도구는 handlers.Handlers 가 맡고, 그래프는 흐름 제어(분기, 확인 대기에서 멈췄다가 재개,
대화 기억 저장)만 맡는다. State 에는 dict·list·str 같은 기본 자료형만 넣는다 (체크포인터가 직렬화해 저장하므로).

공개 인터페이스:
  - build_graph(handlers, checkpointer)  : 그래프 조립 (studio.py 는 체크포인터 없이 부른다)
  - RecipangAgent                        : 그래프 + MCP 연결 + 체크포인터 — app.py 가 쓴다
  - GraphResult                          : 그래프를 한 번 실행한 결과 (화면이 말풍선 하나로 그린다)

생명주기:
  agent = RecipangAgent()
  await agent.start()                                    # 색인 준비 + MCP 서버 연결 (1회)
  r = await agent.handle_query("...", thread_id="t1")    # 요청마다
  if r.waiting:                                          # 담기·빼기·옮기기 확인 대기로 멈췄으면
      r = await agent.resume("t1", chosen=[["쌀"]])      #   체크한 품목으로 실행 (None 이면 취소)
  await agent.stop()

단독 실행: python graph.py   # 그래프 구조(Mermaid) 출력 후 터미널 대화 (확인 대기는 y/N)
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import recipe_db
from handlers import Handlers, RecipeAnswer
from memory import PENDING_WAITING, Turn, context_text, remember
from router import route_query  # router 를 불러올 때 .env(ANTHROPIC_API_KEY)도 함께 로드된다

# ── 핵심 설정값 ─────────────────────────────────────────────────────────
DEFAULT_PAGE_SIZE = 3       # 레시피 검색 결과의 첫 페이지 크기 (화면의 카드 수)
DEFAULT_THREAD = "default"  # thread_id 를 안 넘겼을 때 쓰는 대화방 (단독 실행)
HELP_MESSAGE = (
    "레시피 추천이나 장바구니·냉장고 정리를 도와드릴 수 있어요. "
    "예: '냉장고 재료로 만들 수 있는 매운 요리', '양념류는 다 냉장고에 넣어줘', '오징어볶음 재료 장바구니에 담아줘'."
)
# 확인 대기 작업을 실행하지 않았을 때 기억에 남기는 결과
PENDING_RESULTS = {"cancelled": "취소됨", "skipped": "확인하지 않고 다음 요청으로 넘어감 (실행 안 함)"}


# ═══ State ════════════════════════════════════════════════════════════

class InputState(TypedDict, total=False):
    """그래프 입력 — Studio 의 입력 칸에도 이 두 키만 보인다."""

    query: str       # 사용자 요청
    page_size: int   # 레시피 검색 결과의 첫 페이지 크기 (없으면 DEFAULT_PAGE_SIZE)


class State(InputState, total=False):
    """그래프 State. 노드는 바꿀 키만 dict 로 반환하고, LangGraph 가 State 에 합친다.

    키는 두 부류다.
      - 턴을 넘어 유지 : turns (단기 기억 — memory.remember reducer 로 합친다)
      - 이번 턴 작업 메모: 나머지 (덮어쓰기). 체크포인터에 함께 저장되므로 route 가 매 턴 비운다.
    주의: 노드 이름(route, storage, recipe ...)과 같은 키 이름은 쓰지 않는다 (그래서 decision, recipe_result).
    """

    turns: Annotated[list[Turn], remember]
    turn_id: int                # 이번 턴 번호 (확인 결과를 같은 턴에 반영할 때 쓴다)
    decision: dict              # RouteDecision 을 dict 로
    storage_lines: list[str]    # 바로 보여줄 냉장고·장바구니 문장 (조회 결과, 바꿀 품목이 없을 때의 안내, 되묻기)
    pending: list[dict]         # 확인 대기 작업 [{"action", "place", "items", "notes", "question"}, ...]
    recipe_result: dict | None  # 레시피 검색 결과 (recipe_payload() 형식). 검색하지 않았으면 None
    message: str                # 어느 분기에도 해당하지 않을 때의 답변 (라우터의 reply 또는 HELP_MESSAGE)
    pending_status: str         # "" / "waiting" / "approved" / "done" / "cancelled" / "skipped"
    chosen: list[list[str]]     # 확인한 품목 (작업마다)
    executed: list[str]         # 확인 후 실행 결과 문장


def recipe_payload(answer: RecipeAnswer) -> dict:
    """RecipeAnswer → State 에 넣을 dict (화면이 카드와 [더보기]를 그리는 데 필요한 것만)."""
    result = answer.result
    return {
        "text": answer.text,
        "conditions": answer.query.describe(),
        "unsupported": list(answer.query.unsupported),
        "owned": answer.owned,
        "ranked_ids": result.ranked_ids,
        "total": result.total,
        "shown": [rec.name for rec in result.recipes],  # 첫 페이지에 보여준 레시피 (기억의 '보여준 레시피')
        "suggestions": [
            {"ingredient": s.ingredient, "recipes": [rec.name for rec in recipe_db.describe(s.recipe_ids, answer.owned)]}
            for s in result.suggestions
        ],
    }


# ═══ 노드 ═════════════════════════════════════════════════════════════

class RecipangNodes:
    """그래프의 노드 6개. build_graph() 가 메서드를 노드로 등록한다.

    노드를 클래스 메서드로 둔 이유: 노드가 Handlers(MCP 연결·해석·검색)를 써야 하는데,
    이것은 State 에 넣을 값이 아니라 "그래프를 만들 때 정하는 설정"이다.

    Args:
        handlers: start() 로 MCP 연결을 열어 두면 재사용하고,
                  열지 않았으면(Studio) 도구가 필요할 때마다 MCP 서버를 띄웠다가 닫는다.
    """

    def __init__(self, handlers: Handlers) -> None:
        self.handlers = handlers

    async def route(self, state: State) -> dict:
        """[턴의 시작점] 요청을 분류하고 지난 턴의 작업 메모를 비운다 (체크포인터가 State 전체를 저장하므로)."""
        turns = state.get("turns", [])
        decision = await route_query(state["query"], memory_context=context_text(turns))
        return {
            "turn_id": turns[-1]["turn_id"] + 1 if turns else 1,
            "page_size": state.get("page_size") or DEFAULT_PAGE_SIZE,
            "decision": decision.model_dump(),
            "storage_lines": [], "pending": [], "recipe_result": None, "message": "",
            "pending_status": "", "chosen": [], "executed": [],
        }

    async def storage(self, state: State) -> dict:
        """냉장고·장바구니: 조회는 바로 문장으로, 담기·빼기·옮기기는 확인 대기 작업으로 (아무것도 바꾸지 않는다)."""
        lines, planned = await self.handlers.handle_storage_request(state["query"], context_text(state.get("turns", [])))
        return {"storage_lines": lines, "pending": [{**asdict(p), "question": p.question()} for p in planned]}

    async def recipe(self, state: State) -> dict:
        """레시피 검색: 해석 → SQL 거르기 → 벡터 정렬 → 설명 문장."""
        # 설명 문장이 냉장고·장바구니 상태와 어긋나지 않도록, 확인 대기 작업은 아직 실행 전이라고 함께 넘긴다
        context = state.get("storage_lines", []) + [
            f"(확인 대기 — 아직 실행 안 함) {p['question']} {', '.join(p['items'])}" for p in state.get("pending", [])
        ]
        answer = await self.handlers.handle_recipe_query(
            state["query"], state["page_size"], storage_lines=context,
            memory_context=context_text(state.get("turns", [])),
        )
        return {"recipe_result": recipe_payload(answer) if answer is not None else None}

    def respond(self, state: State) -> dict:
        """[합류 지점] 답변을 정리하고 이번 턴을 turns 에 기록한다 (확인 대기 작업은 '확인 대기'로)."""
        recipe = state.get("recipe_result")
        storage_lines, pending = state.get("storage_lines", []), state.get("pending", [])
        message = ""
        if not storage_lines and not pending and recipe is None:
            # 도구·검색을 쓰지 않은 요청: 라우터가 분류하면서 만든 답변 (인사, 범위 밖 질문 거절, 되묻기)
            message = state["decision"]["reply"] or HELP_MESSAGE

        turn: Turn = {"turn_id": state["turn_id"], "query": state["query"], "answer": message,
                      "storage": list(storage_lines)}
        if pending:
            turn["pending"] = [f"{p['question']} {', '.join(p['items'])}" for p in pending]
            turn["pending_result"] = PENDING_WAITING
        if recipe is not None:
            turn.update(answer=recipe["text"], conditions=recipe["conditions"],
                        recipes=recipe["shown"], total=recipe["total"])
        # 확인 대기 전에 기록해 두는 이유: 사용자가 확인하지 않고 다음 요청을 보내도 이번 턴이 기억에 남도록
        return {"message": message, "pending_status": "waiting" if pending else "", "turns": [turn]}

    def approval(self, state: State) -> dict:
        """[사용자 확인] 그래프를 멈추고 확인 대기 작업을 호출자(화면·Studio)에게 보여 준다.

        interrupt(payload):
          - 첫 실행 : 그래프를 멈추고 payload 를 호출자에게 전달 (체크포인트 저장)
          - 재개 시 : Command(resume=값) 으로 다시 부르면 이 노드가 처음부터 다시 실행되고, interrupt() 가 그 값을 반환한다
          → interrupt() 앞에는 부작용(DB 쓰기)을 두지 않는다. 실행은 execute 노드가 한다.

        재개 값:
          {"chosen": [[품목, ...], ...]}  작업마다 체크한 품목 (계획에 있던 품목만 받는다)
          true                            모두 실행 (Studio 에서 쓰기 편하게)
          {"skip": true}                  확인하지 않고 다음 요청으로 넘어감 (RecipangAgent.handle_query 가 보낸다)
          그 밖의 값 (false 등)            취소
        """
        pending = state["pending"]
        answer = interrupt({"question": "확인 후 실행할까요?", "actions": pending})
        if answer is True:
            return {"pending_status": "approved", "chosen": [p["items"] for p in pending]}
        if isinstance(answer, dict) and isinstance(answer.get("chosen"), list):
            picked = answer["chosen"] + [[]] * len(pending)  # 작업 수보다 짧게 와도 나머지는 '선택 없음'
            return {
                "pending_status": "approved",
                "chosen": [
                    [i for i in p["items"] if isinstance(picked[n], list) and i in picked[n]]
                    for n, p in enumerate(pending)
                ],
            }
        if isinstance(answer, dict) and answer.get("skip") is True:
            return {"pending_status": "skipped", "chosen": []}
        return {"pending_status": "cancelled", "chosen": []}

    async def execute(self, state: State) -> dict:
        """확인한 품목만 MCP 도구로 실행하고 결과를 이번 턴 기억에 반영한다. 취소·넘어감이면 아무것도 바꾸지 않는다."""
        status, executed = state["pending_status"], []
        if status == "approved":
            executed = await self.handlers.execute_actions(
                [(p["action"], p["place"], items) for p, items in zip(state["pending"], state["chosen"])]
            )
            status, result = "done", "실행됨: " + " / ".join(executed)
        else:
            result = PENDING_RESULTS[status]
        update = {"pending_status": status, "executed": executed}
        turn = next((t for t in state.get("turns", []) if t["turn_id"] == state["turn_id"]), None)
        if turn is not None:  # 같은 turn_id 로 보내면 remember 가 그 턴을 바꾼다
            update["turns"] = [{**turn, "pending_result": result}]
        return update


# ═══ 그래프 조립 ══════════════════════════════════════════════════════

def after_route(state: State) -> str:
    """냉장고·장바구니가 필요하면 storage 부터 (레시피 설명이 그 결과를 알아야 하므로), 아니면 recipe / respond."""
    d = state["decision"]
    if d["needs_fridge"] or d["needs_cart"]:
        return "storage"
    return "recipe" if d["needs_recipe"] else "respond"


def after_storage(state: State) -> str:
    return "recipe" if state["decision"]["needs_recipe"] else "respond"


def after_respond(state: State) -> str:
    """확인 대기 작업이 있으면 approval 에서 멈추고, 없으면 끝."""
    return "approval" if state.get("pending") else END


def build_graph(handlers: Handlers, *, checkpointer=None):
    """그래프를 조립해 컴파일한다.

    Args:
        handlers:     노드가 해석·검색·MCP 도구 호출에 쓰는 Handlers.
        checkpointer: thread_id 단위로 State 를 저장 → 대화 기억과 interrupt 재개에 필요.
                      langgraph dev(Studio)는 서버가 체크포인터를 붙이므로 None 으로 둔다.
    """
    nodes = RecipangNodes(handlers)
    g = StateGraph(State, input_schema=InputState)
    for name in ("route", "storage", "recipe", "respond", "approval", "execute"):
        g.add_node(name, getattr(nodes, name))
    g.add_edge(START, "route")
    # 세 번째 인자(path_map) = 갈 수 있는 목적지 — 그래프 그림과 검증에 쓰인다
    g.add_conditional_edges("route", after_route, ["storage", "recipe", "respond"])
    g.add_conditional_edges("storage", after_storage, ["recipe", "respond"])
    g.add_edge("recipe", "respond")
    g.add_conditional_edges("respond", after_respond, ["approval", END])
    g.add_edge("approval", "execute")
    g.add_edge("execute", END)
    return g.compile(checkpointer=checkpointer)


# ═══ 실행 래퍼 ═══════════════════════════════════════════════════════

@dataclass
class GraphResult:
    """그래프를 한 번 실행한 결과 (handle_query 또는 resume).

    Attributes:
        decision:       라우터 판단 (needs_recipe, needs_fridge, needs_cart, reasoning, reply).
        storage_lines:  바로 보여줄 냉장고·장바구니 문장.
        pending:        확인 대기 작업 [{"action", "place", "items", "notes", "question"}, ...].
        recipe:         레시피 검색 결과 (recipe_payload() 형식). 검색하지 않았으면 None.
        message:        어느 분기에도 해당하지 않을 때의 답변.
        waiting:        확인 대기로 멈춰 있으면 True → 호출 측이 resume() 으로 이어 간다.
        pending_status: "" / "waiting" / "done" / "cancelled" / "skipped".
        executed:       확인 후 실행 결과 문장 (resume 결과일 때).
    """

    decision: dict
    storage_lines: list[str] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    recipe: dict | None = None
    message: str = ""
    waiting: bool = False
    pending_status: str = ""
    executed: list[str] = field(default_factory=list)

    @classmethod
    def from_state(cls, state: dict, waiting: bool) -> GraphResult:
        return cls(
            decision=state["decision"],
            storage_lines=state.get("storage_lines", []),
            pending=state.get("pending", []),
            recipe=state.get("recipe_result"),
            message=state.get("message", ""),
            waiting=waiting,
            pending_status=state.get("pending_status", ""),
            executed=state.get("executed", []),
        )


class RecipangAgent:
    """그래프 + MCP 연결(Handlers) + 체크포인터를 묶어, 요청 하나·확인 하나 단위로 부를 수 있게 한 객체.

    핵심 변수:
        handlers: Handlers — MCP 도구 서버 연결과 해석·검색 함수.
        graph:    컴파일된 그래프. State 는 체크포인터에 thread_id 별로 저장되므로 여러 대화방이 같이 써도 된다.
    """

    def __init__(self, checkpointer=None) -> None:
        """
        Args:
            checkpointer: 대화 기억 저장소. 생략하면 InMemorySaver (프로세스가 끝나면 사라짐).
        """
        self.handlers = Handlers()
        self.graph = build_graph(self.handlers, checkpointer=checkpointer or InMemorySaver())

    async def start(self) -> None:
        """검색 색인을 준비하고 MCP 도구 서버 연결을 연다 (1회)."""
        await self.handlers.start()

    async def stop(self) -> None:
        await self.handlers.stop()

    async def handle_query(self, query: str, thread_id: str = DEFAULT_THREAD, page_size: int = DEFAULT_PAGE_SIZE) -> GraphResult:
        """요청 하나를 처리한다. 담기·빼기·옮기기가 있으면 확인 대기로 멈추고 result.waiting 이 True.

        이 대화방에 아직 확인하지 않은 작업이 있으면, 그 작업은 실행하지 않고 '넘어감'으로 닫은 뒤(기억에 남김)
        새 요청을 처리한다. (그냥 새 입력을 주면 LangGraph 가 이전 확인 대기를 기록 없이 버린다.)
        """
        if await self.pending(thread_id):
            await self._run(Command(resume={"skip": True}), thread_id)
        return await self._run({"query": query, "page_size": page_size}, thread_id)

    async def resume(self, thread_id: str, chosen: list[list[str]] | None) -> GraphResult:
        """확인 대기로 멈춘 턴을 이어서 실행한다 (approval 노드부터).

        Args:
            chosen: 작업마다 실행할 품목 (화면에서 체크한 것). None 이면 취소.

        Raises:
            RuntimeError: 확인을 기다리는 작업이 없을 때 (이미 처리했거나, 앱을 다시 시작해 기억이 사라진 경우).
        """
        snapshot = await self.graph.aget_state(_config(thread_id))
        if snapshot.interrupts:
            return await self._run(Command(resume={"chosen": chosen} if chosen is not None else False), thread_id)
        if "execute" in snapshot.next:  # 지난번 실행이 오류로 멈췄으면 같은 선택으로 다시 실행
            return await self._run(None, thread_id)
        raise RuntimeError("확인을 기다리는 작업이 없어요.")

    async def pending(self, thread_id: str) -> dict | None:
        """이 대화방이 확인 대기로 멈춰 있으면 interrupt payload, 아니면 None (그래프를 실행하지 않는다)."""
        snapshot = await self.graph.aget_state(_config(thread_id))
        return snapshot.interrupts[0].value if snapshot.interrupts else None

    async def turns(self, thread_id: str = DEFAULT_THREAD) -> list[Turn]:
        """이 대화방의 단기 기억 (최근 memory.WINDOW 턴)."""
        snapshot = await self.graph.aget_state(_config(thread_id))
        return snapshot.values.get("turns", []) if snapshot.values else []

    async def _run(self, payload, thread_id: str) -> GraphResult:
        """그래프를 한 번 실행한다. version="v2" → 결과의 .value = State, .interrupts = 멈춘 이유."""
        out = await self.graph.ainvoke(payload, config=_config(thread_id), version="v2")
        return GraphResult.from_state(out.value, waiting=bool(out.interrupts))


def _config(thread_id: str) -> dict:
    """체크포인터가 State 를 저장·복원하는 키 (= 대화방 ID)."""
    return {"configurable": {"thread_id": thread_id}}


def _print_result(r: GraphResult) -> None:
    """터미널 출력 (단독 실행용)."""
    d = r.decision
    print(f"\n[라우터] recipe={d['needs_recipe']}, fridge={d['needs_fridge']}, cart={d['needs_cart']} ({d['reasoning']})")
    for line in r.storage_lines:
        print(f"[안내] {line}")
    for p in r.pending:
        print(f"[확인 대기] {p['question']} {', '.join(p['items'])}")
        for note in p["notes"]:
            print(f"            ({note})")
    if r.recipe:
        print(f"[해석] {r.recipe['conditions']}")
        if r.recipe["unsupported"]:
            print(f"[반영 못한 조건] {', '.join(r.recipe['unsupported'])}")
        print(f"[검색] {r.recipe['total']}개: {r.recipe['shown']}")
        print(f"\nA: {r.recipe['text']}")
    if r.message:
        print(f"\nA: {r.message}")


async def main() -> None:
    """단독 실행: 그래프 구조(Mermaid)를 출력하고 터미널에서 질문을 반복해서 받는다 (최근 memory.WINDOW 턴 기억)."""
    agent = RecipangAgent()
    await agent.start()
    print(agent.graph.get_graph().draw_mermaid())  # mermaid.live 에 붙여 넣으면 그림으로 보인다
    try:
        while True:
            # input() 은 기다리는 동안 이벤트 루프를 멈추므로 별도 스레드에서 실행해 MCP 연결을 유지한다
            question = (await asyncio.to_thread(input, "\nQ (엔터만 누르면 종료): ")).strip()
            if not question:
                break
            result = await agent.handle_query(question)
            _print_result(result)
            if result.waiting:
                ok = (await asyncio.to_thread(input, "모두 실행할까요? (y/N) ")).strip().lower() == "y"
                result = await agent.resume(DEFAULT_THREAD, [p["items"] for p in result.pending] if ok else None)
                for line in result.executed:
                    print(f"[실행] {line}")
                if result.pending_status == "cancelled":
                    print("[취소] 아무것도 바꾸지 않았어요.")
    finally:
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(main())
