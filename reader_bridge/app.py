"""Gate USDT futures reader. Standard library only; fixed GET endpoints only."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE = "https://api.gateio.ws/api/v4/futures/usdt/"
PAGE = {"contract", "limit", "offset"}
HISTORY = PAGE | {"from", "to"}
# Local route -> fixed Gate endpoint, allowed query parameters.
ROUTES = {
    "account": ("accounts", set()),
    "positions": ("positions", {"holding"}),
    "orders": ("orders", PAGE | {"status"}),
    "order-history": ("orders_timerange", HISTORY),
    "trades": ("my_trades_timerange", HISTORY | {"role"}),
    "closed-positions": ("position_close", HISTORY),
    "position-history": ("positions_timerange", HISTORY),
    "account-book": ("account_book", HISTORY | {"type"}),
    "trigger-orders": ("price_orders", PAGE | {"status"}),
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def signature(secret, path, query, timestamp):
    empty_body_hash = hashlib.sha512(b"").hexdigest()
    message = f"GET\n{path}\n{query}\n{empty_body_hash}\n{timestamp}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha512).hexdigest()


def params_for(route, raw_query):
    pairs = parse_qsl(raw_query, keep_blank_values=True, max_num_fields=12)
    params = dict(pairs)
    if len(params) != len(pairs) or params.keys() - ROUTES[route][1]:
        raise ValueError("Duplicate or unsupported query parameter")
    allowed = ROUTES[route][1]
    if "limit" in allowed:
        params.setdefault("limit", "100")
        params.setdefault("offset", "0")
    if "status" in allowed:
        params.setdefault("status", "open")
    if route == "positions":
        params.setdefault("holding", "true")
    if "from" in allowed:
        params.setdefault("to", str(int(time.time())))
        if not re.fullmatch(r"[0-9]{1,10}", params["to"]):
            raise ValueError("to must be a Unix timestamp in seconds")
        params.setdefault("from", str(max(0, int(params["to"]) - 86400)))
    for key, value in params.items():
        if key in {"limit", "offset", "from", "to"}:
            if not re.fullmatch(r"[0-9]{1,10}", value):
                raise ValueError(f"{key} must be a non-negative integer")
            if key == "limit" and not 1 <= int(value) <= 100:
                raise ValueError("limit must be between 1 and 100")
            if key == "offset" and int(value) > 1_000_000:
                raise ValueError("offset is too large")
        elif key == "contract" and not re.fullmatch(r"[A-Z0-9_]{1,40}_USDT", value):
            raise ValueError("contract must look like AVGO_USDT")
        elif key == "status" and value not in {"open", "finished"}:
            raise ValueError("status must be open or finished")
        elif key == "holding" and value not in {"true", "false"}:
            raise ValueError("holding must be true or false")
        elif key == "role" and value not in {"maker", "taker"}:
            raise ValueError("role must be maker or taker")
        elif key == "type" and not re.fullmatch(r"[a-z_]{1,32}", value):
            raise ValueError("Invalid account-book type")
    if "from" in params and not 0 <= int(params["to"]) - int(params["from"]) <= 7 * 86400:
        raise ValueError("Use a time window of at most 7 days per request")
    if route == "position-history" and "contract" not in params:
        raise ValueError("position-history requires contract")
    return params


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward signed credentials to another address.


class GateReader:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret

    def read(self, route, params):
        endpoint = ROUTES[route][0]
        query = urlencode(sorted(params.items()))
        path = "/api/v4/futures/usdt/" + endpoint
        timestamp = str(int(time.time()))
        headers = {
            "KEY": self.key, "Timestamp": timestamp,
            "SIGN": signature(self.secret, path, query, timestamp),
            "Accept": "application/json", "User-Agent": "gate-readonly/1.0",
        }
        result = {"ok": False, "source": "Gate API v4", "endpoint": path,
                  "query": params, "requested_at": utc_now()}
        try:
            request = Request(BASE + endpoint + "?" + query, headers=headers, method="GET")
            with build_opener(NoRedirect()).open(request, timeout=12) as response:
                result["upstream_date"] = response.headers.get("Date")
                data = json.load(response)
            result.update(ok=True, data=data)
            if "limit" in params and isinstance(data, list):
                limit, offset = int(params["limit"]), int(params["offset"])
                # A page is not a complete account history. A full page needs another request.
                result["pagination"] = {
                    "limit": limit, "offset": offset, "count": len(data),
                    "next_offset": offset + len(data) if len(data) >= limit else None,
                    "scope": "one_page_only",
                }
        except HTTPError as exc:
            error = {"kind": "gate_http_error", "http_status": exc.code}
            # Report only Gate's machine label, never echo signed headers/error bodies.
            try:
                label = json.loads(exc.read(8192)).get("label", "")
                if isinstance(label, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", label):
                    error["label"] = label
            except (ValueError, AttributeError):
                pass
            if exc.headers.get("Retry-After"):
                retry = exc.headers["Retry-After"]
                if retry.isdigit():
                    error["retry_after_seconds"] = int(retry)
            result["error"] = error
        except (URLError, TimeoutError, OSError):
            result["error"] = {"kind": "gate_unreachable_or_timeout"}
        except (ValueError, UnicodeError):
            result["error"] = {"kind": "invalid_gate_json"}
        result["received_at"] = utc_now()
        return result

    def snapshot(self):
        # Independent reads, not an atomic account snapshot. Each result has its own time.
        now = int(time.time())
        window = urlencode({"from": now - 86400, "to": now})
        names = [name for name in ROUTES if name != "position-history"]
        def fetch(name):
            query = window if "from" in ROUTES[name][1] else ""
            return name, self.read(name, params_for(name, query))
        with ThreadPoolExecutor(max_workers=4) as pool:
            resources = dict(pool.map(fetch, names))
        return {"ok": all(item["ok"] for item in resources.values()),
                "generated_at": utc_now(), "settle": "usdt",
                "note": "Independent reads; histories cover the last 24h, first page only.",
                "resources": resources}


def query(reader, target):
    parsed = urlsplit(target)
    route = parsed.path.strip("/")
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("Use a local resource name, not a URL")
    if route == "snapshot":
        if parsed.query:
            raise ValueError("snapshot does not accept parameters")
        return reader.snapshot()
    if route not in ROUTES:
        raise KeyError("Unknown read-only resource")
    return reader.read(route, params_for(route, parsed.query))


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass  # No access logs containing account filters or authentication data.

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self.send_json(200, {"ok": True, "service": "gate-readonly", "gate_checked": False})
        auth = self.headers.get_all("Authorization", [])
        expected = "Bearer " + self.server.token
        if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), expected.encode()):
            return self.send_json(401, {"ok": False, "error": "Bearer token required"})
        try:
            route_path = urlsplit(self.path).path
        except ValueError:
            return self.send_json(400, {"ok": False, "error": "Invalid review request"})
        if route_path in {"/review-summary", "/review-trade"}:
            try:
                from review.readonly import query_review, ReviewError
                body = query_review(self.path)
                return self.send_json(200, json.loads(body))
            except Exception as exc:
                # Only our fixed status/message pair may cross the HTTP boundary.
                safe = {(400, 'Invalid review request'), (404, 'Trade not found'),
                        (413, 'Review response too large; use smaller limit'),
                        (503, 'Ledger unavailable')}
                status, message = getattr(exc, 'status', 503), getattr(exc, 'message', 'Ledger unavailable')
                if (status, message) not in safe:
                    status, message = 503, 'Ledger unavailable'
                return self.send_json(status, {"ok": False, "error": message})
        try:
            result = query(self.server.reader, self.path)
            self.send_json(200 if result["ok"] else 502, result)
        except KeyError:
            self.send_json(404, {"ok": False, "error": "Unknown read-only resource"})
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})

    def reject_write(self):
        self.close_connection = True
        self.send_json(405, {"ok": False, "error": "Read-only service: GET only"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = reject_write


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "init-env":
        fd = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write("GATE_API_KEY=\nGATE_API_SECRET=\nREADER_TOKEN=" + secrets.token_hex(32) + "\n")
        print("Created .env (0600). Fill in your Gate read-only key and secret on this server.")
        return 0
    key, secret, token = (os.environ.get(name, "") for name in
                          ("GATE_API_KEY", "GATE_API_SECRET", "READER_TOKEN"))
    if not key or not secret:
        sys.exit("Missing GATE_API_KEY / GATE_API_SECRET")
    reader = GateReader(key, secret)
    if command == "query":
        result = query(reader, sys.argv[2] if len(sys.argv) > 2 else "snapshot")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if command != "serve":
        sys.exit("Usage: app.py [serve | init-env | query RESOURCE]")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        sys.exit("READER_TOKEN must be 32-128 random letters/digits, '_' or '-'")
    server = ThreadingHTTPServer(("0.0.0.0", 8787), Handler)
    server.reader, server.token = reader, token
    print("Gate reader ready on port 8787 (GET only)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
