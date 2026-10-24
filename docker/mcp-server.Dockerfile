# ============================================================================
# mcp-server 이미지: 장바구니·나의 냉장고 MCP 도구 서버 (HTTP transport)
# ----------------------------------------------------------------------------
# 빌드 컨텍스트는 저장소 루트. 도구 서버에 필요한 파일 3개만 복사한다.
# 장바구니·냉장고 DB(recipang.db)는 compose 의 recipang_data 볼륨(/data)에 있다 — agent-app 과 같은 볼륨.
# ============================================================================

FROM python:3.13.16-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY docker/requirements-mcp-server.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_tool_server.py storage.py catalog.py ./

# compose 내부 네트워크(agent-app)에서만 접속한다 (ports 가 아니라 expose)
EXPOSE 8001

# MCP_TRANSPORT=http (compose 가 넣어 줌) → http://0.0.0.0:8001/mcp 로 요청을 기다린다
CMD ["python", "mcp_tool_server.py"]
