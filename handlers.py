"""
recipang — 요청 처리: 그래프 노드가 부르는 냉장고·장바구니 / 레시피 검색 처리 함수
====================================================================================
흐름(어떤 순서로 무엇을 부를지)은 graph.py 의 LangGraph 그래프가 정하고, 이 모듈은 각 단계에서 실제로 할 일을 맡는다.

냉장고·장바구니 (handle_storage_request → 사용자 확인 → execute_actions):
  ① _parse_storage_request()  LLM: 요청 → StorageRequest (작업 목록, 구조화 출력)
  ② _plan_storage_action()    대상(식자재 / 대분류 / 레시피 재료 / 전부)을 실제 품목으로 풀고, 지금 상태 기준으로
                              실제로 바뀌는 품목만 남긴다. 조회는 바로 결과 문장, 담기·빼기·옮기기는 확인 대기(PlannedAction)
  ③ 그래프가 확인 단계(interrupt)에서 멈추고, 사용자가 확인하면 execute_actions() 가 체크된 품목만
     MCP 도구(mcp_tool_server.py)로 실행

레시피 검색 (handle_recipe_query):
  ① _parse_recipe_query()  LLM: 요청 → RecipeQuery (속성 조건 + 나머지 표현, 구조화 출력)
  ② rag_module.retrieve()   SQL 로 거르고 → 벡터로 정렬 (Q1 인데 결과가 없으면 담을 재료 추천)
  ③ _generate_answer()      LLM: 검색 결과로 짧은 설명 문장

이전 대화 맥락:
  memory_context(memory.context_text() 로 만든 이전 대화 요약)를 해석·답변 생성 프롬프트에
  [이전 대화 맥락]으로 넣는다 (day4 방식). "그거 말고 덜 매운 걸로", "2번 레시피 재료 담아줘",
  "방금 담은 거 빼줘" 같은 말은 LLM 이 맥락에서 풀어 해석한다.

예외 처리 (LLM 을 더 부르지 않고, 각 단계의 구조화 출력에 필드를 하나씩 둔다):
  - 도구·검색이 필요 없는 요청 (인사, 범위 밖 질문, 의미 없는 입력, 맥락 없이 "그거")
      → 라우터가 분류하면서 만든 reply 로 답한다 (인사 / 정중히 거절 + 할 수 있는 일 / 되묻기)
  - 카탈로그에 없는 품목 ("노트북", "망고")   → ItemTarget.unknown → "카탈로그에 없는 품목" 안내
  - 가리키는 대상이 불분명함 ("그 레시피 재료") → StorageRequest.clarification → 실행하지 않고 되묻기
  - 검색이 반영할 수 없는 조건 ("우유로 만드는", "10분 안에") → RecipeQuery.unsupported
      → 다른 조건이 있으면 그것으로 검색하고 반영 못 한 조건을 알린다, 없으면 검색하지 않고 UNSUPPORTED_MESSAGE

사용하는 모듈:
  - rag_module.py     : retrieve()             — 레시피 검색 (SQL 거르기 → 벡터 정렬)
  - recipe_db.py      : find_recipe()          — 레시피 이름 → 재료 (담기 대상 풀기)
  - storage.py        : 장바구니 / 나의 냉장고 — 레시피 검색의 내 재료 (읽기만)
  - mcp_tool_server.py: list/add/remove/move_items — MCP(stdio)로 호출하는 장바구니·냉장고 도구
  - Anthropic API     : 해석 · 답변 생성 (Claude Haiku 5.5). API 키는 router.py 가 불러올 때 .env 에서 로드된다
                        (graph.py 가 router.py 를 함께 불러온다).

생명주기:
  handlers = Handlers()
  await handlers.start()   # MCP 연결을 열어 두고 여러 요청에 재사용 (열지 않으면 도구가 필요할 때마다 띄웠다가 닫는다)
  ...                      # 그래프 노드가 handle_storage_request / handle_recipe_query / execute_actions 를 부른다
  await handlers.stop()
"""

from __future__ import annotations

import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from anthropic import AsyncAnthropic
from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport
from pydantic import BaseModel, Field

import catalog
import rag_module
import recipe_db
import storage


def _with_context(query: str, memory_context: str) -> str:
    """LLM 에 보낼 user 메시지: 이전 대화 맥락이 있으면 [이전 대화 맥락] / [이번 요청] 으로 나눠 함께 보낸다."""
    return query if not memory_context else f"[이전 대화 맥락]\n{memory_context}\n\n[이번 요청]\n{query}"


# ── 핵심 설정값 ─────────────────────────────────────────────────────────
MCP_SERVER_SCRIPT = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_tool_server.py"))
PARSE_MODEL = "claude-haiku-5-5"   # 요청 해석 — 빠르고 저렴한 최신 Haiku
ANSWER_MODEL = "claude-haiku-5-5"  # 답변 생성
EFFORT = "low"                     # 짧은 해석·안내 문장이라 생각을 적게 해서 빠르게 답하도록

ANSWER_TOP_N = 3  # 답변 문장에 넘길 상위 레시피 수 (화면의 첫 카드 수와 맞춘다)

# 카탈로그 값 목록 — 냉장고·장바구니 해석 스키마가 이 값만 내도록 제한한다
ALL_INGREDIENTS = tuple(i for c in catalog.list_categories() for i in catalog.list_ingredients(c))
CATEGORIES = tuple(catalog.list_categories())


# ═══ 레시피 검색 분기 ═══════════════════════════════════════════════════════

class RecipeQuery(BaseModel):
    """레시피 요청 해석 결과 — _parse_recipe_query() 의 구조화 출력 스키마.

    속성 값은 recipe_db 의 값 목록으로 제한되므로 LLM 이 목록에 없는 값을 낼 수 없다.
    """

    use_owned: bool = Field(description="내 재료(장바구니+냉장고)로 만들 수 있는 레시피만 원하면 true")
    dish_types: list[Literal[recipe_db.DISH_TYPES]] = Field(description="요리 종류 (하나라도 일치)")
    cuisines: list[Literal[recipe_db.CUISINES]] = Field(description="나라 (하나라도 일치)")
    cooking_methods: list[Literal[recipe_db.COOKING_METHODS]] = Field(description="조리법 (하나라도 일치)")
    tastes: list[Literal[recipe_db.TASTES]] = Field(description="반드시 있어야 하는 맛 태그 (모두 포함)")
    exclude_tastes: list[Literal[recipe_db.TASTES]] = Field(description="없어야 하는 맛 태그")
    tags: list[Literal[recipe_db.TAGS]] = Field(description="상황 태그 (모두 포함)")
    min_intensity: Optional[Literal[0, 1, 2, 3]] = Field(description="강도 하한 (0 담백 ~ 3 자극적), 없으면 null")
    max_intensity: Optional[Literal[0, 1, 2, 3]] = Field(description="강도 상한 (0 담백 ~ 3 자극적), 없으면 null")
    text: str = Field(description="속성으로 표현되지 않는 나머지 표현. 없으면 빈 문자열")
    unsupported: list[str] = Field(
        description="검색이 반영할 수 없는 조건 (특정 재료 포함·제외, 조리 시간, 칼로리, 인분 등). 없으면 빈 목록"
    )

    def is_empty(self) -> bool:
        """검색에 반영할 조건이 하나도 없는지 (내 재료 기준도, 속성 조건도, 나머지 표현도 없음).

        unsupported 는 검색에 반영되지 않으므로 세지 않는다.
        """
        return self.describe() == "조건 없음"

    @classmethod
    def text_only(cls, query: str) -> RecipeQuery:
        """조건 없이 요청 문장 전체를 의미 검색에 넘기는 해석 (해석에 실패했을 때 쓴다)."""
        return cls(
            use_owned=False, dish_types=[], cuisines=[], cooking_methods=[], tastes=[], exclude_tastes=[],
            tags=[], min_intensity=None, max_intensity=None, text=query, unsupported=[],
        )

    def to_filter(self) -> recipe_db.RecipeFilter:
        """SQL 검색 조건(RecipeFilter)으로 바꾼다."""
        return recipe_db.RecipeFilter(
            dish_types=list(self.dish_types),
            cuisines=list(self.cuisines),
            cooking_methods=list(self.cooking_methods),
            tastes=list(self.tastes),
            exclude_tastes=list(self.exclude_tastes),
            tags=list(self.tags),
            min_intensity=self.min_intensity,
            max_intensity=self.max_intensity,
        )

    def describe(self) -> str:
        """해석한 조건을 한 줄로 만든다 (화면 표시·답변 프롬프트용).

        예) "내 재료로 만들 수 있는 것 · 매콤 · 국물 · '비 오는 날'"
        """
        parts = ["내 재료로 만들 수 있는 것"] if self.use_owned else []
        parts += [*self.cuisines, *self.dish_types, *self.cooking_methods, *self.tastes]
        parts += [f"{t} 제외" for t in self.exclude_tastes]
        parts += self.tags
        if self.min_intensity is not None:
            parts.append(f"강도 {self.min_intensity} 이상")
        if self.max_intensity is not None:
            parts.append(f"강도 {self.max_intensity} 이하")
        if self.text:
            parts.append(f"'{self.text}'")
        return " · ".join(parts) or "조건 없음"


PARSE_SYSTEM_PROMPT = f"""\
당신은 장보기 앱의 레시피 검색 요청을 검색 조건으로 바꾸는 해석기입니다.
사용자 요청에서 아래 속성 값으로 확실히 표현되는 조건만 채우세요. 요청에 없는 조건을 추측해서 넣지 마세요.
요청에 장바구니·냉장고에 담기/빼기 같은 부분이 섞여 있으면 그 부분은 무시하세요 (다른 단계가 처리합니다).

속성 값 목록:
- dish_types (요리 종류): {", ".join(recipe_db.DISH_TYPES)}
- cuisines (나라): {", ".join(recipe_db.CUISINES)}
- cooking_methods (조리법): {", ".join(recipe_db.COOKING_METHODS)}
- tastes / exclude_tastes (맛): {", ".join(recipe_db.TASTES)}
- tags (상황): {", ".join(recipe_db.TAGS)}
- min_intensity / max_intensity (강도): 0 담백 ~ 3 자극적 (맵기·짠맛·기름짐 종합)

규칙:
- use_owned: "냉장고", "있는 재료로", "집에 있는 걸로", "지금 만들 수 있는"처럼 내 재료로 만들 수 있는 것을 원하면 true.
- "볶음류", "튀김 요리"처럼 조리 방식을 말하면 cooking_methods 로, "찌개", "죽", "면 요리"처럼 요리 형태를 말하면 dish_types 로.
- "국물 요리"는 tags 의 국물로 (찌개와 국·탕을 모두 포함하도록).
- 매운 / 얼큰한 / 칼칼한 → tastes 매콤. 안 매운 / 맵지 않은 → exclude_tastes 매콤.
- 덜 자극적인 / 순한 / 자극적이지 않은 → max_intensity 1. 자극적인 → min_intensity 2. 아주 자극적인 → min_intensity 3.
- text 에는 위 속성으로 표현되지 않는 나머지 표현만 짧게 넣으세요 (예: "비 오는 날", "든든한").
  "요리", "레시피", "추천해줘", "만들 수 있는"처럼 검색에 의미 없는 말은 넣지 말고, 남는 표현이 없으면 빈 문자열.
- [이전 대화 맥락]이 주어지고 이번 요청이 "그거 말고 덜 매운 걸로", "이번엔 중식으로"처럼 이전 검색을 고치는 말이면,
  맥락의 직전 '해석한 조건'에서 바꾸라는 부분만 바꾼 전체 조건을 채우세요 (바꾸지 않은 조건은 그대로 유지).
- unsupported 에는 검색이 반영할 수 없는 조건을 사용자가 말한 표현 그대로 짧게 넣으세요:
  특정 재료가 들어가거나 빠지는 조건("우유로 만드는", "돼지고기 없이"), 조리 시간("10분 안에"), 칼로리·영양,
  인분, 난이도, 가격. 이런 조건은 비슷한 속성으로 바꾸지 말고, text 에도 넣지 마세요.

예시:
- "냉장고 재료로 매운 볶음 요리" → use_owned true, tastes [매콤], cooking_methods [볶기], text ""
- "비 오는 날 먹을 얼큰한 국물 요리" → tastes [매콤], tags [국물], text "비 오는 날"
- "덜 자극적인 중식 추천해줘" → cuisines [중식], max_intensity 1, text ""
- "든든한 아침" → tags [아침], text "든든한"
- "우유로 만들 수 있는 요리" → unsupported ["우유로 만드는"], 나머지는 모두 비움
- "10분 안에 만드는 매운 요리" → tastes [매콤], unsupported ["10분 안에"]
"""

# 검색에 반영할 조건이 하나도 없고 unsupported 만 있을 때의 안내 (엉뚱한 검색 결과를 보여주지 않는다)
UNSUPPORTED_MESSAGE = (
    "{conditions} 조건으로는 아직 레시피를 찾을 수 없어요. "
    "요리 종류(찌개·볶음 등), 나라, 맛(매콤·담백 등), 상황(국물·술안주 등)이나 '냉장고 재료로'처럼 말씀해 주시면 찾아 드릴게요."
)

ANSWER_SYSTEM_PROMPT = """\
당신은 장보기 앱 recipang 의 레시피 추천 도우미입니다. 사용자 요청과 레시피 검색 결과를 보고 짧은 안내 문장을 씁니다.

규칙:
1. [검색 결과]와 [담으면 좋은 재료]에 있는 레시피·재료만 언급하세요. 지어내지 마세요.
2. 레시피 카드(재료, 만드는 법)는 화면에 따로 보이므로 재료 목록이나 조리 과정을 나열하지 마세요.
3. 2~3문장, 존댓말로 쓰고, 제목·목록·표 같은 마크다운은 쓰지 마세요.
4. 결과가 0개면 조건에 맞는 레시피가 없다고 알려주세요. [담으면 좋은 재료]가 있으면 그 재료를 담으면 만들 수 있는
   레시피를 안내하고, 없으면 조건을 조금 바꿔 다시 물어봐 달라고 하세요.
5. 더 필요한 재료가 있는 레시피는 몇 가지가 더 필요한지 자연스럽게 언급해도 좋습니다.
6. 사용자 요청에 장바구니·냉장고에 담기/빼기 같은 부분이 섞여 있으면, 그 상태는 [장바구니·냉장고 처리 결과]에
   있습니다. '확인 대기'인 작업은 사용자가 아직 확인하지 않아 실행되지 않았으니 담았다/뺐다고 말하지 마세요.
   그 결과와 어긋나는 말을 하지 말고, 처리 결과는 화면에 따로 보이므로 다시 설명할 필요는 없습니다.
7. [이전 대화 맥락]이 있으면 이어지는 대화로 자연스럽게 답하세요 (예: "덜 매운 것으로 다시 찾아봤어요").
   단, 이번 [검색 결과]에 없는 레시피를 추천하지는 마세요.
8. [반영하지 못한 조건]이 있으면 그 조건은 검색에 반영하지 못했다고 먼저 짧게 알리고,
   검색 결과가 그 조건을 만족한다고 말하지 마세요.
"""


@dataclass
class RecipeAnswer:
    """레시피 검색 분기의 결과 (화면이 말풍선과 카드로 그린다).

    Attributes:
        text:   LLM 이 만든 설명 문장 (반영할 조건 없이 unsupported 만 있으면 UNSUPPORTED_MESSAGE).
        query:  해석한 조건.
        result: 검색 결과 (ranked_ids 로 '더보기'를 이어서 보여준다). 검색하지 않았으면 빈 결과.
        owned:  검색 기준이 된 내 재료 (장바구니 + 나의 냉장고).
    """

    text: str
    query: RecipeQuery
    result: rag_module.SearchResult
    owned: list[str]


# ═══ 냉장고·장바구니 분기 ═══════════════════════════════════════════════════

class ItemTarget(BaseModel):
    """작업 대상 — 아래 항목을 모두 합친 품목이 대상이 된다."""

    ingredients: list[Literal[ALL_INGREDIENTS]] = Field(description="이름을 직접 말한 식자재 (카탈로그 이름)")
    categories: list[Literal[CATEGORIES]] = Field(description="대분류 전체를 말한 경우 (예: 양념류 → 양념·오일)")
    recipe_name: str = Field(description="'OO에 필요한 재료'처럼 레시피 재료를 말하면 요리 이름, 없으면 빈 문자열")
    everything: bool = Field(description="'장바구니에 있는 거 다'처럼 그곳에 있는 것 전부면 true")
    unknown: list[str] = Field(description="카탈로그에 없어서 ingredients 에 넣지 못한 품목 (말한 그대로)")


class StorageAction(BaseModel):
    """작업 하나."""

    action: Literal["add", "remove", "move", "list"] = Field(
        description="add 담기 / remove 빼기 / move 반대쪽으로 옮기기 / list 보여주기"
    )
    place: Literal["cart", "fridge"] = Field(
        description="cart 장바구니 / fridge 나의 냉장고. add·remove·list 는 대상 위치, move 는 옮기기 전 위치"
    )
    target: ItemTarget


class StorageRequest(BaseModel):
    """냉장고·장바구니 요청 해석 결과 — _parse_storage_request() 의 구조화 출력 스키마."""

    actions: list[StorageAction] = Field(description="요청 순서대로 실행할 작업 목록")
    clarification: str = Field(description="무엇을 가리키는지 알 수 없는 대상이 있을 때 되물을 질문, 없으면 빈 문자열")


STORAGE_SYSTEM_PROMPT = f"""\
당신은 장보기 앱의 장바구니(cart)·나의 냉장고(fridge) 요청을 실행할 작업 목록으로 바꾸는 해석기입니다.
요청을 순서대로 실행할 작업(actions)으로 나누세요. 레시피를 추천해 달라는 부분은 무시하세요 (다른 단계가 처리합니다).

작업 종류:
- add: place 에 담기
- remove: place 에서 빼기
- move: place 에 있는 것을 반대쪽으로 옮기기 (place 는 옮기기 전 위치)
- list: place 에 담긴 것을 보여주기

대상(target) — 아래를 모두 합친 품목이 대상입니다:
- ingredients: 이름을 직접 말한 식자재. 반드시 카탈로그 이름으로 바꾸세요 (예: 계란 → 달걀, 삼겹살 → 돼지고기).
- categories: "양념류", "채소 다"처럼 대분류 전체를 말한 경우.
- recipe_name: "오징어볶음에 필요한 재료"처럼 레시피의 재료를 말하면 요리 이름, 아니면 빈 문자열.
- everything: "장바구니에 있는 거 다", "냉장고 비워줘"처럼 그곳에 있는 것 전부면 true.
- unknown: 카탈로그에 없는 품목(노트북처럼 식자재가 아닌 것, 카탈로그에 없는 식자재)을 말한 그대로.
  비슷한 다른 식자재로 바꾸지 말고, 요리 이름이 아니면 recipe_name 에 넣지 마세요.
- list 에서 특정 대상을 말하지 않으면 target 은 모두 비우고 everything 은 false.

카탈로그 (대분류: 식자재):
{chr(10).join(f"- {c}: {', '.join(catalog.list_ingredients(c))}" for c in CATEGORIES)}

규칙:
- "양념", "양념류"는 categories [양념·오일] 입니다.
- "다 샀으니까 냉장고로", "장바구니에서 냉장고로"는 move (place cart) 입니다.
- [이전 대화 맥락]이 주어지면 이번 요청의 "그거", "방금", "N번" 같은 말을 맥락에서 찾아 구체적으로 바꾸세요:
  "2번 레시피 재료" → 직전 '보여준 레시피'의 2번 요리 이름을 recipe_name 에,
  "방금 담은 거" → 직전 '확인 요청'에서 실행된 품목을 ingredients 에.
- "그거", "그 레시피"처럼 가리키는 대상을 맥락에서 찾을 수 없거나 여러 개 중 어느 것인지 알 수 없으면,
  추측하지 말고 그 작업은 actions 에서 빼고 clarification 에 되물을 질문을 쓰세요
  (예: "어떤 레시피의 재료를 담을까요? 레시피 이름이나 번호를 알려 주세요."). 알 수 있으면 빈 문자열.

예시:
- "양념류는 다 냉장고에 넣어줘" → add, fridge, categories [양념·오일]
- "오징어볶음에 필요한 재료 장바구니에 다 넣어줘" → add, cart, recipe_name "오징어볶음"
- "장바구니에 있는 거 다 샀으니까 냉장고로 옮겨줘" → move, cart, everything true
- "냉장고에서 양파랑 대파 빼줘" → remove, fridge, ingredients [양파, 대파]
- "냉장고에 양념 뭐 있어?" → list, fridge, categories [양념·오일]
- "장바구니에 망고랑 양파 담아줘" → add, cart, ingredients [양파], unknown [망고]
"""


@dataclass
class PlannedAction:
    """사용자 확인을 기다리는 냉장고·장바구니 작업 하나 (아직 실행하지 않음).

    Attributes:
        action: "add" 담기 / "remove" 빼기 / "move" 반대쪽으로 옮기기.
        place:  add·remove 는 대상 위치, move 는 옮기기 전 위치 ("cart" / "fridge").
        items:  실행할 품목 — 지금 상태 기준으로 실제로 바뀌는 것만 (화면에서 품목별로 체크를 풀 수 있다).
        notes:  함께 보여줄 안내 (기준 레시피, 제외한 품목, 없는 품목 등).
    """

    action: str
    place: str
    items: list[str]
    notes: list[str] = field(default_factory=list)

    def question(self) -> str:
        """확인 질문 (예: "장바구니에 담을까요?")."""
        name = storage.PLACE_NAMES[self.place]
        other_name = storage.PLACE_NAMES[storage.other_place(self.place)]
        return {
            "add": f"{name}에 담을까요?",
            "remove": f"{name}에서 뺄까요?",
            "move": f"{name}에서 {other_name}로 옮길까요?",
        }[self.action]


class Handlers:
    """그래프 노드가 부르는 요청 처리 함수 모음 + MCP 도구 서버 연결.

    핵심 변수:
        mcp_client:  fastmcp Client — mcp_tool_server.py 와의 연결.
        _started:    start() 로 MCP 연결을 열어 두었는지 여부.
    """

    def __init__(self) -> None:
        server_url = os.environ.get("MCP_SERVER_URL")
        if server_url:
            # Docker: 따로 떠 있는 mcp-server 컨테이너에 HTTP 로 접속한다 (예: http://mcp-server:8001/mcp)
            self.mcp_client = Client(server_url)
        else:
            # 로컬: 도구 서버를 지금 실행 중인 Python 으로 띄운다 (stdio). 환경변수를 그대로 넘겨서
            # storage.py 의 DB 위치(RECIPANG_DB_PATH)가 이 프로세스와 같아지게 한다.
            self.mcp_client = Client(
                PythonStdioTransport(
                    MCP_SERVER_SCRIPT, env=dict(os.environ), cwd=str(MCP_SERVER_SCRIPT.parent), keep_alive=False
                )
            )
        self._started = False

    async def start(self) -> None:
        """RAG 색인을 1회 보장하고, MCP 클라이언트 연결을 열어 둔다 (여러 질문을 연달아 처리할 때).

        도구 서버 기동에 약 6초, 첫 도구 호출에 약 2초가 걸리므로 여기서 미리 한 번 호출해 둔다.
        연결을 유지하면 이후 도구 호출은 0.01초 수준이다.
        """
        rag_module.ensure_index()
        await self.mcp_client.__aenter__()
        self._started = True
        await self._call_tool("list_items", {"place": storage.CART})  # 첫 호출 준비

    async def stop(self) -> None:
        """MCP 연결을 닫는다 (서버 하위 프로세스도 함께 종료됨)."""
        if self._started:
            await self.mcp_client.__aexit__(None, None, None)
            self._started = False

    # ── 냉장고·장바구니 분기 ──────────────────────────────────────────────

    async def handle_storage_request(
        self, query: str, memory_context: str = ""
    ) -> tuple[list[str], list[PlannedAction]]:
        """냉장고·장바구니 분기: 요청 해석 → 대상을 실제 품목으로 풀기 (아무것도 바꾸지 않는다).

        조회는 바로 결과 문장으로 돌려주고, 담기·빼기·옮기기는 확인 대기 작업(PlannedAction)으로 돌려준다.
        사용자가 화면에서 품목을 고르고 확인하면 execute_actions() 로 실행한다.

        Returns:
            (바로 보여줄 문장 목록, 확인을 기다리는 작업 목록)
            가리키는 대상을 알 수 없는 부분은 실행하지 않고 되묻는 질문을 문장 목록에 넣는다.
        """
        request = await self._parse_storage_request(query, memory_context)
        lines: list[str] = [request.clarification] if request.clarification else []
        pending: list[PlannedAction] = []
        if not request.actions:
            return lines, pending
        async with AsyncExitStack() as stack:
            if not self._started:  # start() 로 열어 두지 않았으면 이번 요청 동안만 MCP 서버를 띄운다
                await stack.enter_async_context(self.mcp_client)
            for action in request.actions:
                action_lines, planned = await self._plan_storage_action(action)
                lines += action_lines
                if planned is not None:
                    pending.append(planned)
        return lines, pending

    async def execute_actions(self, actions: list[tuple[str, str, list[str]]]) -> list[str]:
        """사용자가 확인한 작업을 MCP 도구로 실행하고 결과 문장을 반환한다.

        Args:
            actions: (action, place, items) 목록 — 화면에서 체크된 품목만. 품목이 빈 작업은 건너뛴다.

        계획한 뒤 확인하기 전까지 상태가 바뀌었을 수 있으므로, 도구 결과(이미 있음 / 반대쪽에 있음 / 없음)를 그대로 알려준다.
        """
        lines: list[str] = []
        async with AsyncExitStack() as stack:
            if not self._started:
                await stack.enter_async_context(self.mcp_client)
            for action, place, items in actions:
                if not items:
                    continue
                name = storage.PLACE_NAMES[place]
                other_name = storage.PLACE_NAMES[storage.other_place(place)]
                if action == "add":
                    result = await self._call_tool("add_items", {"place": place, "ingredients": items})
                    if result["added"]:
                        lines.append(f"{name}에 담았어요: {', '.join(result['added'])}")
                    if result["already"]:
                        lines.append(f"이미 {name}에 있어요: {', '.join(result['already'])}")
                    if result["in_other"]:
                        lines.append(f"{other_name}에 있어서 담지 않았어요: {', '.join(result['in_other'])}")
                elif action == "remove":
                    result = await self._call_tool("remove_items", {"place": place, "ingredients": items})
                    if result["removed"]:
                        lines.append(f"{name}에서 뺐어요: {', '.join(result['removed'])}")
                    if result["not_found"]:
                        lines.append(f"{name}에 없어요: {', '.join(result['not_found'])}")
                else:  # move
                    result = await self._call_tool("move_items", {"from_place": place, "ingredients": items})
                    if result["moved"]:
                        lines.append(f"{name}에서 {other_name}로 옮겼어요: {', '.join(result['moved'])}")
                    if result["not_found"]:
                        lines.append(f"{name}에 없어요: {', '.join(result['not_found'])}")
        return lines or ["선택한 품목이 없어서 아무것도 바꾸지 않았어요."]

    async def _parse_storage_request(self, query: str, memory_context: str = "") -> StorageRequest:
        """LLM 으로 냉장고·장바구니 요청을 StorageRequest(작업 목록)로 바꾼다. 거절하면 작업 없음."""
        client = AsyncAnthropic()
        response = await client.messages.parse(
            model=PARSE_MODEL,
            max_tokens=4000,
            system=STORAGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _with_context(query, memory_context)}],
            output_format=StorageRequest,
            output_config={"effort": EFFORT},
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return StorageRequest(actions=[], clarification="")
        return response.parsed_output

    async def _call_tool(self, name: str, arguments: dict) -> dict:
        """MCP 도구를 이름 + 인자 dict 로 호출하고 결과 dict 를 꺼낸다."""
        return _extract_tool_result(await self.mcp_client.call_tool(name, arguments))

    async def _plan_storage_action(self, action: StorageAction) -> tuple[list[str], PlannedAction | None]:
        """작업 하나의 대상을 실제 품목으로 풀어, 조회면 결과 문장을, 바꾸는 작업이면 확인 대기 작업을 만든다.

        대상 풀기: 직접 말한 식자재 + 대분류의 식자재 + 레시피 재료 (+ everything 이면 그곳에 있는 전부).
        확인 대기 작업에는 지금 상태 기준으로 실제로 바뀌는 품목만 넣는다:
          담기       — 이미 그곳에 있거나 반대쪽에 있는 품목은 제외하고 안내 ('한쪽에만' 규칙)
          빼기·옮기기 — 그곳에 있는 품목만. '없어요' 안내는 이름을 직접 말한 식자재만 (대분류 전체를 나열하지 않도록)
        바뀌는 품목이 하나도 없으면 확인할 것이 없으므로 안내 문장만 돌려준다.

        Returns:
            (바로 보여줄 문장 목록, 확인 대기 작업 또는 None)
        """
        place, target = action.place, action.target
        name = storage.PLACE_NAMES[place]
        other_name = storage.PLACE_NAMES[storage.other_place(place)]
        notes: list[str] = []
        if target.unknown:
            notes.append(f"식자재 카탈로그에 없는 품목이라 다룰 수 없어요: {', '.join(target.unknown)}")

        items = list(target.ingredients)
        for category in target.categories:
            items += catalog.list_ingredients(category)
        if target.recipe_name:
            found = recipe_db.find_recipe(target.recipe_name)
            if found is None:
                notes.append(f"'{target.recipe_name}' 레시피를 찾지 못했어요.")
            else:
                notes.append(f"'{found[0]}' 레시피 재료를 기준으로 했어요.")
                items += found[1]
        here = (await self._call_tool("list_items", {"place": place}))["items"]
        if target.everything:
            items += here
        items = list(dict.fromkeys(items))  # 순서를 유지하며 중복 제거

        if action.action == "list":
            filters = [*target.categories, *target.ingredients]
            if target.recipe_name:
                filters.append(f"'{target.recipe_name}' 재료")
            shown = [i for i in here if i in items] if filters else here
            label = f"{name}의 {', '.join(filters)}" if filters else name
            if not here:
                notes.append(f"{name}가 비어 있어요.")
            elif not shown:
                notes.append(f"{label}에 해당하는 식자재가 없어요.")
            else:
                notes.append(f"{label} ({len(shown)}개): {', '.join(shown)}")
            return notes, None

        if action.action == "add":
            there = (await self._call_tool("list_items", {"place": storage.other_place(place)}))["items"]
            todo = [i for i in items if i not in here and i not in there]
            already = [i for i in items if i in here]
            in_other = [i for i in items if i in there]
            if already:
                notes.append(f"이미 {name}에 있어서 제외했어요: {', '.join(already)}")
            if in_other:
                notes.append(f"{other_name}에 있어서 제외했어요: {', '.join(in_other)}")
        else:
            todo = [i for i in items if i in here]
            missing = [i for i in dict.fromkeys(target.ingredients) if i not in here]
            if missing:
                notes.append(f"{name}에 없어요: {', '.join(missing)}")

        if not todo:
            verb = {"add": "담을", "remove": "뺄", "move": "옮길"}[action.action]
            place_phrase = f"{name}에" if action.action == "add" else f"{name}에서"
            return notes + [f"{place_phrase} {verb} 품목이 없어요."], None
        return [], PlannedAction(action=action.action, place=place, items=todo, notes=notes)

    # ── 레시피 검색 분기 ──────────────────────────────────────────────────

    async def handle_recipe_query(
        self,
        query: str,
        page_size: int = 3,
        storage_lines: list[str] | None = None,
        memory_context: str = "",
    ) -> RecipeAnswer | None:
        """레시피 검색 분기: 요청 해석 → 검색 → 설명 문장 생성.

        Args:
            query:          사용자 요청 (예: "냉장고 재료로 매운 볶음 요리").
            page_size:      검색 결과의 첫 페이지 크기 (화면의 카드 수).
            storage_lines:  같은 요청의 냉장고·장바구니 처리 결과와 확인 대기 작업 (설명 문장이 이와 어긋나지 않도록 넘긴다).
            memory_context: 이전 대화 요약 ("그거 말고 덜 매운 걸로"처럼 이전 검색을 고치는 요청을 풀 때 쓴다).

        Returns:
            RecipeAnswer. 냉장고·장바구니 요청이 함께 있는데 검색 조건이 하나도 없으면 None
            (라우터가 "OO 재료 담아줘"를 레시피 검색으로도 잘못 판단한 경우 — 검색할 것이 없으므로 건너뛴다).
            반영할 조건 없이 지원하지 않는 조건만 있으면("우유로 만들 수 있는 요리") 검색하지 않고,
            빈 결과와 UNSUPPORTED_MESSAGE 를 돌려준다 (조건과 상관없는 결과를 맞는 것처럼 보여주지 않도록).
        """
        storage_lines = storage_lines or []
        recipe_query = await self._parse_recipe_query(query, memory_context)
        owned = storage.list_items(storage.CART) + storage.list_items(storage.FRIDGE)
        if recipe_query.is_empty():
            if recipe_query.unsupported:
                conditions = ", ".join(f"'{u}'" for u in recipe_query.unsupported)
                return RecipeAnswer(
                    text=UNSUPPORTED_MESSAGE.format(conditions=conditions),
                    query=recipe_query,
                    result=rag_module.SearchResult(recipes=[], total=0),
                    owned=owned,
                )
            if storage_lines:
                return None
        result = rag_module.retrieve(
            recipe_query.to_filter(), recipe_query.text or None, owned, recipe_query.use_owned, limit=page_size
        )
        text = await self._generate_answer(query, recipe_query, result, owned, storage_lines, memory_context)
        return RecipeAnswer(text=text, query=recipe_query, result=result, owned=owned)

    async def _parse_recipe_query(self, query: str, memory_context: str = "") -> RecipeQuery:
        """LLM 으로 레시피 요청을 RecipeQuery(검색 조건)로 바꾼다.

        구조화 출력(output_format)이라 응답이 스키마를 벗어나지 않는다.
        모델이 답을 거절하면(stop_reason == "refusal") 조건 없이 요청 문장 전체를 의미 검색에 넘긴다.
        """
        client = AsyncAnthropic()  # API 키는 환경변수 ANTHROPIC_API_KEY 에서 자동으로 읽음
        response = await client.messages.parse(
            model=PARSE_MODEL,
            max_tokens=4000,
            system=PARSE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _with_context(query, memory_context)}],
            output_format=RecipeQuery,
            output_config={"effort": EFFORT},
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return RecipeQuery.text_only(query)
        return response.parsed_output

    async def _generate_answer(
        self,
        query: str,
        recipe_query: RecipeQuery,
        result: rag_module.SearchResult,
        owned: list[str],
        storage_lines: list[str],
        memory_context: str = "",
    ) -> str:
        """검색 결과를 섹션별 프롬프트로 조립해 Claude에게 짧은 안내 문장을 요청한다.

        프롬프트 섹션: [이전 대화 맥락] / [사용자 요청] / [장바구니·냉장고 처리 결과] / [해석한 조건]
                      / [반영하지 못한 조건] / [검색 결과] / [담으면 좋은 재료]
        """
        lines = [f"조건에 맞는 레시피 {result.total}개"]
        for rank, rec in enumerate(result.recipes[:ANSWER_TOP_N], start=1):
            need = f"더 필요한 재료 {len(rec.missing)}개: {', '.join(rec.missing[:5])}" if rec.missing else "바로 만들 수 있음"
            lines.append(f"{rank}. {rec.name} ({rec.dish_type}) — {need}")
        parts = [f"[이전 대화 맥락]\n{memory_context}"] if memory_context else []
        parts.append(f"[사용자 요청]\n{query}")
        if storage_lines:
            parts.append("[장바구니·냉장고 처리 결과]\n" + "\n".join(storage_lines))
        parts.append(f"[해석한 조건]\n{recipe_query.describe()}")
        if recipe_query.unsupported:
            parts.append("[반영하지 못한 조건]\n" + ", ".join(recipe_query.unsupported))
        parts.append("[검색 결과]\n" + "\n".join(lines))
        if result.suggestions:
            suggestion_lines = []
            for s in result.suggestions:
                names = [rec.name for rec in recipe_db.describe(s.recipe_ids, owned)]
                suggestion_lines.append(f"{s.ingredient} — 담으면 바로 만들 수 있는 레시피: {', '.join(names)}")
            parts.append("[담으면 좋은 재료]\n" + "\n".join(suggestion_lines))

        client = AsyncAnthropic()
        response = await client.messages.create(
            model=ANSWER_MODEL,
            max_tokens=2000,
            system=ANSWER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
            output_config={"effort": EFFORT},
        )
        if response.stop_reason == "refusal":
            return "이 요청에는 안내 문장을 만들지 못했어요. 아래 검색 결과를 참고해 주세요."
        return "".join(block.text for block in response.content if block.type == "text").strip()


def _extract_tool_result(mcp_result) -> dict:
    """fastmcp Client.call_tool() 결과에서 구조화된 데이터를 꺼낸다 (Day4 패턴 재사용).

    우선순위: .data (도구가 반환한 dict) → .content[0].text → {"raw": 문자열화한 결과}
    """
    if getattr(mcp_result, "data", None) is not None:
        return mcp_result.data
    if getattr(mcp_result, "content", None):
        return {"text": mcp_result.content[0].text}
    return {"raw": str(mcp_result)}
