"""
Database layer for QuickMart inventory scraper and OSA reporting.
Uses SQLite with strict schemas, parameterized queries, and transactional integrity.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

DEFAULT_DB_PATH = "quickmart.db"


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create a SQLite connection with foreign keys enabled and row factory set."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


@contextmanager
def get_db(db_path: str = DEFAULT_DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for SQLite connections with automatic commit/rollback."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """Initialize database schema with all required tables, constraints, and indexes."""
    with get_db(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                store_id TEXT PRIMARY KEY,
                city TEXT NOT NULL,
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL,
                is_serviceable INTEGER NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sweeps (
                sweep_id TEXT PRIMARY KEY,
                as_of_utc TEXT NOT NULL UNIQUE,
                as_of_ist_date TEXT NOT NULL,
                created_at_utc TEXT NOT NULL
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_sweeps (
                sweep_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                items_count INTEGER NOT NULL DEFAULT 0,
                completed_at_utc TEXT NOT NULL,
                PRIMARY KEY (sweep_id, store_id),
                FOREIGN KEY (sweep_id) REFERENCES sweeps(sweep_id) ON DELETE CASCADE,
                FOREIGN KEY (store_id) REFERENCES stores(store_id)
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS inventory_observations (
                sweep_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                sku_id TEXT NOT NULL,
                name TEXT NOT NULL,
                in_stock INTEGER NOT NULL,
                qty INTEGER NOT NULL,
                price REAL NOT NULL,
                observed_at_utc TEXT NOT NULL,
                PRIMARY KEY (sweep_id, store_id, sku_id),
                FOREIGN KEY (sweep_id) REFERENCES sweeps(sweep_id) ON DELETE CASCADE,
                FOREIGN KEY (store_id) REFERENCES stores(store_id)
            );
        """)

        # Indexes for fast querying
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sweeps_as_of_ist ON sweeps(as_of_ist_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_store_sweeps_lookup ON store_sweeps(store_id, status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stores_city_active ON stores(city, is_active);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_sku ON inventory_observations(sku_id);")


def sync_stores(conn: sqlite3.Connection, stores: list[dict[str, Any]]) -> int:
    """Insert or update discovered stores."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = """
        INSERT INTO stores (store_id, city, name, is_active, is_serviceable, updated_at_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(store_id) DO UPDATE SET
            city = excluded.city,
            name = excluded.name,
            is_active = excluded.is_active,
            is_serviceable = excluded.is_serviceable,
            updated_at_utc = excluded.updated_at_utc;
    """
    rows = [
        (
            s["store_id"],
            s["city"],
            s["name"],
            1 if s.get("is_active") else 0,
            1 if s.get("is_serviceable") else 0,
            now_utc,
        )
        for s in stores
    ]
    conn.executemany(query, rows)
    return len(rows)


def get_tracked_stores(conn: sqlite3.Connection, city: str | None = None) -> list[dict[str, Any]]:
    """
    Return all active stores in QuickMart's network.
    Inactive (decommissioned) stores are excluded from tracking.
    """
    if city:
        cursor = conn.execute(
            "SELECT store_id, city, name, is_active, is_serviceable FROM stores WHERE is_active = 1 AND city = ? ORDER BY store_id ASC",
            (city,),
        )
    else:
        cursor = conn.execute(
            "SELECT store_id, city, name, is_active, is_serviceable FROM stores WHERE is_active = 1 ORDER BY store_id ASC"
        )
    return [dict(row) for row in cursor.fetchall()]


def get_or_create_sweep(conn: sqlite3.Connection, as_of_utc: str, as_of_ist_date: str) -> str:
    """Ensure deterministic sweep record exists for the given as_of timestamp."""
    sweep_id = f"sweep_{as_of_utc}"
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO sweeps (sweep_id, as_of_utc, as_of_ist_date, created_at_utc)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(as_of_utc) DO UPDATE SET
            as_of_ist_date = excluded.as_of_ist_date;
        """,
        (sweep_id, as_of_utc, as_of_ist_date, now_utc),
    )
    return sweep_id


def save_store_sweep_result(
    conn: sqlite3.Connection,
    sweep_id: str,
    store_id: str,
    status: str,
    reason: str | None,
    items: list[dict[str, Any]],
) -> None:
    """
    Persist store sweep results and deduplicated observations transactionally.
    If re-run, existing observations for this store & sweep are replaced idempotently.
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Clean existing observations for this store/sweep to maintain exact idempotency
    conn.execute(
        "DELETE FROM inventory_observations WHERE sweep_id = ? AND store_id = ?",
        (sweep_id, store_id),
    )

    items_count = 0
    if status == "complete" and items:
        # Deduplicate items by sku_id (last occurrence wins)
        deduped: dict[str, dict[str, Any]] = {}
        for it in items:
            deduped[it["sku_id"]] = it

        obs_rows = []
        for it in deduped.values():
            # Standardize observed_at to ISO string
            raw_obs = it.get("observed_at", "")
            try:
                dt = datetime.fromisoformat(raw_obs.replace("Z", "+00:00"))
                obs_utc = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                obs_utc = raw_obs

            # Parse price safely (handles both "237.50" and 237.5)
            try:
                price = float(it["price"])
            except Exception:
                price = 0.0

            obs_rows.append(
                (
                    sweep_id,
                    store_id,
                    it["sku_id"],
                    it["name"],
                    1 if it.get("in_stock") else 0,
                    int(it.get("qty", 0)),
                    price,
                    obs_utc,
                )
            )

        conn.executemany(
            """
            INSERT INTO inventory_observations (
                sweep_id, store_id, sku_id, name, in_stock, qty, price, observed_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            obs_rows,
        )
        items_count = len(obs_rows)

    # Record the store sweep status
    conn.execute(
        """
        INSERT INTO store_sweeps (
            sweep_id, store_id, status, reason, items_count, completed_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(sweep_id, store_id) DO UPDATE SET
            status = excluded.status,
            reason = excluded.reason,
            items_count = excluded.items_count,
            completed_at_utc = excluded.completed_at_utc;
        """,
        (sweep_id, store_id, status, reason, items_count, now_utc),
    )


def get_osa_report(conn: sqlite3.Connection, city: str, ist_date: str) -> dict[str, Any]:
    """
    Calculate OSA and coverage for a city on an IST calendar date.
    Uses aggregate observations: total in-stock / total observations.
    Never averages store-level percentages or SKU percentages.
    """
    # 1. Get all sweeps associated with this IST date
    sweep_rows = conn.execute(
        "SELECT sweep_id, as_of_utc FROM sweeps WHERE as_of_ist_date = ? ORDER BY as_of_utc ASC",
        (ist_date,),
    ).fetchall()
    sweeps = [dict(r) for r in sweep_rows]

    # 2. Get all active stores for this city
    store_rows = conn.execute(
        "SELECT store_id, name FROM stores WHERE city = ? AND is_active = 1 ORDER BY store_id ASC",
        (city,),
    ).fetchall()
    active_stores = [dict(r) for r in store_rows]

    if not sweeps or not active_stores:
        return {
            "city": city,
            "date": ist_date,
            "status": "no_data",
            "osa_pct": None,
            "observations": 0,
            "coverage": {
                "stores_expected": 0,
                "stores_complete": 0,
                "incomplete": [],
            },
            "skus": [],
        }

    # 3. Calculate coverage across all expected (store, sweep) combinations
    stores_expected = len(active_stores) * len(sweeps)
    stores_complete = 0
    incomplete_list = []

    # Pre-fetch all store_sweep statuses for these sweeps and city stores
    store_ids = [s["store_id"] for s in active_stores]
    sweep_ids = [sw["sweep_id"] for sw in sweeps]
    sweep_lookup = {sw["sweep_id"]: sw["as_of_utc"] for sw in sweeps}

    placeholders_stores = ",".join("?" for _ in store_ids)
    placeholders_sweeps = ",".join("?" for _ in sweep_ids)

    query = f"""
        SELECT sweep_id, store_id, status, reason
        FROM store_sweeps
        WHERE store_id IN ({placeholders_stores})
          AND sweep_id IN ({placeholders_sweeps});
    """
    status_rows = conn.execute(query, store_ids + sweep_ids).fetchall()
    status_map = {(r["sweep_id"], r["store_id"]): (r["status"], r["reason"]) for r in status_rows}

    for sw in sweeps:
        sw_id = sw["sweep_id"]
        as_of = sw["as_of_utc"]
        for st in active_stores:
            st_id = st["store_id"]
            stat_info = status_map.get((sw_id, st_id))
            if stat_info and stat_info[0] == "complete":
                stores_complete += 1
            else:
                reason = stat_info[1] if stat_info and stat_info[1] else "no sweep recorded"
                incomplete_list.append({
                    "store_id": st_id,
                    "sweep": as_of,
                    "reason": reason,
                })

    # 4. Aggregate valid observations across complete store sweeps
    obs_query = f"""
        SELECT COUNT(*) as total_obs, SUM(io.in_stock) as in_stock_obs
        FROM inventory_observations io
        JOIN store_sweeps ss ON io.sweep_id = ss.sweep_id AND io.store_id = ss.store_id
        JOIN sweeps s ON io.sweep_id = s.sweep_id
        JOIN stores st ON io.store_id = st.store_id
        WHERE st.city = ?
          AND s.as_of_ist_date = ?
          AND ss.status = 'complete';
    """
    total_obs_row = conn.execute(obs_query, (city, ist_date)).fetchone()
    total_obs = total_obs_row["total_obs"] if total_obs_row else 0
    in_stock_obs = total_obs_row["in_stock_obs"] if total_obs_row and total_obs_row["in_stock_obs"] else 0

    if total_obs == 0:
        return {
            "city": city,
            "date": ist_date,
            "status": "no_data",
            "osa_pct": None,
            "observations": 0,
            "coverage": {
                "stores_expected": stores_expected,
                "stores_complete": stores_complete,
                "incomplete": incomplete_list,
            },
            "skus": [],
        }

    osa_pct = round((in_stock_obs / total_obs) * 100.0, 2)

    # 5. SKU-level metrics
    sku_query = f"""
        SELECT
            io.sku_id,
            MAX(io.name) as name,
            COUNT(*) as observations,
            SUM(io.in_stock) as in_stock
        FROM inventory_observations io
        JOIN store_sweeps ss ON io.sweep_id = ss.sweep_id AND io.store_id = ss.store_id
        JOIN sweeps s ON io.sweep_id = s.sweep_id
        JOIN stores st ON io.store_id = st.store_id
        WHERE st.city = ?
          AND s.as_of_ist_date = ?
          AND ss.status = 'complete'
        GROUP BY io.sku_id
        ORDER BY io.sku_id ASC;
    """
    sku_rows = conn.execute(sku_query, (city, ist_date)).fetchall()
    skus = []
    for r in sku_rows:
        obs = r["observations"]
        stk = r["in_stock"] if r["in_stock"] else 0
        skus.append({
            "sku_id": r["sku_id"],
            "name": r["name"],
            "observations": obs,
            "in_stock": stk,
            "osa_pct": round((stk / obs) * 100.0, 2) if obs > 0 else 0.0,
        })

    return {
        "city": city,
        "date": ist_date,
        "status": "ok",
        "osa_pct": osa_pct,
        "observations": total_obs,
        "coverage": {
            "stores_expected": stores_expected,
            "stores_complete": stores_complete,
            "incomplete": incomplete_list,
        },
        "skus": skus,
    }
