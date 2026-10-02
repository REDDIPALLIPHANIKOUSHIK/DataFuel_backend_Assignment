"""
review_me.py: written by an AI coding assistant in one shot and merged without review.

Your job (write it in REVIEW.md):
  1. Find at least 5 real problems, most serious first. For each, say what goes wrong,
     with a concrete example (not just "bad practice").
  2. Fix the 2-3 most serious ones in this file.
Don't rewrite it from scratch. Reviewing is the skill being tested.
"""
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import requests

PORTAL = "http://127.0.0.1:8765"
HEADERS = {"X-Api-Key": "dfhire-2026"}


def fetch_inventory(store_id, as_of, cursor="0", results=None):
    """Fetch every inventory page for a store, retrying with bounded backoff and timeout."""
    # Fix 1: Avoid mutable default argument results=[] which shared state across store calls
    if results is None:
        results = []

    # Fix 2: Bounded retry loop with timeout and 429 Retry-After handling instead of infinite loop
    max_retries = 5
    r = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(
                f"{PORTAL}/v1/stores/{store_id}/inventory",
                params={"as_of": as_of, "cursor": cursor},
                headers=HEADERS,
                timeout=15.0,
            )
            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", "2"))
                time.sleep(retry_after + 0.2)
                continue
            r.raise_for_status()
            break
        except Exception:
            if attempt == max_retries:
                raise
            time.sleep(0.5 * attempt)

    if r is None:
        return results

    body = r.json()
    results.extend(body.get("items", []))
    if body.get("next_cursor"):
        return fetch_inventory(store_id, as_of, body["next_cursor"], results)
    return results


def save(conn, store_id, items):
    # Fix 3: Parameterized SQL to prevent SQL injection and syntax errors on quotes in product names
    for it in items:
        conn.execute(
            "INSERT INTO inventory VALUES (?, ?, ?, ?, ?, ?)",
            (store_id, it["sku_id"], it["name"], int(it["in_stock"]), int(it.get("qty", 0)), it["observed_at"]),
        )
    conn.commit()


def city_osa(conn, city, day=None):
    """On-shelf availability for a city on a day. Defaults to yesterday."""
    day = day or (date.today() - timedelta(days=1)).isoformat()
    stores = [r[0] for r in conn.execute(
        "SELECT store_id FROM stores WHERE city = ?", (city,))]
    if not stores:
        return 0.0

    # Fix 4: Use in_stock as source of truth (not qty > 0) and micro-aggregate across total observations
    placeholders = ",".join("?" for _ in stores)
    rows = conn.execute(
        f"SELECT in_stock FROM inventory WHERE store_id IN ({placeholders}) AND substr(observed_at, 1, 10) = ?",
        (*stores, day),
    ).fetchall()
    if not rows:
        return 0.0
    in_stock_count = sum(1 for (stk,) in rows if stk == 1)
    return round(100.0 * in_stock_count / len(rows), 2)


if __name__ == "__main__":
    conn = sqlite3.connect("osa.db")
    conn.execute("CREATE TABLE IF NOT EXISTS stores (store_id TEXT, city TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS inventory (store_id TEXT, sku_id TEXT, name TEXT, "
                 "in_stock INT, qty INT, observed_at TEXT)")
    # Fix 5: Pass timezone-aware ISO timestamp required by API contract
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for sid in ["MUM-001", "MUM-002"]:
        conn.execute("INSERT OR IGNORE INTO stores VALUES (?, ?)", (sid, "Mumbai"))
        save(conn, sid, fetch_inventory(sid, now_utc))
    print(city_osa(conn, "Mumbai"))
