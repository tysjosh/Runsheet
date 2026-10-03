"""
uvicorn must have a real WebSocket implementation, declared in requirements.txt.

Staging finding S1: every ``/ws/*`` endpoint and ``/api/fleet/live`` returned
HTTP 404. ``requirements.txt`` pins plain ``uvicorn`` (no ``[standard]`` extra),
which ships no WebSocket library. Without one uvicorn logs "No supported
WebSocket library detected", ignores the Upgrade header and serves the handshake
as a plain GET that matches no HTTP route. Local runs worked only because
``websockets`` sat in dev venvs undeclared, so the first test pins the
declaration itself: an installed-but-undeclared library is exactly the bug.

Validates: staging finding S1.
"""
from __future__ import annotations

import importlib.metadata
import re
import socket
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI

import bootstrap.websockets as ws_bootstrap

REQUIREMENTS = Path(__file__).resolve().parents[2] / "requirements.txt"


def test_requirements_pin_websockets_exactly():
    pins = [
        m.group(1)
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if (m := re.match(r"^websockets==(\S+)$", line.strip()))
    ]
    assert pins, (
        "requirements.txt does not declare websockets; the image then has no "
        "WebSocket library and uvicorn 404s every WS upgrade (S1)"
    )
    # Local/CI parity: the venv the suite runs in must match the pin.
    assert importlib.metadata.version("websockets") == pins[0]


def test_uvicorn_auto_ws_protocol_is_a_real_implementation():
    from uvicorn.protocols.websockets.auto import AutoWebSocketsProtocol

    assert AutoWebSocketsProtocol is not None

    cfg = uvicorn.Config(FastAPI(), ws="auto", lifespan="off", log_level="warning")
    cfg.load()
    assert cfg.ws_protocol_class is not None


def test_unauthenticated_upgrade_is_rejected_by_the_app_not_404(monkeypatch):
    """A real handshake against the real WS routes under a real uvicorn server.

    ``_reject`` closes with 4001 before ``accept()``, which uvicorn sends as an
    HTTP 403 handshake rejection. A 404 means uvicorn had no WS library and
    never handed the upgrade to the app. Loopback only; no SuperTokens core is
    needed because ``_default_ws_verify`` returns ``None`` for an empty token.
    """
    # Another test may have installed a fake verifier; use the default one.
    monkeypatch.setattr(ws_bootstrap, "_ws_session_verifier", None)

    app = FastAPI()
    ws_bootstrap.register_websocket_routes(app)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, ws="auto", lifespan="off", log_level="warning")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"

        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(
                b"GET /ws/agent-activity HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: Upgrade\r\n"
                b"Upgrade: websocket\r\n"
                b"Sec-WebSocket-Version: 13\r\n"
                b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                b"\r\n"
            )
            response = b""
            while b"\r\n" not in response:
                chunk = client.recv(1024)
                if not chunk:
                    break
                response += chunk
        status_line = response.split(b"\r\n", 1)[0].decode("latin-1")

        assert " 404 " not in status_line, (
            f"{status_line!r}: uvicorn did not handle the upgrade (no WS library)"
        )
        assert " 403 " in status_line, status_line
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
