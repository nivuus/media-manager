#!/usr/bin/env python3
"""The Radarr/Sonarr API helpers every maintenance step reads through.

A step that loops over a collection reads each item with .get(): an answer
that is not a list of objects has to stop it as an ApiError, which the step
records, and never as an AttributeError halfway through the loop. The fake
(maintenance_fakes.py) stands in for requests.request, the network boundary.

Run: python3 tests/test_arr_api.py
"""
import pathlib
import sys
from unittest import mock

# stack/ is deployed byte for byte: keep the test run from leaving a
# __pycache__ in it.
sys.dont_write_bytecode = True
REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "stack"))

import requests  # noqa: E402

from maintenance.arr_api import ApiError, Instance, get_json_list  # noqa: E402
from maintenance_fakes import FakeApi, reply  # noqa: E402

RADARR = Instance("radarr", "Radarr", "http://radarr.test:7878", "radarr-key",
                  "RADARR_API_KEY")
URL = "http://radarr.test:7878/api/v3/rootfolder"
FOLDER = {"id": 1, "path": "/data/Movies", "accessible": True}

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def read(answer, **kwargs):
    """get_json_list() against one answer: (result or the ApiError, the calls)."""
    api = FakeApi({("GET", URL): answer})
    with mock.patch("requests.request", new=api):
        try:
            return get_json_list(RADARR, "rootfolder", **kwargs), api.calls
        except ApiError as error:
            return error, api.calls


# --- A list of objects is the answer ----------------------------------------
got, calls = read(reply(200, [FOLDER]))
check("list of objects: returned", got, [FOLDER])
check("list of objects: authenticated",
      [kwargs["headers"] for _, _, kwargs in calls], [{"X-Api-Key": "radarr-key"}])
check("list of objects: default timeout",
      [kwargs["timeout"] for _, _, kwargs in calls], [(10, 60)])
# An empty collection is a valid answer, not an error: no root folder yet.
got, _ = read(reply(200, []))
check("empty list: returned", got, [])

# The query and a longer timeout reach the request.
_, calls = read(reply(200, []), params={"downloadId": "ABC"}, timeout=(10, 120))
check("parameters passed", [(kwargs["params"], kwargs["timeout"])
                            for _, _, kwargs in calls],
      [({"downloadId": "ABC"}, (10, 120))])

# --- Anything else is an ApiError naming the endpoint -----------------------
NOT_A_LIST = [
    ("an object", reply(200, FOLDER)),
    ("a list holding a non-object", reply(200, [FOLDER, "/data/TV Shows"])),
    ("null", reply(200, None)),
    ("not JSON", reply(200, raw=b"<html><body>Radarr is starting</body></html>")),
    ("HTTP 500", reply(500, {"message": "boom"})),
    ("connection refused",
     requests.exceptions.ConnectionError("[Errno 111] Connection refused")),
]
for label, answer in NOT_A_LIST:
    got, _ = read(answer)
    check(f"{label}: ApiError", type(got), ApiError)
    check(f"{label}: endpoint named", "/api/v3/rootfolder" in str(got), True)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("test_arr_api: OK")
