"""Seed a small deterministic demo snapshot for the API recording.

This is an optional demo helper only; production sweep data is still produced by sweep.py.
It creates one historical sweep for 2026-09-28 at 04:30 UTC across active Mumbai,
Delhi and Bengaluru stores so /osa returns meaningful JSON during a demo.
"""
from datetime import datetime, timezone
import db

AS_OF = "2026-09-28T04:30:00Z"
IST_DATE = "2026-09-28"

CITIES = {
    "Mumbai": "MUM",
    "Delhi": "DEL",
    "Bengaluru": "BLR",
}
INACTIVE = {"MUM-009", "DEL-010", "BLR-004", "BLR-010"}
SKU_NAMES = {
    "SKU-0001": "Amul Taaza Toned Milk 500ml",
    "SKU-0002": "Britannia Brown Bread 400g",
    "SKU-0003": "Maggi 2-Minute Noodles 280g",
    "SKU-0004": "Tata Salt 1kg",
}


def main() -> None:
    db.init_db()
    with db.get_db() as conn:
        stores = []
        for city, code in CITIES.items():
            for i in range(1, 11):
                store_id = f"{code}-{i:03d}"
                if store_id in INACTIVE:
                    continue
                stores.append({
                    "store_id": store_id,
                    "city": city,
                    "name": f"QuickMart {city} #{i}",
                    "is_active": True,
                    "is_serviceable": True,
                })
        db.sync_stores(conn, stores)
        sweep_id = db.get_or_create_sweep(conn, AS_OF, IST_DATE)

        # Small deterministic fixture: 4 SKUs per active store.
        for store in stores:
            items = []
            for index, (sku_id, name) in enumerate(SKU_NAMES.items(), start=1):
                # Deliberately varied stock pattern to make the report non-trivial.
                in_stock = ((index + int(store["store_id"][-3:])) % 5) != 0
                items.append({
                    "sku_id": sku_id,
                    "name": name,
                    "in_stock": in_stock,
                    "qty": 12 if in_stock else 0,
                    "price": 100 + index,
                    "observed_at": AS_OF,
                })
            db.save_store_sweep_result(conn, sweep_id, store["store_id"], "complete", None, items)

    print("Demo data seeded for Mumbai, Delhi and Bengaluru on 2026-09-28.")
    print("Run: python -m uvicorn app:app --reload")
    print("Then open: http://127.0.0.1:8000/osa?city=Mumbai&date=2026-09-28")


if __name__ == "__main__":
    main()
