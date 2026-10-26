"""
냉장고 모형 (시작 페이지용 양방향 컴포넌트)
==========================================
fridge_component/index.html 을 Streamlit 컴포넌트로 띄운다. 냉장고를 누르면 문이 열리고,
0.9초 뒤 "open" 값을 Streamlit 에 돌려준다 → app.py 가 그 값을 보고 본문 페이지로 넘어간다.

빌드 없이 HTML 하나로 동작한다: iframe 안의 스크립트가 Streamlit 컴포넌트 프로토콜
(componentReady / setFrameHeight / setComponentValue) 을 postMessage 로 직접 보낸다.

사용:
    import fridge_view
    if fridge_view.render(items_by_category) == "open":
        st.session_state["entered"] = True
        st.rerun()
"""

from __future__ import annotations

import os

import streamlit.components.v1 as components

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fridge_component")
_component = components.declare_component("fridge_door", path=_DIR)

# 식자재 → 이모지. 없으면 DEFAULT_EMOJI. 카탈로그(catalog.py) 이름 기준.
EMOJI = {
    "고구마": "🍠", "감자": "🥔", "당근": "🥕", "시금치": "🥬", "상추": "🥬", "깻잎": "🌿", "부추": "🌿",
    "브로콜리": "🥦", "파프리카": "🫑", "양배추": "🥬", "양파": "🧅", "대파": "🥬", "쪽파": "🌿", "마늘": "🧄",
    "생강": "🫚", "배추": "🥬", "무": "🥕", "오이": "🥒", "애호박": "🥒", "가지": "🍆", "청양고추": "🌶️",
    "풋고추": "🌶️", "토마토": "🍅", "콩나물": "🌱", "숙주": "🌱", "표고버섯": "🍄", "느타리버섯": "🍄",
    "팽이버섯": "🍄", "새송이버섯": "🍄", "양송이버섯": "🍄",
    "사과": "🍎", "배": "🍐", "레몬": "🍋", "바나나": "🍌", "아보카도": "🥑", "호두": "🌰", "아몬드": "🌰",
    "땅콩": "🥜", "쌀": "🍚", "찹쌀": "🍚",
    "고등어": "🐟", "갈치": "🐟", "명태": "🐟", "연어": "🐟", "오징어": "🦑", "낙지": "🐙", "문어": "🐙",
    "전복": "🐚", "바지락": "🐚", "굴": "🦪", "새우": "🦐", "꽃게": "🦀", "명란": "🐟", "새우젓": "🫙",
    "김": "🍙", "미역": "🌿", "다시마": "🌿", "멸치": "🐟", "황태": "🐟",
    "소고기": "🥩", "돼지고기": "🥓", "양고기": "🥩", "닭고기": "🍗", "오리고기": "🦆", "소시지": "🌭",
    "베이컨": "🥓", "달걀": "🥚",
    "단무지": "🥒", "배추김치": "🥬", "깍두기": "🥕", "두부": "🧈", "어묵": "🍢",
    "라면": "🍜", "파스타면": "🍝", "소면": "🍜", "당면": "🍜", "우동면": "🍜", "떡": "🍡", "밀가루": "🌾",
    "부침가루": "🌾", "튀김가루": "🌾", "빵가루": "🍞", "햄": "🥫", "참치캔": "🥫", "카레가루": "🍛",
    "간장": "🫙", "고추장": "🫙", "된장": "🫙", "액젓": "🫙", "식용유": "🫙", "올리브유": "🫒", "참기름": "🫙",
    "들기름": "🫙", "식초": "🫙", "굴소스": "🫙", "케첩": "🍅", "마요네즈": "🫙", "소금": "🧂", "설탕": "🧂",
    "후추": "🧂", "고춧가루": "🌶️", "참깨": "🌾", "맛술": "🍶", "올리고당": "🍯",
    "식빵": "🍞", "모닝빵": "🥐", "베이글": "🥯",
    "우유": "🥛", "두유": "🥛", "요거트": "🥣", "생크림": "🍦", "치즈": "🧀", "버터": "🧈",
}
DEFAULT_EMOJI = "🥫"
MAX_SHELVES = 5


def render(items_by_category: dict[str, list[str]], *, caption: str = "", key: str = "fridge_door",
           signal: bool = True):
    """냉장고를 그린다.

    signal=True  (시작 페이지): 눌러 문을 열면 "open" 을 돌려준다 (아니면 None).
    signal=False (보기 전용):  문만 열고 닫히고, Streamlit 에 아무것도 보내지 않는다 (팔업용).

    items_by_category: {대분류: [식자재, ...]} — 비어 있는 대분류는 빼고 넘긴다. 칸은 최대 MAX_SHELVES 개,
    넘치면 뒤쪽 대분류를 "그 밖에" 칸으로 합친다.
    """
    shelves = [{"label": c, "items": [{"name": n, "emoji": EMOJI.get(n, DEFAULT_EMOJI)} for n in names]}
               for c, names in items_by_category.items() if names]
    if len(shelves) > MAX_SHELVES:
        rest = [it for s in shelves[MAX_SHELVES - 1:] for it in s["items"]]
        shelves = shelves[:MAX_SHELVES - 1] + [{"label": "그 밖에", "items": rest}]
    return _component(shelves=shelves, caption=caption, signal=signal, key=key, default=None)
