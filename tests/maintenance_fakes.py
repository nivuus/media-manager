"""Fakes for the tests of the maintenance scripts: the Radarr/Sonarr API and the log.

FakeApi stands in for requests.request, the network boundary. It answers
from a route table with real requests.Response objects, so raise_for_status()
and JSON parsing behave as they do in production, and it keeps every call so
that a test can check what was sent. Not a test itself: the Makefile only
runs the test_* files.
"""
import http
import json
import logging

import requests


def reply(status=200, body=None, raw=None):
    """A real requests.Response, as the API would send it."""
    response = requests.Response()
    response.status_code = status
    response.reason = http.HTTPStatus(status).phrase
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    response.encoding = "utf-8"
    response._content = raw if raw is not None else json.dumps(body).encode()
    return response


class FakeApi:
    """Stands in for requests.request: answers from a route table, keeps every call.

    A route maps (method, url) to a response or to an exception to raise. A
    call to any other route fails the test instead of reaching the network.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if (method, url) not in self.routes:
            raise AssertionError(f"unexpected call: {method} {url}")
        answer = self.routes[(method, url)]
        if isinstance(answer, Exception):
            raise answer
        answer.url = url
        return answer

    def made(self, method, url):
        """The keyword arguments of every call made to that route."""
        return [kwargs for m, u, kwargs in self.calls if (m, u) == (method, url)]


class LogCapture(logging.Handler):
    """Keeps the log lines of a run, to show them only when a case fails.

    Installed on the root logger, it also keeps the scripts' errors off the
    test output: without any handler, logging prints them to stderr.
    """

    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.getMessage()}")

    def install(self):
        root = logging.getLogger()
        root.addHandler(self)
        root.setLevel(logging.INFO)
        return self
