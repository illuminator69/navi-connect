#!/usr/bin/env python3
"""
A fake Navidrome for the hub's sign-in and user-refresh suites (Worbus P2).

It behaves the way the real one does where the hub leans on it (controller
ruling R10, measured on Navidrome 0.64.2 by lane W, 2026-11-02):

  Subsonic `GET /rest/getUser.view?username=<x>&u=<u>&t=<t>&s=<s>`:
    - the pair is checked first: `t == md5(password + s)` for the user `u`,
      compared without regard to case; a wrong pair or an unknown user is
      `status: failed`, error code 40 (HTTP 200, as Navidrome answers);
    - a user may read only themselves: `username` naming anyone else is code 50,
      admin or not (and an unknown `username` is code 50 too);
    - the answer is the stored, lower-case `username` and `adminRole`.
  `GET /rest/savePlayQueue.view` (the hub's mirror): recorded, answered ok.
  Native API:
    - `POST /auth/login {"username", "password"}` -> 200 {"token": <jwt>, ...},
      401 for a wrong pair; `login_limit` logins in a row answer 429 after it;
      `login_calls` counts every login that arrived, whatever the mode;
    - `GET /api/user` with `x-nd-authorization: Bearer <jwt>`: 401 for a token it
      did not issue (or one revoked by `revoke_tokens()`), 403 for a non-admin's,
      else 200 and a list of {id, userName, name, isAdmin} with `X-Total-Count`
      (the number of users, as Navidrome's REST API sends it). `list_limit` cuts
      the list to its first N rows and leaves `X-Total-Count` the whole number:
      a paginated answer (R21). `unlisted` leaves users out of the list and of
      its count, a complete list without them (they can still log in). Each row's
      `id` is the account's own: kept while its record lives, new for a record
      put in its place (a user deleted and re-created, R38).

Modes (`fake.mode`) for the Subsonic and native routes alike: "ok", "hang"
(answers after `hang_s`, past the hub's 8 s budget), "500".

Usage: `fake = FakeNavidrome({"owner": ("pw", True), "bob": ("pw2", False)})`,
`fake.start()`, `fake.url`, ..., `fake.stop()`.
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import socketserver
import threading
import time
import urllib.parse
import uuid


def _subsonic(payload: dict) -> bytes:
    return json.dumps({"subsonic-response": {"version": "1.16.1", "type": "navidrome",
                                             **payload}}).encode()


def _failed(code: int, message: str) -> bytes:
    return _subsonic({"status": "failed", "error": {"code": code, "message": message}})


class FakeNavidrome:
    def __init__(self, users: dict, hang_s: float = 12.0) -> None:
        # name (lower-cased, as Navidrome stores it) -> {"password", "admin"}
        self.users = {k.lower(): {"password": v[0], "admin": bool(v[1])} for k, v in users.items()}
        self.mode = "ok"
        self.hang_s = hang_s
        self.lock = threading.Lock()
        self.get_user_calls = 0
        self.login_calls = 0
        self.user_list_calls = 0
        self.requests = 0                     # every request that arrived, any route
        self.login_limit: int | None = None   # logins answered before 429s start
        self.list_limit: int | None = None    # /api/user rows returned (R21), None = all
        self.unlisted: set[str] = set()       # users left out of /api/user (and its count)
        self.play_queues: list[dict] = []     # every savePlayQueue: {"u", "ids", ...}
        self._tokens: dict[str, str] = {}     # jwt -> user
        self.server: socketserver.TCPServer | None = None
        self.port = 0

    # ----- control ----------------------------------------------------------- #
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeNavidrome":
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):  # noqa: D102
                pass

            def _send(self, status: int, body: bytes, ctype: str = "application/json",
                      extra: dict | None = None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:  # noqa: BLE001 — the hub gave up waiting
                    pass

            def _mode_gate(self) -> bool:
                if fake.mode == "hang":
                    time.sleep(fake.hang_s)
                if fake.mode == "500":
                    self._send(500, b"<html>oops</html>", "text/html")
                    return False
                return True

            def do_GET(self):  # noqa: N802
                with fake.lock:
                    fake.requests += 1
                path, _, query = self.path.partition("?")
                multi = urllib.parse.parse_qs(query)
                q = {k: v[0] for k, v in multi.items()}
                if path.startswith("/rest/"):
                    if path.startswith("/rest/getUser"):
                        with fake.lock:   # counted on arrival, whatever the mode
                            fake.get_user_calls += 1
                    if not self._mode_gate():
                        return
                    self._send(200, fake._rest(path, q, multi))
                    return
                if path == "/api/user":
                    if not self._mode_gate():
                        return
                    status, body, total = fake._api_user(
                        self.headers.get("x-nd-authorization") or "")
                    self._send(status, body, extra=None if total is None
                               else {"X-Total-Count": str(total)})
                    return
                self._send(404, b'{"error":"not found"}')

            def do_POST(self):  # noqa: N802
                with fake.lock:
                    fake.requests += 1
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.path == "/auth/login":
                    with fake.lock:   # counted on arrival, whatever the mode
                        fake.login_calls += 1
                        calls = fake.login_calls
                    if not self._mode_gate():
                        return
                    status, body = fake._login(raw, calls)
                    self._send(status, body)
                    return
                self._send(404, b'{"error":"not found"}')

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def revoke_tokens(self) -> None:
        with self.lock:
            self._tokens.clear()

    # ----- Subsonic ------------------------------------------------------------ #
    def _authed(self, q: dict) -> str | None:
        u = (q.get("u") or "").lower()
        rec = self.users.get(u)
        if rec is None or not q.get("s") or not q.get("t"):
            return None
        want = hashlib.md5((rec["password"] + q["s"]).encode()).hexdigest()
        return u if q["t"] == want else None

    def _rest(self, path: str, q: dict, multi: dict) -> bytes:
        name = path[len("/rest/"):].removesuffix(".view")
        who = self._authed(q)
        if who is None:
            return _failed(40, "Wrong username or password")
        if name == "getUser":
            asked = (q.get("username") or "").lower()
            if asked != who or asked not in self.users:
                return _failed(50, "User is not authorized for the given operation")
            rec = self.users[asked]
            return _subsonic({"status": "ok", "user": {
                "username": asked, "adminRole": rec["admin"], "scrobblingEnabled": True,
                "settingsRole": True, "streamRole": True, "folder": [1]}})
        if name == "savePlayQueue":
            with self.lock:
                self.play_queues.append({"u": who, "ids": list(multi.get("id", [])),
                                         "current": q.get("current"),
                                         "position": q.get("position")})
            return _subsonic({"status": "ok"})
        return _subsonic({"status": "ok"})

    # ----- native API ---------------------------------------------------------- #
    def _login(self, raw: bytes, calls: int) -> tuple[int, bytes]:
        if self.login_limit is not None and calls > self.login_limit:
            return 429, b'{"error":"too many requests"}'
        try:
            body = json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001
            return 400, b'{"error":"bad request"}'
        user = str(body.get("username") or "").lower()
        rec = self.users.get(user)
        if rec is None or body.get("password") != rec["password"]:
            return 401, b'{"error":"Invalid username or password"}'
        token = "jwt." + os.urandom(12).hex()
        with self.lock:
            self._tokens[token] = user
        return 200, json.dumps({"id": user, "username": user, "name": user,
                                "isAdmin": rec["admin"], "token": token}).encode()

    def _api_user(self, header: str) -> tuple[int, bytes, int | None]:
        with self.lock:
            self.user_list_calls += 1
        token = header[7:] if header.startswith("Bearer ") else ""
        with self.lock:
            user = self._tokens.get(token)
        if user is None:
            return 401, b'{"error":"Unauthorized"}', None
        rec = self.users.get(user)
        if rec is None or not rec["admin"]:
            return 403, b'{"error":"Forbidden"}', None
        # Navidrome's own id per account, as the real list carries it (lane W
        # measured `id`, `userName`, `isAdmin`): stable for a record, new for a new
        # one, so a user deleted and re-created (a new dict under the same name)
        # gets a new id (R38).
        rows = [{"id": r.setdefault("id", str(uuid.uuid4())), "userName": n, "name": n,
                 "isAdmin": r["admin"]}
                for n, r in self.users.items() if n not in self.unlisted]
        total = len(rows)
        if self.list_limit is not None:
            rows = rows[:self.list_limit]
        return 200, json.dumps(rows).encode(), total


def subsonic_pair(password: str, salt: str | None = None) -> tuple[str, str]:
    """(salt, token) as a client mints them: token = md5(password + salt)."""
    salt = salt or os.urandom(6).hex()
    return salt, hashlib.md5((password + salt).encode()).hexdigest()
