"""
app.py: FastAPI reporting server for QuickMart On-Shelf Availability (OSA).
Provides GET /osa?city=<city>&date=<date> with coverage and SKU-level breakdown.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

import db

IST = timezone(timedelta(hours=5, minutes=30))
SUPPORTED_CITIES = {"Mumbai", "Delhi", "Bengaluru"}
DB_PATH = os.environ.get("DB_PATH", db.DEFAULT_DB_PATH)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensure database schema is ready on server start."""
    db.init_db(DB_PATH)
    yield

app = FastAPI(
    title="DataFuel QuickMart OSA API",
    description="Reports On-Shelf Availability (OSA) and store sweep coverage for QuickMart.",
    version="1.0.0",
    lifespan=lifespan,
)


def get_yesterday_ist() -> str:
    """Calculate yesterday's date in Indian Standard Time (IST)."""
    now_ist = datetime.now(IST)
    yesterday = now_ist - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/osa")
def get_osa(
    city: str = Query(..., description="Target city: Mumbai, Delhi, or Bengaluru"),
    date: str | None = Query(
        None,
        description="IST calendar date (YYYY-MM-DD). Defaults to yesterday in IST.",
    ),
) -> dict[str, Any]:
    # 1. Validate city
    # City matching: case-insensitive check to be user-friendly, but enforce supported names
    normalized_city = None
    for c in SUPPORTED_CITIES:
        if c.lower() == city.strip().lower():
            normalized_city = c
            break

    if not normalized_city:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid city '{city}'. Supported cities are: {', '.join(sorted(SUPPORTED_CITIES))}.",
        )

    # 2. Validate or default date
    target_date = date.strip() if date else get_yesterday_ist()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", target_date):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid date format '{target_date}'. Expected format: YYYY-MM-DD.",
        )

    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid calendar date '{target_date}'.",
        )

    # 3. Retrieve report from SQLite
    with db.get_db(DB_PATH) as conn:
        report = db.get_osa_report(conn, normalized_city, target_date)

    return report
