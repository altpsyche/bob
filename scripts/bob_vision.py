"""ONE-B2 — vision capability (screen capture + image prep), ported from the pwsh describe/screenshot
handlers onto the Python agent loop. One importable core shared by the `bob describe` / `bob screenshot`
CLI handlers (bob.cli) and, later (ONE-C), the agent tools. Cross-platform with no PowerShell and no
System.Drawing: Pillow for resize + Windows/macOS capture, and grim/spectacle/scrot/import for Linux
capture. Image encoding itself lives in bob_loop._image_content_block (DRY) — this module only produces
image file paths the loop consumes via run_agent(images=[...])."""
import os
import shutil
import subprocess
import sys
import tempfile

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
    inputs) — matching the pre-ONE-B2 Linux behavior. Never raises: image prep must not abort a describe."""
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


def capture_screen() -> str:
    """Capture the primary screen to a temp PNG and return its path. Windows/macOS via Pillow's
    ImageGrab; Linux via the first available of grim/spectacle/scrot/import. Raises RuntimeError with an
    actionable message when no capture backend is available or capture produced nothing."""
    out = _tmp_png()
    if sys.platform in ("win32", "darwin"):
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
