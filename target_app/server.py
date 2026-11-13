#!/usr/bin/env python3
"""Mock banking application server with fault injection.

Intentionally hostile surface:
- Table-based layouts, no flexbox/grid
- No id, data-testid, or data-cy attributes
- Adjacent <td> text is the only element identifier
- Account detail rendered inside <iframe>
- Fault injection by member ID (see FAULT_TABLE below)
"""

from __future__ import annotations

import html
import http.server
import os
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Optional

STATIC_DIR = Path(__file__).parent / "static"
PORT = int(os.environ.get("PORT", "8080"))

VALID_USERNAME = "operator"
VALID_PASSWORD = "demo123"

# In-memory session store: session_id -> state dict
_sessions: dict = {}
_sessions_lock = threading.Lock()

# Happy-path member data
MEMBERS = {
    "10001": {"name": "Jane Doe",      "status": "Active", "savings": "$1,234.56", "checking": "$456.78"},
    "10002": {"name": "Robert Smith",  "status": "Active", "savings": "$5,678.90", "checking": "$1,200.00"},
    "99998": {"name": "Morgan Lee",    "status": "Active", "savings": "$9,999.00", "checking": "$888.00"},
    "99997": {"name": "Alex Carter",   "status": "Frozen", "savings": "$0.00",     "checking": "$0.00"},
    "99995": {"name": "Maria Nguyen",  "status": "Active", "savings": "$800.00",   "checking": "$200.00"},
    "99994": {"name": "Chris Patel",   "status": "Active", "savings": "$300.00",   "checking": "$100.00"},
    "99991": {"name": "Sam Nguyen",    "status": "Active", "savings": "$2,345.67", "checking": "$890.12"},
}

# FAULT_TABLE — member IDs and their injected scenarios
# 10001  happy path
# 10002  second happy path (validation replay)
# 99999  BUSINESS_OUTCOME: MEMBER_NOT_FOUND
# 99998  NEEDS_HUMAN: supervisor verification interstitial
# 99997  BUSINESS_OUTCOME: ACCOUNT_FROZEN
# 99996  session timeout (fires once per session)
# 99995  RECOVERABLE: known confirm() dialog
# 99994  NEEDS_HUMAN: unknown confirm() dialog
# 99993  HARD_FAILURE: APP_ERROR (HTTP 500)
# 99991  slow-load (1.2 s server delay, tests polling loop)


def _new_session() -> tuple[str, dict]:
    sid = str(uuid.uuid4())
    state = {"logged_in": False, "timeout_fired": False}
    with _sessions_lock:
        _sessions[sid] = state
    return sid, state


def _get_session(cookie_header: Optional[str]) -> tuple:
    if cookie_header:
        for part in cookie_header.split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == "session":
                sid = v.strip()
                with _sessions_lock:
                    if sid in _sessions:
                        return sid, _sessions[sid]
    return _new_session()


class BankingHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silence default access log

    # ------------------------------------------------------------------ helpers

    def _send_html(self, body: str, status: int = 200, extra: Optional[dict] = None):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(encoded)

    def _redirect(self, location: str, set_session: Optional[str] = None):
        self.send_response(302)
        self.send_header("Location", location)
        if set_session:
            self.send_header("Set-Cookie", f"session={set_session}; Path=/; HttpOnly")
        self.end_headers()

    def _serve_static(self, filename: str):
        path = STATIC_DIR / filename
        if not path.exists():
            self._send_html(_page("Not Found", "<p>Page not found.</p>"), 404)
            return
        self._send_html(path.read_text(encoding="utf-8"))

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        return dict(urllib.parse.parse_qsl(raw))

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        query = dict(urllib.parse.parse_qsl(qs))
        sid, session = _get_session(self.headers.get("Cookie"))

        if path in ("/", "/login"):
            self._serve_static("login.html")
        elif path == "/dashboard":
            if not session.get("logged_in"):
                self._redirect("/login")
            else:
                self._serve_static("dashboard.html")
        elif path == "/member/search":
            # GET /member/search: used after supervisor authorization redirect
            if not session.get("logged_in"):
                self._redirect("/login")
                return
            member_id = session.pop("supervisor_authorized_member", None)
            if member_id:
                member = MEMBERS.get(member_id, {"name": "Unknown", "status": "Active"})
                self._send_html(_results_page(member_id, member["name"], member["status"]))
            else:
                self._send_html(_page("Member Search", "<p>No pending search.</p>"))
        elif path == "/account":
            if not session.get("logged_in"):
                self._redirect("/login")
            else:
                self._send_html(_account_page(query.get("member_id", "")))
        elif path == "/account-frame":
            self._send_html(_account_frame(query.get("member_id", "")))
        elif path == "/interstitial":
            self._serve_static("interstitial.html")
        else:
            self._send_html(_page("Not Found", "<p>Page not found.</p>"), 404)

    # ------------------------------------------------------------------ POST

    def do_POST(self):
        path, _, _ = self.path.partition("?")
        body = self._read_body()
        sid, session = _get_session(self.headers.get("Cookie"))

        if path == "/login":
            if (body.get("username") == VALID_USERNAME
                    and body.get("password") == VALID_PASSWORD):
                session["logged_in"] = True
                self._redirect("/dashboard", set_session=sid)
            else:
                self._send_html(_login_error())

        elif path == "/member/search":
            if not session.get("logged_in"):
                self._redirect("/login")
                return
            self._handle_search(body.get("member_id", "").strip(), sid, session)

        elif path == "/supervisor/authorize":
            if not session.get("logged_in"):
                self._redirect("/login")
                return
            # Supervisor granted access — pop the pending member and redirect to search results.
            pending = session.pop("pending_member_id", None)
            if pending:
                session["supervisor_authorized_member"] = pending
            self._redirect("/member/search")

        else:
            self._send_html(_page("Not Found", "<p>Page not found.</p>"), 404)

    # ------------------------------------------------------------------ fault injection

    def _handle_search(self, member_id: str, sid: str, session: dict):

        # HTTP 500 — APP_ERROR
        if member_id == "99993":
            self._send_html(
                _page("Internal Server Error",
                      "<p>An unexpected server error occurred. Please contact support.</p>"),
                500,
            )
            return

        # Session timeout — fires once per session
        if member_id == "99996" and not session.get("timeout_fired"):
            session["timeout_fired"] = True
            session["logged_in"] = False
            self._send_html(_session_expired())
            return

        # Supervisor verification interstitial — NEEDS_HUMAN
        # Store pending_member_id so /supervisor/authorize can retrieve it after handoff.
        if member_id == "99998":
            session["pending_member_id"] = "99998"
            self._redirect("/interstitial")
            return

        # Member not found — BUSINESS_OUTCOME
        if member_id == "99999":
            self._send_html(_not_found(member_id))
            return

        # Account frozen — BUSINESS_OUTCOME
        if member_id == "99997":
            self._send_html(_results_page("99997", "Alex Carter", "Frozen"))
            return

        # Known confirm() dialog — RECOVERABLE
        if member_id == "99995":
            self._send_html(_results_known_dialog(member_id))
            return

        # Unknown confirm() dialog — NEEDS_HUMAN
        if member_id == "99994":
            self._send_html(_results_unknown_dialog(member_id))
            return

        # Slow-load — 1.2s delay before returning results (tests polling loop)
        if member_id == "99991":
            time.sleep(1.2)
            member = MEMBERS.get(member_id)
            if member:
                self._send_html(_results_page(member_id, member["name"], member["status"]))
            else:
                self._send_html(_not_found(member_id))
            return

        # Happy path
        member = MEMBERS.get(member_id)
        if member:
            self._send_html(_results_page(member_id, member["name"], member["status"]))
        else:
            self._send_html(_not_found(member_id))


# ------------------------------------------------------------------ HTML builders
# All pages use table layouts, no ids, no semantic containers.

def _page(title: str, body_content: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><title>{html.escape(title)}</title></head>
<body>
<table cellpadding="8" border="0" width="600">
  <tr><td><font size="4"><b>{html.escape(title)}</b></font></td></tr>
  <tr><td>{body_content}</td></tr>
</table>
</body>
</html>"""


def _login_error() -> str:
    return """<!DOCTYPE html>
<html>
<head><title>Member Services Portal</title></head>
<body>
<form action="/login" method="post">
<table cellpadding="8" border="0">
  <tr><td colspan="2"><font size="4"><b>Member Services Portal</b></font></td></tr>
  <tr><td colspan="2"><font color="red">Invalid username or password. Please try again.</font></td></tr>
  <tr>
    <td>Username</td>
    <td><input type="text" name="username"></td>
  </tr>
  <tr>
    <td>Password</td>
    <td><input type="password" name="password"></td>
  </tr>
  <tr>
    <td></td>
    <td><input type="submit" value="Sign In"></td>
  </tr>
</table>
</form>
</body>
</html>"""


def _not_found(member_id: str) -> str:
    safe_id = html.escape(member_id)
    return f"""<!DOCTYPE html>
<html>
<head><title>Member Search</title></head>
<body>
<table cellpadding="8" border="0" width="600">
  <tr><td><font size="4"><b>Member Search</b></font></td></tr>
  <tr>
    <td><font color="red">No member found with ID {safe_id}. Please verify the member ID and try again.</font></td>
  </tr>
  <tr><td><a href="/dashboard">New Search</a></td></tr>
</table>
</body>
</html>"""


def _results_page(member_id: str, name: str, status: str) -> str:
    safe_id = html.escape(member_id)
    safe_name = html.escape(name)
    safe_status = html.escape(status)
    return f"""<!DOCTYPE html>
<html>
<head><title>Member Search Results</title></head>
<body>
<table cellpadding="8" border="0" width="600">
  <tr><td><font size="4"><b>Member Search Results</b></font></td></tr>
  <tr>
    <td>
      <table border="1" cellpadding="4" cellspacing="0" width="100%">
        <tr>
          <td><b>Member ID</b></td>
          <td><b>Name</b></td>
          <td><b>Status</b></td>
          <td><b>Action</b></td>
        </tr>
        <tr>
          <td>{safe_id}</td>
          <td>{safe_name}</td>
          <td>{safe_status}</td>
          <td><a href="/account?member_id={safe_id}">View Account {safe_id}</a></td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def _account_page(member_id: str) -> str:
    safe_id = html.escape(member_id)
    return f"""<!DOCTYPE html>
<html>
<head><title>Account Details</title></head>
<body>
<table cellpadding="8" border="0" width="640">
  <tr><td><font size="4"><b>Account Details</b></font></td></tr>
  <tr><td>Viewing account for member: {safe_id}</td></tr>
  <tr>
    <td>
      <iframe name="account-frame"
              src="/account-frame?member_id={safe_id}"
              width="580" height="160" frameborder="1">
      </iframe>
    </td>
  </tr>
  <tr><td><a href="/dashboard">Back to Search</a></td></tr>
</table>
</body>
</html>"""


def _account_frame(member_id: str) -> str:
    member = MEMBERS.get(member_id, {"savings": "N/A", "checking": "N/A"})
    safe_savings = html.escape(member["savings"])
    safe_checking = html.escape(member["checking"])
    return f"""<!DOCTYPE html>
<html>
<head><title>Account Balances</title></head>
<body>
<table border="1" cellpadding="6" cellspacing="0">
  <tr>
    <td><b>Account Type</b></td>
    <td><b>Balance</b></td>
    <td><b>Last Transaction</b></td>
  </tr>
  <tr>
    <td>Savings</td>
    <td>{safe_savings}</td>
    <td>2026-11-10</td>
  </tr>
  <tr>
    <td>Checking</td>
    <td>{safe_checking}</td>
    <td>2026-11-11</td>
  </tr>
</table>
</body>
</html>"""


def _session_expired() -> str:
    return """<!DOCTYPE html>
<html>
<head><title>Session Expired</title></head>
<body>
<table cellpadding="8" border="0">
  <tr><td><font size="4"><b>Session Expired</b></font></td></tr>
  <tr><td>Your session has expired due to inactivity. Please sign in again to continue.</td></tr>
  <tr><td><a href="/login">Return to Sign In</a></td></tr>
</table>
</body>
</html>"""


def _results_known_dialog(member_id: str) -> str:
    safe_id = html.escape(member_id)
    return f"""<!DOCTYPE html>
<html>
<head><title>Member Search Results</title></head>
<script>
window.onload = function() {{
  confirm("This account has a pending review. Click OK to continue.");
}};
</script>
<body>
<table cellpadding="8" border="0" width="600">
  <tr><td><font size="4"><b>Member Search Results</b></font></td></tr>
  <tr>
    <td>
      <table border="1" cellpadding="4" cellspacing="0" width="100%">
        <tr>
          <td><b>Member ID</b></td>
          <td><b>Name</b></td>
          <td><b>Status</b></td>
          <td><b>Action</b></td>
        </tr>
        <tr>
          <td>{safe_id}</td>
          <td>Maria Nguyen</td>
          <td>Active</td>
          <td><a href="/account?member_id={safe_id}">View Account {safe_id}</a></td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def _results_unknown_dialog(member_id: str) -> str:
    return """<!DOCTYPE html>
<html>
<head><title>Member Search Results</title></head>
<script>
window.onload = function() {
  confirm("SYSTEM ALERT XK-291: Regulatory hold detected. Manual authorization required before proceeding.");
};
</script>
<body>
<table cellpadding="8" border="0" width="600">
  <tr><td><font size="4"><b>Member Search Results</b></font></td></tr>
  <tr><td>Loading account information...</td></tr>
</table>
</body>
</html>"""


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("", PORT), BankingHandler)
    print(f"Banking app running at http://localhost:{PORT}")
    print(f"Login: {VALID_USERNAME} / {VALID_PASSWORD}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
