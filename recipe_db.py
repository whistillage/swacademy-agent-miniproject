"""
recipang — 레시피 DB와 추천 조회 (SQLite)
===========================================
정형화된 레시피(data/recipes.json)를 recipes.db 테이블로 옮기고,
내 재료(장바구니 + 나의 냉장고)를 기준으로 레시피를 추천 순서대로 조회한다.

  data/recipes.json ──build_db()──▶ recipes.db
                                    ├── recipes            (id, name, source, dish_type, cuisine, cooking_method, intensity)
                                    ├── recipe_tastes      (recipe_id, taste)   맛 태그 — 레시피당 여러 개
                                    ├── recipe_tags        (recipe_id, tag)     상황 태그 — 레시피당 여러 개
                                    ├── recipe_ingredients (recipe_id, seq, raw, amount, type, catalog)
                                    └── recipe_steps       (recipe_id, step_no, description)

레시피 속성 (recipes.json 의 meta):
  dish_type      요리 종류  밥 / 죽 / 국·탕 / 찌개 / 볶음 / 조림 / 찜 / 구이 / 무침·나물 / 전·튀김 / 면·분식 / 샐러드 / 빵·간식 / 반찬
  cuisine        나라       한식 / 양식 / 중식 / 일식
  cooking_method 조리법     볶기 / 끓이기 / 굽기 / 튀기기 / 부치기 / 찌기 / 조리기 / 무치기 / 조리 안 함 (대표 1개)
  taste_tags     맛 태그    매콤 / 짭짤 / 달콤 / 새콤 / 고소 / 담백 (여러 개)
  intensity      강도       0 담백 ~ 3 자극적 (맵기·짠맛·기름짐 종합)
  tags           상황 태그  국물 / 밥반찬 / 한그릇 / 술안주 / 간식 / 아침 / 도시락 / 가벼운 / 아이 반찬 / 해장 / 손님상 / 야식 (여러 개)

추천 규칙 (설계안 TODO 3):
  - 더 필요한 재료 = 레시피 재료(양념 포함) 중 내 재료에 없는 것.
    카탈로그에 없는 재료(catalog 가 NULL)는 담을 수 없으므로 항상 더 필요한 재료로 센다.
  - 정렬: 더 필요한 재료(양념 포함)가 적은 순
          → 같으면 그중 양념이 아닌 재료(주재료·부재료)가 많은 순 (= 없는 양념이 적은 순)
          → 같으면 id 순.
  - 내 재료를 하나도 쓰지 않는 레시피도 포함한다.
  - Recommendation 의 missing 목록에서는 양념을 뒤쪽에 둔다 (화면 표시 순서).

검색용 조회 (설계안 TODO 5 — rag_module.retrieve() 가 벡터 검색과 묶어서 쓴다):
  - RecipeFilter 로 속성 조건을 받아 SQL 로 거른다.
    한 레시피에 값이 하나인 속성(요리 종류·나라·조리법)은 목록 중 하나라도 일치하면 되고 (OR),
    값이 여러 개인 속성(맛 태그·상황 태그)은 목록을 모두 가져야 한다 (AND). 빈 항목은 조건 없음.
  - 만들 수 있음 = 양념까지 포함해 레시피 재료를 모두 갖고 있음.
  - 담을 재료 추천 = 조건에 맞으면서 딱 1개만 부족한 레시피를 그 부족한 재료별로 센다 (많은 순).

공개 인터페이스:
  - ensure_db()             : recipes.db 가 없으면 recipes.json 으로 만든다 (조회 함수가 자동 호출)
  - build_db()              : recipes.db 를 새로 만든다 (recipes.json 을 고친 뒤 실행)
  - count_recipes()         : 전체 레시피 수 ('더보기' 버튼을 보여줄지 판단용)
  - recommend(owned, limit) : 추천 순서대로 상위 limit 개의 Recommendation
  - RecipeFilter            : 검색용 속성 조건
  - filter_recipes(filter, owned=None)    : 조건에 맞는 레시피 id (owned 를 주면 만들 수 있는 것만)
  - suggest_ingredients(filter, owned)    : 담으면 조건에 맞는 레시피가 가장 많이 생기는 재료
  - describe(recipe_ids, owned)           : id 순서대로 Recommendation (카드 정보)
  - find_recipe(name)                     : 요리 이름으로 레시피를 찾아 (이름, 재료 카탈로그 이름) 반환
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass, field

# ── 핵심 설정값 ─────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "data", "recipes.json")  # 원본 (사람이 고치는 파일)
DB_PATH = os.path.join(BASE_DIR, "recipes.db")               # recipes.json 에서 만들어지는 DB

SEASONING = "양념"  # 정렬 두 번째 기준과 missing 목록 순서(뒤쪽)에 쓰는 재료 구분(type)

# 레시피 속성 값 목록 — recipes.json 의 meta 와 검색 조건(RecipeFilter)이 쓰는 값
DISH_TYPES = ("밥", "죽", "국·탕", "찌개", "볶음", "조림", "찜", "구이", "무침·나물", "전·튀김", "면·분식", "샐러드", "빵·간식", "반찬")
CUISINES = ("한식", "양식", "중식", "일식")
COOKING_METHODS = ("볶기", "끓이기", "굽기", "튀기기", "부치기", "찌기", "조리기", "무치기", "조리 안 함")
TASTES = ("매콤", "짭짤", "달콤", "새콤", "고소", "담백")
TAGS = ("국물", "밥반찬", "한그릇", "술안주", "간식", "아침", "도시락", "가벼운", "아이 반찬", "해장", "손님상", "야식")

_SCHEMA = """
CREATE TABLE recipes (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    source         TEXT NOT NULL,
    dish_type      TEXT,
    cuisine        TEXT,
    cooking_method TEXT,
    intensity      INTEGER        -- 0 담백 ~ 3 자극적
);
CREATE TABLE recipe_tastes (
    recipe_id TEXT NOT NULL REFERENCES recipes(id),
    taste     TEXT NOT NULL,
    PRIMARY KEY (recipe_id, taste)
);
CREATE TABLE recipe_tags (
    recipe_id TEXT NOT NULL REFERENCES recipes(id),
    tag       TEXT NOT NULL,
    PRIMARY KEY (recipe_id, tag)
);
CREATE TABLE recipe_ingredients (
    recipe_id TEXT NOT NULL REFERENCES recipes(id),
    seq       INTEGER NOT NULL,  -- 레시피 안에서 재료 순서
    raw       TEXT NOT NULL,     -- 원본 재료 이름
    amount    TEXT,
    type      TEXT NOT NULL,     -- 주재료 / 부재료 / 양념
    catalog   TEXT,              -- 카탈로그 식자재 이름 (카탈로그에 없으면 NULL)
    PRIMARY KEY (recipe_id, seq)
);
CREATE TABLE recipe_steps (
    recipe_id   TEXT NOT NULL REFERENCES recipes(id),
    step_no     INTEGER NOT NULL,
    description TEXT NOT NULL,
    PRIMARY KEY (recipe_id, step_no)
);
"""

# 재료마다 '내 재료에 있는지(in_owned)'와 '양념인지(is_seasoning)'를 표시하는 임시 뷰.
# 추천 순위(SUM)와 화면에 보여줄 재료 목록이 모두 이 뷰를 쓰므로 규칙은 여기 한 곳에만 있다.
# owned 는 recommend() 가 연결마다 만드는 임시 테이블(내 재료 이름 목록)이다.
_FLAGGED_VIEW = f"""
CREATE TEMP VIEW flagged AS
SELECT recipe_id, seq, raw, amount, type, catalog,
       type = '{SEASONING}' AS is_seasoning,
       catalog IS NOT NULL AND catalog IN (SELECT name FROM owned) AS in_owned
FROM recipe_ingredients
"""


@dataclass
class Recommendation:
    """추천 레시피 하나.

    Attributes:
        recipe_id:   레시피 id (예: "gen-001").
        name:        요리 이름.
        dish_type:   요리 종류 (예: "볶음").
        missing:     더 필요한 재료 (양념 포함, 양념은 뒤쪽). 비어 있으면 바로 만들 수 있다.
        ingredients: 전체 재료 [{"name", "amount", "type"}] (레시피 순서) — 화면의 재료 목록용.
        steps:       만드는 법 (순서대로).

    missing 은 카탈로그 이름이고, 카탈로그에 없는 재료는 원본 이름(raw)이다.
    """

    recipe_id: str
    name: str
    dish_type: str | None
    missing: list[str]
    ingredients: list[dict]
    steps: list[str]


@dataclass
class RecipeFilter:
    """검색용 속성 조건. 빈 항목은 조건 없음 (값 목록은 모듈 docstring 의 '레시피 속성' 참고).

    Attributes:
        dish_types:      요리 종류 — 하나라도 일치 (OR). 예) ["볶음"]
        cuisines:        나라 — 하나라도 일치 (OR). 예) ["중식"]
        cooking_methods: 조리법 — 하나라도 일치 (OR). 예) ["볶기"]
        tastes:          맛 태그 — 모두 가져야 함 (AND). 예) ["매콤", "달콤"]
        exclude_tastes:  맛 태그 — 하나도 없어야 함. 예) "안 매운" → ["매콤"]
        tags:            상황 태그 — 모두 가져야 함 (AND). 예) ["술안주"]
        min_intensity:   강도 하한 (0~3). 예) "아주 자극적인" → 3
        max_intensity:   강도 상한 (0~3). 예) "덜 자극적인" → 1
    """

    dish_types: list[str] = field(default_factory=list)
    cuisines: list[str] = field(default_factory=list)
    cooking_methods: list[str] = field(default_factory=list)
    tastes: list[str] = field(default_factory=list)
    exclude_tastes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    min_intensity: int | None = None
    max_intensity: int | None = None


@dataclass
class IngredientSuggestion:
    """담을 재료 추천 하나.

    Attributes:
        ingredient: 담을 재료 (카탈로그 이름).
        recipe_ids: 이 재료만 담으면 만들 수 있게 되는, 조건에 맞는 레시피 id.
    """

    ingredient: str
    recipe_ids: list[str]


def build_db() -> int:
    """recipes.json 을 읽어 recipes.db 를 새로 만든다.

    임시 파일에 다 만든 뒤 교체하므로, 중간에 실패해도 기존 DB가 깨지지 않는다.

    Returns:
        저장한 레시피 수.
    """
    with open(DATA_PATH, encoding="utf-8") as f:
        recipes = json.load(f)

    tmp_path = DB_PATH + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    with closing(sqlite3.connect(tmp_path)) as conn:
        with conn:
            conn.executescript(_SCHEMA)
            for r in recipes:
                meta = r["meta"]
                conn.execute(
                    "INSERT INTO recipes (id, name, source, dish_type, cuisine, cooking_method, intensity)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        r["id"], r["name"], r["source"],
                        meta.get("dish_type"), meta.get("cuisine"), meta.get("cooking_method"), meta.get("intensity"),
                    ),
                )
                conn.executemany(
                    "INSERT INTO recipe_tastes (recipe_id, taste) VALUES (?, ?)",
                    [(r["id"], taste) for taste in meta.get("taste_tags", [])],
                )
                conn.executemany(
                    "INSERT INTO recipe_tags (recipe_id, tag) VALUES (?, ?)",
                    [(r["id"], tag) for tag in meta.get("tags", [])],
                )
                conn.executemany(
                    "INSERT INTO recipe_ingredients (recipe_id, seq, raw, amount, type, catalog)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (r["id"], seq, i["raw"], i["amount"], i["type"], i["catalog"])
                        for seq, i in enumerate(r["ingredients"], start=1)
                    ],
                )
                conn.executemany(
                    "INSERT INTO recipe_steps (recipe_id, step_no, description) VALUES (?, ?, ?)",
                    [(r["id"], n, step) for n, step in enumerate(r["steps"], start=1)],
                )
    os.replace(tmp_path, DB_PATH)
    return len(recipes)


def ensure_db() -> None:
    """recipes.db 가 없으면 만든다.

    주의: recipes.json 을 고쳤다면 recipes.db 를 지우거나 build_db() 를 실행해야 반영된다
         (파일이 있는지만 확인하고, 내용이 바뀌었는지는 확인하지 않기 때문).
    """
    if not os.path.exists(DB_PATH):
        build_db()


def _connect() -> sqlite3.Connection:
    ensure_db()
    return sqlite3.connect(DB_PATH)


def count_recipes() -> int:
    """전체 레시피 수를 반환한다."""
    with closing(_connect()) as conn:
        return conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]


def _prepare_owned(conn: sqlite3.Connection, owned: Iterable[str]) -> None:
    """연결에 내 재료 임시 테이블(owned)과 그것을 쓰는 flagged 임시 뷰를 만든다."""
    conn.execute("CREATE TEMP TABLE owned (name TEXT PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO owned (name) VALUES (?)", [(name,) for name in owned])
    conn.execute(_FLAGGED_VIEW)


def _filter_sql(f: RecipeFilter) -> tuple[str, list]:
    """RecipeFilter 를 recipes 테이블(별칭 r)에 대한 WHERE 조건식과 파라미터로 바꾼다."""
    clauses, params = ["1"], []
    # 값이 하나인 속성: 목록 중 하나라도 일치 (컬럼 이름은 고정된 값이라 f-string 으로 넣어도 안전)
    for column, values in (("dish_type", f.dish_types), ("cuisine", f.cuisines), ("cooking_method", f.cooking_methods)):
        if values:
            clauses.append(f"r.{column} IN ({', '.join('?' * len(values))})")
            params += values
    # 값이 여러 개인 속성: 목록을 모두 가져야 함 / 제외 목록은 하나도 없어야 함
    for taste in f.tastes:
        clauses.append("EXISTS (SELECT 1 FROM recipe_tastes t WHERE t.recipe_id = r.id AND t.taste = ?)")
        params.append(taste)
    for taste in f.exclude_tastes:
        clauses.append("NOT EXISTS (SELECT 1 FROM recipe_tastes t WHERE t.recipe_id = r.id AND t.taste = ?)")
        params.append(taste)
    for tag in f.tags:
        clauses.append("EXISTS (SELECT 1 FROM recipe_tags g WHERE g.recipe_id = r.id AND g.tag = ?)")
        params.append(tag)
    if f.min_intensity is not None:
        clauses.append("r.intensity >= ?")
        params.append(f.min_intensity)
    if f.max_intensity is not None:
        clauses.append("r.intensity <= ?")
        params.append(f.max_intensity)
    return " AND ".join(clauses), params


# 레시피 r 에서 내 재료에 없는 재료 수 (양념 포함) — flagged 뷰가 있어야 쓸 수 있다
_MISSING_COUNT = "(SELECT COUNT(*) FROM flagged x WHERE x.recipe_id = r.id AND NOT x.in_owned)"


def filter_recipes(f: RecipeFilter, owned: Iterable[str] | None = None) -> list[str]:
    """속성 조건에 맞는 레시피 id 를 id 순서로 반환한다.

    Args:
        f:     속성 조건.
        owned: 주면 그 재료로 만들 수 있는 레시피만 남긴다 (양념 포함 모든 재료를 갖고 있어야 함).
    """
    where, params = _filter_sql(f)
    with closing(_connect()) as conn:
        if owned is not None:
            _prepare_owned(conn, owned)
            where += f" AND {_MISSING_COUNT} = 0"
        return [row[0] for row in conn.execute(f"SELECT r.id FROM recipes r WHERE {where} ORDER BY r.id", params)]


def suggest_ingredients(f: RecipeFilter, owned: Iterable[str], limit: int = 3) -> list[IngredientSuggestion]:
    """담으면 조건에 맞는 레시피를 가장 많이 만들 수 있게 되는 재료를 limit 개 반환한다.

    조건에 맞으면서 딱 1개만 부족한 레시피를 모아, 그 부족한 재료별로 레시피 수를 센다 (많은 순 → 재료 이름 순).
    부족한 재료가 카탈로그에 없으면(담을 수 없으면) 세지 않는다.
    """
    where, params = _filter_sql(f)
    with closing(_connect()) as conn:
        _prepare_owned(conn, owned)
        rows = conn.execute(
            f"""
            SELECT m.catalog, GROUP_CONCAT(r.id)
            FROM recipes r
            JOIN flagged m ON m.recipe_id = r.id AND NOT m.in_owned AND m.catalog IS NOT NULL
            WHERE {where} AND {_MISSING_COUNT} = 1
            GROUP BY m.catalog
            ORDER BY COUNT(*) DESC, m.catalog
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return [IngredientSuggestion(ingredient, sorted(ids.split(","))) for ingredient, ids in rows]


def describe(recipe_ids: list[str], owned: Iterable[str]) -> list[Recommendation]:
    """recipe_ids 순서대로 Recommendation(카드에 필요한 정보)을 만든다. missing 은 owned 기준이다."""
    with closing(_connect()) as conn:
        _prepare_owned(conn, owned)
        return _load_recommendations(conn, recipe_ids)


def find_recipe(name: str) -> tuple[str, list[str]] | None:
    """요리 이름으로 레시피를 찾아 (레시피 이름, 재료의 카탈로그 이름 목록)을 반환한다. 없으면 None.

    공백은 무시한다. 이름이 같은 레시피를 먼저 찾고, 없으면 한쪽 이름이 다른 쪽에 들어 있는 레시피 중
    이름이 가장 짧은 것을 고른다 (예: "오징어볶음 레시피" → 오징어볶음).
    재료는 양념까지 레시피 순서대로이고, 카탈로그에 없는 재료(담을 수 없음)는 뺀다.
    """
    key = name.replace(" ", "")
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT id, name FROM recipes ORDER BY id").fetchall()
        exact = [row for row in rows if row[1].replace(" ", "") == key]
        partial = sorted(
            (row for row in rows if key and (key in row[1].replace(" ", "") or row[1].replace(" ", "") in key)),
            key=lambda row: len(row[1]),
        )
        match = (exact or partial or [None])[0]
        if match is None:
            return None
        ingredients = [
            row[0]
            for row in conn.execute(
                "SELECT catalog FROM recipe_ingredients WHERE recipe_id = ? AND catalog IS NOT NULL ORDER BY seq",
                (match[0],),
            )
        ]
    return match[1], ingredients


def recommend(owned: Iterable[str], limit: int) -> list[Recommendation]:
    """내 재료로 레시피를 추천 순서대로 limit 개 반환한다 (정렬 규칙은 모듈 docstring 참고).

    Args:
        owned: 내 재료 이름 (장바구니 + 나의 냉장고, 카탈로그 이름).
        limit: 반환할 최대 개수. '더보기'를 누를 때마다 늘려서 다시 호출한다.
    """
    with closing(_connect()) as conn:
        _prepare_owned(conn, owned)
        ranked = conn.execute(
            """
            SELECT r.id
            FROM recipes r
            JOIN flagged f ON f.recipe_id = r.id
            GROUP BY r.id
            ORDER BY SUM(NOT f.in_owned), SUM(NOT f.in_owned AND NOT f.is_seasoning) DESC, r.id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return _load_recommendations(conn, [row[0] for row in ranked])


def _load_recommendations(conn: sqlite3.Connection, recipe_ids: list[str]) -> list[Recommendation]:
    """recipe_ids 순서대로 Recommendation 을 만든다 (_prepare_owned 가 먼저 호출돼 있어야 함)."""
    recommendations = []
    for recipe_id in recipe_ids:
        name, dish_type = conn.execute("SELECT name, dish_type FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        rows = conn.execute(
            "SELECT raw, amount, type, catalog, is_seasoning, in_owned FROM flagged WHERE recipe_id = ? ORDER BY seq",
            (recipe_id,),
        ).fetchall()
        # 양념을 뒤로 보낸 순서 (정렬은 안정적이라 같은 그룹 안에서는 레시피 순서가 유지된다)
        seasoning_last = sorted(rows, key=lambda row: row[4])
        steps = [
            row[0]
            for row in conn.execute(
                "SELECT description FROM recipe_steps WHERE recipe_id = ? ORDER BY step_no", (recipe_id,)
            )
        ]
        recommendations.append(
            Recommendation(
                recipe_id=recipe_id,
                name=name,
                dish_type=dish_type,
                missing=[catalog or raw for raw, _, _, catalog, _, in_owned in seasoning_last if not in_owned],
                ingredients=[{"name": raw, "amount": amount, "type": type_} for raw, amount, type_, *_ in rows],
                steps=steps,
            )
        )
    return recommendations


def main() -> None:
    """단독 실행 데모: recipes.db 를 새로 만들고, 예시 재료로 상위 10개 추천을 출력한다."""
    print(f"recipes.db 생성: 레시피 {build_db()}개")

    owned = ["돼지고기", "양파", "대파", "배추김치", "두부", "간장", "마늘", "고춧가루", "설탕", "식용유"]
    print(f"내 재료: {owned}\n")
    for rank, rec in enumerate(recommend(owned, limit=10), start=1):
        print(f"{rank:2d}. {rec.name} ({rec.dish_type}) — 더 필요 {len(rec.missing)}개 {rec.missing}")


if __name__ == "__main__":
    main()
