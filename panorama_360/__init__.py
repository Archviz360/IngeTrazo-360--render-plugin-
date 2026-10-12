# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
# Copyright (C) 2026 D.S (Archviz360).
"""360-degree image export - persistent cameras and native rendering.

The panel creates any number of cameras in the model, places/aims them with
snapping, previews their direction in the viewport and exports a selected
camera or a whole batch. Six 90-degree views from the native 2D exporter are
stitched into a true equirectangular panorama; no external renderer is used.
"""
from __future__ import annotations

import base64
import json
import math
import mimetypes
import re
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from PySide6.QtCore import (
    QLineF, QPointF, QSettings, QSize, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QFont, QIcon, QImage, QPainter, QPen,
    QPixmap, QVector3D,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QTextEdit, QToolBar, QVBoxLayout, QWidget, QMenu,
)

from . import panorama_core as pano
from core.i18n import tr
from views.fold_section import FoldSection, narrow, wrapping_form


_SETTINGS = "panorama360/"
_PROJECTION_LABELS = (
    ("equirectangular", "Equirectangular 360 deg (2:1)"),
    ("cubemap_faces", "Cube map - six-face ZIP"),
    ("cubemap_strip", "Cube map - horizontal strip"),
    ("cubemap_cross", "Cube map - 4 x 3 cross"),
)


def _state(app) -> dict:
    raw = app.document_data({}) or {}
    return {"cameras": pano.clean_cameras(raw.get("cameras", []))
            if isinstance(raw, dict) else []}


def _store(app, cameras) -> None:
    app.set_document_data({"cameras": pano.clean_cameras(cameras)})


def _safe_name(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE)
    return value.strip("-.") or "panorama"


def _pick_tool(prompt, done, cancelled):
    """A private snapping tool used for camera placement and aiming."""
    from tools.base import Tool

    class _Pick(Tool):
        name = prompt
        shortcut = None
        uses_snap = True

        def on_activate(self, viewport):
            pass

        def on_deactivate(self, viewport):
            pass

        def on_hover(self, ctx):
            ctx.viewport.update()

        def on_click(self, ctx):
            done(QVector3D(ctx.world))

        def on_key(self, viewport, key, modifiers):
            if key == Qt.Key_Escape:
                cancelled()
                return True
            return False

        def rubber_band_lines(self):
            return []

    return _Pick()


class PanoramaView(QWidget):
    """Mouse-draggable perspective window into an equirectangular image."""

    placeRequested = Signal(str, float, float)
    markerMoved = Signal(str, float, float)
    markerEditRequested = Signal(str)
    markerDeleteRequested = Signal(str)
    navigateRequested = Signal(str)

    def __init__(self, image: QImage, initial_yaw=0.0, centre_heading=0.0,
                 north_heading=0.0, parent=None, hotspots=None) -> None:
        super().__init__(parent)
        source = image.scaled(4096, 2048, Qt.KeepAspectRatio,
                              Qt.SmoothTransformation)
        self._source = pano.prepare_equirectangular(source)
        self._frame = QImage()
        self._yaw = float(initial_yaw)
        self._pitch = 0.0
        self._fov = 75.0
        self._centre_heading = float(centre_heading)
        self._north_heading = float(north_heading)
        self._press = None
        self._marker_drag = None
        self._hotspots = hotspots if hotspots is not None else []
        self._snap_navigation = True
        self._test_navigation = False
        self._cursor_readout = None
        self.setMinimumSize(640, 360)
        self.setCursor(Qt.OpenHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._render)
        self._schedule()

    def _schedule(self) -> None:
        if not self._timer.isActive():
            self._timer.start(0)

    def _render(self) -> None:
        if self.width() < 2 or self.height() < 2:
            return
        self._frame = pano.equirectangular_view(
            self._source, self.width(), self.height(), self._yaw,
            self._pitch, self._fov)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(12, 12, 12))
        if not self._frame.isNull():
            painter.drawImage(self.rect(), self._frame)
        painter.setPen(Qt.white)
        bearing = (self._centre_heading + self._yaw -
                   self._north_heading) % 360.0
        painter.drawText(12, 24, tr(
            "Bearing {heading} deg | Tilt {tilt} deg | FOV {fov} deg",
            heading=int(bearing), tilt=int(self._pitch),
            fov=int(self._fov)))
        if self._cursor_readout is not None:
            painter.drawText(12, 46, tr(
                "Hotspot heading {heading:.1f} deg | Tilt {tilt:.1f} deg",
                heading=self._cursor_readout[0],
                tilt=self._cursor_readout[1]))
        self._paint_hotspots(painter)
        painter.end()

    def set_hotspots(self, hotspots) -> None:
        self._hotspots = hotspots
        self.update()

    def set_snap_navigation(self, enabled) -> None:
        self._snap_navigation = bool(enabled)

    def set_test_navigation(self, enabled) -> None:
        self._test_navigation = bool(enabled)
        self.setCursor(Qt.PointingHandCursor if enabled else Qt.OpenHandCursor)

    @staticmethod
    def _signed_angle(value) -> float:
        return ((float(value) + 180.0) % 360.0) - 180.0

    def _project_hotspot(self, hotspot):
        relative = math.radians(self._signed_angle(
            hotspot["heading"] - self._centre_heading))
        vertical = math.radians(hotspot["tilt"])
        vector = (math.cos(vertical) * math.cos(relative),
                  math.cos(vertical) * math.sin(relative),
                  math.sin(vertical))
        yaw, pitch = math.radians(self._yaw), math.radians(self._pitch)
        cy, sy = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        front = (cp * cy, cp * sy, sp)
        right = (-sy, cy, 0.0)
        up = (-sp * cy, -sp * sy, cp)
        dot = lambda a, b: sum(x * y for x, y in zip(a, b))
        depth = dot(vector, front)
        if depth <= 1e-6:
            return None
        tangent = math.tan(math.radians(self._fov) / 2.0)
        aspect = self.width() / max(self.height(), 1)
        nx = dot(vector, right) / (depth * tangent * aspect)
        ny = dot(vector, up) / (depth * tangent)
        if abs(nx) > 1.15 or abs(ny) > 1.15:
            return None
        return QPointF((nx * 0.5 + 0.5) * self.width(),
                       (0.5 - ny * 0.5) * self.height())

    def _screen_angles(self, position) -> tuple[float, float]:
        width, height = max(self.width(), 1), max(self.height(), 1)
        aspect = width / height
        tangent = math.tan(math.radians(self._fov) / 2.0)
        sx = ((position.x() / width) * 2.0 - 1.0) * aspect * tangent
        sy = (1.0 - (position.y() / height) * 2.0) * tangent
        yaw, pitch = math.radians(self._yaw), math.radians(self._pitch)
        cy, syaw = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        front = (cp * cy, cp * syaw, sp)
        right = (-syaw, cy, 0.0)
        up = (-sp * cy, -sp * syaw, cp)
        ray = tuple(front[i] + sx * right[i] + sy * up[i]
                    for i in range(3))
        length = math.sqrt(sum(value * value for value in ray))
        ray = tuple(value / length for value in ray)
        heading = (self._centre_heading +
                   math.degrees(math.atan2(ray[1], ray[0]))) % 360.0
        tilt = math.degrees(math.asin(max(-1.0, min(1.0, ray[2]))))
        return heading, tilt

    def _paint_hotspots(self, painter) -> None:
        painter.setFont(QFont())
        for hotspot in self._hotspots:
            point = self._project_hotspot(hotspot)
            if point is None:
                continue
            size = max(20, min(72, int(hotspot.get("size", 42))))
            radius = size / 2.0
            color = (QColor(35, 105, 210, 225) if hotspot["type"] == "info"
                     else QColor(230, 55, 45, 225))
            painter.setBrush(color)
            painter.setPen(QPen(Qt.white, 2))
            painter.drawEllipse(point, radius, radius)
            painter.setPen(Qt.white)
            symbol = "i" if hotspot["type"] == "info" else "->"
            painter.drawText(QPointF(point.x() - 5, point.y() + 5), symbol)
            painter.drawText(QPointF(point.x() + radius + 6,
                                     point.y() + 5), hotspot["label"])

    def _marker_at(self, position):
        for hotspot in reversed(self._hotspots):
            point = self._project_hotspot(hotspot)
            if point is None:
                continue
            radius = max(14.0, int(hotspot.get("size", 42)) / 2.0)
            if QLineF(point, position).length() <= radius:
                return hotspot
        return None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            hotspot = self._marker_at(event.position())
            if hotspot is not None and self._test_navigation:
                if hotspot["type"] == "navigation" and hotspot["target"]:
                    self.navigateRequested.emit(hotspot["target"])
                elif hotspot["type"] == "info":
                    self.markerEditRequested.emit(hotspot["id"])
                event.accept()
                return
            if hotspot is not None:
                self._marker_drag = hotspot["id"]
                self._cursor_readout = (hotspot["heading"], hotspot["tilt"])
                self.setCursor(Qt.SizeAllCursor)
                event.accept()
                return
            self._press = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
        elif event.button() == Qt.RightButton:
            hotspot = self._marker_at(event.position())
            if hotspot is not None:
                menu = QMenu(self)
                edit = menu.addAction(tr("Edit hotspot..."))
                delete = menu.addAction(tr("Delete hotspot"))
                chosen = menu.exec(self.mapToGlobal(event.position().toPoint()))
                if chosen is edit:
                    self.markerEditRequested.emit(hotspot["id"])
                elif chosen is delete:
                    self.markerDeleteRequested.emit(hotspot["id"])
                event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._marker_drag is not None:
            hotspot = next((item for item in self._hotspots
                            if item["id"] == self._marker_drag), None)
            if hotspot is not None:
                heading, tilt = self._screen_angles(event.position())
                if self._snap_navigation and hotspot["type"] == "navigation":
                    tilt = 0.0
                hotspot["heading"], hotspot["tilt"] = heading, tilt
                self._cursor_readout = (heading, tilt)
                self.update()
            event.accept()
            return
        if self._press is None:
            return
        pos = event.position()
        delta = pos - self._press
        self._press = pos
        self._yaw = (self._yaw - delta.x() * 0.18) % 360.0
        self._pitch = max(-89.0, min(89.0,
                                    self._pitch + delta.y() * 0.18))
        self._schedule()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            if self._marker_drag is not None:
                hotspot = next((item for item in self._hotspots
                                if item["id"] == self._marker_drag), None)
                self._marker_drag = None
                self._cursor_readout = None
                self.setCursor(Qt.PointingHandCursor if self._test_navigation
                               else Qt.OpenHandCursor)
                if hotspot is not None:
                    self.markerMoved.emit(
                        hotspot["id"], hotspot["heading"], hotspot["tilt"])
                self.update()
                event.accept()
                return
            self._press = None
            self.setCursor(Qt.OpenHandCursor)
            event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self._test_navigation:
            return
        self._press = None
        self._marker_drag = None
        self.setCursor(Qt.OpenHandCursor)
        hotspot = self._marker_at(event.position())
        if hotspot is not None:
            self.markerEditRequested.emit(hotspot["id"])
            return
        heading, tilt = self._screen_angles(event.position())
        menu = QMenu(self)
        navigation = menu.addAction(tr("Place navigation hotspot"))
        information = menu.addAction(tr("Place information hotspot"))
        chosen = menu.exec(self.mapToGlobal(event.position().toPoint()))
        if chosen is navigation:
            self.placeRequested.emit("navigation", heading, 0.0 if
                                     self._snap_navigation else tilt)
        elif chosen is information:
            self.placeRequested.emit("info", heading, tilt)

    def wheelEvent(self, event) -> None:
        steps = event.angleDelta().y() / 120.0
        self._fov = max(25.0, min(110.0, self._fov - steps * 5.0))
        self._schedule()
        event.accept()


class PanoramaPreviewDialog(QDialog):
    """Interactive approval dialog shown before a full-size export."""

    def __init__(self, image: QImage, camera_name: str, export: dict,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Interactive 360 preview - {name}",
                               name=camera_name))
        self.resize(960, 620)
        layout = QVBoxLayout(self)
        centre = (export["seam_heading"] + 180.0) % 360.0
        initial = ((export["start_heading"] - centre + 180.0) % 360.0) - 180.0
        layout.addWidget(PanoramaView(
            image, initial, centre, export["north_heading"], self), 1)
        help_label = QLabel(tr(
            "Drag to look around. Use the mouse wheel to zoom. This is a "
            "2K draft; the exported image uses the camera's saved settings."))
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        buttons = QDialogButtonBox(self)
        export = buttons.addButton(tr("Export full image..."),
                                   QDialogButtonBox.AcceptRole)
        buttons.addButton(tr("Close preview"), QDialogButtonBox.RejectRole)
        export.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class CompassWidget(QWidget):
    """Clickable compass showing camera, north, start and seam headings."""

    angleClicked = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._values = (0.0, 0.0, 0.0, 180.0)
        self.setMinimumSize(170, 170)

    def set_values(self, camera, north, start, seam) -> None:
        self._values = (float(camera), float(north), float(start), float(seam))
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        centre = QPointF(self.width() / 2.0, self.height() / 2.0)
        radius = max(20.0, min(self.width(), self.height()) / 2.0 - 20.0)
        painter.setPen(QPen(QColor(120, 120, 120), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(centre, radius, radius)
        painter.drawText(QPointF(centre.x() - 5, centre.y() - radius - 4), "0")
        painter.drawText(QPointF(centre.x() + radius + 5, centre.y() + 5), "90")
        painter.drawText(QPointF(centre.x() - 10, centre.y() + radius + 15), "180")
        painter.drawText(QPointF(centre.x() - radius - 24, centre.y() + 5), "270")
        specs = ((self._values[0], QColor(40, 40, 40), 4),
                 (self._values[1], QColor(35, 120, 235), 3),
                 (self._values[2], QColor(35, 180, 95), 3),
                 (self._values[3], QColor(235, 70, 55), 3))
        for heading, color, width in specs:
            angle = math.radians(heading)
            tip = QPointF(centre.x() + math.sin(angle) * radius,
                          centre.y() - math.cos(angle) * radius)
            painter.setPen(QPen(color, width))
            painter.drawLine(centre, tip)
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        centre = QPointF(self.width() / 2.0, self.height() / 2.0)
        delta = event.position() - centre
        if delta.x() * delta.x() + delta.y() * delta.y() < 100.0:
            return
        self.angleClicked.emit(math.degrees(
            math.atan2(delta.x(), -delta.y())) % 360.0)


class HotspotDialog(QDialog):
    """Editor for one navigation arrow or information hotspot."""

    def __init__(self, kind, cameras, current_id, hotspot=None,
                 default_heading=0.0, parent=None) -> None:
        super().__init__(parent)
        self._kind = kind
        self.setWindowTitle(tr("Navigation hotspot") if kind == "navigation"
                            else tr("Information hotspot"))
        form = QFormLayout(self)
        self._label = QLineEdit()
        self._target = QComboBox()
        for camera in cameras:
            if camera["id"] != current_id:
                self._target.addItem(camera["name"], camera["id"])
        self._description = QTextEdit()
        self._description.setMaximumHeight(100)
        self._image = QLineEdit()
        image_row = QHBoxLayout()
        image_row.addWidget(self._image, 1)
        browse = QPushButton(tr("Browse..."))
        browse.clicked.connect(self._browse_image)
        image_row.addWidget(browse)
        self._url = QLineEdit()
        self._heading = QDoubleSpinBox()
        self._heading.setRange(0.0, 359.9)
        self._heading.setWrapping(True)
        self._heading.setSuffix(" deg")
        self._tilt = QDoubleSpinBox()
        self._tilt.setRange(-89.0, 89.0)
        self._tilt.setSuffix(" deg")
        self._size = QDoubleSpinBox()
        self._size.setRange(20, 72)
        self._size.setDecimals(0)
        self._size.setSuffix(" px")
        form.addRow(tr("Label:"), self._label)
        if kind == "navigation":
            form.addRow(tr("Target camera:"), self._target)
        else:
            form.addRow(tr("Description:"), self._description)
            form.addRow(tr("Image:"), image_row)
            form.addRow(tr("Web link:"), self._url)
        form.addRow(tr("Compass heading:"), self._heading)
        form.addRow(tr("Vertical tilt:"), self._tilt)
        form.addRow(tr("Marker size:"), self._size)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        data = hotspot or {}
        self._label.setText(str(data.get("label", "")))
        self._description.setPlainText(str(data.get("description", "")))
        self._image.setText(str(data.get("image", "")))
        self._url.setText(str(data.get("url", "")))
        self._heading.setValue(float(data.get("heading", default_heading)))
        self._tilt.setValue(float(data.get("tilt", 0.0)))
        self._size.setValue(float(data.get("size", 42)))
        target = self._target.findData(data.get("target"))
        if target >= 0:
            self._target.setCurrentIndex(target)

    def _browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Choose hotspot image"), self._image.text(),
            tr("Images (*.png *.jpg *.jpeg *.webp);;All files (*)"))
        if path:
            self._image.setText(path)

    def hotspot(self, old_id=None) -> dict:
        return pano.clean_hotspot({
            "id": old_id or uuid.uuid4().hex,
            "type": self._kind,
            "label": self._label.text(),
            "target": self._target.currentData() or "",
            "description": self._description.toPlainText(),
            "image": self._image.text(),
            "url": self._url.text(),
            "heading": self._heading.value(),
            "tilt": self._tilt.value(),
            "size": int(self._size.value()),
        })


class HotspotPlacementDialog(QDialog):
    """Visual double-click/drag editor layered over a rendered panorama."""

    def __init__(self, image, camera, cameras, export, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Visual hotspot editor - {name}",
                               name=camera["name"]))
        self.resize(1080, 720)
        self.camera = camera
        self.cameras = cameras
        self.action = None
        self.backlinks = []
        self._hotspots = [dict(item) for item in camera.get("hotspots", [])]
        layout = QVBoxLayout(self)
        help_label = QLabel(tr(
            "Drag to look around. Double-click empty space to add a hotspot. "
            "Drag markers to move them. Right-click a marker to edit or delete."))
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        centre = (export["seam_heading"] + 180.0) % 360.0
        initial = ((export["start_heading"] - centre + 180.0) % 360.0) - 180.0
        self.viewer = PanoramaView(
            image, initial, centre, export["north_heading"], self,
            hotspots=self._hotspots)
        self.viewer.placeRequested.connect(self._place)
        self.viewer.markerMoved.connect(self._moved)
        self.viewer.markerEditRequested.connect(self._edit)
        self.viewer.markerDeleteRequested.connect(self._delete)
        self.viewer.navigateRequested.connect(self._test_navigate)
        layout.addWidget(self.viewer, 1)
        options = QHBoxLayout()
        self._snap = QCheckBox(tr("Snap navigation arrows to horizon"))
        self._snap.setChecked(True)
        self._snap.toggled.connect(self.viewer.set_snap_navigation)
        self._return = QCheckBox(tr("Create return hotspot automatically"))
        self._return.setChecked(True)
        self._test = QCheckBox(tr("Test navigation"))
        self._test.toggled.connect(self.viewer.set_test_navigation)
        options.addWidget(self._snap)
        options.addWidget(self._return)
        options.addWidget(self._test)
        options.addStretch(1)
        layout.addLayout(options)
        navigation = QHBoxLayout()
        previous = QPushButton(tr("Previous camera"))
        previous.clicked.connect(lambda: self._switch(-1))
        next_camera = QPushButton(tr("Next camera"))
        next_camera.clicked.connect(lambda: self._switch(1))
        navigation.addWidget(previous)
        navigation.addWidget(next_camera)
        navigation.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText(tr("Save and close"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        navigation.addWidget(buttons)
        layout.addLayout(navigation)

    def _find(self, hotspot_id):
        return next((item for item in self._hotspots
                     if item["id"] == hotspot_id), None)

    def _place(self, kind, heading, tilt) -> None:
        if kind == "navigation" and len(self.cameras) < 2:
            QMessageBox.information(
                self, tr("Hotspots"),
                tr("Create another camera before adding a navigation arrow."))
            return
        data = {"type": kind, "heading": heading, "tilt": tilt, "size": 42}
        dialog = HotspotDialog(
            kind, self.cameras, self.camera["id"], data, heading, self)
        if dialog.exec() != QDialog.Accepted:
            return
        hotspot = dialog.hotspot()
        if hotspot is None:
            return
        self._hotspots.append(hotspot)
        if (kind == "navigation" and hotspot["target"] and
                self._return.isChecked()):
            self.backlinks.append({"hotspot": hotspot["id"],
                                   "target": hotspot["target"]})
        self.viewer.set_hotspots(self._hotspots)

    def _edit(self, hotspot_id) -> None:
        hotspot = self._find(hotspot_id)
        if hotspot is None:
            return
        dialog = HotspotDialog(
            hotspot["type"], self.cameras, self.camera["id"], hotspot,
            hotspot["heading"], self)
        if dialog.exec() != QDialog.Accepted:
            return
        replacement = dialog.hotspot(hotspot["id"])
        if replacement is None:
            return
        index = self._hotspots.index(hotspot)
        self._hotspots[index] = replacement
        for backlink in self.backlinks:
            if backlink["hotspot"] == hotspot_id:
                backlink["target"] = replacement["target"]
        self.viewer.set_hotspots(self._hotspots)

    def _delete(self, hotspot_id) -> None:
        hotspot = self._find(hotspot_id)
        if hotspot is None:
            return
        self._hotspots.remove(hotspot)
        self.backlinks = [item for item in self.backlinks
                          if item["hotspot"] != hotspot_id]
        self.viewer.set_hotspots(self._hotspots)

    def _moved(self, hotspot_id, heading, tilt) -> None:
        hotspot = self._find(hotspot_id)
        if hotspot is not None:
            hotspot["heading"], hotspot["tilt"] = heading, tilt

    def _switch(self, delta) -> None:
        self.action = ("switch", int(delta))
        self.accept()

    def _test_navigate(self, target_id) -> None:
        self.action = ("target", target_id)
        self.accept()

    def hotspots(self):
        return pano.clean_hotspots(self._hotspots)


class BatchExportDialog(QDialog):
    """Checked, reorderable list used to build the batch queue."""

    def __init__(self, cameras, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Selective batch export"))
        self.resize(560, 430)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr(
            "Check cameras to export. Drag items or use Up/Down to reorder.")))
        self._list = QListWidget()
        self._list.setDragDropMode(QAbstractItemView.InternalMove)
        for camera in cameras:
            export = pano.clean_export_settings(camera.get("export"),
                                                camera["heading"])
            item = QListWidgetItem(
                f'{camera["name"]} | {export["width"]} px | '
                f'{export["format"].upper()} | {export["quality"]}')
            item.setData(Qt.UserRole, camera["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable |
                          Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled)
            item.setCheckState(Qt.Checked)
            self._list.addItem(item)
        layout.addWidget(self._list, 1)
        row = QHBoxLayout()
        select_all = QPushButton(tr("Select all"))
        select_all.clicked.connect(lambda: self._check_all(True))
        select_none = QPushButton(tr("Select none"))
        select_none.clicked.connect(lambda: self._check_all(False))
        up = QPushButton(tr("Up"))
        up.clicked.connect(lambda: self._move(-1))
        down = QPushButton(tr("Down"))
        down.clicked.connect(lambda: self._move(1))
        for button in (select_all, select_none, up, down):
            row.addWidget(button)
        layout.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(tr("Export selected"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _check_all(self, checked) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for index in range(self._list.count()):
            self._list.item(index).setCheckState(state)

    def _move(self, delta) -> None:
        row = self._list.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self._list.count():
            return
        item = self._list.takeItem(row)
        self._list.insertItem(target, item)
        self._list.setCurrentRow(target)

    def selected_ids(self) -> list[str]:
        return [self._list.item(i).data(Qt.UserRole)
                for i in range(self._list.count())
                if self._list.item(i).checkState() == Qt.Checked]


class ExportCancelled(Exception):
    pass


class PanoramaPanel(QWidget):
    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self._queue = []
        self._result = None
        self._paused = False
        self._cancel_requested = False
        self._queue_started = 0.0
        self._active_name = ""
        self._view_pick = None
        self._picking = None
        self._view_before_preview = None
        self._thumbnails = {}
        self._thumbnail_busy = False
        self._loading_camera = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(8, 6, 8, 8)
        settings = QSettings()

        # Cameras live in the document and therefore follow copies and saves.
        sec = FoldSection(tr("360 cameras"), _SETTINGS + "open_cameras")
        self._camera_panel = sec.body
        box = QVBoxLayout(sec.body)
        self._cameras = QListWidget()
        self._cameras.setMinimumHeight(150)
        self._cameras.setIconSize(QSize(112, 63))
        self._cameras.setSpacing(3)
        self._cameras.currentRowChanged.connect(self._selected)
        box.addWidget(self._cameras)
        row = QHBoxLayout()
        add = QPushButton(tr("+ From view"))
        add.setToolTip(tr("Create a 360 camera at the current viewport eye"))
        add.clicked.connect(self._add_from_view)
        place = QPushButton(tr("+ Place..."))
        place.setToolTip(tr("Click a floor point; eye height is added"))
        place.clicked.connect(lambda: self._begin_pick("new"))
        row.addWidget(add)
        row.addWidget(place)
        box.addLayout(row)
        thumbnails = QPushButton(tr("Refresh camera thumbnails"))
        thumbnails.setToolTip(tr(
            "Capture a new gallery thumbnail for every saved camera"))
        thumbnails.clicked.connect(self.refresh_thumbnails)
        box.addWidget(thumbnails)

        self._editor = QWidget()
        form = wrapping_form(self._editor)
        form.setContentsMargins(0, 0, 0, 0)
        self._name = QLineEdit()
        self._name.editingFinished.connect(self._edited)
        form.addRow(tr("Name:"), self._name)
        posrow = QHBoxLayout()
        self._xyz = []
        for axis in "XYZ":
            spin = QDoubleSpinBox()
            spin.setRange(-1e8, 1e8)
            spin.setDecimals(3)
            spin.setSuffix(" m")
            spin.setToolTip(axis)
            spin.editingFinished.connect(self._edited)
            self._xyz.append(spin)
            posrow.addWidget(spin)
        form.addRow(tr("Position:"), posrow)
        self._heading = QDoubleSpinBox()
        self._heading.setRange(0.0, 359.9)
        self._heading.setDecimals(1)
        self._heading.setWrapping(True)
        self._heading.setSuffix(" deg")
        self._heading.setToolTip(tr("0 deg north, 90 deg east"))
        self._heading.editingFinished.connect(self._edited)
        form.addRow(tr("Heading:"), self._heading)
        self._tilt = QDoubleSpinBox()
        self._tilt.setRange(-90.0, 90.0)
        self._tilt.setDecimals(1)
        self._tilt.setSuffix(" deg")
        self._tilt.setToolTip(tr(
            "Used by camera preview; 360 export levels the horizon automatically"))
        self._tilt.editingFinished.connect(self._edited)
        form.addRow(tr("Tilt:"), self._tilt)
        self._eye_height = QDoubleSpinBox()
        self._eye_height.setRange(0.0, 20.0)
        self._eye_height.setDecimals(2)
        self._eye_height.setValue(float(settings.value(
            _SETTINGS + "eye_height", 1.65)))
        self._eye_height.setSuffix(" m")
        form.addRow(tr("Placement eye height:"), self._eye_height)
        row = QHBoxLayout()
        move = QPushButton(tr("Move..."))
        move.clicked.connect(lambda: self._begin_pick("move"))
        aim = QPushButton(tr("Aim..."))
        aim.clicked.connect(lambda: self._begin_pick("aim"))
        row.addWidget(move)
        row.addWidget(aim)
        form.addRow(row)
        row = QHBoxLayout()
        self._preview_button = QPushButton(tr("View camera preview"))
        self._preview_button.setToolTip(tr(
            "View from the selected camera with the 90-degree capture lens"))
        self._preview_button.clicked.connect(self._start_preview)
        self._normal_view_button = QPushButton(tr("Back to normal view"))
        self._normal_view_button.setToolTip(tr(
            "Restore the perspective view from before the camera preview"))
        self._normal_view_button.clicked.connect(self._stop_preview)
        self._normal_view_button.setEnabled(False)
        row.addWidget(self._preview_button)
        row.addWidget(self._normal_view_button)
        form.addRow(row)
        self._preview_status = QLabel(tr("Camera preview is off."))
        self._preview_status.setWordWrap(True)
        self._preview_status.setStyleSheet("color: palette(mid);")
        form.addRow(self._preview_status)
        row = QHBoxLayout()
        update = QPushButton(tr("Update from view"))
        update.clicked.connect(self._update_from_view)
        duplicate = QPushButton(tr("Duplicate"))
        duplicate.clicked.connect(self._duplicate)
        delete = QPushButton(tr("Delete"))
        delete.clicked.connect(self._delete)
        row.addWidget(update)
        row.addWidget(duplicate)
        row.addWidget(delete)
        form.addRow(row)
        box.addWidget(self._editor)
        lay.addWidget(sec)

        sec = FoldSection(tr("Virtual tour hotspots"),
                          _SETTINGS + "open_hotspots")
        self._hotspot_panel = sec.body
        box = QVBoxLayout(sec.body)
        hotspot_help = QLabel(tr(
            "Navigation arrows connect cameras. Information hotspots can "
            "show text, an image, and a web link in the HTML tour."))
        hotspot_help.setWordWrap(True)
        hotspot_help.setStyleSheet("color: palette(mid);")
        box.addWidget(hotspot_help)
        self._hotspots = QListWidget()
        self._hotspots.setMinimumHeight(90)
        self._hotspots.itemDoubleClicked.connect(lambda _item: self._edit_hotspot())
        box.addWidget(self._hotspots)
        visual = QPushButton(tr("Place hotspots visually..."))
        visual.setToolTip(tr(
            "Open the panorama and place or drag hotspots directly on it"))
        visual.clicked.connect(self._visual_hotspot_editor)
        box.addWidget(visual)
        row = QHBoxLayout()
        add_navigation = QPushButton(tr("+ Navigation"))
        add_navigation.clicked.connect(lambda: self._add_hotspot("navigation"))
        add_info = QPushButton(tr("+ Information"))
        add_info.clicked.connect(lambda: self._add_hotspot("info"))
        row.addWidget(add_navigation)
        row.addWidget(add_info)
        box.addLayout(row)
        row = QHBoxLayout()
        edit_hotspot = QPushButton(tr("Edit"))
        edit_hotspot.clicked.connect(self._edit_hotspot)
        delete_hotspot = QPushButton(tr("Delete"))
        delete_hotspot.clicked.connect(self._delete_hotspot)
        row.addWidget(edit_hotspot)
        row.addWidget(delete_hotspot)
        box.addLayout(row)
        lay.addWidget(sec)

        # Every camera keeps its own projection, image and destination settings.
        sec = FoldSection(tr("Projection and image"), _SETTINGS + "open_image")
        form = wrapping_form(sec.body)
        per_camera = QLabel(tr("These settings are saved with the selected camera."))
        per_camera.setWordWrap(True)
        per_camera.setStyleSheet("color: palette(mid);")
        form.addRow(per_camera)
        self._projection = QComboBox()
        for key, label in _PROJECTION_LABELS:
            self._projection.addItem(tr(label), key)
        self._set_combo(self._projection, settings.value(
            _SETTINGS + "projection", "equirectangular"))
        self._projection.currentIndexChanged.connect(
            self._export_settings_changed)
        form.addRow(tr("Projection:"), self._projection)
        self._resolution = QComboBox()
        for label, px in (("2K", 2048), ("4K", 4096), ("6K", 6144),
                          ("8K", 8192), ("12K", 12288), ("16K", 16384)):
            self._resolution.addItem(label, px)
        wanted = int(settings.value(_SETTINGS + "width", 4096))
        self._resolution.setCurrentIndex(max(0, self._resolution.findData(wanted)))
        self._resolution.currentIndexChanged.connect(
            self._export_settings_changed)
        self._size = QLabel()
        rrow = QHBoxLayout()
        rrow.addWidget(self._resolution, 1)
        rrow.addWidget(self._size)
        form.addRow(tr("Resolution:"), rrow)
        self._format = QComboBox()
        for label, key in (("PNG", "png"), ("JPEG", "jpeg")):
            self._format.addItem(label, key)
        self._set_combo(self._format, settings.value(_SETTINGS + "format", "png"))
        self._format.currentIndexChanged.connect(self._export_settings_changed)
        form.addRow(tr("Format:"), self._format)
        self._quality = QComboBox()
        for label, key in ((tr("Draft - fast"), "draft"),
                           (tr("Standard"), "standard"),
                           (tr("High - supersampled"), "high"),
                           (tr("Ultra - maximum quality"), "ultra")):
            self._quality.addItem(label, key)
        self._set_combo(self._quality, settings.value(
            _SETTINGS + "quality", "standard"))
        self._quality.currentIndexChanged.connect(self._export_settings_changed)
        form.addRow(tr("Quality:"), self._quality)
        folder_row = QHBoxLayout()
        self._output_folder = QLineEdit()
        self._output_folder.setPlaceholderText(tr("Choose an output folder"))
        self._output_folder.setText(str(settings.value(
            _SETTINGS + "folder", "")))
        self._output_folder.editingFinished.connect(
            self._export_settings_changed)
        self._folder_button = QPushButton(tr("Browse..."))
        self._folder_button.clicked.connect(self._choose_output_folder)
        folder_row.addWidget(self._output_folder, 1)
        folder_row.addWidget(self._folder_button)
        form.addRow(tr("Output folder:"), folder_row)
        self._auto_level = QCheckBox(tr("Auto-level horizon (always on)"))
        self._auto_level.setChecked(True)
        self._auto_level.setEnabled(False)
        self._auto_level.setToolTip(tr(
            "Keeps the panorama horizon horizontal even if the viewport "
            "camera was looking up or down"))
        form.addRow(self._auto_level)
        self._metadata = QCheckBox(tr("Write metadata sidecar"))
        self._metadata.setChecked(str(settings.value(_SETTINGS + "metadata", "1")) != "0")
        form.addRow(self._metadata)
        lay.addWidget(sec)

        sec = FoldSection(tr("Compass and panorama seam"),
                          _SETTINGS + "open_compass")
        form = wrapping_form(sec.body)
        compass_help = QLabel(tr(
            "Click the compass to set the marker selected below. Blue is "
            "north, green is the initial tour view, red is the image seam, "
            "and black is the camera heading."))
        compass_help.setWordWrap(True)
        compass_help.setStyleSheet("color: palette(mid);")
        form.addRow(compass_help)
        self._compass = CompassWidget()
        self._compass.angleClicked.connect(self._compass_clicked)
        form.addRow(self._compass)
        self._compass_target = QComboBox()
        self._compass_target.addItem(tr("North direction"), "north")
        self._compass_target.addItem(tr("Starting direction"), "start")
        self._compass_target.addItem(tr("Panorama seam"), "seam")
        form.addRow(tr("Click sets:"), self._compass_target)
        self._north = self._heading_spin()
        self._start_heading = self._heading_spin()
        self._seam_heading = self._heading_spin()
        for spin in (self._north, self._start_heading, self._seam_heading):
            spin.valueChanged.connect(self._export_settings_changed)
        form.addRow(tr("North:"), self._north)
        form.addRow(tr("Initial view:"), self._start_heading)
        form.addRow(tr("Image seam:"), self._seam_heading)
        row = QHBoxLayout()
        north_camera = QPushButton(tr("North = camera"))
        north_camera.clicked.connect(
            lambda: self._north.setValue(self._heading.value()))
        start_camera = QPushButton(tr("Start = camera"))
        start_camera.clicked.connect(
            lambda: self._start_heading.setValue(self._heading.value()))
        seam_behind = QPushButton(tr("Seam behind"))
        seam_behind.clicked.connect(lambda: self._seam_heading.setValue(
            (self._heading.value() + 180.0) % 360.0))
        for button in (north_camera, start_camera, seam_behind):
            row.addWidget(button)
        form.addRow(row)
        lay.addWidget(sec)

        self._export_settings = (
            self._projection, self._resolution, self._format, self._quality,
            self._output_folder, self._folder_button, self._metadata,
            self._north, self._start_heading, self._seam_heading,
        )

        sec = FoldSection(tr("Export"), _SETTINGS + "open_export")
        box = QVBoxLayout(sec.body)
        lighting = QLabel(tr(
            "Uses IngeTrazo's native 2D image renderer. No Blender or other "
            "software is required."))
        lighting.setWordWrap(True)
        lighting.setStyleSheet("color: palette(mid);")
        box.addWidget(lighting)
        self._where = QLabel()
        self._where.setWordWrap(True)
        box.addWidget(self._where)
        self._interactive = QPushButton(tr("Interactive 360 preview..."))
        self._interactive.setToolTip(tr(
            "Render a draggable 2K panorama preview before exporting"))
        self._interactive.clicked.connect(self._interactive_preview)
        self._go = QPushButton(tr("Export selected 360..."))
        self._go.clicked.connect(self._export_selected)
        self._all = QPushButton(tr("Selective batch export..."))
        self._all.clicked.connect(self._export_all)
        self._tour = QPushButton(tr("Export standalone HTML tour..."))
        self._tour.clicked.connect(self._export_tour)
        box.addWidget(self._interactive)
        box.addWidget(self._go)
        box.addWidget(self._all)
        box.addWidget(self._tour)
        run_row = QHBoxLayout()
        self._pause = QPushButton(tr("Pause"))
        self._pause.clicked.connect(self._toggle_pause)
        self._cancel = QPushButton(tr("Cancel"))
        self._cancel.clicked.connect(self._cancel_export)
        self._pause.setVisible(False)
        self._cancel.setVisible(False)
        run_row.addWidget(self._pause)
        run_row.addWidget(self._cancel)
        box.addLayout(run_row)
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setVisible(False)
        box.addWidget(self._bar)
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.RichText)
        self._status.linkActivated.connect(self._open_result)
        box.addWidget(self._status)
        lay.addWidget(sec)
        lay.addStretch(1)
        narrow(self._name, self._projection, self._resolution, self._format,
               self._output_folder, self._interactive, self._go, self._all,
               self._tour, self._status)
        self._projection_changed()
        self._update_renderer_label()

    @staticmethod
    def _set_combo(combo, data) -> None:
        i = combo.findData(data)
        combo.setCurrentIndex(max(0, i))

    @staticmethod
    def _heading_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 359.9)
        spin.setDecimals(1)
        spin.setWrapping(True)
        spin.setSuffix(" deg")
        return spin

    # ---- Cameras ----------------------------------------------------------
    def cameras(self):
        return _state(self.app)["cameras"]

    def selected_index(self):
        row = self._cameras.currentRow()
        return row if 0 <= row < len(self.cameras()) else None

    def selected_camera(self):
        i = self.selected_index()
        cams = self.cameras()
        return cams[i] if i is not None and i < len(cams) else None

    def refresh(self) -> None:
        chosen = self.selected_camera()
        chosen_id = chosen["id"] if chosen else None
        cams = self.cameras()
        valid_ids = {cam["id"] for cam in cams}
        self._thumbnails = {key: value for key, value in self._thumbnails.items()
                            if key in valid_ids}
        self._cameras.blockSignals(True)
        self._cameras.clear()
        for cam in cams:
            item = QListWidgetItem(self._camera_item_text(cam))
            thumbnail = self._thumbnails.get(cam["id"])
            if thumbnail is not None and not thumbnail.isNull():
                item.setIcon(QIcon(QPixmap.fromImage(thumbnail)))
            self._cameras.addItem(item)
        row = next((i for i, c in enumerate(cams) if c["id"] == chosen_id), 0)
        self._cameras.setCurrentRow(row if cams else -1)
        self._cameras.blockSignals(False)
        self._selected(self._cameras.currentRow())
        self._view_pick = None
        self.app.release_pick()
        self.app.viewport.update()
        QTimer.singleShot(0, self._capture_missing_thumbnails)

    @staticmethod
    def _camera_item_text(camera) -> str:
        export = pano.clean_export_settings(camera.get("export"),
                                            camera["heading"])
        projection = export["projection"].replace("_", " ")
        return (f'{camera["name"]}\n{export["width"]} px | '
                f'{export["format"].upper()} | {export["quality"]} | '
                f'{projection}')

    def _capture_missing_thumbnails(self) -> None:
        if self._thumbnail_busy:
            return
        missing = [(i, cam) for i, cam in enumerate(self.cameras())
                   if cam["id"] not in self._thumbnails]
        if not missing:
            return
        self._thumbnail_busy = True
        try:
            for index, cam in missing:
                try:
                    image = pano.render_camera_image(
                        self.app.viewport, cam, 224, 126, fov=90.0)
                except Exception:
                    continue
                self._thumbnails[cam["id"]] = image
                item = self._cameras.item(index)
                if item is not None:
                    item.setIcon(QIcon(QPixmap.fromImage(image)))
                QApplication.processEvents()
        finally:
            self._thumbnail_busy = False

    def refresh_thumbnails(self) -> None:
        self._thumbnails.clear()
        for index in range(self._cameras.count()):
            self._cameras.item(index).setIcon(QIcon())
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._capture_missing_thumbnails()
        finally:
            QApplication.restoreOverrideCursor()

    def _selected(self, row) -> None:
        cams = self.cameras()
        on = 0 <= row < len(cams)
        self._editor.setEnabled(on)
        if on:
            cam = cams[row]
            export = pano.clean_export_settings(cam.get("export"), cam["heading"])
            controls = [self._name, *self._xyz, self._heading, self._tilt]
            for w in controls:
                w.blockSignals(True)
            self._name.setText(cam["name"])
            for spin, value in zip(self._xyz, cam["pos"]):
                spin.setValue(value)
            self._heading.setValue(cam["heading"])
            self._tilt.setValue(cam["tilt"])
            for w in controls:
                w.blockSignals(False)
            self._loading_camera = True
            export_widgets = (self._projection, self._resolution, self._format,
                              self._quality, self._output_folder, self._north,
                              self._start_heading, self._seam_heading)
            for widget in export_widgets:
                widget.blockSignals(True)
            self._set_combo(self._projection, export["projection"])
            self._set_combo(self._resolution, export["width"])
            self._set_combo(self._format, export["format"])
            self._set_combo(self._quality, export["quality"])
            self._output_folder.setText(export["folder"])
            self._north.setValue(export["north_heading"])
            self._start_heading.setValue(export["start_heading"])
            self._seam_heading.setValue(export["seam_heading"])
            for widget in export_widgets:
                widget.blockSignals(False)
            self._loading_camera = False
            self._update_size_label()
            self._update_compass()
            self._refresh_hotspots(cam)
            if self._view_before_preview is not None:
                self._apply_preview(cam)
        elif self._view_before_preview is not None:
            self._stop_preview()
        if not on:
            self._hotspots.clear()
        self.app.viewport.update()

    def _view_values(self):
        cam = self.app.viewport.camera
        eye, d = cam.eye(), cam.forward()
        heading = math.degrees(math.atan2(d.x(), d.y())) % 360.0
        tilt = math.degrees(math.asin(max(-1.0, min(1.0, d.z()))))
        return [eye.x(), eye.y(), eye.z()], heading, tilt

    def _new_camera(self, pos, heading, tilt, name=None, export=None,
                    hotspots=None):
        cams = self.cameras()
        settings = pano.clean_export_settings(
            export if export is not None else self._current_export_settings(),
            heading)
        if export is None:
            settings["start_heading"] = heading % 360.0
            settings["seam_heading"] = (heading + 180.0) % 360.0
        cams.append({"id": uuid.uuid4().hex, "name": name or tr(
            "360 Camera {n}", n=len(cams) + 1), "pos": list(pos),
            "heading": heading, "tilt": tilt,
            "export": settings,
            "hotspots": pano.clean_hotspots(hotspots or [])})
        _store(self.app, cams)
        self._cameras.setCurrentRow(len(cams) - 1)

    def _add_from_view(self):
        self._new_camera(*self._view_values())

    def _update_from_view(self):
        i = self.selected_index()
        if i is None:
            return
        cams = self.cameras()
        self._thumbnails.pop(cams[i]["id"], None)
        cams[i]["pos"], cams[i]["heading"], cams[i]["tilt"] = self._view_values()
        _store(self.app, cams)

    def _duplicate(self):
        cam = self.selected_camera()
        if cam is None:
            return
        pos = list(cam["pos"])
        pos[0] += 0.25
        self._new_camera(pos, cam["heading"], cam["tilt"],
                         cam["name"] + " " + tr("copy"), cam.get("export"),
                         cam.get("hotspots"))

    def _delete(self):
        i = self.selected_index()
        if i is None:
            return
        if self._view_before_preview is not None:
            self._stop_preview()
        cams = self.cameras()
        removed_id = cams[i]["id"]
        self._thumbnails.pop(removed_id, None)
        del cams[i]
        for camera in cams:
            camera["hotspots"] = [
                spot for spot in camera.get("hotspots", [])
                if not (spot["type"] == "navigation" and
                        spot["target"] == removed_id)]
        _store(self.app, cams)

    def _edited(self):
        i = self.selected_index()
        cams = self.cameras()
        if i is None or i >= len(cams):
            return
        cam = cams[i]
        self._thumbnails.pop(cam["id"], None)
        cam.update(name=self._name.text().strip() or cam["name"],
                   pos=[s.value() for s in self._xyz],
                   heading=self._heading.value(), tilt=self._tilt.value())
        _store(self.app, cams)
        if self._view_before_preview is not None:
            self._apply_preview(cam)

    def _apply_preview(self, cam):
        pano.preview_view(self.app.viewport.camera, cam, fov=90.0)
        self._preview_status.setText(tr(
            "Previewing {name} with a 90-degree lens.", name=cam["name"]))
        self.app.viewport.update()
        self.app.viewport.setFocus()

    def _start_preview(self):
        cam = self.selected_camera()
        if cam is None:
            return
        if self._view_before_preview is None:
            self._view_before_preview = pano.capture_view(
                self.app.viewport.camera)
        self._normal_view_button.setEnabled(True)
        self._apply_preview(cam)

    def _stop_preview(self):
        if self._view_before_preview is None:
            return
        pano.restore_view(self.app.viewport.camera, self._view_before_preview)
        self._view_before_preview = None
        self._normal_view_button.setEnabled(False)
        self._preview_status.setText(tr("Camera preview is off."))
        self.app.viewport.update()
        self.app.viewport.setFocus()

    # ---- Virtual-tour hotspots ------------------------------------------
    def _refresh_hotspots(self, camera=None) -> None:
        camera = camera or self.selected_camera()
        self._hotspots.clear()
        if camera is None:
            return
        names = {item["id"]: item["name"] for item in self.cameras()}
        for hotspot in camera.get("hotspots", []):
            if hotspot["type"] == "navigation":
                target = names.get(hotspot["target"], tr("Missing camera"))
                text = f'-> {target} | {hotspot["label"]}'
            else:
                text = f'i | {hotspot["label"]}'
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, hotspot["id"])
            self._hotspots.addItem(item)

    def _add_hotspot(self, kind) -> None:
        camera = self.selected_camera()
        if camera is None:
            QMessageBox.information(
                self, tr("Hotspots"), tr("Create or select a camera first."))
            return
        if kind == "navigation" and len(self.cameras()) < 2:
            QMessageBox.information(
                self, tr("Hotspots"),
                tr("Create another camera before adding a navigation arrow."))
            return
        export = pano.clean_export_settings(camera.get("export"),
                                            camera["heading"])
        dialog = HotspotDialog(kind, self.cameras(), camera["id"],
                               default_heading=export["start_heading"],
                               parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        hotspot = dialog.hotspot()
        if hotspot is None:
            return
        cams, index = self.cameras(), self.selected_index()
        cams[index].setdefault("hotspots", []).append(hotspot)
        _store(self.app, cams)

    def _edit_hotspot(self) -> None:
        camera = self.selected_camera()
        row = self._hotspots.currentRow()
        if camera is None or not (0 <= row < len(camera.get("hotspots", []))):
            return
        hotspot = camera["hotspots"][row]
        dialog = HotspotDialog(
            hotspot["type"], self.cameras(), camera["id"], hotspot,
            hotspot["heading"], self)
        if dialog.exec() != QDialog.Accepted:
            return
        edited = dialog.hotspot(hotspot["id"])
        if edited is None:
            return
        cams, index = self.cameras(), self.selected_index()
        cams[index]["hotspots"][row] = edited
        _store(self.app, cams)

    def _delete_hotspot(self) -> None:
        camera = self.selected_camera()
        row = self._hotspots.currentRow()
        if camera is None or not (0 <= row < len(camera.get("hotspots", []))):
            return
        cams, index = self.cameras(), self.selected_index()
        del cams[index]["hotspots"][row]
        _store(self.app, cams)

    def _visual_hotspot_editor(self) -> None:
        selected = self.selected_camera()
        if selected is None:
            QMessageBox.information(
                self, tr("Hotspots"), tr("Create or select a camera first."))
            return
        camera_id = selected["id"]
        cache = {}
        while True:
            cameras = self.cameras()
            index = next((i for i, item in enumerate(cameras)
                          if item["id"] == camera_id), None)
            if index is None:
                return
            camera = cameras[index]
            if camera_id not in cache:
                image = self._render_hotspot_panorama(camera)
                if image is None:
                    return
                cache[camera_id] = image
            export = pano.clean_export_settings(camera.get("export"),
                                                camera["heading"])
            dialog = HotspotPlacementDialog(
                cache[camera_id], camera, cameras, export, self)
            if dialog.exec() != QDialog.Accepted:
                return
            cameras = self.cameras()
            index = next((i for i, item in enumerate(cameras)
                          if item["id"] == camera_id), None)
            if index is None:
                return
            cameras[index]["hotspots"] = dialog.hotspots()
            self._apply_return_hotspots(cameras, camera_id, dialog.backlinks)
            _store(self.app, cameras)
            action = dialog.action
            if action is None:
                return
            current = next((i for i, item in enumerate(cameras)
                            if item["id"] == camera_id), 0)
            if action[0] == "switch":
                camera_id = cameras[(current + action[1]) % len(cameras)]["id"]
            elif action[0] == "target":
                if not any(item["id"] == action[1] for item in cameras):
                    return
                camera_id = action[1]
            row = next((i for i, item in enumerate(cameras)
                        if item["id"] == camera_id), 0)
            self._cameras.setCurrentRow(row)

    def _render_hotspot_panorama(self, camera):
        export = pano.clean_export_settings(camera.get("export"),
                                            camera["heading"])
        preset = pano.QUALITY_PRESETS[export["quality"]]
        self._cancel_requested = False
        self._paused = False
        self._queue_started = time.monotonic()
        self._active_name = camera["name"]
        self._set_running(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            faces = pano.render_cube_faces(
                self.app.viewport, camera, 512, overlays=False,
                auto_level=True,
                progress=lambda value: self._queue_progress(value * 0.35))
            image = pano.cube_to_equirectangular(
                faces, 2048, seam_blend=preset["seam"],
                progress=lambda value: self._queue_progress(
                    0.35 + value * 0.65))
            return pano.rotate_equirectangular(
                image, camera["heading"], export["seam_heading"])
        except ExportCancelled:
            self._status.setText(tr("Hotspot panorama cancelled."))
            return None
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Hotspot editor"), str(exc))
            self._status.setText(tr("Could not render the hotspot panorama."))
            return None
        finally:
            QApplication.restoreOverrideCursor()
            self._set_running(False)

    @staticmethod
    def _apply_return_hotspots(cameras, source_id, requests) -> None:
        source = next((item for item in cameras if item["id"] == source_id), None)
        if source is None:
            return
        source_pos = QVector3D(*source["pos"])
        for request in requests:
            target = next((item for item in cameras
                           if item["id"] == request["target"]), None)
            if target is None:
                continue
            if any(spot["type"] == "navigation" and
                   spot["target"] == source_id
                   for spot in target.get("hotspots", [])):
                continue
            delta = source_pos - QVector3D(*target["pos"])
            heading = (math.degrees(math.atan2(delta.x(), delta.y())) % 360.0
                       if delta.lengthSquared() > 1e-12 else 0.0)
            target.setdefault("hotspots", []).append(pano.clean_hotspot({
                "id": uuid.uuid4().hex,
                "type": "navigation",
                "label": tr("Back to {name}", name=source["name"]),
                "target": source_id,
                "heading": heading,
                "tilt": 0.0,
                "size": 42,
            }))

    def _begin_pick(self, purpose):
        if purpose != "new" and self.selected_camera() is None:
            return
        self._picking = purpose
        prompts = {"new": tr("Click the floor under the 360 camera"),
                   "move": tr("Click the camera's new floor point"),
                   "aim": tr("Click where the camera looks")}
        self.app.viewport.set_active_tool(_pick_tool(
            prompts[purpose], self._picked, self._end_pick))
        self.app.window.statusBar().showMessage(prompts[purpose] + " - " + tr("Esc cancels"))
        self.app.viewport.setFocus()

    def _picked(self, point):
        purpose = self._picking
        if purpose == "new":
            _pos, heading, tilt = self._view_values()
            self._new_camera((point.x(), point.y(), point.z() + self._eye_height.value()),
                             heading, tilt)
        else:
            i, cams = self.selected_index(), self.cameras()
            if i is not None:
                cam = cams[i]
                self._thumbnails.pop(cam["id"], None)
                if purpose == "move":
                    cam["pos"] = [point.x(), point.y(),
                                  point.z() + self._eye_height.value()]
                elif purpose == "aim":
                    d = point - QVector3D(*cam["pos"])
                    if d.lengthSquared() > 1e-12:
                        d.normalize()
                        cam["heading"] = math.degrees(math.atan2(d.x(), d.y())) % 360.0
                        cam["tilt"] = math.degrees(math.asin(d.z()))
                _store(self.app, cams)
        self._end_pick()

    def _end_pick(self):
        self._picking = None
        self.app.window.statusBar().clearMessage()
        activate = getattr(self.app.window, "_activate_tool", None)
        if callable(activate):
            activate("select")

    # ---- Settings ---------------------------------------------------------
    def _projection_changed(self):
        self._update_size_label()

    def _current_export_settings(self) -> dict:
        return pano.clean_export_settings({
            "projection": self._projection.currentData(),
            "width": self._resolution.currentData(),
            "format": self._format.currentData(),
            "folder": self._output_folder.text(),
            "quality": self._quality.currentData(),
            "north_heading": self._north.value(),
            "start_heading": self._start_heading.value(),
            "seam_heading": self._seam_heading.value(),
        }, self._heading.value())

    def _update_compass(self) -> None:
        self._compass.set_values(
            self._heading.value(), self._north.value(),
            self._start_heading.value(), self._seam_heading.value())

    def _compass_clicked(self, heading) -> None:
        target = self._compass_target.currentData()
        spin = {"north": self._north, "start": self._start_heading,
                "seam": self._seam_heading}.get(target)
        if spin is not None:
            spin.setValue(heading)

    def _export_settings_changed(self, *_args) -> None:
        self._update_size_label()
        if self._loading_camera:
            return
        i = self.selected_index()
        cams = self.cameras()
        if i is None or i >= len(cams):
            return
        cams[i]["export"] = self._current_export_settings()
        self._update_compass()
        self._remember()
        _store(self.app, cams)

    def _choose_output_folder(self) -> None:
        current = self._output_folder.text().strip()
        if not current:
            document = getattr(self.app.window, "_current_path", None)
            current = str(Path(document).parent if document else Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, tr("Choose this camera's output folder"), current)
        if folder:
            self._output_folder.setText(folder)
            self._export_settings_changed()

    def _update_size_label(self):
        key = self._projection.currentData()
        width = int(self._resolution.currentData() or 4096)
        w, h = pano.dimensions(key, width)
        suffix = tr("final") if key not in pano.CUBE_PROJECTIONS else tr("face / final")
        self._size.setText(f"{w} x {h} px {suffix}")

    def _remember(self):
        s = QSettings()
        values = {
            "projection": self._projection.currentData(),
            "width": self._resolution.currentData(),
            "format": self._format.currentData(),
            "quality": self._quality.currentData(),
            "folder": self._output_folder.text().strip(),
            "metadata": int(self._metadata.isChecked()),
            "eye_height": self._eye_height.value(),
        }
        for key, value in values.items():
            s.setValue(_SETTINGS + key, value)

    def _update_renderer_label(self):
        self._where.setText(tr(
            "Native 360 export is ready. Each camera keeps its own settings."))

    # ---- Export -----------------------------------------------------------
    @staticmethod
    def _destination_suffix(camera):
        export = pano.clean_export_settings(camera.get("export"),
                                            camera["heading"])
        if export["projection"] == "cubemap_faces":
            return ".zip"
        return pano.FORMATS[export["format"]]["suffix"]

    def _interactive_preview(self):
        camera = self.selected_camera()
        if camera is None:
            QMessageBox.information(
                self, tr("360 preview"), tr("Create or select a camera first."))
            return
        export = pano.clean_export_settings(camera.get("export"),
                                            camera["heading"])
        preset = pano.QUALITY_PRESETS[export["quality"]]
        self._remember()
        self._result = None
        self._cancel_requested = False
        self._paused = False
        self._queue_started = time.monotonic()
        self._active_name = camera["name"]
        self._set_running(True)
        self._status.setText(tr(
            "Rendering interactive preview for {name}...",
            name=camera["name"]))
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            faces = pano.render_cube_faces(
                self.app.viewport, camera, 512, overlays=False,
                auto_level=True,
                progress=lambda value: self._queue_progress(value * 0.35))
            image = pano.cube_to_equirectangular(
                faces, 2048, seam_blend=preset["seam"],
                progress=lambda value: self._queue_progress(
                    0.35 + value * 0.65))
            image = pano.rotate_equirectangular(
                image, camera["heading"], export["seam_heading"])
        except ExportCancelled:
            self._status.setText(tr("360 preview cancelled."))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("360 preview"), str(exc))
            self._status.setText(tr("360 preview failed."))
            return
        finally:
            QApplication.restoreOverrideCursor()
            self._set_running(False)
        self._status.setText(tr("Interactive preview is ready."))
        dialog = PanoramaPreviewDialog(image, camera["name"], export, self)
        if dialog.exec() == QDialog.Accepted:
            self._export_selected()

    def _export_selected(self):
        cam = self.selected_camera()
        if cam is None:
            QMessageBox.information(self, tr("360 export"), tr("Create or select a camera first."))
            return
        export = pano.clean_export_settings(cam.get("export"), cam["heading"])
        suffix = self._destination_suffix(cam)
        doc = getattr(self.app.window, "_current_path", None)
        base = (Path(export["folder"]) if export["folder"] else
                (Path(doc).parent if doc else Path.home()))
        start = base / (_safe_name(cam["name"]) + suffix)
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export 360 image"), str(start),
            tr("360 output (*{suffix});;All files (*)", suffix=suffix))
        if path:
            path = Path(path)
            if not path.suffix:
                path = path.with_suffix(suffix)
            export["folder"] = str(path.parent)
            cam["export"] = export
            cams = self.cameras()
            i = self.selected_index()
            if i is not None and i < len(cams):
                cams[i]["export"] = export
                _store(self.app, cams)
            self._start_queue([(cam, path)])

    def _export_all(self):
        cams = self.cameras()
        if not cams:
            QMessageBox.information(self, tr("360 export"), tr("Create a camera first."))
            return
        dialog = BatchExportDialog(cams, self)
        if dialog.exec() != QDialog.Accepted:
            return
        by_id = {camera["id"]: camera for camera in cams}
        cams = [by_id[key] for key in dialog.selected_ids() if key in by_id]
        if not cams:
            QMessageBox.information(
                self, tr("Batch export"), tr("Select at least one camera."))
            return
        missing_folder = any(not pano.clean_export_settings(
            cam.get("export"), cam["heading"])["folder"] for cam in cams)
        fallback = ""
        if missing_folder:
            document = getattr(self.app.window, "_current_path", None)
            start = str(Path(document).parent if document else Path.home())
            fallback = QFileDialog.getExistingDirectory(
                self, tr("Choose a folder for cameras without one"), start)
            if not fallback:
                return
        used = set()
        jobs = []
        for cam in cams:
            export = pano.clean_export_settings(cam.get("export"),
                                                cam["heading"])
            folder = Path(export["folder"] or fallback)
            suffix = self._destination_suffix(cam)
            stem, n = _safe_name(cam["name"]), 2
            unique = stem
            key = (str(folder).casefold(), unique.casefold())
            while key in used:
                unique, n = f"{stem}-{n}", n + 1
                key = (str(folder).casefold(), unique.casefold())
            used.add(key)
            jobs.append((cam, folder / (unique + suffix)))
        self._start_queue(jobs)

    def _export_tour(self) -> None:
        cameras = self.cameras()
        if not cameras:
            QMessageBox.information(
                self, tr("HTML tour"), tr("Create a camera first."))
            return
        root = QFileDialog.getExistingDirectory(
            self, tr("Choose the standalone tour folder"), str(Path.home()))
        if not root:
            return
        root = Path(root)
        panorama_folder = root / "panoramas"
        jobs = []
        rendered = []
        for camera in cameras:
            clone = json.loads(json.dumps(camera))
            export = pano.clean_export_settings(clone.get("export"),
                                                clone["heading"])
            export.update(projection="equirectangular", format="jpeg",
                          folder=str(panorama_folder))
            clone["export"] = export
            destination = panorama_folder / (clone["id"] + ".jpg")
            jobs.append((clone, destination))
            rendered.append(clone)
        if not self._start_queue(jobs):
            return
        try:
            index = self._write_tour_files(root, rendered)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("HTML tour"), str(exc))
            self._status.setText(tr("HTML tour creation failed."))
            return
        self._result = index
        self._status.setText(tr("HTML tour complete.") +
                             ' <a href="#open">' + tr("Open result") + "</a>")

    @staticmethod
    def _write_tour_files(root: Path, rendered) -> Path:
        tour_data = []
        valid_ids = {camera["id"] for camera in rendered}
        for camera in rendered:
            export = pano.clean_export_settings(camera.get("export"),
                                                camera["heading"])
            hotspots = []
            for hotspot in camera.get("hotspots", []):
                item = dict(hotspot)
                if item["type"] == "navigation" and item["target"] not in valid_ids:
                    continue
                source = Path(item.get("image", ""))
                if item["type"] == "info" and source.is_file():
                    item["image"] = PanoramaPanel._file_data_url(source)
                elif item["type"] == "info":
                    item["image"] = ""
                hotspots.append(item)
            panorama = root / "panoramas" / (camera["id"] + ".jpg")
            tour_data.append({
                "id": camera["id"], "name": camera["name"],
                "image": PanoramaPanel._file_data_url(panorama),
                "north_heading": export["north_heading"],
                "start_heading": export["start_heading"],
                "seam_heading": export["seam_heading"],
                "hotspots": hotspots,
            })
        index = root / "index.html"
        index.write_text(pano.tour_html(tour_data), encoding="utf-8")
        return index

    @staticmethod
    def _file_data_url(path: Path) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _start_queue(self, jobs):
        self._remember()
        self._queue = list(jobs)
        self._result = None
        self._paused = False
        self._cancel_requested = False
        self._queue_started = time.monotonic()
        self._set_running(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        success = False
        try:
            total = len(self._queue)
            for index, (camera, destination) in enumerate(self._queue, 1):
                self._active_name = camera["name"]
                self._queue_progress((index - 1) / max(total, 1))
                self._native_export(camera, Path(destination), index, total)
                self._result = Path(destination)
            success = True
        except ExportCancelled:
            self._status.setText(tr("Export cancelled."))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("360 export"), str(exc))
            self._status.setText(tr("360 export failed."))
        finally:
            self._queue.clear()
            QApplication.restoreOverrideCursor()
            self._set_running(False)
        if not success:
            return False
        self._status.setText(tr("Export complete.") + ' <a href="#open">' +
                             tr("Open result") + "</a>")
        return True

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self._pause.setText(tr("Resume") if self._paused else tr("Pause"))

    def _cancel_export(self) -> None:
        self._cancel_requested = True
        self._paused = False

    def _check_queue_controls(self) -> None:
        QApplication.processEvents()
        while self._paused and not self._cancel_requested:
            self._status.setText(tr("Export paused."))
            QApplication.processEvents()
            time.sleep(0.05)
        if self._cancel_requested:
            raise ExportCancelled()

    def _queue_progress(self, value) -> None:
        self._check_queue_controls()
        value = max(0.0, min(1.0, float(value)))
        self._bar.setValue(int(value * 1000))
        elapsed = max(0.0, time.monotonic() - self._queue_started)
        eta = int(elapsed * (1.0 - value) / value) if value > 0.01 else 0
        eta_text = tr(" | about {seconds} s remaining", seconds=eta) if eta else ""
        self._status.setText(tr(
            "Rendering {name}: {percent}%{eta}", name=self._active_name,
            percent=int(value * 100), eta=eta_text))

    def _native_export(self, camera, destination: Path,
                       job_index: int, job_total: int) -> None:
        """Capture six native 2D views and assemble one true 360 image."""
        export = pano.clean_export_settings(camera.get("export"),
                                            camera["heading"])
        projection = export["projection"]
        width = export["width"]
        preset = pano.QUALITY_PRESETS[export["quality"]]
        face_size = (width if projection in pano.CUBE_PROJECTIONS else
                     max(256, width // 4))
        render_size = max(64, min(8192, int(round(
            face_size * preset["scale"]))))
        base = (job_index - 1) / max(job_total, 1)
        span = 1.0 / max(job_total, 1)
        faces = pano.render_cube_faces(
            self.app.viewport, camera, render_size, overlays=False,
            auto_level=True,
            progress=lambda value: self._queue_progress(
                base + span * value * 0.4))
        if render_size != face_size:
            faces = {name: image.scaled(
                face_size, face_size, Qt.IgnoreAspectRatio,
                Qt.SmoothTransformation) for name, image in faces.items()}
        self._queue_progress(base + span * 0.42)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fmt = export["format"]
        meta = pano.manifest(camera, projection, width, fmt)
        with tempfile.TemporaryDirectory(prefix="ingetrazo-360-") as folder:
            folder = Path(folder)
            suffix = pano.FORMATS[fmt]["suffix"]
            paths = {}
            for name, image in faces.items():
                path = folder / (name + suffix)
                if not image.save(str(path), None, preset["jpeg"]):
                    raise RuntimeError(tr("Could not save cube face {name}.",
                                          name=name))
                paths[name] = path
                self._check_queue_controls()
            if projection == "equirectangular":
                image = pano.cube_to_equirectangular(
                    faces, width, seam_blend=preset["seam"],
                    progress=lambda value: self._queue_progress(
                        base + span * (0.45 + value * 0.45)))
                image = pano.rotate_equirectangular(
                    image, camera["heading"], export["seam_heading"])
                image = pano.sharpen_image(image, preset["sharpen"])
                self._queue_progress(base + span * 0.94)
                if not image.save(str(destination), None, preset["jpeg"]):
                    raise RuntimeError(tr("Could not save the 360 image."))
                if fmt == "jpeg":
                    pano.add_gpano_xmp(destination, width, width // 2)
            elif projection == "cubemap_faces":
                with zipfile.ZipFile(destination, "w",
                                     zipfile.ZIP_DEFLATED) as archive:
                    for name, path in paths.items():
                        archive.write(path, "faces/" + path.name)
                    archive.writestr("manifest.json",
                                     json.dumps(meta, indent=2))
            else:
                self._compose_cube(paths, destination, projection, face_size)
        if self._metadata.isChecked() and projection != "cubemap_faces":
            destination.with_suffix(destination.suffix + ".json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8")
        self._queue_progress(base + span)

    @staticmethod
    def _compose_cube(faces, destination, projection, size):
        images = {name: QImage(str(path)) for name, path in faces.items()}
        bad = [name for name, image in images.items() if image.isNull()]
        if bad:
            raise RuntimeError("Could not read cube faces: " + ", ".join(bad))
        if projection == "cubemap_strip":
            canvas = QImage(size * 6, size, QImage.Format_RGBA8888)
            slots = [(name, i, 0) for i, name in enumerate(
                ("front", "right", "back", "left", "up", "down"))]
        else:
            canvas = QImage(size * 4, size * 3, QImage.Format_RGBA8888)
            slots = [("up", 1, 0), ("left", 0, 1), ("front", 1, 1),
                     ("right", 2, 1), ("back", 3, 1), ("down", 1, 2)]
        canvas.fill(Qt.transparent)
        painter = QPainter(canvas)
        for name, x, y in slots:
            painter.drawImage(x * size, y * size, images[name])
        painter.end()
        if not canvas.save(str(destination)):
            raise RuntimeError("Could not save " + str(destination))

    def _set_running(self, running):
        self._bar.setVisible(running)
        self._interactive.setVisible(not running)
        self._go.setVisible(not running)
        self._all.setVisible(not running)
        self._tour.setVisible(not running)
        self._pause.setVisible(running)
        self._cancel.setVisible(running)
        self._camera_panel.setEnabled(not running)
        self._hotspot_panel.setEnabled(not running)
        for widget in self._export_settings:
            widget.setEnabled(not running)
        if not running:
            self._pause.setText(tr("Pause"))
            self._projection_changed()

    def _open_result(self, _link=None):
        if self._result is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._result)))

    # ---- Selectable viewport camera icons --------------------------------
    def pick_camera(self, viewport, px, py):
        best, distance = None, 14.0
        for i, cam in enumerate(self.cameras()):
            p = viewport._world_to_pixel(QVector3D(*cam["pos"]))
            if p is None:
                continue
            d = math.hypot(p[0] - px, p[1] - py)
            if d <= distance:
                best, distance = i, d
        return best

    def camera_picked(self, index):
        self._view_pick = index
        if index is not None and 0 <= index < self._cameras.count():
            self._cameras.setCurrentRow(index)
        self.app.viewport.update()

    def delete_camera_at(self, index):
        cams = self.cameras()
        if 0 <= index < len(cams):
            if self._view_before_preview is not None:
                self._stop_preview()
            removed_id = cams[index]["id"]
            self._thumbnails.pop(removed_id, None)
            del cams[index]
            for camera in cams:
                camera["hotspots"] = [
                    spot for spot in camera.get("hotspots", [])
                    if not (spot["type"] == "navigation" and
                            spot["target"] == removed_id)]
            _store(self.app, cams)


def draw_cameras(panel, viewport, painter):
    if panel._view_before_preview is not None:
        return
    cams = panel.cameras()
    selected = panel.selected_index()
    painter.setRenderHint(QPainter.Antialiasing)
    font = QFont()
    font.setPointSize(8)
    painter.setFont(font)
    for i, cam in enumerate(cams):
        pos = QVector3D(*cam["pos"])
        p = viewport._world_to_pixel(pos)
        tip = viewport._world_to_pixel(pos + pano.direction(cam) * 0.8)
        if p is None:
            continue
        color = QColor(122, 74, 190)
        painter.setPen(QPen(color, 2.0))
        painter.setBrush(QColor(205, 184, 235, 210))
        centre = QPointF(p[0], p[1])
        painter.drawEllipse(centre, 7.0, 7.0)
        if tip is not None:
            painter.drawLine(QLineF(centre, QPointF(tip[0], tip[1])))
        if i == panel._view_pick:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(242, 115, 41), 3.0))
            painter.drawEllipse(centre, 15.0, 15.0)
        if i == selected:
            painter.setPen(QPen(QColor(45, 45, 45)))
            painter.drawText(QPointF(p[0] + 12, p[1] - 9), cam["name"])


def _plugin_icon() -> QIcon:
    """Load the supplied Archviz360 artwork in source and packaged layouts."""
    here = Path(__file__).resolve()
    for candidate in (here.with_name("icon.png"),
                      here.with_name("panorama_360_icon.png")):
        if candidate.is_file():
            icon = QIcon(str(candidate))
            if not icon.isNull():
                return icon
    return QIcon()


def _install_toolbar(app, dock) -> None:
    """Add an independently movable, dockable 360 Export logo button."""
    window = getattr(app, "window", None)
    if window is None or not hasattr(window, "addToolBar"):
        return

    object_name = "extension_panorama_360_toolbar"
    if window.findChild(QToolBar, object_name) is not None:
        return

    toolbar = None
    maker = getattr(window, "_new_toolbar", None)
    if callable(maker):
        try:
            toolbar = maker(tr("360 Export"), object_name)
        except Exception:
            toolbar = None
    if toolbar is None:
        toolbar = QToolBar(tr("360 Export"), window)
        toolbar.setObjectName(object_name)
        toolbar.setMovable(True)
        toolbar.setFloatable(True)
        toolbar.setAllowedAreas(Qt.AllToolBarAreas)
        reference = next((item for item in window.findChildren(QToolBar)
                          if item is not toolbar), None)
        toolbar.setIconSize(reference.iconSize() if reference else QSize(32, 32))
        toolbar.setToolButtonStyle(Qt.ToolButtonIconOnly)
        window.addToolBar(Qt.TopToolBarArea, toolbar)

    action = QAction(_plugin_icon(), tr("360 Export"), window)
    tip = tr("Open 360 Export cameras and panorama settings.")
    action.setToolTip(tip)
    action.setStatusTip(tip)
    action.triggered.connect(lambda _checked=False: app.show_panel(dock))
    toolbar.addAction(action)
    toolbar._panorama_360_refs = (action, dock)


def setup(app) -> None:
    panel = PanoramaPanel(app)
    dock = app.add_panel(tr("360 Export"), panel)
    menu_action = app.add_menu_action(
        tr("Export 360 images..."), lambda: app.show_panel(dock),
        tip=tr("Create panorama cameras and export immersive images."))
    if menu_action is not None:
        menu_action.setIcon(_plugin_icon())
    _install_toolbar(app, dock)
    app.on_document_changed(panel.refresh)
    app.add_overlay(lambda vp, painter: draw_cameras(panel, vp, painter))
    app.add_pickable(panel.pick_camera, on_select=panel.camera_picked,
                     delete=panel.delete_camera_at)
