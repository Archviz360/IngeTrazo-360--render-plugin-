# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Marco Sumari Tellez and IngeTrazo contributors.
"""360Â° image export â€” persistent cameras and native panoramic rendering.

The panel creates any number of cameras in the model, places/aims them with
snapping, previews their direction in the viewport and exports a selected
camera or a whole batch. Six 90Â° views from IngeTrazo's own 2D exporter are
stitched into a true equirectangular panorama; no external renderer is used.
"""
from __future__ import annotations

import json
import math
import re
import tempfile
import uuid
import zipfile
from pathlib import Path

from PySide6.QtCore import QLineF, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QImage, QPainter, QPen, QVector3D
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from . import panorama_core as pano
from core.i18n import tr
from views.fold_section import FoldSection, narrow, wrapping_form


_SETTINGS = "panorama360/"
_PROJECTION_LABELS = (
    ("equirectangular", "Equirectangular 360Â° (2:1)"),
    ("cubemap_faces", "Cube map â€” six-face ZIP"),
    ("cubemap_strip", "Cube map â€” horizontal strip"),
    ("cubemap_cross", "Cube map â€” 4 Ã— 3 cross"),
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


class PanoramaPanel(QWidget):
    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self._found = None
        self._queue = []
        self._result = None
        self._cube_face = 0
        self._cancelled = False
        self._view_pick = None
        self._picking = None
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
        box = QVBoxLayout(sec.body)
        self._cameras = QListWidget()
        self._cameras.setMinimumHeight(82)
        self._cameras.currentRowChanged.connect(self._selected)
        box.addWidget(self._cameras)
        row = QHBoxLayout()
        add = QPushButton(tr("+ From view"))
        add.setToolTip(tr("Create a 360 camera at the current viewport eye"))
        add.clicked.connect(self._add_from_view)
        place = QPushButton(tr("+ Placeâ€¦"))
        place.setToolTip(tr("Click a floor point; eye height is added"))
        place.clicked.connect(lambda: self._begin_pick("new"))
        row.addWidget(add)
        row.addWidget(place)
        box.addLayout(row)

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
        self._heading.setSuffix(" Â°")
        self._heading.setToolTip(tr("0Â° north, 90Â° east"))
        self._heading.editingFinished.connect(self._edited)
        form.addRow(tr("Heading:"), self._heading)
        self._tilt = QDoubleSpinBox()
        self._tilt.setRange(-90.0, 90.0)
        self._tilt.setDecimals(1)
        self._tilt.setSuffix(" Â°")
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
        move = QPushButton(tr("Moveâ€¦"))
        move.clicked.connect(lambda: self._begin_pick("move"))
        aim = QPushButton(tr("Aimâ€¦"))
        aim.clicked.connect(lambda: self._begin_pick("aim"))
        look = QPushButton(tr("Look through"))
        look.clicked.connect(self._look_through)
        row.addWidget(move)
        row.addWidget(aim)
        row.addWidget(look)
        form.addRow(row)
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

        # Projection and image settings are user preferences, not model data.
        sec = FoldSection(tr("Projection and image"), _SETTINGS + "open_image")
        form = wrapping_form(sec.body)
        self._projection = QComboBox()
        for key, label in _PROJECTION_LABELS:
            self._projection.addItem(tr(label), key)
        self._set_combo(self._projection, settings.value(
            _SETTINGS + "projection", "equirectangular"))
        self._projection.currentIndexChanged.connect(self._projection_changed)
        form.addRow(tr("Projection:"), self._projection)
        self._resolution = QComboBox()
        for label, px in (("2K", 2048), ("4K", 4096), ("6K", 6144),
                          ("8K", 8192), ("12K", 12288), ("16K", 16384)):
            self._resolution.addItem(label, px)
        wanted = int(settings.value(_SETTINGS + "width", 4096))
        self._resolution.setCurrentIndex(max(0, self._resolution.findData(wanted)))
        self._resolution.currentIndexChanged.connect(self._update_size_label)
        self._size = QLabel()
        rrow = QHBoxLayout()
        rrow.addWidget(self._resolution, 1)
        rrow.addWidget(self._size)
        form.addRow(tr("Resolution:"), rrow)
        self._engine = QComboBox()
        self._engine.addItem(tr("IngeTrazo native renderer"), "native")
        self._engine.setEnabled(False)
        form.addRow(tr("Engine:"), self._engine)
        self._quality = QComboBox()
        self._quality.addItem(tr("Viewport quality"))
        self._quality.setEnabled(False)
        form.addRow(tr("Quality:"), self._quality)
        self._format = QComboBox()
        for label, key in (("PNG", "png"), ("JPEG", "jpeg")):
            self._format.addItem(label, key)
        self._set_combo(self._format, settings.value(_SETTINGS + "format", "png"))
        self._format.currentIndexChanged.connect(self._format_changed)
        form.addRow(tr("Format:"), self._format)
        self._depth = QComboBox()
        form.addRow(tr("Color depth:"), self._depth)
        self._transparent = QCheckBox(tr("Transparent background"))
        self._transparent.setChecked(False)
        self._transparent.setEnabled(False)
        form.addRow(self._transparent)
        self._ground = QCheckBox(tr("Ground that catches shadows"))
        self._ground.setChecked(str(settings.value(_SETTINGS + "ground", "1")) != "0")
        self._ground.setVisible(False)
        form.addRow(self._ground)
        self._metadata = QCheckBox(tr("Write metadata sidecar"))
        self._metadata.setChecked(str(settings.value(_SETTINGS + "metadata", "1")) != "0")
        form.addRow(self._metadata)
        self._blend = QCheckBox(tr("Compatibility setting"))
        self._blend.setChecked(False)
        self._blend.setVisible(False)
        form.addRow(self._blend)
        lay.addWidget(sec)

        sec = FoldSection(tr("VR and fisheye"), _SETTINGS + "open_advanced")
        form = wrapping_form(sec.body)
        self._fov = QDoubleSpinBox()
        self._fov.setRange(1.0, 360.0)
        self._fov.setValue(float(settings.value(_SETTINGS + "fisheye_fov", 180.0)))
        self._fov.setSuffix(" Â°")
        form.addRow(tr("Fisheye field of view:"), self._fov)
        self._lens = QDoubleSpinBox()
        self._lens.setRange(0.1, 1000.0)
        self._lens.setDecimals(1)
        self._lens.setValue(float(settings.value(_SETTINGS + "fisheye_lens", 10.5)))
        self._lens.setSuffix(" mm")
        form.addRow(tr("Fisheye lens:"), self._lens)
        self._eyes = QDoubleSpinBox()
        self._eyes.setRange(0.0, 1.0)
        self._eyes.setDecimals(3)
        self._eyes.setSingleStep(0.005)
        self._eyes.setValue(float(settings.value(_SETTINGS + "eye_separation", 0.065)))
        self._eyes.setSuffix(" m")
        form.addRow(tr("Stereo eye separation:"), self._eyes)
        self._pole = QCheckBox(tr("Stereo pole merge"))
        self._pole.setChecked(str(settings.value(_SETTINGS + "pole_merge", "1")) != "0")
        form.addRow(self._pole)
        self._exposure = QDoubleSpinBox()
        self._exposure.setRange(-10.0, 10.0)
        self._exposure.setDecimals(1)
        self._exposure.setSingleStep(0.5)
        self._exposure.setValue(float(settings.value(_SETTINGS + "exposure", -0.6)))
        self._exposure.setSuffix(" EV")
        form.addRow(tr("Exposure:"), self._exposure)
        lay.addWidget(sec)
        sec.setVisible(False)

        self._export_settings = (
            self._projection, self._resolution, self._engine, self._quality,
            self._format, self._depth, self._transparent, self._ground,
            self._metadata, self._blend, self._fov, self._lens, self._eyes,
            self._pole, self._exposure,
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
        self._go = QPushButton(tr("Export selected 360â€¦"))
        self._go.clicked.connect(self._export_selected)
        self._all = QPushButton(tr("Export all camerasâ€¦"))
        self._all.clicked.connect(self._export_all)
        self._cancel = QPushButton(tr("Cancel export"))
        self._cancel.clicked.connect(self._cancel_export)
        self._cancel.setVisible(False)
        box.addWidget(self._go)
        box.addWidget(self._all)
        box.addWidget(self._cancel)
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
        narrow(self._name, self._projection, self._resolution, self._engine,
               self._quality, self._format, self._go, self._all, self._status)
        self._format_changed()
        self._projection_changed()
        self._update_renderer_label()

    @staticmethod
    def _set_combo(combo, data) -> None:
        i = combo.findData(data)
        combo.setCurrentIndex(max(0, i))

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
        self._cameras.blockSignals(True)
        self._cameras.clear()
        for cam in cams:
            self._cameras.addItem(cam["name"])
        row = next((i for i, c in enumerate(cams) if c["id"] == chosen_id), 0)
        self._cameras.setCurrentRow(row if cams else -1)
        self._cameras.blockSignals(False)
        self._selected(self._cameras.currentRow())
        self._view_pick = None
        self.app.release_pick()
        self.app.viewport.update()

    def _selected(self, row) -> None:
        cams = self.cameras()
        on = 0 <= row < len(cams)
        self._editor.setEnabled(on)
        if on:
            cam = cams[row]
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
        self.app.viewport.update()

    def _view_values(self):
        cam = self.app.viewport.camera
        eye, d = cam.eye(), cam.forward()
        heading = math.degrees(math.atan2(d.x(), d.y())) % 360.0
        tilt = math.degrees(math.asin(max(-1.0, min(1.0, d.z()))))
        return [eye.x(), eye.y(), eye.z()], heading, tilt

    def _new_camera(self, pos, heading, tilt, name=None):
        cams = self.cameras()
        cams.append({"id": uuid.uuid4().hex, "name": name or tr(
            "360 Camera {n}", n=len(cams) + 1), "pos": list(pos),
            "heading": heading, "tilt": tilt})
        _store(self.app, cams)
        self._cameras.setCurrentRow(len(cams) - 1)

    def _add_from_view(self):
        self._new_camera(*self._view_values())

    def _update_from_view(self):
        i = self.selected_index()
        if i is None:
            return
        cams = self.cameras()
        cams[i]["pos"], cams[i]["heading"], cams[i]["tilt"] = self._view_values()
        _store(self.app, cams)

    def _duplicate(self):
        cam = self.selected_camera()
        if cam is None:
            return
        pos = list(cam["pos"])
        pos[0] += 0.25
        self._new_camera(pos, cam["heading"], cam["tilt"],
                         cam["name"] + " " + tr("copy"))

    def _delete(self):
        i = self.selected_index()
        if i is None:
            return
        cams = self.cameras()
        del cams[i]
        _store(self.app, cams)

    def _edited(self):
        i = self.selected_index()
        cams = self.cameras()
        if i is None or i >= len(cams):
            return
        cam = cams[i]
        cam.update(name=self._name.text().strip() or cam["name"],
                   pos=[s.value() for s in self._xyz],
                   heading=self._heading.value(), tilt=self._tilt.value())
        _store(self.app, cams)

    def _look_through(self):
        cam = self.selected_camera()
        if cam is None:
            return
        view = self.app.viewport.camera
        view.look_from(QVector3D(*cam["pos"]), pano.direction(cam))
        view.perspective = True
        view.two_point = False
        self.app.viewport.update()

    def _begin_pick(self, purpose):
        if purpose != "new" and self.selected_camera() is None:
            return
        self._picking = purpose
        prompts = {"new": tr("Click the floor under the 360 camera"),
                   "move": tr("Click the camera's new floor point"),
                   "aim": tr("Click where the camera looks")}
        self.app.viewport.set_active_tool(_pick_tool(
            prompts[purpose], self._picked, self._end_pick))
        self.app.window.statusBar().showMessage(prompts[purpose] + " â€” " + tr("Esc cancels"))
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
        self._engine.setEnabled(False)
        self._quality.setEnabled(False)
        self._update_size_label()
        self._format_changed()

    def _update_size_label(self):
        key = self._projection.currentData()
        width = int(self._resolution.currentData() or 4096)
        w, h = pano.dimensions(key, width)
        suffix = tr("final") if key not in pano.CUBE_PROJECTIONS else tr("face / final")
        self._size.setText(f"{w} Ã— {h} px {suffix}")

    def _format_changed(self):
        key = self._format.currentData() or "png"
        old = self._depth.currentData()
        self._depth.clear()
        for depth in pano.FORMATS[key]["depths"]:
            self._depth.addItem(f"{depth}-bit", depth)
        i = self._depth.findData(old)
        self._depth.setCurrentIndex(max(0, i))
        self._transparent.setEnabled(False)
        packed = self._projection.currentData() in ("cubemap_strip", "cubemap_cross")
        if packed and key in ("exr", "hdr"):
            self._format.setCurrentIndex(self._format.findData("png"))

    def _remember(self):
        s = QSettings()
        values = {
            "projection": self._projection.currentData(),
            "width": self._resolution.currentData(), "engine": self._engine.currentData(),
            "quality": self._quality.currentIndex(), "format": self._format.currentData(),
            "transparent": int(self._transparent.isChecked()),
            "ground": int(self._ground.isChecked()), "metadata": int(self._metadata.isChecked()),
            "blend": int(self._blend.isChecked()), "fisheye_fov": self._fov.value(),
            "fisheye_lens": self._lens.value(), "eye_separation": self._eyes.value(),
            "pole_merge": int(self._pole.isChecked()), "exposure": self._exposure.value(),
            "eye_height": self._eye_height.value(),
        }
        for key, value in values.items():
            s.setValue(_SETTINGS + key, value)

    def _update_renderer_label(self):
        self._where.setText(tr("Native 360 export is ready."))

    # ---- Export -----------------------------------------------------------
    def _destination_suffix(self):
        if self._projection.currentData() == "cubemap_faces":
            return ".zip"
        return pano.FORMATS[self._format.currentData()]["suffix"]

    def _export_selected(self):
        cam = self.selected_camera()
        if cam is None:
            QMessageBox.information(self, tr("360 export"), tr("Create or select a camera first."))
            return
        suffix = self._destination_suffix()
        doc = getattr(self.app.window, "_current_path", None)
        base = Path(doc).parent if doc else Path.home()
        start = base / (_safe_name(cam["name"]) + suffix)
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Export 360 image"), str(start),
            tr("360 output (*{suffix});;All files (*)", suffix=suffix))
        if path:
            path = Path(path)
            if not path.suffix:
                path = path.with_suffix(suffix)
            self._start_queue([(cam, path)])

    def _export_all(self):
        cams = self.cameras()
        if not cams:
            QMessageBox.information(self, tr("360 export"), tr("Create a camera first."))
            return
        folder = QFileDialog.getExistingDirectory(self, tr("Export all 360 cameras"))
        if not folder:
            return
        suffix = self._destination_suffix()
        used = set()
        jobs = []
        for cam in cams:
            stem, n = _safe_name(cam["name"]), 2
            unique = stem
            while unique.casefold() in used:
                unique, n = f"{stem}-{n}", n + 1
            used.add(unique.casefold())
            jobs.append((cam, Path(folder) / (unique + suffix)))
        self._start_queue(jobs)

    def _start_queue(self, jobs):
        self._remember()
        self._queue = list(jobs)
        self._result = None
        self._cancelled = False
        self._set_running(True)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            total = len(self._queue)
            for index, (camera, destination) in enumerate(self._queue, 1):
                self._status.setText(tr(
                    "Rendering {name} with IngeTrazoâ€¦", name=camera["name"]))
                QApplication.processEvents()
                self._native_export(camera, Path(destination), index, total)
                self._result = Path(destination)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("360 export"), str(exc))
            self._status.setText(tr("360 export failed."))
            return
        finally:
            self._queue.clear()
            QApplication.restoreOverrideCursor()
            self._set_running(False)
        self._status.setText(tr("Export complete.") + ' <a href="#open">' +
                             tr("Open result") + "</a>")

    def _native_export(self, camera, destination: Path,
                       job_index: int, job_total: int) -> None:
        """Capture six native 2D views and assemble one true 360 image."""
        projection = self._projection.currentData()
        width = int(self._resolution.currentData())
        face_size = (width if projection in pano.CUBE_PROJECTIONS
                     else max(256, width // 4))
        base = (job_index - 1) / max(job_total, 1)
        span = 1.0 / max(job_total, 1)
        faces = pano.render_cube_faces(self.app.viewport, camera, face_size,
                                       overlays=False)
        self._bar.setValue(int((base + span * 0.35) * 1000))
        destination.parent.mkdir(parents=True, exist_ok=True)
        fmt = self._format.currentData()
        meta = pano.manifest(camera, projection, width, fmt)
        with tempfile.TemporaryDirectory(prefix="ingetrazo-360-") as folder:
            folder = Path(folder)
            suffix = pano.FORMATS[fmt]["suffix"]
            paths = {}
            for name, image in faces.items():
                path = folder / (name + suffix)
                if not image.save(str(path), None, 95):
                    raise RuntimeError(tr("Could not save cube face {name}.",
                                          name=name))
                paths[name] = path
            if projection == "equirectangular":
                image = pano.cube_to_equirectangular(
                    faces, width, progress=lambda value: self._bar.setValue(
                        int((base + span * (0.35 + value * 0.65)) * 1000)))
                if not image.save(str(destination), None, 95):
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
        self._bar.setValue(int((base + span) * 1000))

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
        self._cancel.setVisible(False)
        self._go.setVisible(not running)
        self._all.setVisible(not running)
        for widget in self._export_settings:
            widget.setEnabled(not running)
        if not running:
            self._projection_changed()

    def _cancel_export(self):
        self._cancelled = True
        self._queue.clear()

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
            del cams[index]
            _store(self.app, cams)


def draw_cameras(panel, viewport, painter):
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


def setup(app) -> None:
    panel = PanoramaPanel(app)
    dock = app.add_panel(tr("360 Export"), panel)
    app.add_menu_action(tr("Export 360 imagesâ€¦"), lambda: app.show_panel(dock),
                        tip=tr("Create panorama cameras and export immersive images."))
    app.on_document_changed(panel.refresh)
    app.add_overlay(lambda vp, painter: draw_cameras(panel, vp, painter))
    app.add_pickable(panel.pick_camera, on_select=panel.camera_picked,
                     delete=panel.delete_camera_at)

