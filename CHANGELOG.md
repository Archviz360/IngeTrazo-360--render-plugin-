# Changelog

## 3.1.0

- Added a visual hotspot-placement workflow inside the interactive panorama.
- Double-click empty space to create a navigation or information hotspot at
  the calculated compass heading and vertical tilt.
- Drag existing markers to reposition them and right-click to edit or delete.
- Added optional horizon snapping, marker-size controls, navigation testing,
  Previous/Next camera controls, and automatic return hotspots.
- Numerical heading and tilt remain available for precise adjustment.

## 3.0.1

- Fixed black standalone HTML tours opened directly from disk. Panorama and
  hotspot images are now embedded as data URLs, so WebGL no longer encounters
  the browser's `file://` cross-origin texture restriction.
- Added explicit panorama loading and WebGL error messages instead of leaving
  an unexplained black viewport.

## 3.0.0

- Added navigation and information hotspot editing. Hotspots support target
  cameras, labels, descriptions, images, links, compass headings, and tilt.
- Added standalone offline HTML/WebGL tour export with camera navigation,
  draggable viewing, zoom, compass bearings, and hotspot overlays.
- Added Draft, Standard, High, and Ultra quality presets with supersampling,
  smooth downsampling, cube-seam blending, sharpening, and tuned JPEG quality.
- Added selective batch export with checkboxes, drag or button reordering,
  progress percentage, estimated time, pause/resume, and cancellation.
- Added a clickable visual compass with per-camera north, initial-view, and
  panorama-seam controls. The selected seam is applied to exported panoramas.

## 2.4.0

- Added an interactive 360-degree preview: drag to look around and use the
  mouse wheel to zoom before exporting the full-size image.
- Added a camera thumbnail gallery with automatic captures and a manual
  refresh command.
- Added projection, resolution, format, and output-folder settings saved
  separately with every camera.
- Batch export now respects each camera's individual settings and destination.

## 2.3.0

- Added a live 90-degree preview from the selected 360 camera.
- Added a **Back to normal view** control that restores the exact viewport
  position, direction, lens, and projection used before previewing.
- Switching the selected saved camera updates the active preview directly.
- Camera markers are hidden while previewing for a clean image view.

## 2.2.0

- Added the supplied high-contrast red Archviz360 logo as the 360 Export
  button icon.
- Added an independent toolbar that can be moved, floated, or docked on any
  side of the IngeTrazo window.
- The toolbar button opens and focuses the 360 Export panel.

## 2.1.0

- Fixed corrupted degree signs, ellipses, multiplication signs, and dashes in
  the panel by using encoding-safe interface text.
- Removed obsolete controls that left stray or confusing labels.
- Simplified the panel to settings supported by the native renderer.
- Added automatic horizon leveling to every panorama capture.

## 1.0.0

- Native 360-degree export using IngeTrazo's built-in 2D renderer.
- Equirectangular 2:1 PNG and JPEG output.
- Cubemap cross, horizontal strip, and six-face ZIP output.
- Persistent cameras with snapped placement, aiming, duplication, and batch export.
- GPano metadata for JPEG panoramas.
