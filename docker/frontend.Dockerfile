# ============================================================================
# frontend 이미지: Streamlit 화면 (app.py)
# ----------------------------------------------------------------------------
# 빌드 컨텍스트는 저장소 루트. 화면에 필요한 파일 3개만 복사한다 — 백엔드 코드(그래프·검색·DB)는 넣지 않는다.
# app.py 는 AGENT_API_URL 이 있으면 api_client.ApiBackend 로 agent-app API 만 부른다.
# ============================================================================

FROM python:3.13.16-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY docker/requirements-frontend.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py api_client.py errors.py ./

EXPOSE 8501

# --server.address=0.0.0.0: 호스트 브라우저에서 접속할 수 있도록 모든 인터페이스에 바인딩
# --server.headless=true: 컨테이너에는 브라우저가 없으므로 시작할 때 브라우저를 열지 않는다
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
