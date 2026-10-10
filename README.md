# IngeTrazo Native 360 Export

A native 360-degree panorama exporter for **IngeTrazo**. It uses
IngeTrazo's existing 2D image renderer—no Blender or other external renderer
is required.

The extension captures six 90-degree views around a persistent camera and
stitches them into a standard 2:1 equirectangular image suitable for panorama
viewers, websites, social platforms, and VR tours.

## Author
D.S (Archviz360)


## Features

- Persistent 360 cameras saved inside the `.igz` document
- Create a camera from the current view
- Place cameras on the model with snapping and configurable eye height
- Move, aim, rename, duplicate, delete, and look through cameras
- Export one camera or batch-export every camera
- Equirectangular 360° output at 2K–16K
- PNG and JPEG output
- GPano metadata in JPEG panoramas
- Automatic horizon leveling for straight, upright panoramas
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
    └── panorama_core.py
```

Restart IngeTrazo afterward.

## Usage

1. Open a model in IngeTrazo.
2. Select **Extensions → Export 360 images…**.
3. Choose **From view** to create a camera at the current viewpoint, or
   **Place…** to click a floor position in the model.
4. Use **Aim…** to choose the center direction of the panorama.
5. Select an equirectangular or cubemap projection and resolution.
6. Press **Export selected 360…**.

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
