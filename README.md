<img width="45" height="45" alt ="IngeTrazo_360_AT_icon_cutout" src="https://github.com/user-attachments/assets/75f05633-f2ce-499a-a79f-6167ac87dcd8" />

<img width="2240" height="1120" alt="360-Camera-1-create-a-logo-for-360-render-plugin-there-shall-be" src="https://github.com/user-attachments/assets/773c7f63-3a79-4773-a386-6cf3e75bfebd" />



# IngeTrazo Native 360 Export

A native 360-degree panorama exporter for **IngeTrazo**. It uses
IngeTrazo's existing 2D image renderer

The extension captures six 90-degree views around a persistent camera and
stitches them into a standard 2:1 equirectangular image suitable for panorama
viewers, websites, social platforms, and VR tours.

## Author
D.S (Archviz360)


## Features

- Persistent 360 cameras saved inside the `.igz` document
- Create a camera from the current view
- Place cameras on the model with snapping and configurable eye height
- Move, aim, rename, duplicate, and delete cameras
- Preview the selected camera through its 90° capture lens
- Return from camera preview to the exact previous perspective view
- Inspect a draggable 360° panorama before committing to a full export
- Browse saved cameras in a visual thumbnail gallery
- Save projection, resolution, format, and output folder per camera
- Build navigation and information hotspots between panorama cameras
- Export a dependency-free standalone HTML/WebGL virtual tour
- Choose Draft, Standard, High, or Ultra image-quality processing
- Select and reorder batch cameras, then pause, resume, or cancel export
- Visually set north, the initial tour direction, and the panorama seam
- Export one camera or batch-export every camera
- Equirectangular 360° output at 2K–16K
- PNG and JPEG output
- GPano metadata in JPEG panoramas
- Automatic horizon leveling for straight, upright panoramas
- Movable and floatable toolbar button using the Archviz360 logo
- Cubemap horizontal strip and 4×3 cross layouts
- Six-face cubemap ZIP export
- Restores the original viewport after capturing the panorama
- Uses the current IngeTrazo display style, materials, shadows, and background

## Compatibility

- IngeTrazo **0.5.7**
- Extension API **2**
- Windows package tested
- NumPy, Qt, and the image renderer bundled with IngeTrazo are used

## Installation

### Automatic installation on Windows

Right-click `install.ps1`, choose **Run with PowerShell**, and then restart
IngeTrazo.

If Windows blocks the script, install manually instead.

### Manual installation

Copy the complete `panorama_360` directory into:

```text
%APPDATA%\ingetrazo\plugins\panorama_360
```

The installed structure must be:

```text
plugins/
└── panorama_360/
    ├── __init__.py
    ├── panorama_core.py
    └── icon.png
```

Restart IngeTrazo afterward.

## Usage

1. Open a model in IngeTrazo.
2. Click the **360 Export** logo button, or select
   **Extensions → Export 360 images…**. Drag the button's toolbar handle to
   dock it on any window edge or leave it floating.
3. Choose **From view** to create a camera at the current viewpoint, or
   **Place…** to click a floor position in the model.
4. Use **Aim…** to choose the center direction of the panorama.
5. Press **View camera preview** to inspect the selected camera. Press
   **Back to normal view** to restore your previous viewport perspective.
6. Choose that camera's projection, resolution, format, and output folder.
7. Press **Interactive 360 preview…**, then drag to look around and use the
   mouse wheel to zoom. Choose **Export full image…** when satisfied.
8. You can also press **Export selected 360…** to export without previewing.

Use **Refresh camera thumbnails** after changing the model to update every
image in the camera gallery. **Selective batch export…** uses each camera's
own format, projection, resolution, quality, and destination folder.

### Virtual tours

Use **+ Navigation** to connect the selected camera to another camera. Use
**+ Information** to add text, an image, and an optional web link. Set each
hotspot's compass heading and vertical tilt, then choose **Export standalone
HTML tour…**. Panorama and hotspot images are embedded in the generated
`index.html`, so it opens directly from disk without a local server or an
external JavaScript library.

For easier placement, choose **Place hotspots visually…**:

1. Drag the panorama to look around and use the wheel to zoom.
2. Double-click the desired location and choose Navigation or Information.
3. Drag a marker to reposition it; its heading and tilt are calculated live.
4. Right-click a marker to edit or delete it.
5. Enable **Test navigation** to follow arrows between cameras.

Navigation arrows can snap to the horizon and optionally create a return
hotspot in the destination camera. Previous/Next camera buttons let you edit
the complete tour without closing the visual editor. Marker size and exact
heading/tilt remain editable numerically.

### Quality and batch control

Choose **Draft**, **Standard**, **High**, or **Ultra** for each camera. High
and Ultra render larger source faces and downsample them smoothly; the final
panorama also receives seam blending and sharpening. **Selective batch
export…** lets you check and reorder cameras. During processing, use
**Pause**, **Resume**, or **Cancel** and monitor the estimated remaining time.

### Compass and seam

In **Compass and panorama seam**, select the marker edited by a compass click:
North, Initial view, or Panorama seam. The blue, green, red, and black lines
show north, initial view, seam, and camera heading respectively.

Equirectangular exports are standard 2:1 images—for example, 4096×2048.
The horizon is leveled automatically during export, even if the saved camera
was looking upward or downward.

## How it works

The extension points IngeTrazo's camera in six directions: front, right,
back, left, up, and down. Each direction is captured through the same native
rendering path used by **Export 2D Graphic**. The six images are then sampled
and combined into a seamless spherical projection.

## License

GNU General Public License v3.0 or later. See [LICENSE](LICENSE).

## example

<img width="2048" height="1024" alt="360-Camera-4" src="https://github.com/user-attachments/assets/dfa9c7d0-cfc2-453d-a3c3-1af809dde6d7" />


