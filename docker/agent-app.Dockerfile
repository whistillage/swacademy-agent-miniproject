# ============================================================================
# agent-app 이미지: FastAPI(api.py) + LangGraph 그래프 + 레시피 검색 + MCP 클라이언트
# ----------------------------------------------------------------------------
# 빌드 컨텍스트는 저장소 루트 (compose: build.context: .) — 코드가 루트에 있으므로
# 필요한 파일을 루트 기준 경로로 복사한다. 제외할 파일(.env, venv 등)은 루트의 .dockerignore 에 있다.
# ============================================================================

# 로컬 venv 와 같은 Python 을 패치 버전까지 고정한다 (slim = 불필요한 OS 패키지를 뺀 경량 이미지)
FROM python:3.13.16-slim

# PYTHONUNBUFFERED: print 로그를 바로 내보내 `docker compose logs` 에서 실시간으로 보이게 한다
# FASTEMBED_CACHE_PATH: 임베딩 모델을 받을 위치 (아래 RUN 에서 미리 받아 이미지에 넣는다)
ENV PYTHONUNBUFFERED=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed_cache

WORKDIR /app

# 의존성을 코드보다 먼저 설치 — 코드만 바뀌었을 때는 이 레이어를 캐시에서 재사용한다
COPY docker/requirements-agent-app.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY data/ data/

# 레시피 DB(recipes.db)를 만들고 임베딩 모델(약 0.22GB)을 미리 받아 둔다 → 컨테이너 첫 기동 때 기다리지 않게.
# 레시피 벡터 색인은 qdrant 컨테이너에 저장되므로 여기서가 아니라 기동할 때(api.py 의 lifespan) 만든다.
# data/recipes.json 을 고쳤다면 이미지를 다시 빌드해야 한다 (--build).
RUN python -c "import recipe_db, rag_module; recipe_db.build_db(); rag_module._embedder()"

EXPOSE 8000

# --host 0.0.0.0: 다른 컨테이너(frontend)와 호스트에서 접속할 수 있도록 모든 인터페이스에 바인딩
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
