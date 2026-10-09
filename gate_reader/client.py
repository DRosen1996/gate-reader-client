"""Fixed-host GET transport. Never logs credentials or upstream response bodies."""

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://gate-reader.gatelens.site"
ENDPOINTS = frozenset({"health", "account", "positions", "orders", "order-history",
    "trades", "closed-positions", "position-history", "account-book",
    "trigger-orders", "snapshot"})
HISTORY = frozenset({"order-history", "trades", "closed-positions",
    "position-history", "account-book"})
MAX_WINDOW = 7 * 24 * 60 * 60


class ReaderError(Exception):
    """Safe error, excluding credentials, account data and upstream messages."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReaderError("Redirect refused")


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReaderError("Invalid " + name)
    return value


class Client:
    def __init__(self, timeout=20):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ReaderError("Invalid timeout")
        self.timeout = timeout
        # ProxyHandler uses environment HTTPS proxy configuration, including the
        # cloud environment's Network secret substitution proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler(), _NoRedirect())

    def query(self, endpoint, **params):
        if endpoint not in ENDPOINTS:
            raise ReaderError("Unsupported endpoint")
        allowed = set()
        if endpoint not in {"health", "account", "snapshot"}:
            allowed.update({"limit", "offset", "contract"})
        if endpoint in HISTORY:
            allowed.update({"from", "to"})
        if endpoint == "positions":
            allowed.add("holding")
            params.setdefault("holding", True)
        if set(params) - allowed:
            raise ReaderError("Unsupported parameter")
        for key in ("limit", "offset", "from", "to"):
            if key in params:
                _integer(params[key], key, 1 if key == "limit" else 0)
        if params.get("limit", 1) > 100:
            raise ReaderError("limit exceeds 100")
        if "contract" in params:
            import re
            if not isinstance(params["contract"], str) or not re.fullmatch(r"[A-Z0-9]+_USDT", params["contract"]):
                raise ReaderError("Invalid contract")
        if endpoint == "position-history" and "contract" not in params:
            raise ReaderError("position-history requires contract")
        if "holding" in params:
            if not isinstance(params["holding"], bool):
                raise ReaderError("Invalid holding")
            params["holding"] = str(params["holding"]).lower()
        if endpoint in HISTORY:
            if ("from" in params) != ("to" in params):
                raise ReaderError("Supply both from and to")
            if "from" not in params:
                params["to"] = int(time.time())
                params["from"] = max(0, params["to"] - 86400)
            if params["to"] < params["from"] or params["to"] - params["from"] > MAX_WINDOW:
                raise ReaderError("Invalid history window (maximum 7 days)")
        headers = {"Accept": "application/json"}
        if endpoint != "health":
            token = os.environ.get("READER_TOKEN")
            if not token:
                raise ReaderError("Configure READER_TOKEN with the Network secret proxy placeholder")
            if any(ord(c) < 32 or ord(c) > 126 for c in token):
                raise ReaderError("Invalid READER_TOKEN configuration")
            headers["Authorization"] = "Bearer " + token
        url = BASE_URL + "/" + endpoint
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.geturl() != url:
                    raise ReaderError("Unexpected response URL")
                if not 200 <= response.status < 300:
                    raise ReaderError("HTTP status " + str(response.status))
                result = json.loads(response.read())
        except ReaderError:
            raise
        except urllib.error.HTTPError as exc:
            raise ReaderError("Redirect refused" if 300 <= exc.code < 400 else "HTTP status " + str(exc.code)) from None
        except Exception:
            raise ReaderError("Request failed (network, timeout or invalid JSON)") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ReaderError("Invalid or unsuccessful reader response")
        return result

    def pages(self, endpoint, **params):
        """Yield original envelopes; terminate only on explicit null next_offset."""
        if endpoint in {"health", "account", "snapshot"}:
            raise ReaderError("Endpoint does not support pagination")
        params.setdefault("limit", 100)
        params.setdefault("offset", 0)
        # Freeze the default history window across pages.
        if endpoint in HISTORY and "from" not in params and "to" not in params:
            params["to"] = int(time.time())
            params["from"] = max(0, params["to"] - 86400)
        while True:
            result = self.query(endpoint, **params)
            pagination = result.get("pagination")
            if not isinstance(pagination, dict) or "next_offset" not in pagination:
                raise ReaderError("Missing pagination.next_offset")
            following = pagination["next_offset"]
            if following is not None:
                _integer(following, "next_offset")
                if following <= params["offset"]:
                    raise ReaderError("Pagination did not advance")
            yield result
            if following is None:
                return
            params["offset"] = following

    def history(self, endpoint, start, end, *, records_path=("data",), include_context=False, **params):
        """Yield every raw record across overlapping, paginated windows.

        Never infer event identity from content. With include_context=True,
        wrap the untouched record with window/offset provenance and flag prior
        identical JSON as a possible duplicate only. No record is discarded.
        Boundary inclusion semantics are unverified; this is not a complete
        or atomic ledger guarantee.
        """
        if endpoint not in HISTORY:
            raise ReaderError("Endpoint is not historical")
        _integer(start, "start")
        _integer(end, "end")
        if end < start or set(params) & {"from", "to", "offset"}:
            raise ReaderError("Invalid history range or pagination override")
        if not isinstance(records_path, tuple) or not records_path or not all(isinstance(k, str) for k in records_path):
            raise ReaderError("Invalid records_path")
        if not isinstance(include_context, bool):
            raise ReaderError("Invalid include_context")
        seen = set()
        cursor = start
        while True:
            stop = min(cursor + MAX_WINDOW, end)
            offset = 0
            for page in self.pages(endpoint, **params, **{"from": cursor, "to": stop}):
                records = page
                for key in records_path:
                    if not isinstance(records, dict) or key not in records:
                        raise ReaderError("Missing explicit records path")
                    records = records[key]
                if not isinstance(records, list):
                    raise ReaderError("Records path must select an array")
                for index, record in enumerate(records):
                    if include_context:
                        fingerprint = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                        possible_duplicate = fingerprint in seen
                        seen.add(fingerprint)
                        yield {"record": record, "reader_context": {
                            "window_from": cursor, "window_to": stop,
                            "offset": offset, "index": index,
                            "possible_duplicate": possible_duplicate}}
                    else:
                        yield record
                offset = page["pagination"]["next_offset"]
            if stop == end:
                return
            cursor = stop  # overlap boundary; do not guess inclusive semantics
