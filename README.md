# DataFuel Backend Take-Home: QuickMart OSA Pipeline & API

A production-grade Python backend system built for QuickMart quick-commerce inventory collection and On-Shelf Availability (OSA) reporting.

---

## Architecture Overview

```
                      +-----------------------------+
                      |   QuickMart Partner Portal  |
                      |      (mock_portal.py)       |
                      +--------------+--------------+
                                     |
                         HTTP (Rate-limited, 429,
                         500/503 errors, Soft-ban)
                                     v
                        +-------------------------+
                        |      sweep.py CLI       |
                        | (Scraper, RateLimiter,  |
                        |  Backoff, Dedup, Sync)  |
                        +------------+------------+
                                     |
                       Transactional Parameterized
                                    SQL
                                     v
                      +-----------------------------+
                      |   SQLite Database Storage   |
                      |       (quickmart.db)        |
                      +--------------+--------------+
                                     |
                       Micro-aggregate queries &
                       coverage reporting
                                     v
                        +-------------------------+
                        |       app.py API        |
                        |    (FastAPI/Uvicorn)    |
                        | GET /osa?city=..&date=..|
                        +-------------------------+
```

### Core Components
1. **Scraper (`sweep.py`):**
   - Automated store discovery via `/v1/stores?page=N`.
   - Active store roster tracking (26 active stores across Mumbai, Delhi, Bengaluru).
   - Safe sequential request pacing (`RateLimiter` with `min_interval = 0.38s`).
   - Bounded retries with exponential backoff and jitter for transient errors (500, 503).
   - Dynamic 429 rate-limit backoff respecting `Retry-After` headers.
   - Forensic soft-ban detection via `meta.source == "edge"` with automated recovery wait.
   - Transparent handling of partial snapshots (`partial: True`).
   - Multi-page cursor pagination with in-memory SKU deduplication.
2. **Database Engine (`db.py`):**
   - SQLite with WAL (Write-Ahead Logging) mode and foreign key enforcement.
   - Fully parameterized SQL preventing SQL injection and syntax breakage.
   - Deterministic sweep identifiers (`sweep_{as_of_utc}`).
   - Atomic transactions ensuring 100% idempotency across re-runs.
3. **Reporting Service (`app.py`):**
   - High-performance FastAPI server providing `GET /osa?city=<city>&date=<date>`.
   - Robust input validation (supported cities: `Mumbai`, `Delhi`, `Bengaluru`).
   - Accurate UTC to IST calendar date conversion.
   - Micro-aggregate OSA calculation: $\frac{\sum \text{in\_stock}}{\sum \text{observations}} \times 100$.
   - Explicit `status: "no_data"` for days lacking data (never returns 0%).
   - Transparent coverage metrics exposing incomplete dark stores.
4. **Test Suite (`tests/test_osa.py`):**
   - 9 automated pytest tests covering boundary conversions, idempotency, ghost inventory, deduplication, and HTTP error handling.

---

## Installation & Setup

### Prerequisites
- Python 3.10+ (tested on Python 3.14)
- Git

### 1. Clone the Repository
```bash
git clone https://github.com/REDDIPALLIPHANIKOUSHIK/DataFuel_backend_Assignment.git
cd DataFuel_backend_Assignment
```

### 2. Create and Activate Virtual Environment
```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

---

## Running the Pipeline

### Step 1: Start the QuickMart Mock Portal
In Terminal 1:
```bash
python mock_portal.py
```
*QuickMart serves on `http://127.0.0.1:8765`. Keep this terminal open.*

Verify it is running:
```bash
curl http://127.0.0.1:8765/v1/health
# Response: {"ok": true}
```

---

### Step 2: Run the Historical Inventory Sweeps
In Terminal 2, run all six required sweeps in sequence:

```bash
# Sweep 1 (IST Date: 2026-09-27)
python sweep.py --as-of 2026-09-27T04:30:00Z

# Sweep 2 (IST Date: 2026-09-27)
python sweep.py --as-of 2026-09-27T10:30:00Z

# Sweep 3 (IST Date: 2026-09-28)
python sweep.py --as-of 2026-09-27T19:00:00Z

# Sweep 4 (IST Date: 2026-09-28)
python sweep.py --as-of 2026-09-28T04:30:00Z

# Sweep 5 (IST Date: 2026-09-28) - captures DEL-004 partial snapshot
python sweep.py --as-of 2026-09-28T10:30:00Z

# Sweep 6 (IST Date: 2026-09-29)
python sweep.py --as-of 2026-09-28T18:40:00Z
```

#### Example Sweep Output
```
============================================================
SWEEP SUMMARY: 2026-09-28T10:30:00Z (IST: 2026-09-28)
============================================================
26 stores: 25 complete, 1 incomplete (DEL-004: partial snapshot) · 27.8s
Store:
DEL-004

Reason:
partial snapshot

Duration:
27.8s
============================================================
```

---

### Step 3: Start the FastAPI Server
In Terminal 2 (or Terminal 3):
```bash
uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```
*API docs are accessible at `http://127.0.0.1:8000/docs`.*

---

### Step 4: Query the OSA Report API

#### 1. Mumbai on 2026-09-28
```bash
curl "http://127.0.0.1:8000/osa?city=Mumbai&date=2026-09-28"
```
```json
{
  "city": "Mumbai",
  "date": "2026-09-28",
  "status": "ok",
  "osa_pct": 86.06,
  "observations": 753,
  "coverage": {
    "stores_expected": 27,
    "stores_complete": 27,
    "incomplete": []
  },
  "skus": [ ... ]
}
```

#### 2. Delhi on 2026-09-28 (Includes Partial Coverage)
```bash
curl "http://127.0.0.1:8000/osa?city=Delhi&date=2026-09-28"
```
```json
{
  "city": "Delhi",
  "date": "2026-09-28",
  "status": "ok",
  "osa_pct": 79.63,
  "observations": 761,
  "coverage": {
    "stores_expected": 27,
    "stores_complete": 26,
    "incomplete": [
      {
        "store_id": "DEL-004",
        "sweep": "2026-09-28T10:30:00Z",
        "reason": "partial snapshot"
      }
    ]
  },
  "skus": [ ... ]
}
```

#### 3. Bengaluru on 2026-09-28
```bash
curl "http://127.0.0.1:8000/osa?city=Bengaluru&date=2026-09-28"
```
```json
{
  "city": "Bengaluru",
  "date": "2026-09-28",
  "status": "ok",
  "osa_pct": 89.57,
  "observations": 690,
  "coverage": {
    "stores_expected": 24,
    "stores_complete": 24,
    "incomplete": []
  },
  "skus": [ ... ]
}
```

#### 4. Invalid City Handling
```bash
curl "http://127.0.0.1:8000/osa?city=Kolkata&date=2026-09-28"
# HTTP 400 Bad Request
# {"detail": "Invalid city 'Kolkata'. Supported cities are: Bengaluru, Delhi, Mumbai."}
```

#### 5. No Data Handling
```bash
curl "http://127.0.0.1:8000/osa?city=Mumbai&date=2026-09-15"
# {"city": "Mumbai", "date": "2026-09-15", "status": "no_data", "osa_pct": null, "observations": 0, ...}
```

---

## Running the Automated Test Suite

Run the full pytest suite:
```bash
python -m pytest -v
```

All 9 tests pass cleanly:
```
tests/test_osa.py::test_utc_to_ist_boundary PASSED                       [ 11%]
tests/test_osa.py::test_in_stock_used_instead_of_qty PASSED              [ 22%]
tests/test_osa.py::test_duplicate_observations_do_not_inflate_osa PASSED [ 33%]
tests/test_osa.py::test_incomplete_sweep_not_interpreted_as_out_of_stock PASSED [ 44%]
tests/test_osa.py::test_aggregate_osa_calculation PASSED                 [ 55%]
tests/test_osa.py::test_sweep_idempotency PASSED                         [ 66%]
tests/test_osa.py::test_no_data_does_not_return_zero_percent PASSED      [ 77%]
tests/test_osa.py::test_fastapi_invalid_city_returns_400 PASSED          [ 88%]
tests/test_osa.py::test_fastapi_valid_request PASSED                     [100%]

============================== 9 passed in 0.78s ==============================
```

---

## Idempotency Verification

Running any sweep multiple times produces identical database rows and identical calculations:
```bash
# First execution
python sweep.py --as-of 2026-09-28T04:30:00Z
# Database Observations: 744 | Mumbai OSA: 86.45% (251 observations)

# Second execution
python sweep.py --as-of 2026-09-28T04:30:00Z
# Database Observations: 744 | Mumbai OSA: 86.45% (251 observations)
```

---

## Repository Files

| File | Purpose | Status |
|---|---|---|
| `mock_portal.py` | QuickMart Partner Portal mock server | **Preserved Unchanged** |
| `API.md` | QuickMart API specification contract | **Preserved Unchanged** |
| `sweep.py` | Reliable CLI inventory scraper | **Created** |
| `db.py` | SQLite schema and transactional query engine | **Created** |
| `app.py` | FastAPI server reporting `/osa` with coverage | **Created** |
| `review_me.py` | Fixed critical bugs while preserving structure | **Modified & Fixed** |
| `REVIEW.md` | In-depth code review of `review_me.py` (8 issues) | **Created** |
| `NOTES.md` | Architectural decisions and question answers | **Created** |
| `AI_LOG.md` | Authentic log of AI prompts, caught errors & fixes | **Created** |
| `RECORDING.md` | Video recording and walkthrough placeholders | **Created** |
| `tests/test_osa.py`| Automated test suite covering mandatory requirements | **Created** |
| `requirements.txt` | Python package dependencies | **Created** |
| `.gitignore` | Excludes databases, virtual environments, and caches | **Created** |
