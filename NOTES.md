# Engineering Notes & Decisions

This document outlines the architectural, data integrity, and reliability decisions implemented for the QuickMart data collection pipeline and OSA reporting API.

---

## 1. Core Architectural Decisions

### Store Tracking Strategy
- **Store Discovery:** The scraper queries `/v1/stores?page=N` and traverses all pages until `next_page is None`, discovering all 30 QuickMart locations across Mumbai, Delhi, and Bengaluru.
- **Tracking Rule:** The scraper tracks **active stores (`is_active == True`)**.
  - `is_active` defines network membership (`false` indicates a decommissioned or permanently shut down store). Decommissioned stores (`MUM-009`, `DEL-010`, `BLR-004`, `BLR-010`) are recorded in the store registry but excluded from active inventory sweeps.
  - `is_serviceable` reflects momentary order-taking capability (which flips during rain, maintenance, or temporary staffing shortages). An unserviceable active store (such as `DEL-006`) remains in the network and maintains inventory snapshots; historical availability must not be conflated with temporary delivery serviceability.
- **Roster:** Exactly **26 active stores** are tracked across the 3 cities (9 Mumbai, 9 Delhi, 8 Bengaluru).

### Rate Limiting & Safe Request Pacing
- **Platform Constraints:**
  - Burst limit: ~8 requests/second across clients (triggers HTTP 429).
  - Fair-use threshold: > 30 inventory requests within any 10-second rolling window triggers automatic soft-ban degradation without HTTP errors.
- **Implementation:**
  - A client-side `RateLimiter` enforces a minimum interval of **0.38 seconds** between consecutive outbound HTTP requests (~2.6 req/sec).
  - This safe sequential execution guarantees the client stays below both the 8 req/s burst limit and the 30 req/10s soft-ban threshold during standard sweeps.

### HTTP 429 & Retry-After Handling
- When the portal responds with HTTP 429:
  - The client extracts the `Retry-After` header value (defaulting to 2.0s if omitted).
  - Adds a randomized jitter buffer (`+0.2s` to `+0.4s`) to prevent thundering herd behavior.
  - Retries up to `MAX_RETRIES = 6` attempts with bounded backoff.

### Transient Server Failures (500 & 503)
- **503 Upstream Unavailable:** QuickMart introduces ~7% random 503 errors.
- **500 Internal Server Error:** QuickMart's `MUM-007` deterministically returns HTTP 500 on the first two attempts of any snapshot.
- **Solution:**
  - Implemented bounded exponential backoff with jitter: `delay = min(8.0, 0.5 * 2^(attempt-1)) + uniform(0.05, 0.20)`.
  - `MAX_RETRIES = 6` ensures `MUM-007` reliably clears its first two 500 errors (succeeding on attempt 3) while absorbing intermittent 503 errors.
  - If retries exhaust, failure is isolated to that specific store: status is marked `incomplete`, reason logged, and the scraper proceeds to the next store. The entire sweep never crashes.

### HTTP Timeouts
- All requests use an explicit timeout of **15.0 seconds**.
- **Rationale:** QuickMart simulates slow requests with an intentional 8-second delay (~3% probability). A short timeout (e.g. 5s) would prematurely abort valid slow responses; leaving timeout unset (`None`) risks indefinitely blocking the scraper thread on dropped TCP sockets. 15 seconds safely accommodates 8-second spikes with headroom while preventing hangs.

### Soft-Ban / Degraded Data Detection Strategy
- **Platform Behavior:** Under sustained load, QuickMart degrades responses: returns HTTP 200 with an empty or truncated `items` array, `next_cursor: None`, and `"meta": {"source": "edge"}` (normal responses return `"meta": {"source": "origin"}`).
- **Strategy:**
  1. **Prevention:** Client-side rate limiting (0.38s interval) prevents crossing the 30 req/10s limit.
  2. **Forensic Detection:** Every response inspects `meta.source`. If `"edge"` is detected, the response is recognized as degraded.
  3. **Automated Recovery:** The platform's soft ban lasts 20 seconds. Upon detection, the scraper logs a warning, pauses for **22.0 seconds** for the ban window to fully expire, and retries.
  4. **Data Integrity:** Degraded edge responses are never recorded as complete data. If unrecovered, the store sweep is marked `incomplete` with reason `"soft ban detected (degraded edge response)"`.

### Partial Snapshots & Missing Data
- QuickMart returns `partial: True` when it cannot assemble a complete snapshot (e.g. `DEL-004` on `2026-09-28T10:30:00Z`).
- **Rule:** Missing data is **never** converted into out-of-stock, and incomplete data is **never** marked complete.
- When `partial: True` is received:
  - Store sweep status is recorded as `incomplete` with reason `"partial snapshot"`.
  - No partial observations are counted towards OSA calculations.
  - The incomplete store and sweep are transparently surfaced in `coverage.incomplete` in the API.

### Database Schema & Transactional Idempotency
- **Schema:** Four relational SQLite tables:
  - `stores`: Roster of all discovered stores and operational flags.
  - `sweeps`: Unique sweep registry mapped to UTC timestamp and IST calendar date.
  - `store_sweeps`: Per-store sweep execution metadata (`complete` / `incomplete`, reason, item counts).
  - `inventory_observations`: Deduplicated observation items (`sweep_id`, `store_id`, `sku_id`, `name`, `in_stock`, `qty`, `price`, `observed_at_utc`).
- **Idempotency:**
  - Sweep ID is deterministic: `sweep_{as_of_utc}`.
  - Before writing observations for a `(sweep_id, store_id)` pair, existing observations are deleted and replaced in a single transaction.
  - Verification: Running `2026-09-28T04:30:00Z` twice resulted in identical observation counts (744) and identical OSA percentages (86.45%).

### Timezone & Midnight Boundary Handling
- Sweeps are conducted at UTC timestamps.
- The `/osa` API requests an **Indian Standard Time (IST = UTC + 5:30)** calendar date.
- UTC timestamps are converted using Python timezone-aware `datetime.astimezone(timezone(timedelta(hours=5, minutes=30)))`.
  - Example: `2026-09-27T19:00:00Z` is `2026-09-28 00:30:00 IST` -> belongs to IST date `2026-09-28`.
- Date matching is performed on the computed IST calendar day, completely avoiding naive substring slicing.

### OSA Calculation Standard
- **Metric Definition:**
  $$\text{OSA \%} = \frac{\sum \text{in\_stock observations across valid complete sweeps}}{\sum \text{total observations across valid complete sweeps}} \times 100$$
- `in_stock` (boolean) is the sole ground truth. `qty` is informational and ignored for availability (accommodates "ghost" inventory where `in_stock=True` but `qty=0`).
- Store percentages and SKU percentages are never averaged (micro-aggregation over all observations).

---

## 2. Answers to Assignment Questions

### Question 1: A brand says Delhi availability dropped from 92% to 41% yesterday. What do you check first?
Before responding to the brand or assuming physical stockouts, verify data pipeline integrity:
1. **Sweep Coverage & Incomplete Stores:** Check `coverage.stores_complete` vs `stores_expected` and `coverage.incomplete` for Delhi on that date. Did multiple Delhi dark stores fail sweeps, return partial snapshots, or experience network timeouts?
2. **Total Observation Counts:** Compare total observation volume in Delhi yesterday vs the prior day. A steep drop in observations indicates that dark stores were missing from sweeps rather than products being out of stock.
3. **Platform Degradation / Soft-Ban:** Verify whether the scraper encountered soft-ban edge responses or rate limiting that caused truncated item listings.
4. **Timezone Misalignment:** Confirm that Delhi's sweeps were correctly mapped to the IST calendar date (particularly around the 18:30 UTC / midnight IST boundary) rather than evaluated against UTC days.
5. **Operational Outages vs Out of Stock:** Check if Delhi dark stores were marked unserviceable or temporarily shut down due to regional disruptions (rain, curfew, staffing).

### Question 2: Dashboard says ₹4.20 lakh but store totals equal ₹4.61 lakh. Which number do you show and why?
**Show the store-level aggregate (₹4.61 lakh) with an explicit reconciliation disclosure:**
- **Why:** In quick-commerce data systems, the bottom-up store-level inventory/sales data represents granular, auditable ground truth. Top-level dashboard summary numbers frequently suffer from asynchronous caching, delayed batch rollups, exclusion of late-synced dark stores, or pre-calculated estimates that do not reflect line-item realities.
- **Best Practice:** Display the verifiable store-level sum (₹4.61 lakh) as the primary figure, display the platform dashboard's figure (₹4.20 lakh) as a reference comparison, and explicitly state the ₹41,000 variance with an explanation of potential sync latency or un-reconciled order cancellations. Never silently alter or manipulate ground-truth observations to match an upstream summary.

### Question 3: Where would you NOT use an AI/LLM in this project and why?
An AI/LLM must **NOT** be used for:
1. **Mathematical and Availability Calculations:** Computing OSA percentages, observation totals, and ratios. LLMs are probabilistic token sequence predictors and are incapable of guaranteed arithmetic precision.
2. **Timezone and Calendar Date Conversions:** Handling UTC-to-IST offsets and midnight calendar boundaries. Deterministic standard-library datetime logic is required.
3. **Scraper Concurrency, Rate Limiting, and Retry State Machines:** Managing HTTP backoff, socket timeouts, and token buckets.
4. **Database Querying and Transaction Control:** Executing database transactions, upserts, and schema validations.
- **Why:** Production data engineering requires absolute determinism, mathematical accuracy, and idempotency. Introducing stochastic LLM inference into calculation or ingestion paths creates non-deterministic hallucinations, silent calculation bugs, and un-reproducible states.

---

## 3. Bonus: Scaling to 20,000 Stores Every 30 Minutes

To scale this pipeline to 20,000 stores every 30 minutes:
1. **Distributed Task Orchestration:** Use Celery, Temporal, or Apache Kafka + worker pools to schedule individual store sweep tasks asynchronously.
2. **Horizontal Scraping Workers & IP Rotation:**
   - 20,000 stores every 30 minutes = ~11.1 stores/second.
   - At ~3 pages per store, this requires ~33.3 requests/second sustained.
   - A distributed proxy pool (residential or data center egress IPs) rotating API requests would prevent IP-level rate limits while enforcing per-store rate limits.
3. **Partitioned Ingestion & High-Throughput Storage:**
   - Migrate from SQLite to a distributed analytical database (such as ClickHouse or PostgreSQL with TimescaleDB) optimized for high-volume time-series ingestion.
   - Buffer observations in memory or Kafka topics and ingest in micro-batches (e.g. 5,000 rows per batch) using bulk insert protocols (`COPY`).
4. **Change Data Capture & Delta Sweeping:**
   - Implement `If-Modified-Since` / ETag headers if supported by partner APIs to only fetch modified inventories, drastically cutting bandwidth and processing time.
