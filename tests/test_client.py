import io
import json
import os
import socket
import unittest
import urllib.error
from unittest.mock import Mock, patch

from gate_reader.client import BASE_URL, MAX_WINDOW, Client, ReaderError, _NoRedirect
from gate_reader.__main__ import main


class Response:
    status = 200

    def __init__(self, url, payload):
        self.url, self.payload = url, payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self):
        return self.url

    def read(self):
        return json.dumps(self.payload).encode()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"READER_TOKEN": "mock-placeholder"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = Client()
        self.client._opener = Mock()
        self.client._opener.open.side_effect = lambda req, **kw: Response(req.full_url, {"ok": True, "data": []})

    def test_fixed_host_get_authorization_and_timeout(self):
        self.client.query("positions", contract="MSFT_USDT")
        req = self.client._opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertTrue(req.full_url.startswith(BASE_URL + "/positions?"))
        self.assertIn("holding=true", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bearer mock-placeholder")
        self.assertNotIn("mock-placeholder", req.full_url)
        self.assertEqual(self.client._opener.open.call_args.kwargs, {"timeout": 20})

    def test_missing_secret_fails_before_request(self):
        del os.environ["READER_TOKEN"]
        with self.assertRaisesRegex(ReaderError, "Configure READER_TOKEN"):
            self.client.query("account")
        self.client._opener.open.assert_not_called()

    def test_health_is_public(self):
        del os.environ["READER_TOKEN"]
        self.client.query("health")
        req = self.client._opener.open.call_args.args[0]
        self.assertIsNone(req.get_header("Authorization"))

    def test_secret_header_injection(self):
        os.environ["READER_TOKEN"] = "mock\nplaceholder"
        with self.assertRaisesRegex(ReaderError, "configuration"):
            self.client.query("account")
        self.client._opener.open.assert_not_called()

    def test_errors_never_include_upstream_or_token(self):
        for error in [socket.timeout("mock-placeholder"),
            urllib.error.URLError("mock-placeholder"),
            urllib.error.HTTPError(BASE_URL, 502, "mock-placeholder", {}, io.BytesIO(b"private-account"))]:
            with self.subTest(error=type(error).__name__):
                self.client._opener.open.side_effect = error
                with self.assertRaises(ReaderError) as caught:
                    self.client.query("account")
                self.assertNotIn("mock-placeholder", str(caught.exception))
                self.assertNotIn("private-account", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_redirect_handler_refuses_every_destination(self):
        handler = _NoRedirect()
        for code in (301, 302, 303, 307, 308):
            with self.assertRaisesRegex(ReaderError, "Redirect refused"):
                handler.redirect_request(None, None, code, "", {}, BASE_URL + "/account")

    def test_redirect_http_error_is_safe(self):
        self.client._opener.open.side_effect = urllib.error.HTTPError(BASE_URL, 302, "private", {}, None)
        with self.assertRaisesRegex(ReaderError, "Redirect refused"):
            self.client.query("account")

    def test_unexpected_final_url(self):
        self.client._opener.open.side_effect = lambda *a, **kw: Response(BASE_URL + "/health", {"ok": True})
        with self.assertRaisesRegex(ReaderError, "Unexpected response URL"):
            self.client.query("account")

    def test_non_2xx_response(self):
        response = Response(BASE_URL + "/account", {"ok": True})
        response.status = 502
        self.client._opener.open.side_effect = lambda *a, **kw: response
        with self.assertRaisesRegex(ReaderError, "502"):
            self.client.query("account")

    def test_invalid_payload(self):
        for payload in [[], {"ok": False, "error": "private"}, {"ok": 1}]:
            self.client._opener.open.side_effect = lambda req, **kw: Response(req.full_url, payload)
            with self.assertRaisesRegex(ReaderError, "reader response"):
                self.client.query("account")

    def test_malformed_json(self):
        response = Response(BASE_URL + "/account", None)
        response.read = lambda: b"invalid"
        self.client._opener.open.side_effect = lambda *a, **kw: response
        with self.assertRaisesRegex(ReaderError, "invalid JSON"):
            self.client.query("account")

    def test_invalid_parameters_fail_without_network(self):
        cases = [("https://invalid.test", {}), ("../account", {}),
            ("account", {"limit": 1}), ("positions", {"limit": 101}),
            ("positions", {"offset": -1}), ("positions", {"limit": True}),
            ("positions", {"holding": "true"}), ("positions", {"contract": "../x"}),
            ("position-history", {}), ("trades", {"from": 0}),
            ("trades", {"from": 2, "to": 1}),
            ("trades", {"from": 0, "to": MAX_WINDOW + 1})]
        for endpoint, params in cases:
            with self.subTest(endpoint=endpoint, params=params), self.assertRaises(ReaderError):
                self.client.query(endpoint, **params)
        self.client._opener.open.assert_not_called()

    def test_timeout_validation(self):
        for timeout in (0, -1, True, float("nan"), float("inf"), "20"):
            with self.assertRaises(ReaderError):
                Client(timeout)

    def test_default_history_24h(self):
        with patch("gate_reader.client.time.time", return_value=200000):
            self.client.query("trades")
        url = self.client._opener.open.call_args.args[0].full_url
        self.assertIn("from=113600", url)
        self.assertIn("to=200000", url)

    def test_pages_stop_on_null_and_freeze_window(self):
        self.client.query = Mock(side_effect=[{"pagination": {"next_offset": 100}},
            {"pagination": {"next_offset": None}}])
        with patch("gate_reader.client.time.time", side_effect=[200000, 300000]):
            self.assertEqual(len(list(self.client.pages("trades"))), 2)
        calls = self.client.query.call_args_list
        self.assertEqual(calls[1].kwargs["offset"], 100)
        self.assertEqual(calls[0].kwargs["to"], calls[1].kwargs["to"])

    def test_pagination_missing_or_not_advancing_fails(self):
        for result in ({}, {"pagination": {}}, {"pagination": {"next_offset": 0}},
                       {"pagination": {"next_offset": "100"}}, {"pagination": {"next_offset": False}}):
            self.client.query = Mock(return_value=result)
            with self.assertRaises(ReaderError):
                list(self.client.pages("trades"))

    def test_history_windows_overlap_preserve_boundary_records(self):
        self.client.query = Mock(side_effect=[{"data": [{"x": 1}, {"x": 2}], "pagination": {"next_offset": None}},
            {"data": [{"x": 2}, {"x": 3}], "pagination": {"next_offset": None}}])
        self.assertEqual(list(self.client.history("trades", 0, MAX_WINDOW + 5)), [{"x": 1}, {"x": 2}, {"x": 2}, {"x": 3}])
        calls = self.client.query.call_args_list
        self.assertEqual((calls[0].kwargs["from"], calls[0].kwargs["to"]), (0, MAX_WINDOW))
        self.assertEqual((calls[1].kwargs["from"], calls[1].kwargs["to"]), (MAX_WINDOW, MAX_WINDOW + 5))

    def test_history_exact_window_and_zero_range(self):
        for end in (0, MAX_WINDOW):
            self.client.query = Mock(return_value={"data": [], "pagination": {"next_offset": None}})
            self.assertEqual(list(self.client.history("trades", 0, end)), [])
            self.client.query.assert_called_once()
            self.assertEqual(self.client.query.call_args.kwargs["from"], 0)
            self.assertEqual(self.client.query.call_args.kwargs["to"], end)

    def test_no_inferred_records_shape(self):
        self.client.pages = Mock(return_value=iter([{"data": {"items": []}}]))
        with self.assertRaisesRegex(ReaderError, "array"):
            list(self.client.history("trades", 0, 1))
        self.client.pages = Mock(return_value=iter([{"data": {"items": [{"mock": 1}]}, "pagination": {"next_offset": None}}]))
        self.assertEqual(list(self.client.history("trades", 0, 1, records_path=("data", "items"))), [{"mock": 1}])

    def test_history_invalid_range_or_override(self):
        for endpoint, start, end, params in [("snapshot", 0, 1, {}), ("trades", 2, 1, {}),
            ("trades", False, 1, {}), ("trades", 0, 1, {"offset": 100})]:
            with self.assertRaises(ReaderError):
                list(self.client.history(endpoint, start, end, **params))

    def test_proxy_handler_uses_environment_https_proxy(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.invalid:8080"}, clear=True):
            client = Client()
            proxies = [h.proxies for h in client._opener.handlers if hasattr(h, "proxies")]
            self.assertTrue(any(p.get("https") == "http://proxy.invalid:8080" for p in proxies))

    def test_cli_missing_secret_safe_failure(self):
        del os.environ["READER_TOKEN"]
        with patch("sys.argv", ["gate_reader", "account"]), patch("sys.stderr", new_callable=io.StringIO) as stderr, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main(), 1)
            self.assertIn("Configure READER_TOKEN", stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")

    def test_history_consumes_all_pages_each_window(self):
        self.client.query = Mock(side_effect=[
            {"data": [{"mock": 1}], "pagination": {"next_offset": 100}},
            {"data": [{"mock": 2}], "pagination": {"next_offset": None}},
            {"data": [{"mock": 2}, {"mock": 3}], "pagination": {"next_offset": None}},
        ])
        self.assertEqual(list(self.client.history("trades", 0, MAX_WINDOW + 1)),
            [{"mock": 1}, {"mock": 2}, {"mock": 2}, {"mock": 3}])
        calls = self.client.query.call_args_list
        self.assertEqual([call.kwargs["offset"] for call in calls], [0, 100, 0])
        self.assertEqual([call.kwargs["from"] for call in calls], [0, 0, MAX_WINDOW])

    def test_identical_content_distinct_events_preserved_in_page_and_across_pages(self):
        # No documented stable identity field: each occurrence may be a separate
        # legitimate event, including occurrences with entirely identical data.
        for endpoint in ("trades", "account-book"):
            self.client.query = Mock(side_effect=[
                {"data": [{"amount": 1}, {"amount": 1}], "pagination": {"next_offset": 100}},
                {"data": [{"amount": 1}], "pagination": {"next_offset": None}}])
            self.assertEqual(list(self.client.history(endpoint, 0, 1)), [{"amount": 1}] * 3)

    def test_context_marks_content_match_without_discarding_or_mutating_records(self):
        record = {"amount": 1, "reader_context": "mock upstream field"}
        self.client.query = Mock(side_effect=[
            {"data": [record, record.copy()], "pagination": {"next_offset": 100}},
            {"data": [record.copy()], "pagination": {"next_offset": None}},
            {"data": [record.copy()], "pagination": {"next_offset": None}}])
        result = list(self.client.history("account-book", 0, MAX_WINDOW + 1, include_context=True))
        self.assertEqual(len(result), 4)
        self.assertTrue(all(entry["record"] == record for entry in result))
        self.assertEqual([entry["reader_context"]["possible_duplicate"] for entry in result], [False, True, True, True])
        self.assertEqual([entry["reader_context"]["offset"] for entry in result], [0, 0, 100, 0])
        self.assertEqual([entry["reader_context"]["index"] for entry in result], [0, 1, 0, 0])
        self.assertEqual(result[-1]["reader_context"]["window_from"], MAX_WINDOW)
        self.assertEqual(result[-1]["reader_context"]["window_to"], MAX_WINDOW + 1)

    def test_excluded_boundary_does_not_fabricate_missing_event(self):
        # Simulate a server excluding the shared boundary from both requests.
        # Client cannot establish completeness or invent the absent event.
        self.client.query = Mock(side_effect=[
            {"data": [{"time": MAX_WINDOW - 1}], "pagination": {"next_offset": None}},
            {"data": [{"time": MAX_WINDOW + 1}], "pagination": {"next_offset": None}}])
        self.assertEqual(list(self.client.history("trades", 0, MAX_WINDOW + 2)),
            [{"time": MAX_WINDOW - 1}, {"time": MAX_WINDOW + 1}])

    def test_cli_history_enables_context(self):
        with patch("sys.argv", ["gate_reader", "trades", "--history", "--from", "0", "--to", "1"]), patch("gate_reader.__main__.Client") as client:
            client.return_value.history.return_value = []
            self.assertEqual(main(), 0)
            client.return_value.history.assert_called_once_with("trades", 0, 1, include_context=True)


if __name__ == "__main__":
    unittest.main()
