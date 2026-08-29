"""Authenticated file indexing endpoints with explicit workspace boundaries."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from pydantic import BaseModel, Field

from openjarvis.media.file_index import FileIndexer

router = APIRouter(prefix="/api/files", tags=["files"])


class FileSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=50, ge=1, le=100)


class FileIndexPathRequest(BaseModel):
    relative_path: str = Field(min_length=1, max_length=4096)


def _root(request: Request) -> Path:
    root = Path(os.environ.get("OPENJARVIS_FILE_ROOT", tempfile.gettempdir())) / "openjarvis-files"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _indexer(request: Request) -> FileIndexer:
    indexer = getattr(request.app.state, "file_indexer", None)
    if indexer is None:
        root = _root(request)
        indexer = FileIndexer(root)
        request.app.state.file_indexer = indexer
    return indexer


@router.post("/upload")
async def upload(request: Request, upload: UploadFile = File(...)) -> dict[str, Any]:
    if not upload.filename:
        raise HTTPException(status_code=400, detail="filename is required")
    name = Path(upload.filename).name
    if name in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="invalid filename")
    target = _root(request) / name
    if target.exists():
        target = _root(request) / f"{target.stem}-{os.urandom(6).hex()}{target.suffix}"
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="file exceeds 100MB upload limit")
                output.write(chunk)
        record = _indexer(request).index_file(target)
        return {"path": record.path, "filename": record.filename, "sha256": record.sha256, "size_bytes": record.size_bytes, "mime_type": record.mime_type, "text_chars": record.text_chars}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        # Files remain in the explicit user-owned workspace for re-index/search.
        pass


@router.post("/index")
async def index_path(payload: FileIndexPathRequest, request: Request) -> dict[str, Any]:
    try:
        record = _indexer(request).index_file(_root(request) / payload.relative_path)
        return {"path": record.path, "filename": record.filename, "sha256": record.sha256, "size_bytes": record.size_bytes, "mime_type": record.mime_type, "text_chars": record.text_chars}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/index-tree")
async def index_tree(request: Request, max_files: int = 1000) -> dict[str, Any]:
    records = _indexer(request).index_tree(max_files=max_files)
    return {"indexed": len(records), "files": [record.path for record in records]}


@router.post("/search")
async def search(payload: FileSearchRequest, request: Request) -> dict[str, Any]:
    return {"query": payload.query, "results": _indexer(request).search(payload.query, limit=payload.limit)}


def install_file_routes(app: Any) -> None:
    app.include_router(router)


__all__ = ["install_file_routes", "router"]
