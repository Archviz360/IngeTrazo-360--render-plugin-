# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
# Copyright (C) 2026 D.S (Archviz360).
"""Native 360-degree image export used by :mod:`plugins.panorama_360`.

Six ordinary IngeTrazo 2D exports are captured around a document camera and
stitched into an equirectangular image. No external renderer is involved.
"""
from __future__ import annotations

import math
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
                      auto_level: bool = True) -> dict:
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
        for name, toward, face_up in views:
            _set_view(live, eye, toward, face_up)
            image = viewport.render_image(size, size, overlays=overlays)
            if image is None or image.isNull():
                raise RuntimeError(f"IngeTrazo could not render cube face {name}")
            faces[name] = image.copy()
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


def cube_to_equirectangular(faces: dict, width: int,
                            progress=None):
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
        dominant = np.maximum(np.maximum(np.abs(a), np.abs(b)), np.abs(c))
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
        if progress is not None:
            progress(y1 / height)
    image = QImage(output.data, width, height, output.strides[0],
                   QImage.Format_RGBA8888)
    return image.copy()


def clean_export_settings(raw=None) -> dict:
    """Return JSON-safe per-camera export settings with durable defaults."""
    raw = raw if isinstance(raw, dict) else {}
    projection = str(raw.get("projection", DEFAULT_EXPORT["projection"]))
    if projection not in PROJECTIONS:
        projection = DEFAULT_EXPORT["projection"]
    fmt = str(raw.get("format", DEFAULT_EXPORT["format"])).lower()
    if fmt not in FORMATS:
        fmt = DEFAULT_EXPORT["format"]
    try:
        width = max(256, min(32768, int(raw.get(
            "width", DEFAULT_EXPORT["width"]))))
    except (TypeError, ValueError):
        width = DEFAULT_EXPORT["width"]
    folder = re.sub(r"[\x00-\x1f]", "", str(raw.get("folder", ""))).strip()
    return {"projection": projection, "width": width, "format": fmt,
            "folder": folder[:4096]}


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
        "export": clean_export_settings(raw.get("export")),
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
