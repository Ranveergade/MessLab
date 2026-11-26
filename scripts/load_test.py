"""A dependency-free HTTP load smoke test for a running MessLab deployment.

It logs in once through the legacy password form (enable
``MESSLAB_ALLOW_PASSWORD_LOGIN=1`` for the test account), then replays a set of
authenticated GET paths with a fixed concurrency. Use it to catch slow pages and
unexpected 5xx responses before inviting students.

Example::

    python scripts/load_test.py \
        --base-url http://127.0.0.1:8000 \
        --username loadtest --password 'local-only-pass' \
        --requests 300 --concurrency 15

The account must already exist. Delete it after the test.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_PATHS = (
    "/healthz",
    "/manager/",
    "/manager/plan/",
    "/manager/live/",
    "/manager/waste/",
    "/manager/menu/day/",
)


def _csrf(html: str) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    if not match:
        raise SystemExit("Could not find a CSRF token on the password login page.")
    return match.group(1)


def login(base_url: str, username: str, password: str) -> str:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    page = opener.open(f"{base_url}/login/password/", timeout=30).read().decode()
    data = urllib.parse.urlencode(
        {
            "csrfmiddlewaretoken": _csrf(page),
            "username": username,
            "password": password,
            "next": "/manager/",
        }
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/login/password/",
        data=data,
        headers={"Referer": f"{base_url}/login/password/"},
    )
    response = opener.open(request, timeout=30)
    if not any(cookie.name == "sessionid" for cookie in jar):
        raise SystemExit(
            f"Login failed (HTTP {response.status}). Check the account and "
            "MESSLAB_ALLOW_PASSWORD_LOGIN."
        )
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in jar)


def fetch(base_url: str, path: str, cookie: str) -> tuple[int, float, int]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        headers={"Cookie": cookie, "Accept": "text/html"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return response.status, time.perf_counter() - started, len(body)
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, time.perf_counter() - started, 0


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", required=True, help="Login username (not email).")
    parser.add_argument("--password", required=True)
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--path", action="append", dest="paths")
    args = parser.parse_args()

    paths = args.paths or list(DEFAULT_PATHS)
    cookie = login(args.base_url.rstrip("/"), args.username, args.password)
    jobs = [paths[i % len(paths)] for i in range(args.requests)]

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda path: fetch(args.base_url, path, cookie), jobs))
    elapsed = time.perf_counter() - started

    latencies = [result[1] for result in results]
    statuses: dict[int, int] = {}
    for status, _latency, _size in results:
        statuses[status] = statuses.get(status, 0) + 1

    print(f"requests={len(results)} concurrency={args.concurrency} elapsed={elapsed:.2f}s")
    print(f"throughput={len(results) / elapsed:.1f} req/s")
    print(
        "latency_ms "
        f"mean={statistics.fmean(latencies) * 1000:.0f} "
        f"p50={percentile(latencies, 0.50) * 1000:.0f} "
        f"p95={percentile(latencies, 0.95) * 1000:.0f} "
        f"p99={percentile(latencies, 0.99) * 1000:.0f} "
        f"max={max(latencies) * 1000:.0f}"
    )
    print("statuses=" + ", ".join(f"{code}:{count}" for code, count in sorted(statuses.items())))
    return 1 if any(code >= 500 for code in statuses) else 0


if __name__ == "__main__":
    sys.exit(main())
