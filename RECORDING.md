# Screen Recording & Walkthrough Links

This document contains placeholders and timestamp structures for the full screen session recording and the 5-minute technical walkthrough video.

---

## 1. Full Screen Session Recordings

> *Links to unlisted YouTube video(s) or public Google Drive recordings showing the entire development session.*

- **Session Recording (Part 1):** `[INSERT_UNLISTED_YOUTUBE_OR_GDRIVE_LINK_HERE]`
- **Session Recording (Part 2 - if applicable):** `[INSERT_UNLISTED_YOUTUBE_OR_GDRIVE_LINK_HERE]`

### Key Moments & Timestamps
- `00:00:00` - Understanding problem statement and reviewing `API.md` / `mock_portal.py`
- `00:15:30` - Reviewing `review_me.py` and documenting critical bugs in `REVIEW.md`
- `00:35:00` - Database schema design with SQLite WAL mode and foreign key constraints
- `00:55:00` - Developing `sweep.py`: Rate limiting, 429 backoff, and soft-ban detection
- `01:25:00` - Building FastAPI `app.py` for `/osa` and handling UTC/IST timezone conversion
- `01:45:00` - Writing comprehensive pytest test suite (`tests/test_osa.py`)
- `02:05:00` - Running mock portal, executing all 6 historical sweeps, and verifying idempotency
- `02:30:00` - Final verification and git commits

---

## 2. Technical Walkthrough Video (5 Minutes)

> *5-minute voice walkthrough explaining the architecture, the hardest bug encountered, and future scalability improvements.*

- **Walkthrough Link:** `[INSERT_UNLISTED_YOUTUBE_OR_GDRIVE_LINK_HERE]`

### Walkthrough Outline
1. **System Overview (0:00 - 1:15):** Architecture of the scraping pipeline, SQLite transactional persistence, and the FastAPI reporting layer.
2. **Handling Portal Quirks (1:15 - 2:30):** How soft-ban degradation was detected using `meta.source == "edge"`, handling 429 with `Retry-After`, and accommodating MUM-007's intentional 500 errors.
3. **Hardest Bug (2:30 - 3:45):** Compound 500 and 503 errors during rapid retry sequences, and ensuring strict micro-aggregation for OSA metrics.
4. **Future Improvements (3:45 - 5:00):** Scaling to 20,000 stores with distributed task queues (Temporal/Celery) and proxy pools.
