"""Allowlisted, data-only handlers for durable jobs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from openjarvis.media.file_index import FileIndexer
from openjarvis.media.video_index import search_transcript

from .store import Job


def file_index_handler(job: Job, progress: Any) -> list[str]:
    root = Path(os.environ.get("OPENJARVIS_FILE_ROOT", "/tmp")) / "openjarvis-files"
    indexer = FileIndexer(root)
    try:
        relative = job.prompt.strip().replace("\\", "/")
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise ValueError("file_index job requires a relative workspace path")
        progress(0.25, {"phase": "validating_path"})
        record = indexer.index_file(root / relative)
        progress(1.0, {"phase": "indexed", "path": record.path, "sha256": record.sha256})
        return [record.path]
    finally:
        indexer.close()


def video_transcript_search_handler(job: Job, progress: Any) -> list[str]:
    try:
        request = json.loads(job.prompt)
        transcript = request.get("transcript", [])
        query = str(request.get("query", ""))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("video_transcript_search prompt must be JSON") from exc
    if not isinstance(transcript, list) or len(transcript) > 100_000 or not query:
        raise ValueError("video_transcript_search requires bounded transcript and query")
    progress(0.5, {"phase": "searching", "segments": len(transcript)})
    hits = search_transcript(transcript, query)
    result_dir = Path(os.environ.get("OPENJARVIS_ARTIFACT_DIR", "/tmp")) / "openjarvis-artifacts"
    result_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = result_dir / f"video-search-{job.id}.json"
    output.write_text(json.dumps({"query": query, "hits": [{"timestamp_seconds": hit.timestamp_seconds, "text": hit.text} for hit in hits]}, ensure_ascii=False), encoding="utf-8")
    progress(1.0, {"phase": "completed", "hits": len(hits)})
    return [str(output)]


__all__ = ["file_index_handler", "video_transcript_search_handler"]
