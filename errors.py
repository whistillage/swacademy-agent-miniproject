"""
recipang — 백엔드 호출 오류 (화면이 잡아서 안내 문장으로 보여 준다)
=================================================================
로컬 실행(backend.LocalBackend)과 Docker 실행(api_client.ApiBackend)이 같은 예외를 던지므로,
화면(app.py)은 어느 쪽이든 이 두 가지만 잡으면 된다.
이 파일은 다른 모듈을 불러오지 않는다 — frontend 컨테이너에도 그대로 들어간다.
"""


class BackendError(Exception):
    """백엔드 호출 실패. 메시지는 화면에 그대로 보여 줄 수 있는 문장이다."""


class NoPendingError(BackendError):
    """확인을 기다리는 작업이 없음 (이미 처리했거나, 서버를 다시 시작해 대화 기억이 사라진 경우)."""
