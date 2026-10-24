"""
LangGraph Studio 진입점 — `langgraph dev` 가 langgraph.json 을 보고 이 파일의 graph 를 불러온다
=============================================================================================
실행 (recipang 폴더에서, langgraph-cli[inmem] 설치 필요 — README 참고):
  langgraph dev
  → 터미널에 뜨는 Studio 주소를 브라우저로 열면 그래프 그림·State·노드별 실행 과정을 볼 수 있다.

app.py 가 쓰는 RecipangAgent 와 다른 점:
  - 체크포인터를 넘기지 않는다. LangGraph 서버가 thread(대화방)·체크포인트·interrupt 재개를 직접 관리한다.
    같은 thread 로 이어 물으면 turns(단기 기억)가 복원된다.
  - MCP 연결을 미리 열지 않는다 (start() 를 부를 곳이 없으므로). 도구가 필요할 때마다 서버를 띄웠다가 닫는다.

입력: {"query": "매운 볶음 요리 추천해줘"}
확인 대기(approval)에서 멈추면 resume 값으로 이어 간다:
  true                     모두 실행
  {"chosen": [["쌀"]]}     작업마다 실행할 품목만
  false                    취소
주의: Streamlit 앱과 동시에 실행하면 로컬 Qdrant 폴더 잠금 오류가 날 수 있다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # graph, handlers 같은 같은 폴더 모듈을 찾을 수 있게

from graph import build_graph  # noqa: E402
from handlers import Handlers  # noqa: E402

graph = build_graph(Handlers())
