"""
recipang — 식자재 담기 / 레시피 추천 화면 (Streamlit)
=======================================================
본문을 좌우 반반으로 나눈다. 왼쪽은 장보기(대분류 탭에서 식자재를 골라 장바구니 / 나의 냉장고에 담기),
오른쪽은 레시피 추천 대화창이다. 장바구니와 나의 냉장고는 사이드바에서 보고 뺀다.

실행 (recipang 폴더에서):
  streamlit run app.py

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

동작:
  - 체크박스는 여러 탭에 걸쳐 고를 수 있고, 담기 버튼을 누르면 고른 품목 전체가 담긴 뒤 체크가 풀린다.
  - 같은 품목은 장바구니/냉장고 중 한쪽에만 담긴다 (storage.add_items 규칙).
    담지 못한 품목은 안내 메시지로 보여준다.
  - 사이드바의 나의 냉장고는 식자재와 양념(SEASONING_CATEGORY 대분류)을 나눠 보여준다 (표시만 나눔, 저장은 같음).
  - 레시피 추천은 대화 형식이다. 답변은 대화 기록에 쌓이고, 답변마다 그 시점의 장바구니 + 냉장고 재료를 저장해 두므로
    순서와 카드 내용은 그 시점 기준으로 유지된다. 답변 안의 [더보기]를 누를 때마다 RECIPE_PAGE_SIZE 개씩 늘어난다.
    · 입력창(자연어): graph.py 의 LangGraph 그래프(RecipangAgent.handle_query)로 보낸다 — 라우터가 레시피 검색 /
      냉장고 / 장바구니를 판단한다. 냉장고·장바구니 조회는 바로 보여주고, 담기·빼기·옮기기가 있으면 그래프가 확인 단계
      (interrupt)에서 멈춘다. 화면은 품목별 체크박스로 확인을 받고, [담기]/[빼기]/[옮기기] 를 누르면 체크된 품목으로
      그래프를 재개해 실행한다 ([취소]면 아무것도 바꾸지 않음). 확인하지 않고 새 요청을 보내면 그 확인은 '넘어감'으로 닫힌다.
      레시피 검색(LLM 해석 → SQL 거르기 → 벡터 정렬 → LLM 설명 문장)은 바로 실행한다.
      직접 누르는 버튼([+ 재료], 장보기 담기, 사이드바 [빼기])은 확인 없이 바로 실행한다.
    · [내 재료로 추천](빠른 검색): LLM 없이 recipe_db.recommend() — 더 필요한 재료가 적은 순.
  - 에이전트(RecipangAgent)는 앱 프로세스에 하나만 만들고(st.cache_resource), 전용 이벤트 루프 스레드에서 MCP 도구
    서버와의 연결을 유지한다. 그래서 도구 서버 기동(약 6초)은 처음 한 번만 기다린다.
  - 레시피 카드는 제목 → 재료 → 만드는 법 → 더 필요한 재료 순서로 그린다.
    더 필요한 재료(양념 포함, 양념은 뒤쪽)와 '담으면 좋은 재료'는 [+ 재료] 버튼으로 바로 장바구니에 담는다.
    지금 장바구니나 냉장고에 있는 재료는 '✓ 재료'(누를 수 없음)로 바뀐다.
  - 대화 기록은 세션 동안만 유지된다.
  - 단기 기억: 브라우저 세션마다 대화방 id(thread_id)를 하나 두고, 그래프의 체크포인터가 대화방별로 최근 5턴
    (memory.WINDOW)을 기억한다 ("그거 말고 덜 매운 걸로", "2번 레시피 재료 담아줘" 같은 후속 요청용).
    기록하는 것: 입력창 요청과 답변, 확인 요청의 결과([담기] 등으로 실행 / [취소] / 넘어감).
    기록하지 않는 것: [내 재료로 추천] 결과, 직접 누르는 버튼으로 담기·빼기, [더보기]로 더 펼친 레시피.
    기억은 앱 프로세스 메모리에 있으므로 앱을 다시 시작하면 사라진다.
"""

from __future__ import annotations

import asyncio
import threading
import uuid

import anthropic
import streamlit as st

import catalog
import recipe_db
import storage
from graph import RecipangAgent  # graph 를 불러오면 router(.env 로드 포함), handlers, memory, rag_module 도 함께 로드된다

COLUMNS_PER_ROW = 3      # 장보기 탭 안에서 체크박스를 한 줄에 몇 개씩 놓을지
RECIPE_PAGE_SIZE = 3     # 추천 답변에 처음에 / [더보기] 한 번에 몇 개씩 보여줄지
ADD_BUTTONS_PER_ROW = 3  # 레시피 카드에서 [+ 재료] 버튼을 한 줄에 몇 개씩 놓을지
CHAT_HEIGHT = 600        # 레시피 추천 대화 기록 영역의 높이(px). 넘치면 이 안에서 스크롤된다
SEASONING_CATEGORY = "양념·오일"  # 나의 냉장고에서 '양념'으로 묶어 보여줄 카탈로그 대분류


def _checkbox_key(ingredient: str) -> str:
    """식자재 체크박스의 session_state 키."""
    return f"pick_{ingredient}"


def _selected_ingredients() -> list[str]:
    """모든 탭에서 체크된 식자재를 카탈로그 순서대로 모은다."""
    return [
        ingredient
        for category in catalog.list_categories()
        for ingredient in catalog.list_ingredients(category)
        if st.session_state.get(_checkbox_key(ingredient))
    ]


def _on_add(place: str) -> None:
    """담기 버튼 콜백: 고른 품목을 담고, 결과 메시지를 남기고, 체크를 푼다.

    콜백은 화면을 다시 그리기 전에 실행되므로, 여기서는 체크박스 상태(session_state)를 바꿀 수 있다.
    """
    selected = _selected_ingredients()
    if not selected:
        st.session_state["messages"] = [("warning", "담을 식자재를 먼저 골라주세요.")]
        return

    result = storage.add_items(place, selected)
    name = storage.PLACE_NAMES[place]
    other_name = storage.PLACE_NAMES[storage.other_place(place)]
    messages = []
    if result.added:
        messages.append(("success", f"{name}에 담았습니다: {', '.join(result.added)}"))
    if result.already:
        messages.append(("info", f"이미 {name}에 있습니다: {', '.join(result.already)}"))
    if result.in_other:
        messages.append(("warning", f"{other_name}에 있어서 담지 않았습니다: {', '.join(result.in_other)}"))
    st.session_state["messages"] = messages

    for ingredient in selected:
        st.session_state[_checkbox_key(ingredient)] = False


def _render_items(place: str, items: list[str]) -> None:
    """품목마다 이름과 [빼기] 버튼을 한 줄로 그린다."""
    for ingredient in items:
        name_col, button_col = st.columns([3, 1])
        name_col.write(ingredient)
        button_col.button(
            "빼기",
            key=f"remove_{place}_{ingredient}",
            on_click=storage.remove_item,
            args=(place, ingredient),
        )


def _render_place(place: str) -> None:
    """사이드바에 장바구니 또는 냉장고 목록을 [빼기] 버튼과 함께 그린다.

    나의 냉장고는 식자재와 양념(SEASONING_CATEGORY 대분류)을 나눠서 보여준다.
    """
    items = storage.list_items(place)
    st.subheader(f"{storage.PLACE_NAMES[place]} ({len(items)})")
    if not items:
        st.caption("비어 있음")
        return
    if place != storage.FRIDGE:
        _render_items(place, items)
        return

    seasonings = [i for i in items if catalog.get_category(i) == SEASONING_CATEGORY]
    foods = [i for i in items if i not in seasonings]
    for label, group in (("식자재", foods), ("양념", seasonings)):
        if group:
            st.markdown(f"**{label}** ({len(group)})")
            _render_items(place, group)


def _render_shop() -> None:
    """장보기 칸: 담기 결과 메시지, 대분류 탭과 체크박스, 담기 버튼."""
    # 직전 담기 결과 메시지 (한 번 보여주고 지운다)
    for level, text in st.session_state.pop("messages", []):
        getattr(st, level)(text)

    categories = catalog.list_categories()
    for tab, category in zip(st.tabs(categories), categories):
        with tab:
            cols = st.columns(COLUMNS_PER_ROW)
            for i, ingredient in enumerate(catalog.list_ingredients(category)):
                cols[i % COLUMNS_PER_ROW].checkbox(ingredient, key=_checkbox_key(ingredient))

    # 다른 탭에서 고른 품목은 화면에 안 보이므로 선택 목록을 따로 보여준다
    selected = _selected_ingredients()
    st.caption(f"선택: {', '.join(selected)}" if selected else "선택한 식자재가 없습니다.")

    cart_col, fridge_col = st.columns(2)
    cart_col.button("장바구니에 담기", on_click=_on_add, args=(storage.CART,), type="primary")
    fridge_col.button("냉장고에 담기", on_click=_on_add, args=(storage.FRIDGE,))


def _on_add_to_cart(ingredient: str) -> None:
    """레시피 카드의 [+ 재료] 버튼 콜백: 재료 하나를 장바구니에 담는다."""
    storage.add_items(storage.CART, [ingredient])


def _render_add_buttons(label: str, items: list[str], key_prefix: str, current: dict[str, str]) -> None:
    """재료마다 버튼을 그린다.

    Args:
        label:      버튼 줄 위에 보여줄 제목 (예: "더 필요한 재료").
        items:      재료 이름 목록.
        key_prefix: 버튼 key 접두어 (답변·레시피마다 달라야 함).
        current:    지금 장바구니/냉장고에 있는 재료 → 담을 곳(CART/FRIDGE).
                    여기 있는 재료는 '✓ 재료'로, 없으면 [+ 재료](장바구니에 담기)로 그린다.
    """
    st.write(label)
    cols = st.columns(ADD_BUTTONS_PER_ROW)
    for i, name in enumerate(items):
        col = cols[i % ADD_BUTTONS_PER_ROW]
        key = f"{key_prefix}_{i}"
        if name in current:
            col.button(f"✓ {name}", key=key, disabled=True, help=f"{storage.PLACE_NAMES[current[name]]}에 있음")
        elif catalog.get_category(name) is None:
            col.button(name, key=key, disabled=True, help="카탈로그에 없는 재료라 담을 수 없음")
        else:
            col.button(f"+ {name}", key=key, on_click=_on_add_to_cart, args=(name,), help="장바구니에 담기")


def _render_recipe(rank: int, rec: recipe_db.Recommendation, key_prefix: str, current: dict[str, str]) -> None:
    """추천 레시피 카드 하나를 그린다: 제목 → 재료 → 만드는 법 → 더 필요한 재료.

    key_prefix / current 는 _render_add_buttons 참고.
    """
    with st.container(border=True):
        need = f"{len(rec.missing)}개 더 필요" if rec.missing else "바로 만들 수 있음"
        st.markdown(f"**{rank}. {rec.name}** · {rec.dish_type} · {need}")
        st.markdown("**재료**\n\n" + "\n".join(f"- {i['name']} {i['amount']} ({i['type']})" for i in rec.ingredients))
        st.markdown("**만드는 법**\n\n" + "\n".join(f"{n}. {step}" for n, step in enumerate(rec.steps, start=1)))
        if rec.missing:
            _render_add_buttons("더 필요한 재료", rec.missing, key_prefix, current)


def _on_recommend() -> None:
    """[내 재료로 추천] 버튼 콜백: 대화 기록에 내 요청과 추천 답변을 추가한다.

    답변에는 누른 시점의 내 재료(owned)를 저장해 두므로, 그 답변의 순서와 카드 내용은
    이후에 재료가 바뀌어도 유지된다 (카드에서 재료를 담아도 목록이 재정렬되지 않도록).
    """
    owned = storage.list_items(storage.CART) + storage.list_items(storage.FRIDGE)
    chat = st.session_state.setdefault("chat", [])
    chat.append({"role": "user", "text": "지금 재료로 레시피 추천해줘"})
    chat.append({"role": "assistant", "recipes": {"owned": owned, "limit": RECIPE_PAGE_SIZE}})


def _on_more(message_no: int) -> None:
    """답변 안의 [더보기] 버튼 콜백: 그 답변의 카드를 한 페이지만큼 늘린다."""
    message = st.session_state["chat"][message_no]
    (message.get("recipes") or message["search"])["limit"] += RECIPE_PAGE_SIZE


@st.cache_resource(show_spinner="에이전트를 준비하는 중이에요 (처음 한 번, 몇 초 걸려요)...")
def _agent_runtime() -> tuple[asyncio.AbstractEventLoop, object]:
    """에이전트(graph.RecipangAgent)와 그 전용 이벤트 루프(별도 스레드)를 앱 프로세스에 하나만 만든다.

    MCP 도구 서버를 처음 한 번만 띄우고 연결을 유지해서, 요청마다 서버 기동(약 6초)을 기다리지 않게 한다.
    MCP 연결은 그 연결을 연 이벤트 루프에서만 쓸 수 있으므로, 모든 요청을 이 루프로 보낸다.
    그래프의 체크포인터(대화 기억, 확인 대기 상태)도 이 객체 안에 있다 — 세션마다 thread_id 로 구분한다.
    주의: graph.py, handlers.py 를 고쳤다면 앱을 다시 시작해야 반영된다 (이 객체가 계속 재사용되므로).
    """
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True, name="recipang-agent-loop").start()
    agent = RecipangAgent()
    asyncio.run_coroutine_threadsafe(agent.start(), loop).result()
    return loop, agent


def _thread_id() -> str:
    """이 브라우저 세션의 대화방 id — 그래프의 체크포인터가 이 id 로 대화 기억과 확인 대기 상태를 저장한다."""
    if "thread_id" not in st.session_state:
        st.session_state["thread_id"] = uuid.uuid4().hex
    return st.session_state["thread_id"]


def _answer_prompt(prompt: str) -> None:
    """자연어 요청을 그래프(RecipangAgent.handle_query)로 처리하고, 요청과 답변을 대화 기록에 추가한다.

    답변 = 냉장고·장바구니 안내(storage) + 확인 대기 작업(pending) + 레시피 검색 결과(search) + 안내 문장(text) 중 있는 것.
    검색 결과는 정렬된 전체 id 를 저장해 두고, [더보기]는 다시 검색하지 않고 여기서 이어서 꺼낸다.
    오류는 종류별로 안내 문장을 답변 대신 남긴다.
    아직 확인하지 않은 이전 확인 대기는 그래프가 '넘어감'으로 닫으므로 화면에서도 닫는다.
    """
    chat = st.session_state.setdefault("chat", [])
    for message in chat:
        if message.get("pending", {}).get("status") == "waiting":
            message["pending"]["status"] = "skipped"
    chat.append({"role": "user", "text": prompt})
    try:
        loop, agent = _agent_runtime()
        with st.spinner("처리하는 중이에요..."):
            future = asyncio.run_coroutine_threadsafe(
                agent.handle_query(prompt, thread_id=_thread_id(), page_size=RECIPE_PAGE_SIZE), loop
            )
            answer = future.result()
    except anthropic.APIConnectionError:
        chat.append({"role": "assistant", "text": "AI 서버에 연결하지 못했어요. 인터넷 연결을 확인하고 다시 시도해 주세요."})
    except anthropic.RateLimitError:
        chat.append({"role": "assistant", "text": "요청이 많아 지금은 처리하지 못했어요. 잠시 뒤에 다시 시도해 주세요."})
    except anthropic.APIStatusError as e:
        chat.append({"role": "assistant", "text": f"AI 요청이 실패했어요 (오류 코드 {e.status_code}). API 키 설정을 확인해 주세요."})
    except anthropic.AnthropicError as e:  # API 키가 없을 때 등
        chat.append({"role": "assistant", "text": f"AI를 호출하지 못했어요: {e}"})
    except Exception as e:  # MCP 도구 서버 연결이 끊긴 경우 등 — 다음 요청에서 새로 연결하도록 비운다 (대화 기억도 사라짐)
        _agent_runtime.clear()
        chat.append({"role": "assistant", "text": f"요청을 처리하지 못했어요 ({type(e).__name__}: {e}). 다시 시도해 주세요."})
    else:
        message = {"role": "assistant", "storage": answer.storage_lines, "text": answer.message}
        if answer.waiting:
            message["pending"] = {
                "status": "waiting",  # waiting → done / cancelled / skipped
                "actions": answer.pending,  # [{"action", "place", "items", "notes", "question"}, ...]
                "result": [],  # 실행 후 결과 문장
                "error": "",   # 실행 실패 안내 (다시 시도할 수 있도록 waiting 유지)
            }
        if answer.recipe is not None:
            message["search"] = {**answer.recipe, "limit": RECIPE_PAGE_SIZE}
        chat.append(message)


def _confirm_key(message_no: int, action_no: int, item_no: int) -> str:
    """확인 대기 작업의 품목 체크박스 session_state 키."""
    return f"confirm_{message_no}_{action_no}_{item_no}"


def _resume(chosen: list[list[str]] | None):
    """확인 대기로 멈춘 그래프를 이어서 실행한다 (chosen: 작업마다 체크한 품목, None 이면 취소)."""
    loop, agent = _agent_runtime()
    return asyncio.run_coroutine_threadsafe(agent.resume(_thread_id(), chosen), loop).result()


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
        result = _resume(chosen)
    except RuntimeError as e:  # 확인 대기가 남아 있지 않음 (앱을 다시 시작해 기억이 사라진 경우 등)
        pending.update(status="cancelled", error=str(e))
    except Exception as e:  # MCP 도구 서버 연결이 끊긴 경우 등 — 확인 대기 상태를 유지해 다시 누를 수 있게 한다
        pending["error"] = f"실행하지 못했어요 ({type(e).__name__}: {e}). 다시 눌러 주세요."
    else:
        pending.update(status="done", result=result.executed, error="")


def _on_cancel(message_no: int) -> None:
    """확인 대기 작업의 [취소] 버튼 콜백: 그래프를 '취소'로 재개한다 (아무것도 바꾸지 않고 기억에 '취소됨'을 남김)."""
    pending = st.session_state["chat"][message_no]["pending"]
    try:
        _resume(None)
    except Exception:  # noqa: BLE001 — 재개하지 못해도 실행된 것은 없으므로 화면에서는 취소로 처리한다
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
    for rank, rec in enumerate(recipe_db.describe(ranked_ids[:limit], search["owned"]), start=1):
        _render_recipe(rank, rec, f"add_{message_no}_{rec.recipe_id}", current)
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
    for rank, rec in enumerate(recipe_db.recommend(owned, limit), start=1):
        _render_recipe(rank, rec, f"add_{message_no}_{rec.recipe_id}", current)
    if limit < recipe_db.count_recipes():
        st.button("더보기", key=f"more_{message_no}", on_click=_on_more, args=(message_no,))


def _render_chat() -> None:
    """레시피 추천 칸: 대화 기록(높이 고정, 스크롤) → [내 재료로 추천] 빠른 검색 버튼 → 입력창."""
    chat = st.session_state.get("chat", [])
    current = {name: place for place in storage.PLACE_NAMES for name in storage.list_items(place)}

    with st.container(height=CHAT_HEIGHT):
        if not chat:
            st.caption(
                "레시피 추천이나 장바구니·냉장고 정리를 자유롭게 말해 보세요. "
                "예: '냉장고 재료로 만들 수 있는 매운 요리', '양념류는 다 냉장고에 넣어줘', "
                "'오징어볶음 재료 장바구니에 담아줘', '장바구니에 있는 거 다 냉장고로 옮겨줘'. "
                "아래 [내 재료로 추천]을 누르면 장바구니와 냉장고 재료로 바로 추천해 드려요."
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

    st.button(
        "내 재료로 추천",
        on_click=_on_recommend,
        type="primary",
        help="장바구니와 냉장고 재료로, 더 필요한 재료가 적은 레시피부터 바로 추천해요 (AI를 거치지 않아요)",
    )
    if prompt := st.chat_input("원하는 레시피를 물어보세요 (예: 냉장고 재료로 만들 수 있는 매운 요리)"):
        _answer_prompt(prompt)
        st.rerun()


def main() -> None:
    st.set_page_config(page_title="recipang", layout="wide")
    st.title("recipang")

    with st.sidebar:
        _render_place(storage.CART)
        st.divider()
        _render_place(storage.FRIDGE)

    shop_col, chat_col = st.columns(2, gap="large")
    with shop_col:
        st.subheader("장보기")
        _render_shop()
    with chat_col:
        st.subheader("레시피 추천")
        _render_chat()


main()
