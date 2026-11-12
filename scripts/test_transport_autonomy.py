"""Offline transport invariants with the pinned httpx/XHS implementation."""
import json
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import xhs_research as r


class RaisingBody(httpx.SyncByteStream):
    def __init__(self, error):
        self.error, self.reads, self.closed = error, 0, False

    def __iter__(self):
        self.reads += 1
        raise self.error
        yield b""

    def close(self):
        self.closed = True


class TransportAutonomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / "synthetic-run"
        self.run.mkdir()
        r.write_json(self.run / "run.json", {"format": r.FORMAT, "directory": str(self.run), "closed": False})
        self.url = "https://edith.xiaohongshu.com/api/sns/web/v2/comment/page?xsec_token=DO_NOT_LOG"
        self.calls = []

    def api(self, handler):
        api = r.client_class(self.run)({"a1": "synthetic", "web_session": "synthetic"})
        api._http.close()
        def tracked(request):
            self.calls.append(request)
            return handler(request)
        # Deliberately permissive defaults prove the adapter enforces its own.
        api._http = httpx.Client(transport=httpx.MockTransport(tracked), follow_redirects=True, timeout=None)
        api._rate_limit_delay = lambda: None
        self.addCleanup(api.close)
        return api

    def test_connect_and_header_read_timeouts_have_narrow_redacted_signal(self):
        for error in (httpx.ConnectTimeout, httpx.ReadTimeout):
            with self.subTest(error=error.__name__):
                self.calls.clear()
                def fail(request):
                    raise error("DO_NOT_LOG credentials", request=request)
                api = self.api(fail)
                with self.assertRaises(r.TransientTransportError) as caught:
                    api._request_with_retry("GET", self.url)
                details = caught.exception.safe_details
                self.assertEqual(details, {"schema": "xhs-transient-transport-v1", "retryable": True,
                    "timeout_kind": error.__name__, "method": "GET", "endpoint": "/api/sns/web/v2/comment/page",
                    "response_received": False, "attempt_count": 1})
                self.assertNotIn("DO_NOT_LOG", str(caught.exception) + json.dumps(details))
                self.assertEqual(len(self.calls), 1)
                self.assertTrue((self.run / "last-request.json").exists())
                self.assertFalse((self.run / "STOP.json").exists())

    def test_write_pool_network_errors_are_not_retryable(self):
        for error in (httpx.WriteTimeout, httpx.PoolTimeout, httpx.ConnectError, httpx.RemoteProtocolError):
            with self.subTest(error=error.__name__):
                self.calls.clear()
                def fail(request): raise error("DO_NOT_LOG", request=request)
                api = self.api(fail)
                with self.assertRaises(r.ResearchError) as caught: api._request_with_retry("GET", self.url)
                self.assertNotIsInstance(caught.exception, r.TransientTransportError)
                self.assertEqual(len(self.calls), 1)
                self.assertNotIn("DO_NOT_LOG", str(caught.exception))

    def test_body_read_timeout_is_not_no_response_timeout(self):
        body = RaisingBody(httpx.ReadTimeout("late body timeout"))
        api = self.api(lambda request: httpx.Response(200, stream=body))
        with self.assertRaises(r.ResearchError) as caught: api._request_with_retry("GET", self.url)
        self.assertNotIsInstance(caught.exception, r.TransientTransportError)
        self.assertIn("after HTTP response", str(caught.exception))
        self.assertEqual(body.reads, 1)
        self.assertTrue(body.closed)
        self.assertEqual(len(self.calls), 1)

    def test_timeout_with_attached_response_is_not_retryable(self):
        def fail(request):
            error = httpx.ReadTimeout("synthetic", request=request)
            error.response = httpx.Response(200, request=request)
            raise error
        api = self.api(fail)
        with self.assertRaises(r.ResearchError) as caught: api._request_with_retry("GET", self.url)
        self.assertNotIsInstance(caught.exception, r.TransientTransportError)

    def test_new_stop_wins_over_transport_timeout(self):
        def fail(request):
            r.write_json(self.run / "STOP.json", {"reason": "synthetic external access stop"})
            raise httpx.ReadTimeout("synthetic", request=request)
        api = self.api(fail)
        with self.assertRaises(r.StopRun): api._request_with_retry("GET", self.url)
        self.assertEqual(len(self.calls), 1)

    def test_restriction_headers_stop_before_stalled_body(self):
        for status in (401, 403, 429, 461, 471):
            with self.subTest(status=status):
                (self.run / "STOP.json").unlink(missing_ok=True)
                self.calls.clear()
                body = RaisingBody(httpx.ReadTimeout("must not read a restricted body"))
                api = self.api(lambda request: httpx.Response(status, stream=body))
                with self.assertRaises(r.StopRun): api._request_with_retry("GET", self.url)
                self.assertEqual(body.reads, 0)
                self.assertTrue(body.closed)
                self.assertIn(str(status), r.read_json(self.run / "STOP.json")["reason"])
                with self.assertRaises(r.StopRun): api._request_with_retry("GET", self.url)
                self.assertEqual(len(self.calls), 1)

    def test_redirect_cannot_contact_next_endpoint(self):
        api = self.api(lambda request: httpx.Response(302, headers={"location": "https://elsewhere.invalid/private"}))
        with self.assertRaises(r.StopRun): api._request_with_retry("GET", self.url, follow_redirects=True)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("unconfirmed", r.read_json(self.run / "STOP.json")["reason"])

    def test_500_is_a_single_nonretryable_http_failure(self):
        api = self.api(lambda request: httpx.Response(503, json={"msg": "unavailable"}))
        with self.assertRaises(r.ResearchError) as caught: api._request_with_retry("GET", self.url)
        self.assertNotIsInstance(caught.exception, r.TransientTransportError)
        self.assertEqual(len(self.calls), 1)

    def test_timeout_cannot_be_disabled_by_caller_and_success_body_stays_readable(self):
        api = self.api(lambda request: httpx.Response(200, json={"success": True, "data": {"comments": [], "has_more": False}}))
        response = api._request_with_retry("GET", self.url, timeout=None)
        self.assertEqual(self.calls[0].extensions["timeout"], r.REQUEST_TIMEOUTS)
        self.assertTrue(response.is_closed)
        self.assertEqual(api._handle_response(response), {"comments": [], "has_more": False})

    def test_api_login_verify_or_ip_block_is_still_stop(self):
        for code in (-100, 300012, 300015, 300017, 300031):
            with self.subTest(code=code):
                (self.run / "STOP.json").unlink(missing_ok=True)
                api = self.api(lambda request: httpx.Response(200, json={"success": False, "code": code}))
                response = api._request_with_retry("GET", self.url)
                with self.assertRaises(r.StopRun): api._handle_response(response)

    def test_optional_prewarm_cannot_swallow_transient_or_access_stop(self):
        api = self.api(lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("synthetic", request=request)))
        with patch("xhs_cli.client_mixins._acquire_search_session", return_value=("synthetic", True)), \
                patch("xhs_cli.client.sign_main_api", return_value={}):
            with self.assertRaises(r.TransientTransportError): api.search_notes("synthetic offline")
        self.assertEqual([c.url.path for c in self.calls], ["/api/sns/web/v1/search/onebox"])

    def test_total_deadline_interrupts_one_send_without_background_retry(self):
        def slow(_request):
            time.sleep(0.4)
            return httpx.Response(200, json={})
        api = self.api(slow)
        original = r.transport_deadline
        started = time.monotonic()
        with patch.object(r, "transport_deadline", lambda: original(0.04)):
            with self.assertRaises(r.TransportDeadlineExceeded): api._request_with_retry("GET", self.url)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_total_deadline_also_bounds_body_and_closes_stream(self):
        class SlowBody(httpx.SyncByteStream):
            closed = False
            def __iter__(self):
                while True:
                    time.sleep(0.01)
                    yield b" "
            def close(self): self.closed = True
        body = SlowBody()
        api = self.api(lambda request: httpx.Response(200, stream=body))
        original = r.transport_deadline
        started = time.monotonic()
        with patch.object(r, "transport_deadline", lambda: original(0.04)):
            with self.assertRaises(r.TransportDeadlineExceeded): api._request_with_retry("GET", self.url)
        self.assertTrue(body.closed)
        self.assertEqual(len(self.calls), 1)
        self.assertLess(time.monotonic() - started, 0.3)

    def test_restriction_recording_happens_after_transport_timer_is_disarmed(self):
        api = self.api(lambda request: httpx.Response(461, json={}))
        original = r.stop
        def checked_stop(run, reason):
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
            original(run, reason)
        with patch.object(r, "stop", checked_stop):
            with self.assertRaises(r.StopRun): api._request_with_retry("GET", self.url)

    def test_existing_timer_and_worker_thread_fail_before_transport(self):
        api = self.api(lambda request: httpx.Response(200, json={}))
        prior = signal.getsignal(signal.SIGALRM)
        signal.setitimer(signal.ITIMER_REAL, 5)
        try:
            with self.assertRaises(r.ResearchError): api._request_with_retry("GET", self.url)
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0)
            self.assertIs(signal.getsignal(signal.SIGALRM), prior)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        errors = []
        def worker():
            try: api._request_with_retry("GET", self.url)
            except r.ResearchError as error: errors.append(error)
        thread = threading.Thread(target=worker); thread.start(); thread.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.calls, [])

    def test_main_thread_alarm_handler_restored_after_success_and_failure(self):
        before = signal.getsignal(signal.SIGALRM)
        for fail in (False, True):
            def handler(_request):
                if fail: raise httpx.ConnectTimeout("synthetic")
                return httpx.Response(200, json={})
            api = self.api(handler)
            try: api._request_with_retry("GET", self.url)
            except r.TransientTransportError: pass
            self.assertIs(signal.getsignal(signal.SIGALRM), before)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
