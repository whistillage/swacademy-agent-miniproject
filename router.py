"""
recipang — 라우터: 레시피 검색이 필요한가, 냉장고·장바구니 도구가 필요한가
==========================================================================
질문 하나를 받아 "레시피 검색이 필요한가", "나의 냉장고를 다루는가", "장바구니를 다루는가"를
구조화된 출력으로 분류하는 작고 빠른 LLM 호출.

전체 흐름:
  질문(+이전 대화 맥락) ──▶ route_query()
                              │  ① 프롬프트 구성
                              │  ② Claude(Haiku) 호출 — 구조화 출력(RouteDecision 스키마)
                              ▼
                        RouteDecision(needs_recipe, needs_fridge, needs_cart, reasoning, reply)
                              │
                              ▼
                  graph.py 의 route 노드가 이 값을 보고 다음 노드(storage / recipe / respond)를 결정

이 모듈의 공개 인터페이스:
  - RouteDecision  : 라우팅 결과 (Pydantic 모델 — 구조화 출력 스키마이기도 하다)
  - route_query()  : 질문을 분류하는 비동기 함수 (graph.py 가 import 해서 사용)
"""

from __future__ import annotations

import asyncio
import os

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field


# .env 의 ANTHROPIC_API_KEY 를 환경변수로 로드 (실행 위치와 무관하게 스크립트 폴더 기준)
# - AsyncAnthropic() 은 인자 없이 생성하면 환경변수 ANTHROPIC_API_KEY 를 자동으로 읽는다.
# - graph.py 가 이 모듈을 불러오므로 handlers.py 의 LLM 호출도 이 키를 쓴다 (여기서 한 번만 로드하면 된다).
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


# ── 핵심 설정값 ─────────────────────────────────────────────────────────
ROUTER_MODEL = "claude-haiku-5-5"  # 분류만 하므로 가볍고 빠른 모델로 충분
EFFORT = "low"                     # 짧은 분류라 생각을 적게 해서 빠르게 답하도록

ROUTER_SYSTEM_PROMPT = """\
당신은 장보기 앱 recipang 의 요청을 분류하는 라우터입니다. 사용자 질문(및 이전 대화 맥락)을 보고
무엇이 필요한지 판단하세요. 여러 개가 동시에 필요할 수 있습니다.

판단 기준:
- needs_recipe: 레시피를 추천받거나 찾고 싶어 함 (예: "매운 볶음 요리 추천해줘", "냉장고 재료로 뭐 만들 수 있어?")
- needs_fridge: 나의 냉장고에 담기 / 빼기 / 옮기기 / 조회를 원함 (예: "양념류는 다 냉장고에 넣어줘", "냉장고에 뭐 있어?")
- needs_cart:   장바구니에 담기 / 빼기 / 옮기기 / 조회를 원함 (예: "오징어볶음에 필요한 재료 장바구니에 다 넣어줘")
- 장바구니와 냉장고 사이에서 옮기는 요청은 needs_fridge 와 needs_cart 가 모두 true 입니다.
- "냉장고 재료로 추천해줘", "냉장고 재료로 뭐 만들 수 있어?"처럼 내 재료를 근거로 레시피를 찾는 것은
  needs_recipe 만 true 입니다. 냉장고를 바꾸거나 목록을 보여달라는 게 아니므로 needs_fridge 는 false 입니다.
- "OO에 필요한 재료를 담아줘", "OO 재료 장바구니에 넣어줘"는 레시피 추천이 아니라 담기 요청입니다.
  레시피 이름이 나와도 추천·검색을 원하지 않으므로 needs_recipe 는 false 입니다.
- 인사처럼 어느 것도 아니면 모두 false 입니다.
- "그거 말고 덜 매운 걸로", "2번 레시피 재료 담아줘", "방금 담은 거 빼줘"처럼 이전 대화를 가리키는 질문은
  [이전 대화 맥락]에서 가리키는 대상을 찾아 판단하세요. 이전 검색 조건을 바꿔 다시 찾는 것은 needs_recipe 입니다.
- [이전 대화 맥락]이 없는데 "그거 담아줘", "방금 거 빼줘", "2번 알려줘"처럼 앞 대화를 가리키는 말로만 대상을 말하면
  가리킬 대상이 없으므로 모두 false 입니다.

reply — 세 값이 모두 false 일 때만 사용자에게 보낼 답변을 쓰고, 하나라도 true 면 빈 문자열로 두세요:
- 인사·감사 → 짧게 인사하고 할 수 있는 일을 소개하세요.
- 할 수 있는 일과 관계없는 질문(날씨 등)이나, 요리와 관련됐지만 레시피 검색으로 답할 수 없는 질문(칼로리, 영양,
  보관법, 요리 상식 등) → 그 정보는 드릴 수 없다고 정중히 말하고 할 수 있는 일을 안내하세요.
  칼로리 수치나 보관 기간 같은 답을 직접 쓰지 마세요.
- 무슨 뜻인지 알 수 없는 입력 → 원하는 것을 다시 말해 달라고 하고 요청 예시를 하나 들어 주세요.
- 가리킬 대상이 없는 "그거", "방금 거" → 무엇을 말하는지 구체적으로 알려 달라고 되물으세요.
- 2문장 이내, 존댓말, 제목·목록 같은 마크다운 없이 쓰세요.
- 할 수 있는 일: 레시피 추천(요리 종류·나라·조리법·맛·상황 조건, 장바구니·냉장고 재료로 만들 수 있는 요리),
  장바구니·나의 냉장고에 식자재 담기 / 빼기 / 옮기기 / 조회.
"""


class RouteDecision(BaseModel):
    """라우터의 판단 결과.

    Attributes:
        needs_recipe: True 이면 그래프가 레시피 검색 노드(recipe)를 실행한다.
        needs_fridge: True 이면 나의 냉장고를 다루는 도구 호출이 필요하다.
        needs_cart:   True 이면 장바구니를 다루는 도구 호출이 필요하다.
        reasoning:    LLM이 그렇게 판단한 한 줄 이유 (디버깅/로그 출력용).
        reply:        세 값이 모두 false 일 때 사용자에게 바로 보낼 답변 (인사, 범위 밖 질문 거절, 되묻기).
                      분류하면서 함께 만들어서, 도구·검색이 필요 없는 요청에 LLM 을 한 번 더 부르지 않는다.
    """

    needs_recipe: bool = Field(description="레시피 추천·검색이 필요하면 true")
    needs_fridge: bool = Field(description="나의 냉장고 담기/빼기/옮기기/조회가 필요하면 true")
    needs_cart: bool = Field(description="장바구니 담기/빼기/옮기기/조회가 필요하면 true")
    reasoning: str = Field(description="판단 이유 한 줄")
    reply: str = Field(description="세 값이 모두 false 일 때만 사용자에게 보낼 답변, 아니면 빈 문자열")


async def route_query(query: str, memory_context: str = "") -> RouteDecision:
    """질문을 분류해 RouteDecision을 반환한다.

    Args:
        query:          이번 턴의 사용자 질문.
        memory_context: 단기 메모리가 만든 이전 대화 요약. 비어 있으면 질문만 보낸다.

    흐름:
        ① 맥락 유무에 따라 user 메시지 구성
        ② 라우팅 전용 LLM 호출 — 구조화 출력이라 응답이 항상 RouteDecision 스키마를 따른다
        ③ 모델이 답을 거절하면 레시피 검색으로 폴백 (검색은 부작용이 없어 안전하다)
    """
    # ① 이전 맥락이 있으면 [이전 대화 맥락] / [이번 질문] 으로 구분해 함께 전달
    user_content = query if not memory_context else f"[이전 대화 맥락]\n{memory_context}\n\n[이번 질문]\n{query}"

    # ② 라우팅 전용 LLM 호출 — 답변 생성 호출과 분리된 "작고 빠른" 호출
    client = AsyncAnthropic()  # API 키는 환경변수 ANTHROPIC_API_KEY 에서 자동으로 읽음
    response = await client.messages.parse(
        model=ROUTER_MODEL,
        max_tokens=2000,
        system=ROUTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
        output_format=RouteDecision,
        output_config={"effort": EFFORT},
    )

    # ③ 거절 시 폴백
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return RouteDecision(
            needs_recipe=True, needs_fridge=False, needs_cart=False, reasoning="분류 실패 — 레시피 검색으로 폴백", reply=""
        )
    return response.parsed_output


async def main() -> None:
    """단독 실행 데모: 유형별 질문이 각각 어떻게 분류되는지 확인한다."""
    questions = [
        "매운 볶음 요리 추천해줘",                          # 기대: 레시피
        "냉장고 재료로 뭐 만들 수 있어?",                    # 기대: 레시피
        "양념류는 다 냉장고에 넣어줘",                       # 기대: 냉장고
        "오징어볶음에 필요한 재료 장바구니에 다 넣어줘",     # 기대: 장바구니
        "장바구니에 있는 거 다 샀으니까 냉장고로 옮겨줘",    # 기대: 냉장고 + 장바구니
        "고추장 장바구니에 담고 매운 요리 추천해줘",         # 기대: 장바구니 + 레시피
        "안녕!",                                             # 기대: 모두 false + 인사 답변
        "김치찌개 칼로리 얼마야?",                           # 기대: 모두 false + 정중히 거절
    ]

    for q in questions:
        decision = await route_query(q)
        print(f"Q: {q}")
        print(f"  recipe={decision.needs_recipe}, fridge={decision.needs_fridge}, cart={decision.needs_cart}")
        print(f"  reasoning: {decision.reasoning}")
        if decision.reply:
            print(f"  reply: {decision.reply}")
        print()


if __name__ == "__main__":
    # route_query 가 async 함수이므로 이벤트 루프를 띄워 실행한다.
    asyncio.run(main())
