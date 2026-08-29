"""Authenticated allowlisted news-monitor endpoints."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from openjarvis.monitoring.news import NewsMonitor

router = APIRouter(prefix="/api/monitor/news", tags=["news"])


class NewsFetchRequest(BaseModel):
    feed_url: str = Field(min_length=1, max_length=2048)


def _monitor(request: Request) -> NewsMonitor:
    monitor = getattr(request.app.state, "news_monitor", None)
    if monitor is None:
        configured = [item.strip() for item in os.environ.get("OPENJARVIS_NEWS_FEEDS", "").split(",") if item.strip()]
        monitor = NewsMonitor(configured)
        request.app.state.news_monitor = monitor
    return monitor


@router.get("/feeds")
async def feeds(request: Request) -> dict[str, Any]:
    return {"feeds": list(_monitor(request).allowed_feeds)}


@router.post("/fetch")
async def fetch(payload: NewsFetchRequest, request: Request) -> dict[str, Any]:
    try:
        items = _monitor(request).fetch(payload.feed_url)
        return {"items": [{"item_id": item.item_id, "title": item.title, "url": item.url, "summary": item.summary, "published_at": item.published_at, "source": item.source} for item in items]}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


__all__ = ["router"]
