from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """业务错误：携带稳定 code、HTTP 状态码与按 JSON Pointer 汇总的字段错误。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        fields: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields or {}


def error_body(code: str, message: str, fields: dict[str, str] | None = None) -> dict:
    body: dict = {"error": {"code": code, "message": message}}
    if fields:
        body["error"]["fields"] = fields
    return body


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message, exc.fields),
    )
