"""CPU-first file indexing with a SQLite FTS5 search catalog.

The indexer only reads files under an explicit root, applies size/count limits,
and treats parser/OCR dependencies as optional. It never executes a file.
"""

from __future__ import annotations

import hashlib
import mimetypes
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_FILE_BYTES = 100 * 1024 * 1024
_MAX_TEXT_CHARS = 2_000_000
_ALLOWED_SUFFIXES = {".txt", ".md", ".rst", ".json", ".csv", ".log", ".py", ".ts", ".tsx", ".js", ".jsx", ".yaml", ".yml", ".toml", ".ini", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    filename: str
    sha256: str
    size_bytes: int
    mime_type: str
    text_chars: int


class FileIndexer:
    def __init__(self, root_dir: str | Path, db_path: str | Path = "") -> None:
        self.root = Path(root_dir).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db_path = Path(db_path).expanduser().resolve() if db_path else self.root / ".openjarvis-file-index.sqlite3"
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, filename TEXT, sha256 TEXT, size_bytes INTEGER, mime_type TEXT, text_chars INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS file_search USING fts5(path UNINDEXED, filename, content)")
        self._db.commit()

    def _safe_path(self, path: str | Path) -> Path:
        target = Path(path).expanduser().resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("file is outside the configured index root") from exc
        if not target.is_file():
            raise ValueError("file does not exist")
        if target.stat().st_size > _MAX_FILE_BYTES:
            raise ValueError("file exceeds the 100MB indexing limit")
        if target.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise ValueError("file type is not supported by the indexer")
        return target

    def index_file(self, path: str | Path) -> FileRecord:
        target = self._safe_path(path)
        raw = target.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        text = self._extract_text(target, raw)[:_MAX_TEXT_CHARS]
        relative = str(target.relative_to(self.root))
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._db.execute("INSERT OR REPLACE INTO files(path, filename, sha256, size_bytes, mime_type, text_chars) VALUES (?, ?, ?, ?, ?, ?)", (relative, target.name, digest, len(raw), mime, len(text)))
        self._db.execute("DELETE FROM file_search WHERE path = ?", (relative,))
        self._db.execute("INSERT INTO file_search(path, filename, content) VALUES (?, ?, ?)", (relative, target.name, text))
        self._db.commit()
        return FileRecord(relative, target.name, digest, len(raw), mime, len(text))

    def index_tree(self, *, max_files: int = 10_000) -> list[FileRecord]:
        records: list[FileRecord] = []
        for path in sorted(self.root.rglob("*")):
            if len(records) >= max(1, min(int(max_files), 10_000)):
                break
            if path.is_file() and path.name != self.db_path.name and path.suffix.lower() in _ALLOWED_SUFFIXES:
                try:
                    records.append(self.index_file(path))
                except ValueError:
                    continue
        return records

    def search(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        safe_limit = max(1, min(int(limit), 100))
        try:
            rows = self._db.execute("SELECT path, filename, snippet(file_search, 2, '[', ']', '…', 20) FROM file_search WHERE file_search MATCH ? LIMIT ?", (query, safe_limit)).fetchall()
        except sqlite3.OperationalError:
            needle = f"%{query}%"
            rows = self._db.execute("SELECT path, filename, substr(content, 1, 240) FROM file_search WHERE filename LIKE ? OR content LIKE ? LIMIT ?", (needle, needle, safe_limit)).fetchall()
        return [{"path": row[0], "filename": row[1], "snippet": row[2]} for row in rows]

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _extract_text(path: Path, raw: bytes) -> str:
        if path.suffix.lower() in {".txt", ".md", ".rst", ".json", ".csv", ".log", ".py", ".ts", ".tsx", ".js", ".jsx", ".yaml", ".yml", ".toml", ".ini"}:
            return raw.decode("utf-8", errors="replace")
        if path.suffix.lower() == ".pdf":
            try:
                import pdfplumber
                with pdfplumber.open(path) as pdf:
                    return "\n".join((page.extract_text() or "") for page in pdf.pages[:100])
            except Exception:
                return ""
        try:
            from PIL import Image
            import pytesseract
            with Image.open(path) as image:
                return pytesseract.image_to_string(image)
        except Exception:
            return ""


__all__ = ["FileIndexer", "FileRecord"]
