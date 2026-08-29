"""Bounded CPU-only 3D inspection and preview primitives.

Only geometry data is parsed. No shaders, plugins, embedded scripts, or external
references are executed. Rendering is capped for predictable CPU and memory use.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_MODEL_BYTES = 100 * 1024 * 1024
_MAX_VERTICES = 1_000_000
_MAX_FACES = 500_000


@dataclass(frozen=True, slots=True)
class Model3DStats:
    filename: str
    format: str
    vertices: int
    faces: int
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]


def _validate(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise ValueError("3D model must be an existing file")
    if target.stat().st_size > _MAX_MODEL_BYTES:
        raise ValueError("3D model exceeds the 100MB limit")
    if target.suffix.lower() not in {".obj", ".stl", ".gltf"}:
        raise ValueError("only OBJ, STL, and glTF files are supported")
    return target


def _bounds(vertices: list[tuple[float, float, float]]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if not vertices:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    return tuple(min(v[i] for v in vertices) for i in range(3)), tuple(max(v[i] for v in vertices) for i in range(3))


def _obj(path: Path) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.strip().split()
            if not parts:
                continue
            if parts[0] == "v" and len(parts) >= 4 and len(vertices) < _MAX_VERTICES:
                try:
                    vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    continue
            elif parts[0] == "f" and len(parts) >= 4 and len(faces) < _MAX_FACES:
                indices = []
                for token in parts[1:4]:
                    try:
                        indices.append(int(token.split("/")[0]) - 1)
                    except ValueError:
                        indices = []
                        break
                if len(indices) == 3 and all(0 <= i < len(vertices) for i in indices):
                    faces.append(tuple(indices))
    return vertices, faces


def _stl(path: Path) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    data = path.read_bytes()
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + count * 50 == len(data):
            for offset in range(84, len(data), 50):
                tri = []
                for index in range(3):
                    tri.append(struct.unpack_from("<fff", data, offset + 12 + index * 12))
                base = len(vertices)
                vertices.extend(tri)
                faces.append((base, base + 1, base + 2))
                if len(faces) >= _MAX_FACES:
                    break
            return vertices, faces
    for line in data.decode("utf-8", errors="replace").splitlines():
        parts = line.strip().split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            try:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                if len(vertices) % 3 == 0:
                    base = len(vertices) - 3
                    faces.append((base, base + 1, base + 2))
            except ValueError:
                continue
        if len(faces) >= _MAX_FACES:
            break
    return vertices[:_MAX_VERTICES], faces[:_MAX_FACES]


def inspect_model(path: str | Path) -> Model3DStats:
    target = _validate(path)
    if target.suffix.lower() == ".obj":
        vertices, faces = _obj(target)
        fmt = "obj"
    elif target.suffix.lower() == ".stl":
        vertices, faces = _stl(target)
        fmt = "stl"
    else:
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid glTF JSON") from exc
        vertices = []
        faces = []
        # glTF is inspected as a manifest only; binary buffers and external URIs
        # are deliberately not fetched or executed by this CPU-only endpoint.
        for mesh in document.get("meshes", []):
            for primitive in mesh.get("primitives", []):
                faces.extend([(-1, -1, -1)] * min(int(primitive.get("count", 0) or 0), _MAX_FACES - len(faces)))
        return Model3DStats(target.name, "gltf", 0, len(faces), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    low, high = _bounds(vertices)
    return Model3DStats(target.name, fmt, len(vertices), len(faces), low, high)


def render_preview(path: str | Path, output_path: str | Path) -> str:
    target = _validate(path)
    if target.suffix.lower() == ".obj":
        vertices, faces = _obj(target)
    elif target.suffix.lower() == ".stl":
        vertices, faces = _stl(target)
    else:
        raise ValueError("glTF preview requires a renderer integration; manifest inspection is supported")
    if not vertices or not faces:
        raise ValueError("model has no renderable geometry")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    except ImportError as exc:
        raise ValueError("matplotlib is required for 3D preview") from exc
    sampled = faces[:5000]
    polygons = [[vertices[i] for i in face] for face in sampled]
    figure = plt.figure(figsize=(8, 6), dpi=120)
    axis = figure.add_subplot(111, projection="3d")
    axis.add_collection3d(Poly3DCollection(polygons, alpha=0.75, linewidths=0.15, edgecolor="#24415c"))
    low, high = _bounds(vertices)
    center = [(low[i] + high[i]) / 2 for i in range(3)]
    radius = max(max(high[i] - low[i] for i in range(3)) / 2, 0.5)
    axis.set_xlim(center[0] - radius, center[0] + radius)
    axis.set_ylim(center[1] - radius, center[1] + radius)
    axis.set_zlim(center[2] - radius, center[2] + radius)
    axis.set_axis_off()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, format="png", bbox_inches="tight")
    plt.close(figure)
    return str(output)


__all__ = ["Model3DStats", "inspect_model", "render_preview"]
