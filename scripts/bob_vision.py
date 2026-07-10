"""Vision capability (screen capture + image prep) on the Python agent loop.
One importable core shared by the `bob describe` / `bob screenshot`
CLI handlers (bob.cli) and the agent tools. Cross-platform:
Pillow for resize + Windows/macOS capture, and grim/spectacle/scrot/import for Linux
capture. Image encoding itself lives in bob_loop._image_content_block (DRY) — this module only produces
image file paths the loop consumes via run_agent(images=[...])."""
import os
import shutil
import subprocess
import tempfile

import osenv   # OS decisions go through the one seam (so BOB_FORCE_OS drives them in tests)

# Linux capture backends, in preference order (Wayland first, then X11 tools). Each maps a tool name
# to the argv that writes a PNG to `out`.
_LINUX_CAPTURE = {
    "grim":      lambda out: ["grim", out],
    "spectacle": lambda out: ["spectacle", "-b", "-n", "-o", out],
    "scrot":     lambda out: ["scrot", out],
    "import":    lambda out: ["import", "-window", "root", out],
}


def _tmp_png() -> str:
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    return path


def resize_image(path: str, max_dim: int = 1024) -> str:
    """Downscale to `max_dim` on the longest edge (large screenshots blow the vision context window).
    Returns a NEW temp path when it resized, else the ORIGINAL path unchanged; never upscales. Uses
    Pillow when installed and sends the image as-is when it is not (the vision model tolerates larger
    inputs) — matching the prior Linux behavior. Never raises: image prep must not abort a describe."""
    try:
        from PIL import Image
    except ImportError:
        return path
    try:
        with Image.open(path) as im:
            w, h = im.size
            scale = min(max_dim / w, max_dim / h, 1.0)
            if scale >= 1.0:
                return path
            resized = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            out = _tmp_png()
            resized.save(out, "PNG")
            return out
    except Exception:
        return path


def scale_factor(w: int, h: int, max_long_edge: int = 1280, max_pixels: int = 1_150_000) -> float:
    """A single uniform downscale factor (<= 1.0) to fit a screenshot within the model's long-edge and
    megapixel limits, matching Anthropic's reference formula. Never upscales. The SAME factor maps both
    axes, so aspect ratio is preserved and coordinates map back with one number."""
    if w <= 0 or h <= 0:
        return 1.0
    long_edge = max(w, h)
    edge_scale = max_long_edge / long_edge
    px_scale = (max_pixels / (w * h)) ** 0.5
    return min(1.0, edge_scale, px_scale)


def to_screen(model_xy, scale: float):
    """Map a coordinate the model gave (in the resized-image space it was shown) back to REAL screen
    pixels: screen = model / scale. This is the fix for clicks landing in the wrong place when the
    screenshot was downscaled before the model saw it."""
    x, y = model_xy
    return (int(round(x / scale)), int(round(y / scale)))


def to_model(screen_xy, scale: float):
    """Map a real screen coordinate into the resized-image space the model sees: model = screen * scale."""
    x, y = screen_xy
    return (int(round(x * scale)), int(round(y * scale)))


def resize_for_control(path: str, max_long_edge: int = 1280, max_pixels: int = 1_150_000):
    """Downscale a screenshot for computer-use and return (out_path, scale, (width, height)) where the
    dims are the ACTUAL sent image size (so the tool reports them to the model verbatim) and scale is the
    factor to map the model's click coordinates back to screen pixels. Falls back to (path, 1.0, size)
    when Pillow is absent or on any error; never raises (image prep must not abort an action)."""
    try:
        from PIL import Image
    except ImportError:
        return path, 1.0, None
    try:
        with Image.open(path) as im:
            w, h = im.size
            scale = scale_factor(w, h, max_long_edge, max_pixels)
            if scale >= 1.0:
                return path, 1.0, (w, h)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            out = _tmp_png()
            im.resize((nw, nh), Image.LANCZOS).save(out, "PNG")
            return out, scale, (nw, nh)
    except Exception:
        return path, 1.0, None


def capture_screen() -> str:
    """Capture the primary screen to a temp PNG and return its path. Windows/macOS via Pillow's
    ImageGrab; Linux via the first available of grim/spectacle/scrot/import. Raises RuntimeError with an
    actionable message when no capture backend is available or capture produced nothing."""
    out = _tmp_png()
    if osenv.os_name() in ("windows", "macos"):
        try:
            from PIL import ImageGrab
        except ImportError as e:
            raise RuntimeError("screen capture on this OS needs Pillow (pip install pillow)") from e
        ImageGrab.grab().save(out, "PNG")
    else:
        tool = next((t for t in _LINUX_CAPTURE if shutil.which(t)), None)
        if not tool:
            raise RuntimeError("no screenshot tool found — install grim, spectacle, scrot, or "
                               "imagemagick, or pass an image to `bob describe`")
        subprocess.run(_LINUX_CAPTURE[tool](out), check=True)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError("screenshot capture produced no image")
    return out
