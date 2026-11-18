"""Consistent error model. Responses carry both `detail` (v0.6 compatible) and a structured `error` object."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("dir.errors")


class AppError(Exception):
    status = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None,
                 hint: str | None = None, details: Any = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        elif self.code == "VALIDATION_ERROR":
            self.status = 422          # one status for every kind of invalid input
        self.hint = hint
        self.details = details


class NotFound(AppError):
    status, code = 404, "NOT_FOUND"


class Conflict(AppError):
    status, code = 409, "CONFLICT"


class Unavailable(AppError):
    status, code = 503, "UNAVAILABLE"


class Forbidden(AppError):
    status, code = 403, "FORBIDDEN"


def _body(request: Request, status: int, code: str, message: str, hint=None, details=None) -> dict:
    rid = getattr(request.state, "request_id", None)
    err = {"code": code, "message": message, "request_id": rid}
    if hint:
        err["hint"] = hint
    if details is not None:
        err["details"] = details
    return {"detail": message, "error": err}


def install_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id_mw(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id", "")[:40] or uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=_body(request, exc.status, exc.code, exc.message, exc.hint, exc.details))

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        code = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        msg = exc.detail if isinstance(exc.detail, str) else "Request failed"
        resp = JSONResponse(status_code=exc.status_code, content=_body(request, exc.status_code, code, msg))
        for k, v in (exc.headers or {}).items():
            resp.headers[k] = v
        return resp

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        fields = [{"field": ".".join(str(p) for p in e.get("loc", []) if p != "body"), "problem": e.get("msg", "invalid")}
                  for e in exc.errors()[:10]]
        msg = "Invalid request: " + "; ".join(f"{f['field'] or 'request'}: {f['problem']}" for f in fields[:3])
        return JSONResponse(status_code=422, content=_body(request, 422, "VALIDATION_ERROR", msg, details=fields))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", "-")
        log.exception("unhandled error request_id=%s path=%s", rid, request.url.path)
        return JSONResponse(status_code=500, content=_body(request, 500, "INTERNAL_ERROR",
                            "Internal error. The details were logged with the request id."))
