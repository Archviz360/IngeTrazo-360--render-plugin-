# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
# Copyright (C) 2026 D.S (Archviz360).
"""Native 360-degree image export used by :mod:`plugins.panorama_360`.

Six ordinary IngeTrazo 2D exports are captured around a document camera and
stitched into an equirectangular image. No external renderer is involved.
"""
from __future__ import annotations

import math
import json
import re
import struct
from pathlib import Path

from PySide6.QtGui import QVector3D

PROJECTIONS = ("equirectangular", "cubemap_faces", "cubemap_strip",
               "cubemap_cross")
FORMATS = {
    "png": {"suffix": ".png", "depths": (8,)},
    "jpeg": {"suffix": ".jpg", "depths": (8,)},
}
DEFAULT_EXPORT = {
    "projection": "equirectangular",
    "width": 4096,
    "format": "png",
    "folder": "",
    "quality": "standard",
    "north_heading": 0.0,
    "start_heading": 0.0,
    "seam_heading": 180.0,
}
QUALITY_PRESETS = {
    "draft": {"scale": 0.5, "seam": 0, "sharpen": 0.0, "jpeg": 88},
    "standard": {"scale": 1.0, "seam": 1, "sharpen": 0.15, "jpeg": 94},
    "high": {"scale": 1.5, "seam": 2, "sharpen": 0.3, "jpeg": 96},
    "ultra": {"scale": 2.0, "seam": 3, "sharpen": 0.45, "jpeg": 98},
}
CUBE_PROJECTIONS = frozenset(("cubemap_faces", "cubemap_strip",
                              "cubemap_cross"))
_VIEW_FIELDS = ("distance", "yaw", "pitch", "fov_deg", "aspect", "znear",
                "zfar", "perspective", "two_point")


def capture_view(camera) -> dict:
    """Return an independent snapshot of every mutable viewport view field."""
    state = {name: getattr(camera, name) for name in _VIEW_FIELDS}
    state["target"] = QVector3D(camera.target)
    state["up"] = QVector3D(camera.up)
    return state


def restore_view(camera, state: dict) -> None:
    """Restore a snapshot produced by :func:`capture_view` exactly."""
    for name in _VIEW_FIELDS:
        setattr(camera, name, state[name])
    camera.target = QVector3D(state["target"])
    camera.up = QVector3D(state["up"])


def preview_view(camera, panorama: dict, fov: float = 90.0) -> None:
    """Put an OrbitCamera at a saved panorama camera for live preview."""
    panorama = clean_camera(panorama)
    if panorama is None:
        raise ValueError("invalid panorama camera")
    camera.look_from(QVector3D(*panorama["pos"]), direction(panorama))
    camera.up = QVector3D(0.0, 0.0, 1.0)
    camera.perspective = True
    camera.two_point = False
    camera.fov_deg = max(1.0, min(179.0, float(fov)))


def _set_view(camera, eye: QVector3D, forward: QVector3D,
              up: QVector3D) -> None:
    """Point an OrbitCamera exactly, including the vertical cube faces."""
    d = QVector3D(forward).normalized()
    back = -d
    camera.distance = 10.0
    camera.yaw = math.atan2(back.y(), back.x())
    camera.pitch = math.asin(max(-1.0, min(1.0, back.z())))
    camera.target = QVector3D(eye) + d * camera.distance
    camera.up = QVector3D(up).normalized()
    camera.perspective = True
    camera.two_point = False
    camera.fov_deg = 90.0
    camera.aspect = 1.0


def render_cube_faces(viewport, camera: dict, size: int,
                      overlays: bool = False,
                      auto_level: bool = True, progress=None) -> dict:
    """Render six 90-degree faces with IngeTrazo's own 2D-image renderer.

    The live viewport camera is restored exactly before returning, even when
    a graphics-driver error interrupts a face.  ``camera`` is one cleaned
    extension camera and ``size`` is the square face size in pixels.
    """
    camera = clean_camera(camera)
    if camera is None:
        raise ValueError("invalid panorama camera")
    size = max(64, min(8192, int(size)))
    live = viewport.camera
    saved = capture_view(live)
    eye = QVector3D(*camera["pos"])
    if auto_level:
        # A 360 panorama has no ordinary frame edge to hide a tilted horizon.
        # Keep its equator parallel to the model's XY plane regardless of the
        # source viewport pitch; heading still determines the centre/seam.
        heading = math.radians(camera["heading"])
        forward = QVector3D(math.sin(heading), math.cos(heading), 0.0)
    else:
        forward = direction(camera).normalized()
    world_up = QVector3D(0.0, 0.0, 1.0)
    right = QVector3D.crossProduct(forward, world_up)
    if right.lengthSquared() < 1e-12:
        right = QVector3D(1.0, 0.0, 0.0)
    right.normalize()
    up = QVector3D.crossProduct(right, forward).normalized()
    views = (
        ("front", forward, up), ("right", right, up),
        ("back", -forward, up), ("left", -right, up),
        ("up", up, -forward), ("down", -up, forward),
    )
    faces = {}
    try:
        for index, (name, toward, face_up) in enumerate(views, 1):
            _set_view(live, eye, toward, face_up)
            image = viewport.render_image(size, size, overlays=overlays)
            if image is None or image.isNull():
                raise RuntimeError(f"IngeTrazo could not render cube face {name}")
            faces[name] = image.copy()
            if progress is not None:
                progress(index / len(views))
    finally:
        restore_view(live, saved)
        viewport.update()
    return faces


def render_camera_image(viewport, camera: dict, width: int = 192,
                        height: int = 108, fov: float = 90.0):
    """Render one temporary camera view and restore the viewport exactly."""
    width = max(64, min(2048, int(width)))
    height = max(64, min(2048, int(height)))
    live = viewport.camera
    saved = capture_view(live)
    try:
        preview_view(live, camera, fov=fov)
        live.aspect = width / height
        image = viewport.render_image(width, height, overlays=False)
        if image is None or image.isNull():
            raise RuntimeError("IngeTrazo could not render the camera preview")
        return image.copy()
    finally:
        restore_view(live, saved)
        viewport.update()


def _rgba_array(image):
    """A detached ``height x width x 4`` uint8 array from a QImage."""
    import numpy as np
    from PySide6.QtGui import QImage
    image = image.convertToFormat(QImage.Format_RGBA8888)
    raw = np.frombuffer(image.bits(), dtype=np.uint8,
                        count=image.sizeInBytes())
    rows = raw.reshape(image.height(), image.bytesPerLine())
    return rows[:, :image.width() * 4].reshape(
        image.height(), image.width(), 4).copy()


def prepare_equirectangular(image):
    """Prepare a QImage once for fast interactive perspective views."""
    return _rgba_array(image)


def equirectangular_view(source, width: int, height: int, yaw: float = 0.0,
                         pitch: float = 0.0, fov: float = 75.0):
    """Project an equirectangular image into an ordinary draggable view.

    ``yaw``, ``pitch`` and ``fov`` are degrees. ``source`` may be a QImage or
    the array returned by :func:`prepare_equirectangular`.
    """
    import numpy as np
    from PySide6.QtGui import QImage

    src = _rgba_array(source) if isinstance(source, QImage) else source
    if not isinstance(src, np.ndarray) or src.ndim != 3 or src.shape[2] != 4:
        raise ValueError("an RGBA equirectangular image is required")
    width = max(64, min(2048, int(width)))
    height = max(64, min(2048, int(height)))
    yaw = math.radians(float(yaw) % 360.0)
    pitch = math.radians(max(-89.0, min(89.0, float(pitch))))
    fov = math.radians(max(20.0, min(120.0, float(fov))))

    aspect = width / height
    half_y = math.tan(fov * 0.5)
    half_x = half_y * aspect
    x = (((np.arange(width, dtype=np.float32) + 0.5) / width) * 2.0 - 1.0)
    y = (1.0 - ((np.arange(height, dtype=np.float32) + 0.5) /
                height) * 2.0)
    screen_x, screen_y = np.meshgrid(x * half_x, y * half_y)

    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    # Basis in panorama coordinates: A front, B right, C up.
    a = cp * cy + screen_x * -sy + screen_y * -sp * cy
    b = cp * sy + screen_x * cy + screen_y * -sp * sy
    c = sp + screen_y * cp
    length = np.sqrt(a * a + b * b + c * c)
    a, b, c = a / length, b / length, c / length
    longitude = np.arctan2(b, a)
    latitude = np.arcsin(np.clip(c, -1.0, 1.0))

    src_h, src_w = src.shape[:2]
    px = (longitude / (2.0 * np.pi) + 0.5) * src_w - 0.5
    py = (0.5 - latitude / np.pi) * src_h - 0.5
    x0 = np.floor(px).astype(np.int32) % src_w
    x1 = (x0 + 1) % src_w
    y0 = np.clip(np.floor(py).astype(np.int32), 0, src_h - 1)
    y1 = np.minimum(y0 + 1, src_h - 1)
    fx = (px - np.floor(px))[..., None]
    fy = (py - np.floor(py))[..., None]
    top = src[y0, x0] * (1.0 - fx) + src[y0, x1] * fx
    bottom = src[y1, x0] * (1.0 - fx) + src[y1, x1] * fx
    output = np.clip(top * (1.0 - fy) + bottom * fy, 0, 255).astype(np.uint8)
    image = QImage(output.data, width, height, output.strides[0],
                   QImage.Format_RGBA8888)
    return image.copy()


def rotate_equirectangular(image, camera_heading: float,
                           seam_heading: float):
    """Place the requested world heading on the panorama's left/right seam."""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QImage, QPainter

    width, height = image.width(), image.height()
    source_x = int(round((((float(seam_heading) - float(camera_heading) +
                           180.0) % 360.0) / 360.0) * width)) % width
    if source_x == 0:
        return image
    output = QImage(width, height, image.format())
    output.fill(0)
    painter = QPainter(output)
    first = width - source_x
    painter.drawImage(QRect(0, 0, first, height), image,
                      QRect(source_x, 0, first, height))
    painter.drawImage(QRect(first, 0, source_x, height), image,
                      QRect(0, 0, source_x, height))
    painter.end()
    return output


def sharpen_image(image, amount: float = 0.0):
    """Memory-bounded unsharp finishing filter for Standard/High/Ultra."""
    import numpy as np
    from PySide6.QtGui import QImage

    amount = max(0.0, min(1.0, float(amount)))
    pixels = image.width() * image.height()
    if amount <= 0.0 or pixels > 40_000_000:
        return image
    src = _rgba_array(image)
    height, width = src.shape[:2]
    output = np.empty_like(src)
    for y0 in range(0, height, 64):
        y1 = min(height, y0 + 64)
        rows = np.arange(y0, y1)
        above = np.maximum(rows - 1, 0)
        below = np.minimum(rows + 1, height - 1)
        centre = src[rows].astype(np.float32)
        blur = (centre * 4.0 + src[above].astype(np.float32) +
                src[below].astype(np.float32) +
                np.roll(centre, 1, axis=1) +
                np.roll(centre, -1, axis=1)) / 8.0
        output[rows] = np.clip(centre + amount * (centre - blur),
                               0, 255).astype(np.uint8)
        output[rows, :, 3] = src[rows, :, 3]
    result = QImage(output.data, width, height, output.strides[0],
                    QImage.Format_RGBA8888)
    return result.copy()


def cube_to_equirectangular(faces: dict, width: int,
                            progress=None, seam_blend: int = 0):
    """Stitch native cube-face QImages into a 2:1 equirectangular QImage.

    Conversion is vectorised in small row bands, so an 8K panorama does not
    allocate several full-size floating-point coordinate maps at once.
    Sampling is bilinear to keep diagonal edges and cube seams smooth.
    """
    import numpy as np
    from PySide6.QtGui import QImage
    names = ("front", "right", "back", "left", "up", "down")
    if any(name not in faces for name in names):
        raise ValueError("six cube faces are required")
    src = {name: _rgba_array(faces[name]) for name in names}
    size = src["front"].shape[0]
    if any(a.shape[:2] != (size, size) for a in src.values()):
        raise ValueError("cube faces must be equally sized squares")
    width = max(256, min(32768, int(width)))
    height = width // 2
    output = np.empty((height, width, 4), dtype=np.uint8)
    longitude = ((np.arange(width, dtype=np.float32) + 0.5) /
                 width * (2.0 * np.pi) - np.pi)[None, :]

    def sample(image, u, v):
        x = np.clip((u + 1.0) * 0.5 * (size - 1), 0, size - 1)
        y = np.clip((1.0 - v) * 0.5 * (size - 1), 0, size - 1)
        x0, y0 = np.floor(x).astype(np.int32), np.floor(y).astype(np.int32)
        x1, y1 = np.minimum(x0 + 1, size - 1), np.minimum(y0 + 1, size - 1)
        fx, fy = (x - x0)[:, None], (y - y0)[:, None]
        top = image[y0, x0] * (1.0 - fx) + image[y0, x1] * fx
        bottom = image[y1, x0] * (1.0 - fx) + image[y1, x1] * fx
        return np.clip(top * (1.0 - fy) + bottom * fy, 0, 255).astype(np.uint8)

    for y0 in range(0, height, 64):
        y1 = min(y0 + 64, height)
        latitude = (np.pi * 0.5 - (np.arange(y0, y1, dtype=np.float32) + 0.5)
                    / height * np.pi)[:, None]
        coslat = np.cos(latitude)
        a = coslat * np.cos(longitude)       # front
        b = coslat * np.sin(longitude)       # right
        c = np.broadcast_to(np.sin(latitude), a.shape)  # up
        aa, ab, ac = np.abs(a), np.abs(b), np.abs(c)
        dominant = np.maximum(np.maximum(aa, ab), ac)
        middle = aa + ab + ac - dominant - np.minimum(np.minimum(aa, ab), ac)
        seam = (dominant - middle) < (0.018 * dominant)
        band = output[y0:y1]
        specs = (
            ("front", (dominant == np.abs(a)) & (a >= 0), b, c, a),
            ("back", (dominant == np.abs(a)) & (a < 0), -b, c, -a),
            ("right", (dominant == np.abs(b)) & (b >= 0), -a, c, b),
            ("left", (dominant == np.abs(b)) & (b < 0), a, c, -b),
            ("up", (dominant == np.abs(c)) & (c >= 0), b, -a, c),
            ("down", (dominant == np.abs(c)) & (c < 0), b, a, -c),
        )
        for name, mask, numerator_u, numerator_v, denominator in specs:
            if np.any(mask):
                band[mask] = sample(src[name],
                                    numerator_u[mask] / denominator[mask],
                                    numerator_v[mask] / denominator[mask])
        for _ in range(max(0, min(3, int(seam_blend)))):
            work = band.astype(np.uint16)
            smooth = ((np.roll(work, 1, axis=1) + work * 2 +
                       np.roll(work, -1, axis=1)) // 4).astype(np.uint8)
            band[seam] = smooth[seam]
            seam = (seam | np.roll(seam, 1, axis=1) |
                    np.roll(seam, -1, axis=1))
        if progress is not None:
            progress(y1 / height)
    image = QImage(output.data, width, height, output.strides[0],
                   QImage.Format_RGBA8888)
    return image.copy()


def _heading(value, fallback=0.0) -> float:
    try:
        value = float(value) % 360.0
        return value if math.isfinite(value) else float(fallback) % 360.0
    except (TypeError, ValueError):
        return float(fallback) % 360.0


def clean_export_settings(raw=None, heading: float = 0.0) -> dict:
    """Return JSON-safe per-camera export settings with durable defaults."""
    raw = raw if isinstance(raw, dict) else {}
    projection = str(raw.get("projection", DEFAULT_EXPORT["projection"]))
    if projection not in PROJECTIONS:
        projection = DEFAULT_EXPORT["projection"]
    fmt = str(raw.get("format", DEFAULT_EXPORT["format"])).lower()
    if fmt not in FORMATS:
        fmt = DEFAULT_EXPORT["format"]
    quality = str(raw.get("quality", DEFAULT_EXPORT["quality"])).lower()
    if quality not in QUALITY_PRESETS:
        quality = DEFAULT_EXPORT["quality"]
    try:
        maximum = 8192 if projection in CUBE_PROJECTIONS else 32768
        width = max(256, min(maximum, int(raw.get(
            "width", DEFAULT_EXPORT["width"]))))
    except (TypeError, ValueError):
        width = DEFAULT_EXPORT["width"]
    folder = re.sub(r"[\x00-\x1f]", "", str(raw.get("folder", ""))).strip()
    return {"projection": projection, "width": width, "format": fmt,
            "folder": folder[:4096], "quality": quality,
            "north_heading": _heading(raw.get("north_heading", 0.0)),
            "start_heading": _heading(raw.get("start_heading", heading),
                                      heading),
            "seam_heading": _heading(raw.get("seam_heading", heading + 180.0),
                                     heading + 180.0)}


def clean_hotspot(raw, number: int = 1) -> dict | None:
    """Validate one navigation or information hotspot."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type", "info")).lower()
    if kind not in ("navigation", "info"):
        kind = "info"
    label = re.sub(r"[\x00-\x1f]", "", str(raw.get("label", ""))).strip()
    description = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "",
                         str(raw.get("description", ""))).strip()
    target = str(raw.get("target", "")).strip() if kind == "navigation" else ""
    image = re.sub(r"[\x00-\x1f]", "", str(raw.get("image", ""))).strip()
    url = re.sub(r"[\x00-\x1f]", "", str(raw.get("url", ""))).strip()
    try:
        tilt = max(-89.0, min(89.0, float(raw.get("tilt", 0.0))))
    except (TypeError, ValueError):
        tilt = 0.0
    try:
        size = max(20, min(72, int(raw.get("size", 42))))
    except (TypeError, ValueError):
        size = 42
    return {
        "id": str(raw.get("id") or f"hotspot-{number}"),
        "type": kind,
        "label": label or ("Go to camera" if kind == "navigation" else "Info"),
        "target": target,
        "description": description[:10000],
        "image": image[:4096],
        "url": url[:4096],
        "heading": round(_heading(raw.get("heading", 0.0)), 6),
        "tilt": round(tilt, 6),
        "size": size,
    }


def clean_hotspots(raw) -> list[dict]:
    out = []
    seen = set()
    for item in raw if isinstance(raw, list) else []:
        hotspot = clean_hotspot(item, len(out) + 1)
        if hotspot is None:
            continue
        base, key, n = hotspot["id"], hotspot["id"], 2
        while key in seen:
            key, n = f"{base}-{n}", n + 1
        hotspot["id"] = key
        seen.add(key)
        out.append(hotspot)
    return out


def clean_camera(raw, number: int = 1) -> dict | None:
    """Return one JSON-safe panorama camera, or ``None`` when malformed."""
    try:
        pos = [float(v) for v in raw["pos"]][:3]
        heading = float(raw.get("heading", 0.0)) % 360.0
        tilt = max(-90.0, min(90.0, float(raw.get("tilt", 0.0))))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if len(pos) != 3 or not all(math.isfinite(v) for v in pos):
        return None
    name = re.sub(r"[\x00-\x1f]", "", str(raw.get("name", ""))).strip()
    return {
        "id": str(raw.get("id") or f"camera-{number}"),
        "name": name or f"360 Camera {number}",
        "pos": pos,
        "heading": round(heading, 6),
        "tilt": round(tilt, 6),
        "export": clean_export_settings(raw.get("export"), heading),
        "hotspots": clean_hotspots(raw.get("hotspots", [])),
    }


def clean_cameras(raw) -> list[dict]:
    """Validate the list stored in extension document data."""
    out = []
    seen = set()
    for item in raw if isinstance(raw, list) else []:
        cam = clean_camera(item, len(out) + 1)
        if cam is None:
            continue
        base, key, n = cam["id"], cam["id"], 2
        while key in seen:
            key, n = f"{base}-{n}", n + 1
        cam["id"] = key
        seen.add(key)
        out.append(cam)
    return out


def direction(camera: dict) -> QVector3D:
    """The camera's forward direction (heading 0 = north, tilt positive up)."""
    h, t = math.radians(camera["heading"]), math.radians(camera["tilt"])
    return QVector3D(math.sin(h) * math.cos(t),
                     math.cos(h) * math.cos(t), math.sin(t))


def dimensions(projection: str, width: int) -> tuple[int, int]:
    """Final image dimensions (a cubemap face uses ``width`` as its size)."""
    width = max(256, min(32768, int(width)))
    if projection == "cubemap_strip":
        return width * 6, width
    if projection == "cubemap_cross":
        return width * 4, width * 3
    return width, width // 2


def manifest(camera: dict, projection: str, width: int, fmt: str) -> dict:
    """Portable metadata written beside cube faces and batch exports."""
    w, h = dimensions(projection, width)
    return {
        "generator": "IngeTrazo 360 Export",
        "projection": projection,
        "width": w, "height": h,
        "format": fmt,
        "auto_level": True,
        "camera": clean_camera(camera),
        "cube_faces": ["front", "right", "back", "left", "up", "down"]
                      if projection in CUBE_PROJECTIONS else None,
    }


def add_gpano_xmp(path: Path, width: int, height: int) -> bool:
    """Insert standard Google Photo Sphere XMP into a JPEG, in place.

    The APP1 segment is put immediately after SOI.  Existing pixel data is
    untouched, and a file already marked as GPano is left alone.
    """
    path = Path(path)
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8") or b"GPano:ProjectionType" in data:
        return False
    xml = f'''<x:xmpmeta xmlns:x="adobe:ns:meta/">
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description xmlns:GPano="http://ns.google.com/photos/1.0/panorama/"
 GPano:UsePanoramaViewer="True" GPano:ProjectionType="equirectangular"
 GPano:FullPanoWidthPixels="{int(width)}" GPano:FullPanoHeightPixels="{int(height)}"
 GPano:CroppedAreaImageWidthPixels="{int(width)}"
 GPano:CroppedAreaImageHeightPixels="{int(height)}"
 GPano:CroppedAreaLeftPixels="0" GPano:CroppedAreaTopPixels="0"/>
</rdf:RDF></x:xmpmeta>'''.encode("utf-8")
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xml
    if len(payload) + 2 > 65535:
        return False
    app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    path.write_bytes(data[:2] + app1 + data[2:])
    return True


def tour_html(cameras: list[dict]) -> str:
    """Return a dependency-free WebGL panorama tour as one HTML document."""
    data = json.dumps(cameras, ensure_ascii=False).replace("</", "<\\/")
    template = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IngeTrazo 360 Tour</title>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#111;color:#fff;font:14px system-ui,sans-serif}
#view{position:fixed;inset:0;width:100%;height:100%}#top{position:fixed;left:14px;right:14px;top:12px;display:flex;gap:10px;align-items:center;z-index:3;pointer-events:none}
#top>*{pointer-events:auto}select,#compass{background:#171717dd;color:#fff;border:1px solid #777;border-radius:5px;padding:7px 10px}#compass{margin-left:auto}
#spots{position:fixed;inset:0;z-index:2;pointer-events:none}.hotspot{position:absolute;transform:translate(-50%,-50%);pointer-events:auto;border:2px solid #fff;border-radius:999px;background:#ef3b2dcc;color:#fff;min-width:42px;height:42px;padding:0 12px;font-weight:700;box-shadow:0 2px 12px #000;cursor:pointer}.hotspot.info{background:#1769d2dd}
#help{position:fixed;left:14px;bottom:12px;background:#111b;padding:7px 10px;border-radius:5px;z-index:3}
#error{display:none;position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);max-width:620px;background:#7d1717;color:#fff;padding:16px 20px;border:1px solid #ff8a8a;border-radius:8px;z-index:6;white-space:pre-wrap}
#modal{display:none;position:fixed;inset:0;background:#0009;z-index:5;align-items:center;justify-content:center}#card{max-width:620px;max-height:80vh;overflow:auto;background:#222;padding:20px;border-radius:10px;box-shadow:0 8px 40px #000}#card img{max-width:100%;height:auto}#card a{color:#75b9ff}#close{float:right}
</style></head><body>
<canvas id="view"></canvas><div id="spots"></div>
<div id="top"><select id="camera"></select><span id="title"></span><span id="compass"></span></div>
<div id="help">Drag to look around · Mouse wheel to zoom · Click hotspots to navigate</div>
<div id="error"></div>
<div id="modal"><div id="card"><button id="close">Close</button><h2 id="infoTitle"></h2><div id="infoImage"></div><p id="infoText"></p><p><a id="infoLink" target="_blank" rel="noopener"></a></p></div></div>
<script>
const tour=__TOUR_DATA__;const canvas=document.getElementById('view'),spots=document.getElementById('spots'),picker=document.getElementById('camera'),title=document.getElementById('title'),compass=document.getElementById('compass');
const gl=canvas.getContext('webgl',{antialias:true});if(!gl){document.body.innerHTML='<h2>WebGL is required to view this tour.</h2>';throw new Error('WebGL unavailable')}
const vs='attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}';
const fs='precision highp float;uniform sampler2D tex;uniform vec2 res;uniform float yaw,pitch,fov;const float PI=3.141592653589793;void main(){vec2 q=(gl_FragCoord.xy/res*2.-1.);q.x*=res.x/res.y;float cy=cos(yaw),sy=sin(yaw),cp=cos(pitch),sp=sin(pitch);vec3 front=vec3(cp*cy,cp*sy,sp),right=vec3(-sy,cy,0.),up=vec3(-sp*cy,-sp*sy,cp);vec3 d=normalize(front+q.x*tan(fov*.5)*right+q.y*tan(fov*.5)*up);vec2 uv=vec2(fract(atan(d.y,d.x)/(2.*PI)+.5),asin(clamp(d.z,-1.,1.))/PI+.5);gl_FragColor=texture2D(tex,uv);}';
function shader(type,src){const s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(s));return s}
const program=gl.createProgram();gl.attachShader(program,shader(gl.VERTEX_SHADER,vs));gl.attachShader(program,shader(gl.FRAGMENT_SHADER,fs));gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(program));gl.useProgram(program);
const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]),gl.STATIC_DRAW);const pos=gl.getAttribLocation(program,'p');gl.enableVertexAttribArray(pos);gl.vertexAttribPointer(pos,2,gl.FLOAT,false,0,0);
const texture=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,texture);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL,true);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,1,1,0,gl.RGBA,gl.UNSIGNED_BYTE,new Uint8Array([35,35,35,255]));
const U={res:gl.getUniformLocation(program,'res'),yaw:gl.getUniformLocation(program,'yaw'),pitch:gl.getUniformLocation(program,'pitch'),fov:gl.getUniformLocation(program,'fov')};let cam=null,yaw=0,pitch=0,fov=75*Math.PI/180,drag=null,loadSerial=0;const errorBox=document.getElementById('error');
const rad=x=>x*Math.PI/180,deg=x=>x*180/Math.PI,wrap=x=>(x%360+360)%360,signed=x=>((x+180)%360+360)%360-180;
function resize(){const d=devicePixelRatio||1,w=Math.round(innerWidth*d),h=Math.round(innerHeight*d);if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;gl.viewport(0,0,w,h)}}
function setCamera(id){cam=tour.find(c=>c.id===id)||tour[0];if(!cam)return;const serial=++loadSerial;picker.value=cam.id;title.textContent=cam.name;yaw=rad(signed(cam.start_heading-(cam.seam_heading+180)));pitch=0;spots.innerHTML='';errorBox.style.display='none';for(const h of cam.hotspots||[]){const b=document.createElement('button');b.className='hotspot '+(h.type==='info'?'info':'navigation');b.textContent=h.type==='navigation'?'➜':'i';b.title=h.label;const size=Math.max(20,Math.min(72,h.size||42));b.style.width=size+'px';b.style.height=size+'px';b.style.minWidth=size+'px';b.onclick=()=>h.type==='navigation'?setCamera(h.target):showInfo(h);h.element=b;spots.appendChild(b)}const img=new Image();img.onload=()=>{if(serial!==loadSerial)return;try{gl.bindTexture(gl.TEXTURE_2D,texture);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,img);errorBox.style.display='none'}catch(e){showError('The panorama texture could not be loaded.\n'+e.message)}};img.onerror=()=>{if(serial===loadSerial)showError('The panorama image could not be decoded.')};img.src=cam.image}
function showError(message){errorBox.textContent=message;errorBox.style.display='block'}
function updateSpots(){if(!cam)return;const cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch),front=[cp*cy,cp*sy,sp],right=[-sy,cy,0],up=[-sp*cy,-sp*sy,cp],aspect=innerWidth/innerHeight,t=Math.tan(fov/2);for(const h of cam.hotspots||[]){const a=rad(signed(h.heading-(cam.seam_heading+180))),p=rad(h.tilt),v=[Math.cos(p)*Math.cos(a),Math.cos(p)*Math.sin(a),Math.sin(p)],dot=(x,y)=>x[0]*y[0]+x[1]*y[1]+x[2]*y[2],z=dot(v,front),nx=dot(v,right)/(z*t*aspect),ny=dot(v,up)/(z*t);if(z<=0||Math.abs(nx)>1.1||Math.abs(ny)>1.1){h.element.style.display='none'}else{h.element.style.display='block';h.element.style.left=((nx*.5+.5)*innerWidth)+'px';h.element.style.top=((.5-ny*.5)*innerHeight)+'px'}}compass.textContent='Bearing '+Math.round(wrap(cam.seam_heading+180+deg(yaw)-cam.north_heading))+'°'}
function frame(){resize();gl.uniform2f(U.res,canvas.width,canvas.height);gl.uniform1f(U.yaw,yaw);gl.uniform1f(U.pitch,pitch);gl.uniform1f(U.fov,fov);gl.drawArrays(gl.TRIANGLES,0,6);updateSpots();requestAnimationFrame(frame)}
canvas.onpointerdown=e=>{drag=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId)};canvas.onpointermove=e=>{if(!drag)return;yaw-= (e.clientX-drag[0])*.004;pitch=Math.max(-1.553,Math.min(1.553,pitch+(e.clientY-drag[1])*.004));drag=[e.clientX,e.clientY]};canvas.onpointerup=()=>drag=null;canvas.onwheel=e=>{e.preventDefault();fov=Math.max(rad(25),Math.min(rad(110),fov+Math.sign(e.deltaY)*rad(4)))},{passive:false};
function showInfo(h){document.getElementById('infoTitle').textContent=h.label;document.getElementById('infoText').textContent=h.description||'';const box=document.getElementById('infoImage');box.innerHTML='';if(h.image){const i=document.createElement('img');i.src=h.image;i.alt=h.label;box.appendChild(i)}const a=document.getElementById('infoLink');a.textContent=h.url||'';a.href=h.url||'#';a.style.display=h.url?'block':'none';document.getElementById('modal').style.display='flex'}document.getElementById('close').onclick=()=>document.getElementById('modal').style.display='none';document.getElementById('modal').onclick=e=>{if(e.target.id==='modal')e.currentTarget.style.display='none'};
for(const c of tour){const o=document.createElement('option');o.value=c.id;o.textContent=c.name;picker.appendChild(o)}picker.onchange=()=>setCamera(picker.value);setCamera(tour[0]?.id);requestAnimationFrame(frame);
</script></body></html>'''
    return template.replace("__TOUR_DATA__", data)
