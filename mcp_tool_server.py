"""
recipang — MCP 도구 서버 (장바구니 / 나의 냉장고)
====================================================
Day4 의 FastMCP 패턴을 그대로 쓴다. storage.py 를 감싸서 장바구니(cart)와 나의 냉장고(fridge)를
다루는 도구를 노출한다. handlers.py 의 MCP Client 가 접속한다.

실행 방식 (환경변수 MCP_TRANSPORT 로 고른다):
  stdio (기본, 로컬 실행)
    handlers.py 의 Client(PythonStdioTransport(...)) 가 이 스크립트를 하위 프로세스로 실행
    → 표준입출력(stdin/stdout)으로 JSON-RPC 메시지를 주고받음
    → client.call_tool("add_items", {"place": "cart", "ingredients": ["오징어"]})
    → 아래 add_items() 가 실행되고 반환 dict 가 클라이언트로 전달됨
  http (Docker 의 mcp-server 컨테이너, day5 방식)
    MCP_TRANSPORT=http 이면 http://MCP_SERVER_HOST:MCP_SERVER_PORT/mcp 로 요청을 기다리고,
    handlers.py 는 MCP_SERVER_URL 로 접속한다. 같은 JSON-RPC 메시지가 HTTP 로 오갈 뿐 도구는 같다.

노출 도구 (place 는 "cart" = 장바구니, "fridge" = 나의 냉장고):
  - list_items(place)                    : 담긴 식자재 목록
  - add_items(place, ingredients)        : 담기 (반대쪽에 있는 품목은 담지 않음 — '한쪽에만' 규칙)
  - remove_items(place, ingredients)     : 빼기
  - move_items(from_place, ingredients)  : 반대쪽으로 옮기기 (예: 장바구니 → 냉장고)
"""

import os
import sys
from typing import Literal

from fastmcp import FastMCP

import catalog
import storage

# MCP 서버 인스턴스. 이름은 클라이언트가 서버를 식별할 때 표시된다.
mcp = FastMCP("recipang-tools-server")

Place = Literal["cart", "fridge"]  # 타입 힌트가 입력 스키마가 되므로 이 두 값만 받는다


# @mcp.tool 데코레이터: 함수 이름 → 도구 이름, 타입 힌트 → 입력 스키마, docstring → 도구 설명으로
# 자동 변환되어 클라이언트(및 LLM)에게 노출된다. 그래서 docstring 과 타입 힌트를 정확히 써야 한다.
@mcp.tool
def list_items(place: Place) -> dict:
    """장바구니(cart) 또는 나의 냉장고(fridge)에 담긴 식자재 목록을 담은 순서대로 반환한다.

    Args:
        place: "cart" 또는 "fridge"
    """
    return {"place": place, "items": storage.list_items(place)}


@mcp.tool
def add_items(place: Place, ingredients: list[str]) -> dict:
    """식자재를 장바구니(cart) 또는 나의 냉장고(fridge)에 담는다.

    같은 품목은 장바구니와 냉장고 중 한쪽에만 담을 수 있어서, 반대쪽에 이미 있는 품목은 담지 않는다.

    Args:
        place:       "cart" 또는 "fridge"
        ingredients: 담을 식자재 이름 (카탈로그 이름, 예: ["오징어", "양파"])
    """
    # 반환 형식: {"added": [...], "already": [...], "in_other": [...], "unknown": [...]}
    #   already  = 이미 같은 곳에 있어서 건너뜀 / in_other = 반대쪽에 있어서 담지 않음
    #   unknown  = 카탈로그에 없는 이름이라 담지 않음
    unknown = [i for i in ingredients if catalog.get_category(i) is None]
    result = storage.add_items(place, [i for i in ingredients if i not in unknown])
    return {"added": result.added, "already": result.already, "in_other": result.in_other, "unknown": unknown}


@mcp.tool
def remove_items(place: Place, ingredients: list[str]) -> dict:
    """장바구니(cart) 또는 나의 냉장고(fridge)에서 식자재를 뺀다.

    Args:
        place:       "cart" 또는 "fridge"
        ingredients: 뺄 식자재 이름
    """
    # 반환 형식: {"removed": [...], "not_found": [...]}  — not_found = 원래 없던 품목
    removed = storage.remove_items(place, ingredients)
    return {"removed": removed, "not_found": [i for i in ingredients if i not in removed]}


@mcp.tool
def move_items(from_place: Place, ingredients: list[str]) -> dict:
    """식자재를 반대쪽으로 옮긴다 (cart → fridge 또는 fridge → cart).

    Args:
        from_place:  옮기기 전 위치. "cart" 면 냉장고로, "fridge" 면 장바구니로 옮긴다.
        ingredients: 옮길 식자재 이름
    """
    # 반환 형식: {"moved": [...], "not_found": [...]}  — not_found = from_place 에 없던 품목
    moved = storage.move_items(from_place, ingredients)
    return {"moved": moved, "not_found": [i for i in ingredients if i not in moved]}


if __name__ == "__main__":
    # stdout은 stdio 방식에서 MCP JSON-RPC 메시지 전용이므로, 로그는 반드시 stderr로 출력한다.
    # (stdout 에 print 하면 클라이언트가 이를 프로토콜 메시지로 해석하려다 연결이 깨진다.)
    if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
        # 컨테이너에서는 다른 컨테이너가 접속할 수 있도록 0.0.0.0 에 바인딩해야 한다
        host = os.environ.get("MCP_SERVER_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_SERVER_PORT", "8001"))
        print(f"recipang-tools-server starting (http://{host}:{port}/mcp)...", file=sys.stderr)
        mcp.run(transport="http", host=host, port=port, show_banner=False)
    else:
        print("recipang-tools-server starting (stdio)...", file=sys.stderr)
        mcp.run(show_banner=False)  # transport = stdio. 클라이언트 연결이 끊길 때까지 블로킹 (시작 배너는 끔)
