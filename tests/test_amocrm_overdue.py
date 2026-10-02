"""Offline tests; real requests/urllib3, mocked HTTP, no credentials or network."""
import json
import unittest
from email.utils import formatdate
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
import requests
from urllib3.exceptions import MaxRetryError
import amocrm_overdue as app

NOW = 1700000000

def lead(lid, status=123, **extra):
    return dict(id=lid, name=f"Сделка {lid}", status_id=status,
                responsible_user_id=100, **extra)

def task(tid, lid, due, completed=False, kind="leads"):
    return dict(id=tid, entity_id=lid, complete_till=due,
                is_completed=completed, entity_type=kind)

def page(kind, rows, has_next=False):
    links = {"next": {"href": "https://ignored.invalid/"}} if has_next else {}
    return {"_embedded": {kind: rows}, "_links": links}

def response(body=None, status=200):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode() if body is not None else b""
    r.url = "https://test.amocrm.ru/api/v4/leads"
    return r

class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls, self.mounts, self.headers = [], {}, {}
        self.closed = False
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def mount(self, prefix, adapter): self.mounts[prefix] = adapter
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception): raise result
        return result

class ReportTests(unittest.TestCase):
    def run_report(self, responses):
        fake = FakeSession(responses)
        with patch.dict(app.os.environ, {"AMO_SUBDOMAIN": "test",
                                        "AMO_ACCESS_TOKEN": "fake-only"}, clear=True), \
             patch.object(app.requests, "Session", return_value=fake), \
             patch.object(app.time, "time", return_value=NOW), \
             patch.object(app.time, "sleep") as sleep:
            result = app.report()
        self.fake, self.sleeps = fake, sleep.call_args_list
        self.assertEqual(fake.headers["Authorization"], "Bearer fake-only")
        self.assertTrue(fake.closed)
        for url, kwargs in fake.calls:
            self.assertEqual(urlsplit(url).hostname, "test.amocrm.ru")
            self.assertEqual(kwargs["timeout"], (5, 30))
            self.assertFalse(kwargs["allow_redirects"])
        return result

    def test_join_and_paginate_both_collections(self):
        responses = [
            response(page("leads", [lead(1), lead(2), lead(3), lead(4)], True)),
            response(page("leads", [lead(5, 142), lead(6, 143),
                      lead(7, is_deleted=True), lead(8), lead(9)])),
            response(page("tasks", [task(20, 2, NOW+1), task(40, 4, NOW-1, True),
                      task(50, 5, NOW-1), task(70, 7, NOW-1)], True)),
            response(page("tasks", [task(30, 3, NOW-1), task(80, 8, NOW-1),
                      task(81, 8, NOW+1), task(80, 8, NOW-1), task(90, 9, NOW),
                      task(100, 1, NOW-1, kind="contacts")])),
        ]
        result = self.run_report(responses)
        self.assertEqual([r["lead_id"] for r in result], [1, 3, 4, 8])
        self.assertEqual([r["reason"] for r in result],
                         ["no_open_tasks", "overdue", "no_open_tasks", "overdue"])
        self.assertEqual(result[-1]["overdue_task_ids"], [80])
        self.assertEqual(len(self.fake.calls), 4)
        for index, (_, kwargs) in enumerate(self.fake.calls):
            query = kwargs["params"]
            self.assertEqual(query["page"], index % 2 + 1)
            self.assertEqual(query["limit"], 250)
            self.assertEqual(query["order[id]"], "asc")
            if index >= 2:
                self.assertEqual(query["filter[entity_type]"], "leads")
                self.assertEqual(query["filter[is_completed]"], 0)
        self.assertEqual([c.args[0] for c in self.sleeps], [0.2] * 4)

    def test_empty_204_leads(self):
        self.assertEqual(self.run_report([response(status=204)]), [])
        self.assertEqual(len(self.fake.calls), 1)

    def test_empty_json_collection(self):
        self.assertEqual(self.run_report([response(page("leads", []))]), [])

    def test_empty_object(self):
        self.assertEqual(self.run_report([response({})]), [])

    def test_empty_204_tasks_means_no_open_tasks(self):
        result = self.run_report([response(page("leads", [lead(1)])),
                                  response(status=204)])
        self.assertEqual(result[0]["reason"], "no_open_tasks")

    def test_closed_only_skips_tasks(self):
        self.assertEqual(self.run_report([response(page("leads", [lead(1, 142),
                                                                  lead(2, 143)]))]), [])
        self.assertEqual(len(self.fake.calls), 1)

    def test_auth_error_fails(self):
        with self.assertRaises(requests.HTTPError):
            self.run_report([response(status=401)])

    def test_partial_task_failure_does_not_return_report(self):
        with self.assertRaises(requests.HTTPError):
            self.run_report([response(page("leads", [lead(1)])),
                response(page("tasks", [task(10, 1, NOW+1)], True)), response(status=403)])

    def test_transport_error_does_not_return_report(self):
        with self.assertRaises(requests.Timeout):
            self.run_report([requests.Timeout("mocked timeout")])

    def test_malformed_json_fails(self):
        with self.assertRaises(requests.exceptions.JSONDecodeError):
            self.run_report([response(status=200)])

    def test_invalid_account_fails_before_io(self):
        with patch.dict(app.os.environ, {"AMO_SUBDOMAIN": "evil.example/x"}, clear=True), \
             patch.object(app.requests, "Session") as session:
            with self.assertRaises(ValueError): app.report()
            session.assert_not_called()

    def test_missing_token_fails(self):
        with patch.dict(app.os.environ, {"AMO_SUBDOMAIN": "test"}, clear=True):
            with self.assertRaises(KeyError): app.report()

    def test_redirect_rejected(self):
        with self.assertRaises(RuntimeError):
            self.run_report([response({}, status=302)])

    def test_page_limit_fails_instead_of_truncating(self):
        responses = [response(page("leads", [lead(i)], True)) for i in range(10000)]
        with self.assertRaisesRegex(RuntimeError, "Page limit"):
            self.run_report(responses)

    def policy(self):
        self.run_report([response({})])
        return self.fake.mounts["https://"].max_retries

    def test_retry_statuses_and_methods(self):
        retry = self.policy()
        for status in (429, 500, 502, 503, 504):
            self.assertTrue(retry.is_retry("GET", status))
        self.assertFalse(retry.is_retry("GET", 401))
        self.assertFalse(retry.is_retry("POST", 500))
        self.assertTrue(retry.respect_retry_after_header)

    def test_retry_after_seconds(self):
        self.assertEqual(self.policy().get_retry_after(
            SimpleNamespace(headers={"Retry-After": "3"})), 3)

    def test_retry_after_http_date(self):
        retry = self.policy()
        with patch.object(app.time, "time", return_value=NOW):
            self.assertEqual(retry.get_retry_after(SimpleNamespace(
                headers={"Retry-After": formatdate(NOW+5, usegmt=True)})), 5)

    def test_retry_backoff_and_exhaustion(self):
        retry = self.policy()
        r = SimpleNamespace(status=429, get_redirect_location=lambda: None)
        delays = []
        for _ in range(3):
            retry = retry.increment("GET", "https://test.amocrm.ru", response=r)
            delays.append(retry.get_backoff_time())
        self.assertEqual(delays, [0, 2, 4])
        with self.assertRaises(MaxRetryError):
            retry.increment("GET", "https://test.amocrm.ru", response=r)

if __name__ == "__main__":
    unittest.main(verbosity=2)
