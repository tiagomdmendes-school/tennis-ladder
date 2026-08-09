"""WSGI entry point.

The standalone server in web.py is all you need to run this on a laptop or a
small VPS. This adapter exists so the same app also runs under gunicorn,
uWSGI, Passenger or PythonAnywhere -- which matters because several of the
cheapest (and free) Python hosts only speak WSGI.

    gunicorn 'ladder.wsgi:application'
    # or point PythonAnywhere's WSGI file at `application` below

Nothing about the app changes: the same router, the same Request objects.
"""

from __future__ import annotations

import io
from typing import Callable, Iterable, List, Optional, Tuple

from .config import CONFIG, Config
from .storage import Database
from .web import COOKIE_NAME, App, Request


class _WSGIHandler:
    """Quacks like the BaseHTTPRequestHandler that Request expects."""

    def __init__(self, environ: dict):
        self.environ = environ
        self.path = environ.get("PATH_INFO", "/")
        query = environ.get("QUERY_STRING", "")
        if query:
            self.path += "?" + query
        self.headers = _Headers(environ)
        self.rfile = environ.get("wsgi.input") or io.BytesIO()
        self.set_cookie: Optional[str] = None

    def log_error(self, fmt: str, *args) -> None:
        stream = self.environ.get("wsgi.errors")
        if stream:
            stream.write((fmt % args) + "\n")


class _Headers:
    """Minimal mapping over the CGI-style header names in `environ`."""

    def __init__(self, environ: dict):
        self.environ = environ

    def get(self, name: str, default=None):
        key = name.upper().replace("-", "_")
        if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            return self.environ.get(key) or default
        return self.environ.get("HTTP_" + key, default)

    def __getitem__(self, name: str):
        value = self.get(name)
        if value is None:
            raise KeyError(name)
        return value


class Application:
    def __init__(self, db: Optional[Database] = None,
                 config: Optional[Config] = None):
        self.config = config or CONFIG
        self.db = db or Database()
        self.db.purge_sessions()
        self.app = App(self.db, self.config)

    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        handler = _WSGIHandler(environ)
        method = environ.get("REQUEST_METHOD", "GET").upper()
        try:
            req = Request(handler, method, self.db)
            response = self.app.dispatch(req)
            req.persist()
        except Exception as exc:                        # noqa: BLE001
            handler.log_error("unhandled: %s", exc)
            from .web import Response
            response = Response("<h1>Something went wrong</h1>", status=500)

        payload = response.body.encode("utf-8")
        headers: List[Tuple[str, str]] = [
            ("Content-Type", response.content_type),
            ("Content-Length", str(len(payload))),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "same-origin"),
        ]
        headers.extend(response.headers)
        if handler.set_cookie:
            secure = "; Secure" if environ.get("wsgi.url_scheme") == "https" else ""
            headers.append((
                "Set-Cookie",
                f"{COOKIE_NAME}={handler.set_cookie}; Path=/; HttpOnly;"
                f" SameSite=Lax{secure}",
            ))
        start_response(f"{response.status} {_REASONS.get(response.status, 'OK')}",
                       headers)
        return [payload]


_REASONS = {200: "OK", 303: "See Other", 400: "Bad Request",
            403: "Forbidden", 404: "Not Found", 500: "Internal Server Error"}

# What WSGI servers look for by default.
application = Application()
