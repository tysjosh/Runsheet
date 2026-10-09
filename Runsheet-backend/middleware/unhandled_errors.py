"""
Unhandled-exception middleware that sits INSIDE CORS.

Why this exists
---------------
``errors/handlers.py`` registers ``app.add_exception_handler(Exception, ...)``.
Starlette routes a handler for bare ``Exception`` (or status 500) to
``ServerErrorMiddleware``, which wraps the whole stack *outside*
``CORSMiddleware``. So every unexpected 500 reached the browser without
``Access-Control-Allow-Origin`` and the UI reported a network error
("Failed to fetch") instead of the error envelope. Root cause:
``.agents/tasks/ui-revamp-2026-10-08/audit.md`` §(i-b).

This pure-ASGI middleware is added last by
``bootstrap.middleware.register_at_import``, which ``main.py`` calls
immediately before adding CORS, so it is the layer just inside CORS. It catches any
``Exception`` escaping the app, logs it with the request id, and answers with
the standard envelope. CORS then adds its headers on the way out.

``AppException``, ``HTTPException`` and request-validation errors never reach
here: Starlette's ``ExceptionMiddleware`` (innermost) handles them first. The
``Exception`` handler in ``errors/handlers.py`` stays registered as the
last-resort fallback for anything raised outside this layer.
"""
import logging
import uuid

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from errors.codes import ErrorCode
from errors.handlers import _query_value_error_in_chain, _query_value_error_response

logger = logging.getLogger(__name__)

#: Same wording as ``errors.handlers.handle_unexpected_exception``.
GENERIC_MESSAGE = "An unexpected error occurred. Please try again later."

_REQUEST_ID_HEADER = "x-request-id"


def _request_id(scope: Scope) -> str:
    """The id ``RequestIDMiddleware`` stored, else the client's header, else new."""
    state = scope.get("state") or {}
    request_id = state.get("request_id") if isinstance(state, dict) else None
    if request_id:
        return str(request_id)
    for name, value in scope.get("headers") or []:
        if name.lower() == b"x-request-id" and value:
            return value.decode("latin-1")
    return str(uuid.uuid4())


class UnhandledErrorMiddleware:
    """Turn an escaped ``Exception`` into a 500 JSON envelope inside CORS."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Once headers are sent there is no way to send a different
            # response; let ServerErrorMiddleware deal with it.
            if response_started:
                raise
            request_id = _request_id(scope)
            request = Request(scope)
            # Keep the N5 behaviour of the old handler: a control character in
            # a filter value is the client's fault and answers 400.
            found = _query_value_error_in_chain(exc)
            if found is not None:
                response = _query_value_error_response(request, request_id, found)
            else:
                logger.error(
                    "Unexpected error occurred",
                    extra={
                        "error_code": ErrorCode.INTERNAL_ERROR.value,
                        "request_id": request_id,
                        "path": scope.get("path"),
                        "method": scope.get("method"),
                        "exception_type": type(exc).__name__,
                    },
                    exc_info=exc,
                )
                # Never echo the exception text to the client.
                response = JSONResponse(
                    status_code=500,
                    content={
                        "error_code": ErrorCode.INTERNAL_ERROR.value,
                        "message": GENERIC_MESSAGE,
                        "details": {},
                        "request_id": request_id,
                    },
                )
            response.headers[_REQUEST_ID_HEADER] = request_id
            await response(scope, receive, send)
