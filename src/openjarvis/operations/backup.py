"""Verified backup/restore primitives for approved OpenJarvis data roots."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

_MAX_FILES = 100_000
_MAX_BYTES = 10 * 1024 * 1024 * 1024


class BackupService:
    def __init__(self, *, source_root: str | Path, excluded_names: set[str] | None = None) -> None:
        self.source = Path(source_root).expanduser().resolve()
        self.source.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.excluded = excluded_names or {".env", ".env.local", "secrets", "credentials.json"}

    def create(self, destination: str | Path) -> dict[str, Any]:
        output = Path(destination).expanduser().resolve()
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        manifest: list[dict[str, Any]] = []
        total = 0
        with tarfile.open(output, "w:gz") as archive:
            for path in sorted(self.source.rglob("*")):
                if not path.is_file() or path.name in self.excluded or output == path:
                    continue
                relative = path.relative_to(self.source)
                size = path.stat().st_size
                total += size
                if len(manifest) >= _MAX_FILES or total > _MAX_BYTES:
                    raise ValueError("backup exceeds configured file or size limit")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                manifest.append({"path": str(relative), "size_bytes": size, "sha256": digest})
                archive.add(path, arcname=str(relative), recursive=False)
            info = {"source": str(self.source), "files": manifest, "total_bytes": total}
            encoded = json.dumps(info, sort_keys=True).encode("utf-8")
            import io
            item = tarfile.TarInfo("MANIFEST.json")
            item.size = len(encoded)
            archive.addfile(item, io.BytesIO(encoded))
        return {"archive": str(output), "files": len(manifest), "total_bytes": total, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}

    def verify(self, archive_path: str | Path) -> dict[str, Any]:
        archive = Path(archive_path).expanduser().resolve()
        if not archive.is_file():
            raise ValueError("backup archive does not exist")
        with tarfile.open(archive, "r:gz") as handle:
            member = handle.getmember("MANIFEST.json")
            manifest = json.loads(handle.extractfile(member).read().decode("utf-8"))
            names = {item.name for item in handle.getmembers() if item.isfile()}
            expected = {str(item["path"]) for item in manifest.get("files", [])} | {"MANIFEST.json"}
            if names != expected:
                raise ValueError("backup manifest does not match archive contents")
        return {"valid": True, "files": len(manifest.get("files", [])), "total_bytes": manifest.get("total_bytes", 0)}

    def restore(self, archive_path: str | Path, destination: str | Path) -> dict[str, Any]:
        verification = self.verify(archive_path)
        target = Path(destination).expanduser().resolve()
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tarfile.open(Path(archive_path).expanduser().resolve(), "r:gz") as handle:
            for member in handle.getmembers():
                target_path = (target / member.name).resolve()
                try:
                    target_path.relative_to(target)
                except ValueError as exc:
                    raise ValueError("archive contains path traversal") from exc
            handle.extractall(target, filter="data")
        return {"restored": True, "destination": str(target), **verification}


__all__ = ["BackupService"]
