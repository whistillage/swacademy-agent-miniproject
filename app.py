"""
recipang — 식자재 담기 / 레시피 추천 화면 (Streamlit)
=======================================================
본문을 좌우 반반으로 나눈다. 왼쪽은 장보기(대분류 탭에서 식자재를 골라 장바구니 / 나의 냉장고에 담기),
오른쪽은 레시피 추천 대화창이다. 장바구니와 나의 냉장고는 사이드바에서 보고 뺀다.

실행 (recipang 폴더에서):
  streamlit run app.py                      # 로컬: 화면과 에이전트를 한 프로세스에서 실행
  docker compose up --build                 # Docker: 이 화면은 frontend 컨테이너에서 agent-app API 를 부른다

화면 구성:
  ┌ 사이드바 ──────────┐  ┌ 장보기 ─────────────────────────┐ ┌ 레시피 추천 ──────────────────┐
  │ 장바구니 (n)        │  │ [채소] [과일·견과·쌀] ...        │ │ ┌ 대화 기록 (스크롤) ───────┐ │
  │  돼지고기   [빼기]  │  │  ☑ 고구마  ☐ 감자  ☑ 당근        │ │ │ 나: 매운 볶음 요리 추천해줘│ │
  │ 나의 냉장고 (n)     │  │  ...                             │ │ │ 에이전트: 설명 문장        │ │
  │  식자재 / 양념      │  │ [장바구니에 담기] [냉장고에 담기] │ │ │   해석한 조건 / 레시피 카드│ │
  └────────────────────┘  └─────────────────────────────────┘ │ │           [더보기]         │ │
                                                               │ └───────────────────────────┘ │
                                                               │ [내 재료로 추천]  ← 빠른 검색 │
                                                               │ [입력창: 원하는 레시피 질문]  │
                                                               └───────────────────────────────┘
  화면이 좁으면 두 칸이 위아래로 쌓인다 (장보기 → 레시피 추천).

백엔드: 화면은 장바구니·냉장고·레시피·에이전트를 직접 다루지 않고 백엔드 객체의 메서드만 부른다 (backend.py 참고).
  - 환경변수 AGENT_API_URL 이 없으면 backend.LocalBackend — 같은 프로세스에서 직접 실행 (로컬)
  - 있으면 api_client.ApiBackend — 그 주소의 agent-app API 를 HTTP 로 부름 (Docker 의 frontend 컨테이너)
  두 백엔드는 메서드·반환값·예외(errors.py)가 같으므로 화면 코드는 하나다.

동작:
  - 체크박스는 여러 탭에 걸쳐 고를 수 있고, 담기 버튼을 누르면 고른 품목 전체가 담긴 뒤 체크가 풀린다.
  - 같은 품목은 장바구니/냉장고 중 한쪽에만 담긴다 (storage.add_items 규칙).
    담지 못한 품목은 안내 메시지로 보여준다.
  - 사이드바의 나의 냉장고는 식자재와 양념(SEASONING_CATEGORY 대분류)을 나눠 보여준다 (표시만 나눔, 저장은 같음).
  - 레시피 추천은 대화 형식이다. 답변은 대화 기록에 쌓이고, 답변마다 그 시점의 장바구니 + 냉장고 재료를 저장해 두므로
    순서와 카드 내용은 그 시점 기준으로 유지된다. 답변 안의 [더보기]를 누를 때마다 RECIPE_PAGE_SIZE 개씩 늘어난다.
    · 입력창(자연어): graph.py 의 LangGraph 그래프로 보낸다 — 라우터가 레시피 검색 / 냉장고 / 장바구니를 판단한다.
      냉장고·장바구니 조회는 바로 보여주고, 담기·빼기·옮기기가 있으면 그래프가 확인 단계(interrupt)에서 멈춘다.
      화면은 품목별 체크박스로 확인을 받고, [담기]/[빼기]/[옮기기] 를 누르면 체크된 품목으로 그래프를 재개해
      실행한다 ([취소]면 아무것도 바꾸지 않음). 확인하지 않고 새 요청을 보내면 그 확인은 '넘어감'으로 닫힌다.
      레시피 검색(LLM 해석 → SQL 거르기 → 벡터 정렬 → LLM 설명 문장)은 바로 실행한다.
      직접 누르는 버튼([+ 재료], 장보기 담기, 사이드바 [빼기])은 확인 없이 바로 실행한다.
    · [내 재료로 추천](빠른 검색): LLM 없이 recipe_db.recommend() — 더 필요한 재료가 적은 순.
  - 레시피 카드는 제목 → 재료 → 만드는 법 → 더 필요한 재료 순서로 그린다.
    더 필요한 재료(양념 포함, 양념은 뒤쪽)와 '담으면 좋은 재료'는 [+ 재료] 버튼으로 바로 장바구니에 담는다.
    지금 장바구니나 냉장고에 있는 재료는 '✓ 재료'(누를 수 없음)로 바뀐다.
  - 대화 기록은 세션 동안만 유지된다.
  - 단기 기억: 브라우저 세션마다 대화방 id(thread_id)를 하나 두고, 그래프의 체크포인터가 대화방별로 최근 5턴
    (memory.WINDOW)을 기억한다 ("그거 말고 덜 매운 걸로", "2번 레시피 재료 담아줘" 같은 후속 요청용).
    기록하는 것: 입력창 요청과 답변, 확인 요청의 결과([담기] 등으로 실행 / [취소] / 넘어감).
    기록하지 않는 것: [내 재료로 추천] 결과, 직접 누르는 버튼으로 담기·빼기, [더보기]로 더 펼친 레시피.
    기억은 에이전트 프로세스 메모리에 있으므로 앱(Docker 에서는 agent-app)을 다시 시작하면 사라진다.
"""

from __future__ import annotations

import os
import uuid

import streamlit as st

import fridge_view
from errors import BackendError, NoPendingError

AGENT_API_URL = os.environ.get("AGENT_API_URL")  # Docker 의 frontend 컨테이너에서만 설정된다

COLUMNS_PER_ROW = 3      # 장보기 탭 안에서 체크박스를 한 줄에 몇 개씩 놓을지
RECIPE_PAGE_SIZE = 3     # 추천 답변에 처음에 / [더보기] 한 번에 몇 개씩 보여줄지
ADD_BUTTONS_PER_ROW = 3  # 레시피 카드에서 [+ 재료] 버튼을 한 줄에 몇 개씩 놓을지
CHAT_HEIGHT = 460        # 레시피 추천 대화 기록 영역의 높이(px). 예시 줄·입력창까지 한 화면에 들어오도록 잡는다
SEASONING_CATEGORY = "양념·오일"  # 나의 냉장고에서 '양념'으로 묶어 보여줄 카탈로그 대분류
CART, FRIDGE = "cart", "fridge"
PLACE_NAMES = {CART: "장바구니", FRIDGE: "나의 냉장고"}

# 로고 파일 (assets/)
_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_FRIDGE = os.path.join(_ASSETS, "recipang_logo_fridge.svg")   # 냉장고 캐릭터만 (아이콘용)
LOGO_WORD = os.path.join(_ASSETS, "recipang_word.svg")           # 워드마크만 (사이드바용)
LOGO_LOCKUP = os.path.join(_ASSETS, "recipang_logo_word.svg")    # 캐릭터 + 워드마크 + 태그라인 (시작 페이지용)

# 대분류 탭 이모지 (catalog.py 의 이름 기준). 없으면 이모지 없이 이름만.
CATEGORY_EMOJI = {
    "채소": "🥬", "과일·견과·쌀": "🍎", "수산·해산·건어물": "🐟", "정육·가공육·달걀": "🥩",
    "국·반찬·메인요리": "🍲", "면·가루·통조림": "🍜", "양념·오일": "🫙", "베이커리": "🍞", "유제품": "🧀",
}
# 요리 종류(dish_type) 이모지 (recipe_db.py 설명의 값 기준)
DISH_EMOJI = {
    "밥": "🍚", "죽": "🥣", "국·탕": "🍲", "찌개": "🍲", "볶음": "🍳", "조림": "🥘", "찜": "♨️", "구이": "🔥",
    "무침·나물": "🥗", "전·튀김": "🥞", "면·분식": "🍜", "샐러드": "🥗", "빵·간식": "🥐", "반찬": "🥢",
}


def _with_emoji(ingredient: str) -> str:
    """알약에 보여줄 라벨: 이모지 + 식자재 이름. 값(value)은 이름 그대로라 저장 로직은 안 바뀜다."""
    return f"{fridge_view.EMOJI.get(ingredient, fridge_view.DEFAULT_EMOJI)} {ingredient}"


@st.cache_resource(show_spinner="준비하는 중이에요...")
def _backend():
    """백엔드를 앱 프로세스에 하나만 만든다 (모든 세션이 같이 쓴다 — 대화는 세션마다 thread_id 로 구분).

    로컬 백엔드는 LangGraph·임베딩 같은 무거운 모듈을 쓰므로, 필요할 때(AGENT_API_URL 이 없을 때)만 불러온다.
    → frontend 컨테이너에는 그 모듈이 없어도 된다.
    """
    if AGENT_API_URL:
        from api_client import ApiBackend

        return ApiBackend(AGENT_API_URL)
    from backend import LocalBackend

    return LocalBackend()


def _catalog() -> dict[str, list[str]]:
    """대분류 → 식자재 목록 (카탈로그 순서)."""
    return _backend().catalog()


def _category_of(ingredient: str) -> str | None:
    """식자재의 대분류. 카탈로그에 없으면 None."""
    return next((c for c, items in _catalog().items() if ingredient in items), None)


def _shop_key(category: str) -> str:
    """장보기 대분류 한 개의 알약(pills) 위젯 session_state 키."""
    return f"shop_{category}"


def _shop_selected() -> dict[str, list[str]]:
    """대분류별 선택 저장소 {대분류: [식자재, ...]}.

    위젯 키(shop_<대분류>)에 기대면 안 된다 — Streamlit 은 화면에 그려지지 않은 위젯의 상태를 지우므로,
    다른 대분류로 넘어가면 직전 대분류의 선택이 사라진다. 그래서 알약이 바뀜 때마다 여기에 복사해 둔다.
    """
    return st.session_state.setdefault("shop_selected", {})


def _on_pick(category: str) -> None:
    """식자재 알약 콜백: 위젯 값을 저장소에 복사한다."""
    _shop_selected()[category] = list(st.session_state.get(_shop_key(category)) or [])


def _selected_ingredients() -> list[str]:
    """모든 대분류에서 고른 식자재를 카탈로그 순서대로 모은다."""
    picked = _shop_selected()
    return [
        ingredient
        for category, ingredients in _catalog().items()
        for ingredient in ingredients
        if ingredient in picked.get(category, [])
    ]


def _on_clear_picks() -> None:
    """[선택 초기화] 콜백: 모든 대분류의 선택을 비운다 (저장소 + 그려져 있는 알약 위젯)."""
    st.session_state["shop_selected"] = {}
    for category in _catalog():
        if _shop_key(category) in st.session_state:
            st.session_state[_shop_key(category)] = []


def _on_add(place: str) -> None:
    """담기 버튼 콜백: 고른 품목을 담고, 결과 메시지를 남기고, 체크를 푼다.

    콜백은 화면을 다시 그리기 전에 실행되므로, 여기서는 체크박스 상태(session_state)를 바꿀 수 있다.
    """
    selected = _selected_ingredients()
    if not selected:
        st.session_state["messages"] = [("warning", "담을 식자재를 먼저 골라주세요.")]
        return

    try:
        result = _backend().add_items(place, selected)
    except BackendError as e:
        st.session_state["messages"] = [("error", str(e))]
        return
    name = PLACE_NAMES[place]
    other_name = PLACE_NAMES[FRIDGE if place == CART else CART]
    messages = []
    if result["added"]:
        messages.append(("success", f"{name}에 담았습니다: {', '.join(result['added'])}"))
    if result["already"]:
        messages.append(("info", f"이미 {name}에 있습니다: {', '.join(result['already'])}"))
    if result["in_other"]:
        messages.append(("warning", f"{other_name}에 있어서 담지 않았습니다: {', '.join(result['in_other'])}"))
    st.session_state["messages"] = messages
    _on_clear_picks()


def _pills_key(place: str, label: str) -> str:
    """사이드바 품목 알약(pills) 위젯의 session_state 키."""
    return f"pills_{place}_{label}"


def _on_remove_selected(place: str, labels: list[str]) -> None:
    """사이드바 [선택한 것 빼기] 콜백: 알약에서 고른 품목을 빼고 선택을 비운다."""
    for label in labels:
        key = _pills_key(place, label)
        for ingredient in st.session_state.get(key) or []:
            try:
                _backend().remove_item(place, ingredient)
            except BackendError as e:
                st.session_state["messages"] = [("error", str(e))]
                return
        st.session_state[key] = []


def _render_place(place: str, items: list[str]) -> None:
    """사이드바에 장바구니 또는 냉장고를 알약(pills)으로 그린다. 눌러 고른 뒤 [선택한 것 빼기] 한 번으로 뻜다.

    나의 냉장고는 식자재와 양념(SEASONING_CATEGORY 대분류)을 나눠서 보여준다.
    """
    st.subheader(f"{PLACE_NAMES[place]} · {len(items)}")
    if not items:
        st.caption("비어 있음")
        return

    if place == FRIDGE:
        seasonings = [i for i in items if _category_of(i) == SEASONING_CATEGORY]
        groups = [("식자재", [i for i in items if i not in seasonings]), ("양념", seasonings)]
    else:
        groups = [("전체", items)]

    chosen_any = False
    labels = []
    for label, group in groups:
        if not group:
            continue
        labels.append(label)
        if place == FRIDGE:
            st.caption(f"{label} · {len(group)}")
        chosen = st.pills(label, group, selection_mode="multi", key=_pills_key(place, label),
                          format_func=_with_emoji, label_visibility="collapsed")
        chosen_any = chosen_any or bool(chosen)
    st.button("선택한 것 빼기", key=f"remove_{place}", disabled=not chosen_any,
              on_click=_on_remove_selected, args=(place, labels), use_container_width=True)


def _render_shop() -> None:
    """장보기 칸: 담기 결과 메시지, 대분류 탭(선택 개수 표시)과 알약, 선택 트레이, 담기 버튼."""
    # 직전 담기 결과 메시지 (한 번 보여주고 지운다)
    for level, text in st.session_state.pop("messages", []):
        getattr(st, level)(text)

    catalog = _catalog()
    categories = list(catalog)

    # 대분류는 탭 대신 알약(단일 선택). 탭은 라벨이 바뀜 때마다(고른 개수 표시) 처음 탭으로 돌아가서
    # 알약에 key 를 주고 고른 개수는 format_func 로만 붙인다 → 위젯은 그대로, 선택도 그대로.
    def _category_label(category: str) -> str:
        n = len(_shop_selected().get(category, []))
        emoji = CATEGORY_EMOJI.get(category, "")
        return f"{emoji} {category} ({n})" if n else f"{emoji} {category}"

    if "shop_category" not in st.session_state:
        st.session_state["shop_category"] = categories[0]
    # 대분류는 세그먼트(띄 모양), 식자재는 알약 — 둘의 생김새를 다르게 해서 "분류 고르기 / 재료 고르기"가 한눈에 구분되게 한다
    category = st.segmented_control("대분류", categories, key="shop_category", format_func=_category_label,
                                    label_visibility="collapsed") or categories[0]
    # 이 대분류의 알약이 처음 그려지면(위젯 상태 없음) 저장소에서 선택을 복원한다
    if _shop_key(category) not in st.session_state:
        st.session_state[_shop_key(category)] = list(_shop_selected().get(category, []))
    with st.container(border=True):
        n_here = len(_shop_selected().get(category, []))
        st.markdown(f"**{CATEGORY_EMOJI.get(category, '')} {category}** · {len(catalog[category])}가지"
                    + (f" · 고름 {n_here}" if n_here else ""))
        st.pills(category, catalog[category], selection_mode="multi", key=_shop_key(category),
                 format_func=_with_emoji, label_visibility="collapsed", on_change=_on_pick, args=(category,))

    # 선택 트레이: 대분류를 넘나들며 고른 걸 한 줄에 모아 보여주고, 오른쪽에 초기화 버튼
    selected = _selected_ingredients()
    tray_col, clear_col = st.columns([3, 1], vertical_alignment="center")
    if selected:
        tray_col.markdown("선택 · " + "  ".join(_with_emoji(s) for s in selected))
    else:
        tray_col.caption("대분류에서 식자재를 골라 주세요.")
    clear_col.button("✕ 비우기", on_click=_on_clear_picks, disabled=not selected, use_container_width=True,
                     help="고른 식자재를 모두 취소")

    cart_col, fridge_col = st.columns(2)
    cart_col.button("장바구니에 담기", on_click=_on_add, args=(CART,), type="primary", use_container_width=True)
    fridge_col.button("냉장고에 담기", on_click=_on_add, args=(FRIDGE,), use_container_width=True)


def _on_add_to_cart(ingredient: str) -> None:
    """레시피 카드의 [+ 재료] 버튼 콜백: 재료 하나를 장바구니에 담는다."""
    try:
        _backend().add_items(CART, [ingredient])
    except BackendError as e:
        st.session_state["messages"] = [("error", str(e))]


def _render_add_buttons(label: str, items: list[str], key_prefix: str, current: dict[str, str]) -> None:
    """재료마다 버튼을 그린다.

    Args:
        label:      버튼 줄 위에 보여줄 제목 (예: "더 필요한 재료").
        items:      재료 이름 목록.
        key_prefix: 버튼 key 접두어 (답변·레시피마다 달라야 함).
        current:    지금 장바구니/냉장고에 있는 재료 → 담을 곳(CART/FRIDGE).
                    여기 있는 재료는 '✓ 재료'로, 없으면 [+ 재료](장바구니에 담기)로 그린다.
    """
    st.caption(label)
    cols = st.columns(ADD_BUTTONS_PER_ROW)
    for i, name in enumerate(items):
        col = cols[i % ADD_BUTTONS_PER_ROW]
        key = f"{key_prefix}_{i}"
        if name in current:
            col.button(f"✓ {name}", key=key, disabled=True, help=f"{PLACE_NAMES[current[name]]}에 있음")
        elif _category_of(name) is None:
            col.button(name, key=key, disabled=True, help="카탈로그에 없는 재료라 담을 수 없음")
        else:
            col.button(_with_emoji(name), key=key, on_click=_on_add_to_cart, args=(name,), help="장바구니에 담기")


def _render_recipe(rank: int, rec: dict, key_prefix: str, current: dict[str, str]) -> None:
    """추천 레시피 카드 하나: 제목 + 상태 배지 → 재료 한 줄 → 더 필요한 재료 버튼 → (접힘) 만드는 법.

    rec 는 recipe_db.Recommendation 의 dict. key_prefix / current 는 _render_add_buttons 참고.
    비교할 때 필요한 건 "뭐 만들 수 있고 뭐가 모자라나"라 조리 순서는 기본으로 접는다.
    """
    with st.container(border=True):
        head, badge = st.columns([3, 1.2])
        head.markdown(f"**{rank}. {DISH_EMOJI.get(rec['dish_type'], '🍽️')} {rec['name']}** · {rec['dish_type']}")
        if rec["missing"]:
            badge.badge(f"{len(rec['missing'])}개 더 필요", color="orange")
        else:
            badge.badge("바로 만들 수 있음", color="green")
        st.caption("재료 · " + ", ".join(f"{i['name']} {i['amount']}".strip() for i in rec["ingredients"]))
        if rec["missing"]:
            _render_add_buttons("더 필요한 재료", rec["missing"], key_prefix, current)
        with st.expander("만드는 법"):
            st.markdown("\n".join(f"{n}. {step}" for n, step in enumerate(rec["steps"], start=1)))


def _on_recommend() -> None:
    """[내 재료로 추천] 버튼 콜백: 대화 기록에 내 요청과 추천 답변을 추가한다.

    답변에는 누른 시점의 내 재료(owned)를 저장해 두므로, 그 답변의 순서와 카드 내용은
    이후에 재료가 바뀌어도 유지된다 (카드에서 재료를 담아도 목록이 재정렬되지 않도록).
    """
    try:
        items = _backend().items()
    except BackendError as e:
        st.session_state["messages"] = [("error", str(e))]
        return
    chat = st.session_state.setdefault("chat", [])
    chat.append({"role": "user", "text": "지금 재료로 레시피 추천해줘"})
    chat.append({"role": "assistant", "recipes": {"owned": items[CART] + items[FRIDGE], "limit": RECIPE_PAGE_SIZE}})


def _on_more(message_no: int) -> None:
    """답변 안의 [더보기] 버튼 콜백: 그 답변의 카드를 한 페이지만큼 늘린다."""
    message = st.session_state["chat"][message_no]
    (message.get("recipes") or message["search"])["limit"] += RECIPE_PAGE_SIZE


def _thread_id() -> str:
    """이 브라우저 세션의 대화방 id — 그래프의 체크포인터가 이 id 로 대화 기억과 확인 대기 상태를 저장한다."""
    if "thread_id" not in st.session_state:
        st.session_state["thread_id"] = uuid.uuid4().hex
    return st.session_state["thread_id"]


def _answer_prompt(prompt: str) -> None:
    """자연어 요청을 백엔드(chat → 그래프)로 처리하고, 요청과 답변을 대화 기록에 추가한다.

    답변 = 냉장고·장바구니 안내(storage) + 확인 대기 작업(pending) + 레시피 검색 결과(search) + 안내 문장(text) 중 있는 것.
    검색 결과는 정렬된 전체 id 를 저장해 두고, [더보기]는 다시 검색하지 않고 여기서 이어서 꺼낸다.
    실패하면 백엔드가 준 안내 문장을 답변 대신 남긴다.
    아직 확인하지 않은 이전 확인 대기는 그래프가 '넘어감'으로 닫으므로 화면에서도 닫는다.
    """
    chat = st.session_state.setdefault("chat", [])
    for message in chat:
        if message.get("pending", {}).get("status") == "waiting":
            message["pending"]["status"] = "skipped"
    chat.append({"role": "user", "text": prompt})
    try:
        with st.spinner("처리하는 중이에요..."):
            answer = _backend().chat(prompt, _thread_id(), RECIPE_PAGE_SIZE)
    except BackendError as e:
        chat.append({"role": "assistant", "text": str(e)})
        return
    message = {"role": "assistant", "storage": answer["storage_lines"], "text": answer["message"]}
    if answer["waiting"]:
        message["pending"] = {
            "status": "waiting",          # waiting → done / cancelled / skipped
            "actions": answer["pending"],  # [{"action", "place", "items", "notes", "question"}, ...]
            "result": [],                 # 실행 후 결과 문장
            "error": "",                  # 실행 실패 안내 (다시 시도할 수 있도록 waiting 유지)
        }
    if answer["recipe"] is not None:
        message["search"] = {**answer["recipe"], "limit": RECIPE_PAGE_SIZE}
    chat.append(message)


def _confirm_key(message_no: int, action_no: int, item_no: int) -> str:
    """확인 대기 작업의 품목 체크박스 session_state 키."""
    return f"confirm_{message_no}_{action_no}_{item_no}"


def _on_confirm(message_no: int) -> None:
    """확인 대기 작업의 [담기]/[빼기]/[옮기기] 버튼 콜백: 체크된 품목으로 그래프를 재개해 실행하고 결과를 남긴다.

    실행 결과는 그래프가 이번 턴 기억에도 반영한다.
    """
    pending = st.session_state["chat"][message_no]["pending"]
    chosen = [
        [item for i, item in enumerate(a["items"]) if st.session_state.get(_confirm_key(message_no, a_no, i), True)]
        for a_no, a in enumerate(pending["actions"])
    ]
    try:
        result = _backend().resume(_thread_id(), chosen)
    except NoPendingError as e:  # 확인 대기가 남아 있지 않음 (앱을 다시 시작해 기억이 사라진 경우 등)
        pending.update(status="cancelled", error=str(e))
    except BackendError as e:  # MCP 도구 서버 연결이 끊긴 경우 등 — 확인 대기 상태를 유지해 다시 누를 수 있게 한다
        pending["error"] = str(e)
    else:
        pending.update(status="done", result=result["executed"], error="")


def _on_cancel(message_no: int) -> None:
    """확인 대기 작업의 [취소] 버튼 콜백: 그래프를 '취소'로 재개한다 (아무것도 바꾸지 않고 기억에 '취소됨'을 남김)."""
    pending = st.session_state["chat"][message_no]["pending"]
    try:
        _backend().resume(_thread_id(), None)
    except BackendError:  # 재개하지 못해도 실행된 것은 없으므로 화면에서는 취소로 처리한다
        pass
    pending["status"] = "cancelled"


def _render_pending(message_no: int, pending: dict) -> None:
    """확인 대기 작업: 작업마다 질문 + 안내 + 품목 체크박스(기본 모두 체크) → [실행 버튼] [취소].

    실행하거나 취소한 뒤에는 결과 문장(또는 취소·넘어감 안내)만 보여준다.
    """
    if pending["status"] == "done":
        st.markdown("\n".join(f"- {line}" for line in pending["result"]))
        return
    if pending["status"] == "cancelled":
        st.caption(f"{pending['error'] or '취소했어요.'} 장바구니와 냉장고는 그대로예요.")
        return
    if pending["status"] == "skipped":
        st.caption("확인하지 않고 새 요청을 보내서 이 작업은 실행하지 않았어요. 장바구니와 냉장고는 그대로예요.")
        return

    for a_no, action in enumerate(pending["actions"]):
        st.write(f"**{action['question']}**")
        for note in action["notes"]:
            st.caption(note)
        cols = st.columns(ADD_BUTTONS_PER_ROW)
        for i, item in enumerate(action["items"]):
            cols[i % ADD_BUTTONS_PER_ROW].checkbox(item, value=True, key=_confirm_key(message_no, a_no, i))
    if pending["error"]:
        st.error(pending["error"])

    kinds = {a["action"] for a in pending["actions"]}
    label = {"add": "담기", "remove": "빼기", "move": "옮기기"}[kinds.pop()] if len(kinds) == 1 else "실행"
    ok_col, cancel_col = st.columns(2)
    ok_col.button(label, key=f"confirm_{message_no}", type="primary", on_click=_on_confirm, args=(message_no,))
    cancel_col.button("취소", key=f"cancel_{message_no}", on_click=_on_cancel, args=(message_no,))


def _render_search_answer(message_no: int, search: dict, current: dict[str, str]) -> None:
    """자연어 검색 답변 말풍선의 내용: 설명 문장 → 해석한 조건(+ 반영하지 못한 조건) → 레시피 카드 → [더보기]
    → 담으면 좋은 재료."""
    st.write(search["text"])
    st.caption(f"해석한 조건: {search['conditions']}")
    if search.get("unsupported"):
        st.caption(f"검색에 반영하지 못한 조건: {', '.join(search['unsupported'])}")
    ranked_ids, limit = search["ranked_ids"], search["limit"]
    for rank, rec in enumerate(_backend().describe(ranked_ids[:limit], search["owned"]), start=1):
        _render_recipe(rank, rec, f"add_{message_no}_{rec['recipe_id']}", current)
    if limit < len(ranked_ids):
        st.button("더보기", key=f"more_{message_no}", on_click=_on_more, args=(message_no,))
    if search["suggestions"]:
        ingredients = [s["ingredient"] for s in search["suggestions"]]
        _render_add_buttons("담으면 좋은 재료", ingredients, f"add_{message_no}_suggest", current)
        for s in search["suggestions"]:
            st.caption(f"{s['ingredient']}을(를) 담으면 만들 수 있어요: {', '.join(s['recipes'])}")


def _render_recipe_answer(message_no: int, owned: list[str], limit: int, current: dict[str, str]) -> None:
    """추천 답변 말풍선의 내용: 안내 문장 → 레시피 카드 → [더보기]."""
    if not owned:
        st.write("장바구니나 나의 냉장고에 재료를 먼저 담고 다시 [내 재료로 추천]을 눌러주세요.")
        return
    st.write(f"장바구니와 나의 냉장고에 있는 재료 {len(owned)}개로 추천하는 레시피예요.")
    result = _backend().recommend(owned, limit)
    for rank, rec in enumerate(result["recipes"], start=1):
        _render_recipe(rank, rec, f"add_{message_no}_{rec['recipe_id']}", current)
    if limit < result["total"]:
        st.button("더보기", key=f"more_{message_no}", on_click=_on_more, args=(message_no,))


# 예시 알약: 짧은 라벨 → 실제로 보낼 문장. 라벨이 짧아야 한 줄에 들어간다.
EXAMPLE_PROMPTS = {
    "🌶️ 매운 요리": "냉장고 재료로 만들 수 있는 매운 요리",
    "🍲 얼큰한 국물": "비 오는 날 먹을 얼큰한 국물 요리",
    "🫙 양념 냉장고로": "양념류는 다 냉장고에 넣어줘",
    "🛒 장바구니": "장바구니 보여줘",
}


def _on_example() -> None:
    """예시 알약을 누르면 그 라벨에 해당하는 문장을 요청으로 보내고 선택을 비운다."""
    label = st.session_state.get("example_pick")
    if label:
        _answer_prompt(EXAMPLE_PROMPTS[label])
    st.session_state["example_pick"] = None


def _render_chat(items: dict[str, list[str]]) -> None:
    """레시피 추천 칸: 대화 기록(높이 고정, 스크롤) → 예시 알약 + [내 재료로 바로 추천] 한 줄 → 입력창."""
    chat = st.session_state.get("chat", [])
    current = {name: place for place, names in items.items() for name in names}

    with st.container(height=CHAT_HEIGHT):
        if not chat:
            # 빈 상태: 마스코트 + 카피 한 줄 (세로 가운데 쯤에 오도록 위에 여백)
            st.markdown("<div style='height:56px'></div>", unsafe_allow_html=True)
            _, mid, _ = st.columns([2.6, 1, 2.6])
            mid.image(LOGO_FRIDGE, use_container_width=True)
            st.markdown(
                """
                <div style="text-align:center;padding:4px 12px 20px">
                  <p style="margin:10px 0 4px;font-size:18px;font-weight:600">냉장고에 있는 걸로, 오늘 뭐 해 먹지</p>
                  <p style="margin:0;color:#8A7A6E;font-size:14px">아래 예시를 누르거나 직접 적어 주세요.<br>장바구니·냉장고 정리도 말로 됩니다.</p>
                </div>
                """,
                unsafe_allow_html=True,
            )
        for message_no, message in enumerate(chat):
            with st.chat_message(message["role"]):
                if "recipes" in message:
                    recipes = message["recipes"]
                    _render_recipe_answer(message_no, recipes["owned"], recipes["limit"], current)
                    continue
                # 에이전트 답변: 냉장고·장바구니 안내 → 확인 대기 작업 → 레시피 검색 답변 → 안내 문장 (있는 것만)
                if message.get("storage"):
                    st.markdown("\n".join(f"- {line}" for line in message["storage"]))
                if "pending" in message:
                    _render_pending(message_no, message["pending"])
                if "search" in message:
                    _render_search_answer(message_no, message["search"], current)
                if message.get("text"):
                    st.write(message["text"])

    # 예시 알약·빠른 추천·입력창을 대화 기록 바로 아래 한 덩어리로 둔다.
    # (chat_input 은 컨테이너 안에 넣어야 페이지 맨 아래 고정이 풀리고 제자리에 그려진다)
    with st.container():
        pills_col, quick_col = st.columns([3, 1], vertical_alignment="center")
        pills_col.pills("예시", list(EXAMPLE_PROMPTS), key="example_pick", on_change=_on_example,
                        label_visibility="collapsed", help="누르면 그 요청을 바로 보내요")
        quick_col.button(
            "🍳 바로 추천",
            on_click=_on_recommend,
            type="primary",
            use_container_width=True,
            help="장바구니와 냉장고 재료로, 더 필요한 재료가 적은 레시피부터 바로 추천해요 (AI를 거치지 않아요)",
        )
        if prompt := st.chat_input("원하는 레시피를 물어보세요 (예: 냉장고 재료로 만들 수 있는 매운 요리)"):
            _answer_prompt(prompt)
            st.rerun()


def _fridge_by_category(fridge_items: list[str]) -> dict[str, list[str]]:
    """냉장고 품목을 카탈로그 대분류 순서로 묶는다 (냉장고 모형의 칸)."""
    by = {c: [i for i in names if i in fridge_items] for c, names in _catalog().items()}
    return {c: names for c, names in by.items() if names}


def _render_landing(items: dict[str, list[str]]) -> None:
    """시작 페이지: 냉장고 하나만. 눌러서 문을 열면 본문으로 넘어간다."""
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        # 로고 록업(캐릭터 + 워드마크 + 태그라인)을 가운데에, 그 아래 열 수 있는 냉장고
        _, logo_col, _ = st.columns([1.6, 2, 1.6])
        logo_col.image(LOGO_LOCKUP, use_container_width=True)
        n = len(items[FRIDGE])
        caption = f"나의 냉장고에 재료 {n}개 · 장바구니 {len(items[CART])}개" if n or items[CART] else "냉장고가 비어 있어요 — 열고 들어가서 장을 봐요"
        if fridge_view.render(_fridge_by_category(items[FRIDGE]), caption=caption,
                              key=f"fridge_door_{st.session_state.get('landing_no', 0)}") == "open":
            st.session_state["entered"] = True
            st.rerun()


@st.dialog("나의 냉장고", width="large")
def _fridge_dialog(items: dict[str, list[str]]) -> None:
    """[냉장고 보기] 팔업: 냉장고 모형을 띄우고, 눌러서 열면 담긴 걸 본다. 페이지는 안 바뀜다."""
    n = len(items[FRIDGE])
    fridge_view.render(_fridge_by_category(items[FRIDGE]), signal=False, key="fridge_peek",
                       caption=f"재료 {n}개 · 냉장고를 눌러 열고 닫으세요" if n else "비어 있어요")


def main() -> None:
    st.set_page_config(page_title="recipang", page_icon=LOGO_FRIDGE, layout="wide")
    # 사이드바 맨 위와 상단 바의 로고. 사이드바를 접으면 냉장고 캐릭터만 남는다.
    st.logo(LOGO_WORD, icon_image=LOGO_FRIDGE, size="large")

    try:
        items = _backend().items()  # 한 번 그릴 때 한 번만 조회해 사이드바와 카드 버튼이 같이 쓴다
    except BackendError as e:  # Docker 에서 agent-app 이 아직 준비되지 않은 경우 등
        st.error(str(e))
        st.stop()

    # 시작 페이지: 냉장고를 열어야 본문이 나온다
    if not st.session_state.get("entered"):
        _render_landing(items)
        return

    with st.sidebar:
        home_col, peek_col = st.columns(2)
        if home_col.button("🏠 홈으로", use_container_width=True, help="시작 화면으로"):
            st.session_state["entered"] = False
            # 컴포넌트 key 를 바꿔야 직전에 보낸 "open" 값이 남아 바로 다시 들어가버리는 걸 막는다
            st.session_state["landing_no"] = st.session_state.get("landing_no", 0) + 1
            st.rerun()
        if peek_col.button("🧊 냉장고 보기", use_container_width=True, help="담긴 걸 냉장고 모형으로 보기"):
            _fridge_dialog(items)
        st.divider()
        _render_place(CART, items[CART])
        st.divider()
        _render_place(FRIDGE, items[FRIDGE])

    shop_col, chat_col = st.columns([2, 3], gap="large")
    with shop_col:
        st.subheader("🛒 장보기")
        _render_shop()
    with chat_col:
        st.subheader("🍳 레시피 추천")
        _render_chat(items)


main()
