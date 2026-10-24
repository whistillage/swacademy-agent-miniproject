"""
recipang — 장바구니 / 나의 냉장고 저장소 (SQLite)
====================================================
사용자가 고른 식자재를 장바구니(cart)와 나의 냉장고(fridge)에 각각 저장한다.
DB는 이 폴더의 recipang.db 파일 하나이고, 장바구니와 냉장고는 테이블을 따로 쓴다.

  recipang.db
  ├── cart   (ingredient TEXT PRIMARY KEY)
  └── fridge (ingredient TEXT PRIMARY KEY)

규칙:
  - 품목 이름만 저장한다. 같은 목록 안에서는 중복되지 않는다 (PRIMARY KEY).
  - 같은 품목은 장바구니와 냉장고 중 한쪽에만 있을 수 있다.
    → add_items() 는 반대쪽에 이미 있는 품목은 담지 않고 AddResult.in_other 로 알려준다.

공개 인터페이스:
  - CART, FRIDGE, PLACE_NAMES      : 담을 곳 식별자와 화면 표시 이름
  - add_items(place, ingredients)  : 여러 품목을 한 번에 담고 AddResult 를 반환
  - remove_item(place, ingredient) : 품목 하나를 뺀다
  - remove_items(place, ingredients)    : 여러 품목을 빼고, 실제로 뺀 품목을 반환
  - move_items(from_place, ingredients) : 반대쪽으로 옮기고, 실제로 옮긴 품목을 반환
  - list_items(place)              : 담긴 품목 목록 (담은 순서)
  - other_place(place)             : 반대쪽 담을 곳 (장바구니 ↔ 냉장고)

DB 위치는 환경변수 RECIPANG_DB_PATH 로 바꿀 수 있다 (MCP 도구 서버 프로세스를 테스트용 DB로 띄울 때 쓴다).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass, field

# ── 핵심 설정값 ─────────────────────────────────────────────────────────
DB_PATH = os.environ.get(  # 없으면 자동 생성
    "RECIPANG_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "recipang.db")
)

CART = "cart"
FRIDGE = "fridge"
# 담을 곳 → 화면 표시 이름. 키가 곧 테이블 이름이다.
PLACE_NAMES: dict[str, str] = {CART: "장바구니", FRIDGE: "나의 냉장고"}


@dataclass
class AddResult:
    """add_items() 의 처리 결과.

    Attributes:
        added:    새로 담긴 품목.
        already:  이미 같은 곳에 있어서 건너뛴 품목.
        in_other: 반대쪽(장바구니 ↔ 냉장고)에 있어서 담지 않은 품목.
    """

    added: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    in_other: list[str] = field(default_factory=list)


def _connect() -> sqlite3.Connection:
    """DB에 연결하고, 테이블이 없으면 만든다."""
    conn = sqlite3.connect(DB_PATH)
    for table in PLACE_NAMES:
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (ingredient TEXT PRIMARY KEY)")
    return conn


def _check_place(place: str) -> None:
    """place 가 CART / FRIDGE 중 하나인지 확인한다.

    테이블 이름은 SQL 파라미터(?)로 넘길 수 없어 f-string 으로 넣기 때문에, 먼저 허용된 값인지 검사한다.

    Raises:
        ValueError: 허용되지 않은 place 일 때.
    """
    if place not in PLACE_NAMES:
        raise ValueError(f"알 수 없는 담을 곳: {place!r} (가능한 값: {list(PLACE_NAMES)})")


def other_place(place: str) -> str:
    """반대쪽 담을 곳을 반환한다 (CART → FRIDGE, FRIDGE → CART)."""
    _check_place(place)
    return FRIDGE if place == CART else CART


def add_items(place: str, ingredients: list[str]) -> AddResult:
    """여러 품목을 place 에 담는다. 이미 있거나 반대쪽에 있는 품목은 건너뛴다.

    Args:
        place:       CART 또는 FRIDGE.
        ingredients: 담을 식자재 이름 목록 (catalog 의 이름).

    Returns:
        AddResult(added, already, in_other)
    """
    other = other_place(place)
    result = AddResult()
    with closing(_connect()) as conn:
        with conn:  # 블록이 정상 종료되면 commit, 예외가 나면 rollback
            here = {row[0] for row in conn.execute(f"SELECT ingredient FROM {place}")}
            there = {row[0] for row in conn.execute(f"SELECT ingredient FROM {other}")}
            for ingredient in ingredients:
                if ingredient in here:
                    result.already.append(ingredient)
                elif ingredient in there:
                    result.in_other.append(ingredient)
                else:
                    conn.execute(f"INSERT INTO {place} (ingredient) VALUES (?)", (ingredient,))
                    here.add(ingredient)  # 같은 요청 안에 중복된 이름이 있어도 한 번만 담기도록
                    result.added.append(ingredient)
    return result


def remove_item(place: str, ingredient: str) -> None:
    """place 에서 품목 하나를 뺀다 (없으면 아무 일도 하지 않음)."""
    _check_place(place)
    with closing(_connect()) as conn:
        with conn:
            conn.execute(f"DELETE FROM {place} WHERE ingredient = ?", (ingredient,))


def remove_items(place: str, ingredients: list[str]) -> list[str]:
    """place 에서 여러 품목을 빼고, 실제로 있어서 뺀 품목을 요청 순서대로 반환한다."""
    _check_place(place)
    with closing(_connect()) as conn:
        with conn:
            here = {row[0] for row in conn.execute(f"SELECT ingredient FROM {place}")}
            removed = [i for i in dict.fromkeys(ingredients) if i in here]  # dict.fromkeys: 순서 유지 중복 제거
            conn.executemany(f"DELETE FROM {place} WHERE ingredient = ?", [(i,) for i in removed])
    return removed


def move_items(from_place: str, ingredients: list[str]) -> list[str]:
    """from_place 에 있는 품목을 반대쪽(장바구니 ↔ 냉장고)으로 옮기고, 실제로 옮긴 품목을 반환한다.

    한 트랜잭션에서 빼고 담으므로 '한쪽에만' 규칙이 깨지지 않는다.
    """
    to_place = other_place(from_place)
    with closing(_connect()) as conn:
        with conn:
            here = {row[0] for row in conn.execute(f"SELECT ingredient FROM {from_place}")}
            moved = [i for i in dict.fromkeys(ingredients) if i in here]
            conn.executemany(f"DELETE FROM {from_place} WHERE ingredient = ?", [(i,) for i in moved])
            conn.executemany(f"INSERT OR IGNORE INTO {to_place} (ingredient) VALUES (?)", [(i,) for i in moved])
    return moved


def list_items(place: str) -> list[str]:
    """place 에 담긴 품목을 담은 순서대로 반환한다."""
    _check_place(place)
    with closing(_connect()) as conn:
        return [row[0] for row in conn.execute(f"SELECT ingredient FROM {place} ORDER BY rowid")]


def main() -> None:
    """단독 실행 데모: 임시 DB에서 담기 / 한쪽에만 규칙 / 빼기를 확인한다 (recipang.db 는 건드리지 않음)."""
    global DB_PATH
    DB_PATH = os.path.join(tempfile.mkdtemp(), "demo.db")

    print(add_items(FRIDGE, ["양파", "달걀"]))
    # 양파는 냉장고에 있어서 in_other, 두 번째 돼지고기는 already
    print(add_items(CART, ["양파", "돼지고기", "돼지고기"]))
    remove_item(FRIDGE, "양파")
    print(add_items(CART, ["양파"]))  # 냉장고에서 뺐으므로 이제 담김

    for place, name in PLACE_NAMES.items():
        print(f"{name}: {list_items(place)}")


if __name__ == "__main__":
    main()
