"""
Comprehensive test suite for QuickMart OSA backend.
Tests:
1. Duplicate observations do not inflate OSA.
2. Incomplete sweep is not interpreted as out-of-stock.
3. UTC -> IST midnight boundary conversion.
4. in_stock is used as source of truth instead of qty.
5. No-data responses return status 'no_data' with osa_pct null (never 0%).
6. Invalid city returns HTTP 400 with a clear error message.
7. City OSA uses total in-stock / total valid observations (micro-aggregation).
8. Sweep execution is idempotent.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import db
from app import app
from sweep import parse_and_validate_timestamp


@pytest.fixture
def test_db():
    """Create an isolated, temporary SQLite database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_utc_to_ist_boundary():
    """Test that timestamps around the UTC->IST midnight boundary are properly assigned."""
    # 18:29:59 UTC + 5:30 = 23:59:59 IST (previous day)
    _, utc_str1, ist_date1 = parse_and_validate_timestamp("2026-09-27T18:29:59Z")
    assert ist_date1 == "2026-09-27"
    assert utc_str1 == "2026-09-27T18:29:59Z"

    # 18:30:00 UTC + 5:30 = 00:00:00 IST (next day midnight)
    _, utc_str2, ist_date2 = parse_and_validate_timestamp("2026-09-27T18:30:00Z")
    assert ist_date2 == "2026-09-28"
    assert utc_str2 == "2026-09-27T18:30:00Z"

    # 19:00:00 UTC + 5:30 = 00:30:00 IST on 2026-09-28
    _, utc_str3, ist_date3 = parse_and_validate_timestamp("2026-09-27T19:00:00Z")
    assert ist_date3 == "2026-09-28"

    # Naive timestamp without timezone must raise ValueError
    with pytest.raises(ValueError, match="must include a timezone"):
        parse_and_validate_timestamp("2026-09-28T04:30:00")


def test_in_stock_used_instead_of_qty(test_db):
    """
    Test that in_stock is the sole availability source of truth.
    Items with in_stock=True and qty=0 (ghost inventory) MUST count as in-stock.
    Items with in_stock=False and qty>0 MUST count as out-of-stock.
    """
    with db.get_db(test_db) as conn:
        db.sync_stores(conn, [
            {"store_id": "MUM-001", "city": "Mumbai", "name": "MUM 1", "is_active": True, "is_serviceable": True}
        ])
        sweep_id = db.get_or_create_sweep(conn, "2026-09-28T04:30:00Z", "2026-09-28")

        items = [
            # Ghost stock: in_stock True, qty 0 -> should be IN STOCK
            {"sku_id": "SKU-0001", "name": "Item 1", "in_stock": True, "qty": 0, "price": 100.0, "observed_at": "2026-09-28T04:30:00Z"},
            # In stock True, qty 10 -> should be IN STOCK
            {"sku_id": "SKU-0002", "name": "Item 2", "in_stock": True, "qty": 10, "price": 50.0, "observed_at": "2026-09-28T04:30:00Z"},
            # Out of stock False, qty 5 -> should be OUT OF STOCK
            {"sku_id": "SKU-0003", "name": "Item 3", "in_stock": False, "qty": 5, "price": 20.0, "observed_at": "2026-09-28T04:30:00Z"},
        ]
        db.save_store_sweep_result(conn, sweep_id, "MUM-001", "complete", None, items)

        report = db.get_osa_report(conn, "Mumbai", "2026-09-28")
        assert report["status"] == "ok"
        assert report["observations"] == 3
        # 2 in stock out of 3 = 66.67%
        assert report["osa_pct"] == 66.67

        # Check SKU-0001 specifically
        sku1 = next(s for s in report["skus"] if s["sku_id"] == "SKU-0001")
        assert sku1["in_stock"] == 1
        assert sku1["osa_pct"] == 100.0


def test_duplicate_observations_do_not_inflate_osa(test_db):
    """Test that duplicate observations across cursor pages are deduplicated."""
    with db.get_db(test_db) as conn:
        db.sync_stores(conn, [
            {"store_id": "MUM-001", "city": "Mumbai", "name": "MUM 1", "is_active": True, "is_serviceable": True}
        ])
        sweep_id = db.get_or_create_sweep(conn, "2026-09-28T04:30:00Z", "2026-09-28")

        # Simulate duplicate item sent by portal pagination
        items = [
            {"sku_id": "SKU-0001", "name": "Item 1", "in_stock": True, "qty": 5, "price": 10.0, "observed_at": "2026-09-28T04:30:00Z"},
            {"sku_id": "SKU-0001", "name": "Item 1", "in_stock": True, "qty": 5, "price": 10.0, "observed_at": "2026-09-28T04:30:00Z"},
            {"sku_id": "SKU-0002", "name": "Item 2", "in_stock": False, "qty": 0, "price": 10.0, "observed_at": "2026-09-28T04:30:00Z"},
        ]
        db.save_store_sweep_result(conn, sweep_id, "MUM-001", "complete", None, items)

        report = db.get_osa_report(conn, "Mumbai", "2026-09-28")
        assert report["observations"] == 2  # Deduplicated from 3 items to 2 SKUs
        assert report["osa_pct"] == 50.0  # 1 in stock / 2 obs


def test_incomplete_sweep_not_interpreted_as_out_of_stock(test_db):
    """
    Test that an incomplete or failed sweep is recorded as incomplete in coverage,
    and its missing products are NEVER fabricated or counted as out-of-stock.
    """
    with db.get_db(test_db) as conn:
        db.sync_stores(conn, [
            {"store_id": "DEL-001", "city": "Delhi", "name": "DEL 1", "is_active": True, "is_serviceable": True},
            {"store_id": "DEL-004", "city": "Delhi", "name": "DEL 4", "is_active": True, "is_serviceable": True},
        ])
        sweep_id = db.get_or_create_sweep(conn, "2026-09-28T10:30:00Z", "2026-09-28")

        # DEL-001 is complete
        del_1_items = [
            {"sku_id": "SKU-0001", "name": "Item 1", "in_stock": True, "qty": 10, "price": 20.0, "observed_at": "2026-09-28T10:30:00Z"},
        ]
        db.save_store_sweep_result(conn, sweep_id, "DEL-001", "complete", None, del_1_items)

        # DEL-004 returned partial snapshot
        db.save_store_sweep_result(conn, sweep_id, "DEL-004", "incomplete", "partial snapshot", [])

        report = db.get_osa_report(conn, "Delhi", "2026-09-28")
        # Only DEL-001's 1 observation should be counted
        assert report["observations"] == 1
        assert report["osa_pct"] == 100.0

        # Coverage must show 2 expected, 1 complete, and DEL-004 incomplete
        cov = report["coverage"]
        assert cov["stores_expected"] == 2
        assert cov["stores_complete"] == 1
        assert len(cov["incomplete"]) == 1
        assert cov["incomplete"][0]["store_id"] == "DEL-004"
        assert cov["incomplete"][0]["reason"] == "partial snapshot"


def test_aggregate_osa_calculation(test_db):
    """
    Test that city OSA is calculated as total in_stock / total valid observations,
    NOT as the unweighted average of per-store percentages.
    """
    with db.get_db(test_db) as conn:
        db.sync_stores(conn, [
            {"store_id": "BLR-001", "city": "Bengaluru", "name": "BLR 1", "is_active": True, "is_serviceable": True},
            {"store_id": "BLR-002", "city": "Bengaluru", "name": "BLR 2", "is_active": True, "is_serviceable": True},
        ])
        sweep_id = db.get_or_create_sweep(conn, "2026-09-28T04:30:00Z", "2026-09-28")

        # BLR-001 has 10 items, all 10 in stock (100%)
        items_store_1 = [
            {"sku_id": f"SKU-{i:04d}", "name": f"P{i}", "in_stock": True, "qty": 5, "price": 10.0, "observed_at": "2026-09-28T04:30:00Z"}
            for i in range(1, 11)
        ]
        db.save_store_sweep_result(conn, sweep_id, "BLR-001", "complete", None, items_store_1)

        # BLR-002 has 90 items, 45 in stock, 45 out of stock (50%)
        items_store_2 = [
            {"sku_id": f"SKU-{i:04d}", "name": f"P{i}", "in_stock": (i <= 55), "qty": 5 if i <= 55 else 0, "price": 10.0, "observed_at": "2026-09-28T04:30:00Z"}
            for i in range(11, 101)
        ]
        db.save_store_sweep_result(conn, sweep_id, "BLR-002", "complete", None, items_store_2)

        report = db.get_osa_report(conn, "Bengaluru", "2026-09-28")
        # Total observations: 10 + 90 = 100
        # Total in stock: 10 + 45 = 55
        # Micro-aggregate OSA = 55.0%
        # (Macro-average would incorrectly give (100 + 50) / 2 = 75.0%)
        assert report["observations"] == 100
        assert report["osa_pct"] == 55.0


def test_sweep_idempotency(test_db):
    """Test that running the exact same sweep twice does NOT create duplicates or alter OSA."""
    with db.get_db(test_db) as conn:
        db.sync_stores(conn, [
            {"store_id": "MUM-001", "city": "Mumbai", "name": "MUM 1", "is_active": True, "is_serviceable": True}
        ])
        sweep_id = db.get_or_create_sweep(conn, "2026-09-28T04:30:00Z", "2026-09-28")

        items = [
            {"sku_id": "SKU-0001", "name": "Bread", "in_stock": True, "qty": 5, "price": 40.0, "observed_at": "2026-09-28T04:30:00Z"},
            {"sku_id": "SKU-0002", "name": "Milk", "in_stock": False, "qty": 0, "price": 30.0, "observed_at": "2026-09-28T04:30:00Z"},
        ]
        # First execution
        db.save_store_sweep_result(conn, sweep_id, "MUM-001", "complete", None, items)
        rep1 = db.get_osa_report(conn, "Mumbai", "2026-09-28")

        # Second execution (simulating re-running sweep)
        db.save_store_sweep_result(conn, sweep_id, "MUM-001", "complete", None, items)
        rep2 = db.get_osa_report(conn, "Mumbai", "2026-09-28")

        # Total observations and OSA must remain identical
        assert rep1["observations"] == rep2["observations"] == 2
        assert rep1["osa_pct"] == rep2["osa_pct"] == 50.0

        # Verify raw table row count
        count = conn.execute("SELECT COUNT(*) FROM inventory_observations").fetchone()[0]
        assert count == 2


def test_no_data_does_not_return_zero_percent(test_db):
    """Test that a date with no sweeps returns status 'no_data' with osa_pct null, not 0%."""
    with db.get_db(test_db) as conn:
        db.sync_stores(conn, [
            {"store_id": "MUM-001", "city": "Mumbai", "name": "MUM 1", "is_active": True, "is_serviceable": True}
        ])
        report = db.get_osa_report(conn, "Mumbai", "2026-10-15")

        assert report["status"] == "no_data"
        assert report["osa_pct"] is None
        assert report["observations"] == 0
        assert report["skus"] == []


def test_fastapi_invalid_city_returns_400(monkeypatch, test_db):
    """Test that requesting an invalid city on GET /osa returns HTTP 400 with helpful message."""
    monkeypatch.setattr("app.DB_PATH", test_db)
    client = TestClient(app)

    response = client.get("/osa?city=Chennai&date=2026-09-28")
    assert response.status_code == 400
    assert "Invalid city 'Chennai'" in response.json()["detail"]


def test_fastapi_valid_request(monkeypatch, test_db):
    """Test GET /osa through FastAPI client with populated data."""
    monkeypatch.setattr("app.DB_PATH", test_db)
    client = TestClient(app)

    with db.get_db(test_db) as conn:
        db.sync_stores(conn, [
            {"store_id": "MUM-001", "city": "Mumbai", "name": "MUM 1", "is_active": True, "is_serviceable": True}
        ])
        sweep_id = db.get_or_create_sweep(conn, "2026-09-28T04:30:00Z", "2026-09-28")
        items = [
            {"sku_id": "SKU-0001", "name": "Bread", "in_stock": True, "qty": 5, "price": 40.0, "observed_at": "2026-09-28T04:30:00Z"},
        ]
        db.save_store_sweep_result(conn, sweep_id, "MUM-001", "complete", None, items)

    response = client.get("/osa?city=Mumbai&date=2026-09-28")
    assert response.status_code == 200
    data = response.json()
    assert data["city"] == "Mumbai"
    assert data["date"] == "2026-09-28"
    assert data["status"] == "ok"
    assert data["osa_pct"] == 100.0
    assert data["observations"] == 1
    assert data["coverage"]["stores_complete"] == 1
    assert len(data["skus"]) == 1
