"""
recipang — 레시피 벡터 검색 (Qdrant 로컬)
===========================================
레시피 1개를 검색용 문서 1개로 만들어 임베딩하고, Qdrant 로컬 컬렉션에 저장한다.
재료 비교와 속성 조건(요리 종류·맛·강도 등)은 SQL(recipe_db)이 맡고, 벡터 검색은
속성으로 표현하기 어려운 자유로운 표현("비 오는 날", "든든한")을 맡는 보조 색인이다.

두 단계로 나뉜다:
  [색인 — build_index(), recipes.json 이 바뀔 때마다]
    data/recipes.json ─recipe_document()─▶ 검색용 문서 (이름·종류·나라·조리법·맛·상황·주재료를 자연스러운 문장으로)
                      ─TextEmbedding────▶ 384차원 벡터
                      ─Qdrant upsert────▶ local_qdrant/ 컬렉션 "recipes" (payload: recipe_id, name, document)
  [검색 — search(), 질문마다]
    질문 ─임베딩─▶ 코사인 유사도 상위 top_k ─▶ [{"recipe_id", "name", "score"}]
    재료·만드는 법 같은 상세 정보는 recipe_id 로 recipes.db 에서 가져온다.

레시피 검색 (설계안 TODO 5 — retrieve()):
  질문을 '속성 조건(RecipeFilter)'과 '속성으로 표현되지 않는 나머지 표현(text)'으로 나눠 받는다.
    ① SQL 로 먼저 거른다            recipe_db.filter_recipes()  (Q1 이면 내 재료로 만들 수 있는 것만)
    ② 그 안에서 text 와 의미가 가까운 순으로 정렬한다   search(text, recipe_ids=후보)
    ③ Q1 인데 만들 수 있는 레시피가 없으면 담을 재료를 추천한다   recipe_db.suggest_ingredients()

공개 인터페이스: recipe_document(), build_index(), ensure_index(), search(), retrieve(), SearchResult
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchAny, PointStruct, VectorParams

import recipe_db

# ── 핵심 설정값 ─────────────────────────────────────────────────────────
LOCAL_QDRANT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local_qdrant")  # 자동 생성
COLLECTION_NAME = "recipes"
# 한국어를 지원하는 다국어 모델. 입력이 128토큰을 넘으면 잘리므로 검색용 문서를 짧게 유지한다.
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
VECTOR_SIZE = 384  # 위 모델의 출력 차원 — 모델을 바꾸면 이 값도 맞춰야 한다

# 검색용 문서에서 속성 값을 문장으로 바꿀 때 쓰는 표현.
# 모든 문서가 "맛: ... 상황: ..." 같은 같은 틀이면 벡터끼리 너무 비슷해지므로 자연스러운 문장으로 쓴다.
INTENSITY_SENTENCES = {0: "자극적이지 않아요", 1: "자극이 적은 순한 편이에요", 2: "자극적인 편이에요", 3: "아주 자극적이에요"}
METHOD_WORDS = {
    "볶기": "볶아서", "끓이기": "끓여서", "굽기": "구워서", "튀기기": "튀겨서", "부치기": "부쳐서",
    "찌기": "쪄서", "조리기": "졸여서", "무치기": "무쳐서", "조리 안 함": "불 없이 간단히",
}
TAG_WORDS = {"국물": "국물 요리", "한그릇": "한 그릇 식사", "아침": "아침 식사", "가벼운": "가벼운 식사"}  # 나머지는 그대로


@lru_cache(maxsize=1)
def _embedder() -> TextEmbedding:
    """임베딩 모델을 한 번만 로드해 재사용한다 (최초 실행 시 모델을 자동 다운로드)."""
    return TextEmbedding(model_name=EMBEDDING_MODEL)


def _josa(word: str, after_batchim: str, after_vowel: str) -> str:
    """마지막 글자의 받침에 맞춰 조사를 붙인다. 예) _josa("제육볶음", "은", "는") → "제육볶음은"

    '으로/로'는 ㄹ 받침 뒤에도 '로'를 쓴다. 한글이 아니면 받침이 없는 것으로 본다.
    """
    code = ord(word[-1]) - 0xAC00
    final = code % 28 if 0 <= code < 11172 else 0  # 0 = 받침 없음, 8 = ㄹ
    has_batchim = final != 0 and not (after_vowel == "로" and final == 8)
    return word + (after_batchim if has_batchim else after_vowel)


def recipe_document(recipe: dict) -> str:
    """레시피 하나(recipes.json 의 원소)를 검색용 문서(자연스러운 문장)로 만든다.

    양념은 빼고 주재료·부재료만 넣는다 (문서를 짧게 유지하고, 요리를 구분하는 재료에 집중하기 위해).
    예) "제육볶음은 한식 볶음 요리예요. 매콤하고 달콤하고 짭짤한 맛이고, 아주 자극적이에요. 볶아서 만들어요.
         밥반찬, 술안주, 도시락으로 좋아요. 재료는 돼지고기, 양파, 대파, 당근이에요."
    """
    meta = recipe["meta"]
    taste = "하고 ".join(meta["taste_tags"]) + "한"  # ["매콤", "달콤"] → "매콤하고 달콤한"
    tags = ", ".join(TAG_WORDS.get(t, t) for t in meta["tags"])
    ingredients = ", ".join(
        i["catalog"] or i["raw"] for i in recipe["ingredients"] if i["type"] != recipe_db.SEASONING
    )
    sentences = [
        f"{_josa(recipe['name'], '은', '는')} {meta['cuisine']} {meta['dish_type']} 요리예요.",
        f"{taste} 맛이고, {INTENSITY_SENTENCES[meta['intensity']]}.",
        f"{METHOD_WORDS[meta['cooking_method']]} 만들어요.",
    ]
    if tags:
        sentences.append(f"{_josa(tags, '으로', '로')} 좋아요.")
    sentences.append(f"재료는 {_josa(ingredients, '이에요', '예요')}.")
    return " ".join(sentences)


def get_qdrant_client() -> QdrantClient:
    """Docker 없이 완전 로컬 모드로 Qdrant를 실행한다.

    주의: 로컬 모드는 한 번에 하나의 클라이언트만 폴더를 열 수 있으므로, 사용 후 반드시 close() 한다.
    """
    return QdrantClient(path=LOCAL_QDRANT_PATH)


def build_index() -> int:
    """recipes.json 으로 컬렉션을 새로 만든다 (이미 있으면 지우고 다시 만든다).

    Returns:
        색인한 레시피 수.
    """
    with open(recipe_db.DATA_PATH, encoding="utf-8") as f:
        recipes = json.load(f)
    documents = [recipe_document(r) for r in recipes]
    vectors = list(_embedder().embed(documents))  # embed 는 제너레이터라 list 로 모두 계산

    client = get_qdrant_client()
    try:
        if client.collection_exists(COLLECTION_NAME):
            client.delete_collection(COLLECTION_NAME)
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                PointStruct(
                    id=i,
                    vector=vector.tolist(),
                    payload={"recipe_id": r["id"], "name": r["name"], "document": doc},
                )
                for i, (r, doc, vector) in enumerate(zip(recipes, documents, vectors))
            ],
        )
    finally:
        client.close()
    return len(recipes)


def ensure_index() -> None:
    """컬렉션이 없으면 만든다.

    주의: recipes.json 을 고쳤다면 build_index() 를 실행해야 반영된다
         (컬렉션이 있는지만 확인하고, 내용이 바뀌었는지는 확인하지 않기 때문).
    """
    client = get_qdrant_client()
    try:
        exists = client.collection_exists(COLLECTION_NAME)
    finally:
        client.close()
    if not exists:
        build_index()


def search(query: str, top_k: int = 5, recipe_ids: list[str] | None = None) -> list[dict]:
    """질문과 의미가 가까운 레시피 top_k 개를 반환한다.

    Args:
        query:      검색 문장.
        top_k:      반환할 최대 개수.
        recipe_ids: 주면 이 레시피들 안에서만 찾는다 (SQL 로 먼저 거른 후보).

    Returns:
        유사도 높은 순 [{"recipe_id": "gen-060", "name": "제육볶음", "score": 0.61}, ...]
        score 는 코사인 유사도 (클수록 가까움). 관련 없는 질문이어도 항상 top_k 개를 반환한다.
    """
    ensure_index()
    query_vector = list(_embedder().embed([query]))[0].tolist()
    query_filter = None
    if recipe_ids is not None:
        query_filter = Filter(must=[FieldCondition(key="recipe_id", match=MatchAny(any=recipe_ids))])

    client = get_qdrant_client()
    try:
        points = client.query_points(
            collection_name=COLLECTION_NAME, query=query_vector, query_filter=query_filter, limit=top_k
        ).points
    finally:
        client.close()
    return [{"recipe_id": p.payload["recipe_id"], "name": p.payload["name"], "score": p.score} for p in points]


@dataclass
class SearchResult:
    """retrieve() 결과.

    Attributes:
        recipes:     조건에 맞는 레시피 상위 limit 개 (카드 정보, missing 은 owned 기준).
        total:       조건에 맞는 레시피 전체 수 ('더보기'를 보여줄지 판단용).
        ranked_ids:  조건에 맞는 레시피 전체 id (정렬 순서). '더보기'는 다시 검색하지 않고 여기서 이어서 꺼낸다.
        suggestions: Q1 인데 만들 수 있는 레시피가 없을 때, 담으면 조건에 맞는 레시피가 생기는 재료 (많이 생기는 순).
    """

    recipes: list[recipe_db.Recommendation]
    total: int
    ranked_ids: list[str] = field(default_factory=list)
    suggestions: list[recipe_db.IngredientSuggestion] = field(default_factory=list)


def retrieve(
    conditions: recipe_db.RecipeFilter,
    text: str | None,
    owned: list[str],
    use_owned: bool,
    limit: int = 3,
) -> SearchResult:
    """레시피 검색: SQL 조건으로 먼저 거르고, 그 안에서 text 와 의미가 가까운 순으로 정렬한다.

    Args:
        conditions: 속성 조건 (요리 종류·나라·조리법·맛·강도·상황 태그).
        text:       속성으로 표현되지 않는 나머지 표현 (예: "비 오는 날"). 없으면 후보를 id 순서로 둔다.
        owned:      내 재료 (장바구니 + 나의 냉장고). 카드의 '더 필요한 재료' 계산에도 쓴다.
        use_owned:  True 면 Q1 — 내 재료로 만들 수 있는 레시피만 (양념 포함 모든 재료를 갖고 있어야 함).
                    False 면 Q2 — 내 재료와 관계없이 조건만 본다.
        limit:      반환할 레시피 수. '더보기'를 누를 때마다 늘려서 다시 호출한다.
    """
    candidates = recipe_db.filter_recipes(conditions, owned if use_owned else None)
    if text and candidates:
        candidates = [r["recipe_id"] for r in search(text, top_k=len(candidates), recipe_ids=candidates)]
    suggestions = recipe_db.suggest_ingredients(conditions, owned) if use_owned and not candidates else []
    return SearchResult(
        recipes=recipe_db.describe(candidates[:limit], owned),
        total=len(candidates),
        ranked_ids=candidates,
        suggestions=suggestions,
    )


def main() -> None:
    """단독 실행 데모: 색인을 새로 만들고, 의미 검색과 레시피 검색(retrieve) 예시를 출력한다."""
    print(f"색인 생성: 레시피 {build_index()}개 → {LOCAL_QDRANT_PATH} ({COLLECTION_NAME})")
    for query in ["술안주로 좋은 거", "비 오는 날 생각나는 음식"]:
        results = search(query)
        print(f"\n[의미 검색] {query}")
        print("   " + " / ".join(f"{r['name']}({r['score']:.2f})" for r in results))

    owned = ["돼지고기", "양파", "대파", "배추김치", "두부", "간장", "마늘", "고춧가루", "설탕", "식용유"]
    F = recipe_db.RecipeFilter
    examples = [
        ("Q2 매운 볶음 요리", F(tastes=["매콤"], cooking_methods=["볶기"]), None, False),
        ("Q2 비 오는 날 먹을 얼큰한 국물", F(tastes=["매콤"], tags=["국물"]), "비 오는 날", False),
        ("Q2 덜 자극적인 중식", F(cuisines=["중식"], max_intensity=1), None, False),
        ("Q1 내 재료로 술안주", F(tags=["술안주"]), None, True),
        ("Q1 내 재료로 중식", F(cuisines=["중식"]), None, True),
    ]
    print(f"\n내 재료: {owned}")
    for label, conditions, text, use_owned in examples:
        result = retrieve(conditions, text, owned, use_owned, limit=5)
        print(f"\n[{label}] 조건에 맞는 레시피 {result.total}개")
        print("   " + " / ".join(f"{r.name}(더 필요 {len(r.missing)})" for r in result.recipes))
        for s in result.suggestions:
            print(f"   → '{s.ingredient}'을(를) 담으면 {len(s.recipe_ids)}개 가능")


if __name__ == "__main__":
    main()
