"""
recipang — 단기 기억 (최근 N턴) : LangGraph State 의 turns reducer
===================================================================
대화 히스토리뿐 아니라, 그 턴에서 해석한 검색 조건 / 보여준 레시피 / 장바구니·냉장고 작업과
확인 결과도 "기억"해서 다음 턴의 라우터·해석·답변 생성 프롬프트에 텍스트로 넣는다 (day4 방식).
"그거 말고 덜 매운 걸로", "2번 레시피 재료 담아줘", "방금 담은 거 빼줘" 같은 후속 요청을 LLM 이 풀 수 있게 하기 위함.

사용 흐름 (graph.py 기준):
  ① 턴 시작: 노드들이 context_text(state["turns"]) 로 이전 대화 요약을 만들어 LLM 에 넘긴다.
  ② 턴 종료: respond 노드가 {"turns": [이번 턴]} 을 반환 → remember() 가 이어 붙이고 최근 WINDOW 턴만 남긴다.
  ③ 확인/취소: execute 노드가 같은 turn_id 의 턴을 보내면 remember() 가 그 턴을 바꾼다 (확인 결과 반영).
  ④ 체크포인터가 thread_id(대화방)별로 State 를 저장 → 같은 thread_id 로 다시 부르면 turns 가 복원된다.

턴을 dict(Turn TypedDict)로 저장하는 이유: 체크포인터는 State 를 직렬화해 저장하므로
dataclass 같은 사용자 클래스보다 dict·list·str 같은 기본 자료형이 안전하다.
"""

from __future__ import annotations

from typing import TypedDict

WINDOW = 5                  # 유지할 최대 턴 수. 작을수록 프롬프트가 짧아지고(비용↓), 클수록 더 먼 과거를 참조할 수 있다(맥락↑).
PENDING_WAITING = "확인 대기"  # 확인 요청을 아직 처리하지 않았을 때의 pending_result


class Turn(TypedDict, total=False):
    """대화 한 턴(요청 1개 + 답변 1개)과 그 답변의 근거.

    Keys:
        turn_id:        턴 번호 (확인 결과를 나중에 같은 턴에 반영할 때 찾는 키).
        query:          사용자 요청.
        answer:         답변 설명 문장 (레시피 설명 또는 안내).
        conditions:     해석한 검색 조건 (레시피 검색을 했을 때, 예: "매콤 · 국물 · '비 오는 날'").
        recipes:        보여준 레시피 이름 (보여준 순서 = 번호).
        total:          조건에 맞는 레시피 전체 수.
        storage:        장바구니·냉장고 안내 문장 (조회 결과, 바꿀 품목이 없을 때의 안내 등).
        pending:        확인을 요청한 작업 설명 (예: "장바구니에 담을까요? 쌀, 마늘").
        pending_result: 확인 요청의 처리 결과 — "확인 대기" / "취소됨" / "실행됨: ..." 등.

    근거까지 저장하는 이유: 다음 턴에 "2번 레시피"나 "방금 담은 거"처럼 이전 결과를 가리키는 말을
    LLM 이 이 기록에서 찾아 풀 수 있게 하기 위함.
    """

    turn_id: int
    query: str
    answer: str
    conditions: str
    recipes: list[str]
    total: int
    storage: list[str]
    pending: list[str]
    pending_result: str


def remember(old: list[Turn] | None, new: list[Turn] | None) -> list[Turn]:
    """turns reducer: 같은 turn_id 의 턴이 오면 바꾸고(확인 결과 반영), 아니면 이어 붙인 뒤 최근 WINDOW 턴만 남긴다."""
    turns = list(old or [])
    for turn in new or []:
        ids = [t["turn_id"] for t in turns]
        if turn["turn_id"] in ids:
            turns[ids.index(turn["turn_id"])] = turn
        else:
            turns.append(turn)
    return turns[-WINDOW:]  # 뒤에서 WINDOW 개만 남김 = 가장 오래된 턴부터 밀려남 (FIFO)


def context_text(turns: list[Turn]) -> str:
    """라우터/해석/답변 생성 프롬프트에 넣을 요약 텍스트를 만든다.

    Returns:
        턴이 없으면 "" (빈 문자열 → 호출 측에서 맥락 섹션 자체를 생략).
        있으면 아래 형식의 여러 줄 문자열 (오래된 턴 → 최신 턴):

            [1턴] 사용자: 비 오는 날 먹을 얼큰한 국물 요리
                  해석한 조건: 매콤 · 국물 · '비 오는 날'
                  보여준 레시피: 1. 꽃게탕, 2. 육개장, 3. 참치김치찌개 (조건에 맞는 12개 중)
                  답변: 비 오는 날에 어울리는 ...
            [2턴] 사용자: 2번 레시피 재료 장바구니에 담아줘
                  확인 요청: 장바구니에 담을까요? 소고기, 숙주 → 실행됨: 장바구니에 담았어요: 소고기, 숙주
    """
    lines: list[str] = []
    for i, t in enumerate(turns, start=1):
        lines.append(f"[{i}턴] 사용자: {t['query']}")
        if t.get("conditions"):
            lines.append(f"      해석한 조건: {t['conditions']}")
        if t.get("recipes"):
            shown = ", ".join(f"{n}. {name}" for n, name in enumerate(t["recipes"], start=1))
            lines.append(f"      보여준 레시피: {shown} (조건에 맞는 {t.get('total', 0)}개 중)")
        for line in t.get("storage", []):
            lines.append(f"      장바구니·냉장고: {line}")
        for request in t.get("pending", []):
            lines.append(f"      확인 요청: {request} → {t.get('pending_result', '')}")
        if t.get("answer"):
            lines.append(f"      답변: {t['answer']}")
    return "\n".join(lines)


def main() -> None:
    """단독 실행 데모: reducer 로 WINDOW+1 턴을 넣어 가장 오래된 턴이 밀려나는지, 확인 결과가 반영되는지 확인한다."""
    turns: list[Turn] = []
    samples: list[Turn] = [
        {"turn_id": 1, "query": "매운 볶음 요리 추천해줘", "conditions": "볶기 · 매콤",
         "recipes": ["김치볶음밥", "제육덮밥", "오징어덮밥"], "total": 14, "answer": "매콤한 볶음 요리 14개를 찾았어요."},
        {"turn_id": 2, "query": "2번 레시피 재료 장바구니에 담아줘",
         "storage": ["'제육덮밥' 레시피 재료를 기준으로 했어요."],
         "pending": ["장바구니에 담을까요? 쌀, 고추장"], "pending_result": PENDING_WAITING},
        *({"turn_id": n, "query": f"고마워 ({n})", "answer": "천만에요!"} for n in range(3, WINDOW + 2)),
    ]
    for t in samples:
        turns = remember(turns, [t])  # 그래프에서는 LangGraph 가 이 reducer 를 대신 호출한다
    # 확인 결과 반영: 같은 turn_id 의 턴을 보내면 바뀐다
    turns = remember(turns, [{**turns[0], "pending_result": "실행됨: 장바구니에 담았어요: 쌀, 고추장"}])

    print(f"현재 유지 중인 턴: {[t['turn_id'] for t in turns]} (WINDOW={WINDOW}, 1턴은 밀려남)")
    print("---")
    print(context_text(turns))


if __name__ == "__main__":
    main()
