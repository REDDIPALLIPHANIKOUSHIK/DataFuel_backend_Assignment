#!/usr/bin/env python3
"""
sweep.py: QuickMart partner portal inventory scraper.
Collects inventory snapshots for tracked stores at a specified ISO-8601 timestamp.
Handles rate limits (429), transient server errors (500, 503), slow responses,
soft-ban detection, partial snapshots, and deduplication.
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests

import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sweep")

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_PORTAL = os.environ.get("PORTAL_URL", "http://127.0.0.1:8765")
DEFAULT_API_KEY = os.environ.get("API_KEY", "dfhire-2026")
DEFAULT_TIMEOUT = 15.0  # Allows 8s slow requests to finish while preventing hangs
MAX_RETRIES = 6
SAFE_REQUEST_INTERVAL = 0.38  # Stay safely under 2.6 req/sec to prevent fair-use soft ban (30 req / 10s)


class RateLimiter:
    """Enforces minimum interval between outbound HTTP requests to respect fair-use limits."""

    def __init__(self, min_interval: float = SAFE_REQUEST_INTERVAL):
        self.min_interval = min_interval
        self.last_request_time = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request_time = time.monotonic()


rate_limiter = RateLimiter()


def parse_and_validate_timestamp(ts_str: str) -> tuple[datetime, str, str]:
    """
    Parse an ISO-8601 timestamp string.
    Ensures timezone is provided.
    Returns:
        (dt_utc, as_of_utc_str, as_of_ist_date_str)
    """
    cleaned = ts_str.strip()
    try:
        dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except Exception as e:
        raise ValueError(f"Invalid ISO-8601 timestamp '{ts_str}': {e}") from e

    if dt.tzinfo is None:
        raise ValueError(f"Timestamp '{ts_str}' must include a timezone (e.g. 2026-09-28T04:30:00Z)")

    dt_utc = dt.astimezone(timezone.utc)
    as_of_utc_str = dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Indian Standard Time calendar date (IST = UTC + 5:30)
    dt_ist = dt_utc.astimezone(IST)
    as_of_ist_date_str = dt_ist.strftime("%Y-%m-%d")

    return dt_utc, as_of_utc_str, as_of_ist_date_str


def make_request_with_retry(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = MAX_RETRIES,
) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
    """
    Make an HTTP GET request with bounded retries, rate limiting, and backoff.
    Handles 429 (respecting Retry-After), 500, 503, and network timeouts.
    """
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        rate_limiter.wait()
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
            resp_headers = dict(resp.headers)

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    return 200, data, resp_headers
                except Exception as e:
                    logger.warning(f"Failed to parse JSON from {url}: {e}")
                    return 200, None, resp_headers

            if resp.status_code == 429:
                retry_after_raw = resp_headers.get("Retry-After", "2")
                try:
                    retry_after = float(retry_after_raw)
                except ValueError:
                    retry_after = 2.0
                sleep_time = retry_after + 0.2 + random.uniform(0.1, 0.3)
                logger.warning(
                    f"HTTP 429 Rate limited on {url}. Retry-After: {retry_after}s. "
                    f"Waiting {sleep_time:.2f}s (attempt {attempt}/{max_retries})"
                )
                time.sleep(sleep_time)
                continue

            if resp.status_code in (500, 503):
                backoff = min(8.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0.05, 0.2)
                logger.warning(
                    f"HTTP {resp.status_code} server error on {url}. "
                    f"Backing off {backoff:.2f}s (attempt {attempt}/{max_retries})"
                )
                time.sleep(backoff)
                continue

            # Non-retryable HTTP error (e.g. 400, 401, 404)
            logger.error(f"HTTP {resp.status_code} error on {url}: {resp.text}")
            try:
                body = resp.json()
            except Exception:
                body = {"error": resp.text}
            return resp.status_code, body, resp_headers

        except (requests.Timeout, requests.ConnectionError) as exc:
            backoff = min(8.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0.05, 0.2)
            logger.warning(
                f"Network exception ({type(exc).__name__}) on {url}: {exc}. "
                f"Retrying in {backoff:.2f}s (attempt {attempt}/{max_retries})"
            )
            time.sleep(backoff)
        except Exception as exc:
            logger.error(f"Unexpected error querying {url}: {exc}")
            raise

    # Exhausted retries
    return 500, {"error": "Exhausted maximum retry attempts"}, {}


def discover_stores(
    session: requests.Session, portal_url: str, api_key: str
) -> list[dict[str, Any]]:
    """
    Paginate through /v1/stores until next_page is None.
    Returns all discovered stores across all pages.
    """
    stores: list[dict[str, Any]] = []
    page = 1
    headers = {"X-Api-Key": api_key}

    while page is not None:
        url = f"{portal_url}/v1/stores"
        status, body, _ = make_request_with_retry(
            session, url, params={"page": page}, headers=headers
        )
        if status != 200 or not body or "stores" not in body:
            raise RuntimeError(f"Failed to discover stores at page {page}: status {status}, body: {body}")

        stores.extend(body.get("stores", []))
        page = body.get("next_page")

    return stores


def fetch_store_inventory(
    session: requests.Session,
    portal_url: str,
    api_key: str,
    store_id: str,
    as_of_str: str,
) -> tuple[str, str | None, list[dict[str, Any]]]:
    """
    Fetch all inventory pages for a given store and timestamp.
    Follows cursor pagination until next_cursor is None.
    Detects soft-ban degradation and partial responses.

    Returns:
        (status, reason, items)
        status: "complete" or "incomplete"
        reason: None if complete, or a descriptive reason string
    """
    headers = {"X-Api-Key": api_key}
    cursor: str | None = "0"
    all_items: list[dict[str, Any]] = []
    page_count = 0

    while True:
        url = f"{portal_url}/v1/stores/{store_id}/inventory"
        params: dict[str, Any] = {"as_of": as_of_str}
        if cursor is not None and cursor != "0":
            params["cursor"] = cursor

        status, body, _ = make_request_with_retry(
            session, url, params=params, headers=headers
        )

        if status != 200 or body is None:
            return "incomplete", f"HTTP {status}: {body.get('error') if body else 'unknown error'}", []

        # 1. Check for partial snapshot flag
        if body.get("partial") is True:
            logger.warning(f"Store {store_id} returned partial snapshot for {as_of_str}")
            return "incomplete", "partial snapshot", []

        # 2. Check for soft-ban degraded response
        # API.md and mock_portal.py specify that CDN "edge" responses indicate soft-ban degradation
        meta = body.get("meta") or {}
        if meta.get("source") == "edge":
            logger.warning(
                f"Store {store_id} returned soft-banned edge response. Waiting 22s for recovery window..."
            )
            time.sleep(22.0)  # Wait for mock portal's 20s soft-ban window to clear
            # Retry once after recovery wait
            status, body, _ = make_request_with_retry(
                session, url, params=params, headers=headers
            )
            if status == 200 and body and (body.get("meta") or {}).get("source") != "edge":
                meta = body.get("meta") or {}
            else:
                return "incomplete", "soft ban detected (degraded edge response)", []

        items = body.get("items", [])
        all_items.extend(items)
        page_count += 1

        next_cursor = body.get("next_cursor")
        if next_cursor is None:
            break
        cursor = str(next_cursor)

    return "complete", None, all_items


def run_sweep(
    as_of: str,
    portal_url: str = DEFAULT_PORTAL,
    api_key: str = DEFAULT_API_KEY,
    db_path: str = db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Execute a complete sweep for the given as_of timestamp."""
    start_time = time.monotonic()
    dt_utc, as_of_utc_str, as_of_ist_date_str = parse_and_validate_timestamp(as_of)

    logger.info(f"Starting sweep for as_of: {as_of_utc_str} (IST date: {as_of_ist_date_str})")
    db.init_db(db_path)

    session = requests.Session()

    # 1. Discover all stores
    logger.info(f"Discovering stores from {portal_url}...")
    discovered = discover_stores(session, portal_url, api_key)
    logger.info(f"Discovered {len(discovered)} total stores.")

    with db.get_db(db_path) as conn:
        db.sync_stores(conn, discovered)
        sweep_id = db.get_or_create_sweep(conn, as_of_utc_str, as_of_ist_date_str)
        # Select active stores (tracked stores)
        tracked_stores = db.get_tracked_stores(conn)

    logger.info(f"Tracking {len(tracked_stores)} active stores for this sweep.")

    complete_count = 0
    incomplete_count = 0
    incomplete_details: list[dict[str, str]] = []

    # 2. Iterate through each tracked store sequentially and fetch inventory
    for idx, store in enumerate(tracked_stores, 1):
        store_id = store["store_id"]
        logger.info(f"[{idx}/{len(tracked_stores)}] Fetching {store_id} ({store['name']})...")

        try:
            status, reason, items = fetch_store_inventory(
                session, portal_url, api_key, store_id, as_of_utc_str
            )
        except Exception as exc:
            logger.error(f"Unexpected error scraping {store_id}: {exc}")
            status = "incomplete"
            reason = f"Exception: {exc}"
            items = []

        with db.get_db(db_path) as conn:
            db.save_store_sweep_result(conn, sweep_id, store_id, status, reason, items)

        if status == "complete":
            complete_count += 1
            logger.info(f"  -> {store_id}: complete ({len(items)} items)")
        else:
            incomplete_count += 1
            incomplete_details.append({"store_id": store_id, "reason": reason or "unknown"})
            logger.warning(f"  -> {store_id}: incomplete ({reason})")

    duration = time.monotonic() - start_time
    minutes = int(duration // 60)
    seconds = int(duration % 60)
    duration_str = f"{minutes}m{seconds:02d}s" if minutes > 0 else f"{duration:.1f}s"

    # 3. Print final structured summary
    total_tracked = len(tracked_stores)
    print("\n" + "=" * 60)
    print(f"SWEEP SUMMARY: {as_of_utc_str} (IST: {as_of_ist_date_str})")
    print("=" * 60)
    if incomplete_count == 0:
        print(f"{total_tracked} stores: {complete_count} complete, 0 incomplete · {duration_str}")
    else:
        incomplete_str = ", ".join(f"{it['store_id']}: {it['reason']}" for it in incomplete_details)
        print(f"{total_tracked} stores: {complete_count} complete, {incomplete_count} incomplete ({incomplete_str}) · {duration_str}")
        for it in incomplete_details:
            print(f"Store:\n{it['store_id']}\n\nReason:\n{it['reason']}")
    print(f"\nDuration:\n{duration_str}")
    print("=" * 60 + "\n")

    return {
        "sweep_id": sweep_id,
        "as_of_utc": as_of_utc_str,
        "as_of_ist_date": as_of_ist_date_str,
        "total_stores": total_tracked,
        "complete": complete_count,
        "incomplete": incomplete_count,
        "incomplete_details": incomplete_details,
        "duration_seconds": duration,
    }


def main():
    parser = argparse.ArgumentParser(description="QuickMart inventory snapshot scraper.")
    parser.add_argument(
        "--as-of",
        dest="as_of",
        required=True,
        help="Snapshot ISO-8601 timestamp with timezone, e.g. 2026-09-28T04:30:00Z",
    )
    parser.add_argument(
        "--portal",
        dest="portal_url",
        default=DEFAULT_PORTAL,
        help=f"QuickMart portal base URL (default: {DEFAULT_PORTAL})",
    )
    parser.add_argument(
        "--api-key",
        dest="api_key",
        default=DEFAULT_API_KEY,
        help="QuickMart API key (default: dfhire-2026)",
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=db.DEFAULT_DB_PATH,
        help=f"SQLite database file path (default: {db.DEFAULT_DB_PATH})",
    )

    args = parser.parse_args()

    try:
        run_sweep(
            as_of=args.as_of,
            portal_url=args.portal_url,
            api_key=args.api_key,
            db_path=args.db_path,
        )
    except Exception as e:
        logger.error(f"Sweep failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
