"""The local media library (a folder of GIFs/videos/images) and preview generation."""

import functools
import hashlib
import os
import shutil
import subprocess
import tempfile

from . import media

EXTENSIONS = (".gif", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".webm", ".mov", ".mkv")
STILLS = (".png", ".jpg", ".jpeg")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "frostframe", "previews")
PREVIEW_WIDTH = 360
PREVIEW_SECONDS = 8
CACHE_LIMIT = 100 * 1024 * 1024    # previews beyond this are pruned, oldest first


@functools.lru_cache(maxsize=None)
def default_dir():
    pictures = None
    try:
        r = subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True, timeout=3)
        pictures = r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        pass
    if not pictures or pictures == os.path.expanduser("~"):
        pictures = os.path.expanduser("~/Pictures")
    return os.path.join(pictures, "frostframe")


def library_dir(cfg):
    path = cfg.get("library_dir") or default_dir()
    os.makedirs(path, exist_ok=True)
    return path


def is_media(path):
    return path.lower().endswith(EXTENSIONS) and not os.path.basename(path).startswith(".")


def list_files(folder):
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names if is_media(n) and os.path.isfile(os.path.join(folder, n))]


def add(folder, src):
    """Copy a file into the library (never overwriting); returns the new path."""
    if not is_media(src):
        raise media.MediaError("%s isn't a supported image, GIF or video" % os.path.basename(src))
    stem, ext = os.path.splitext(os.path.basename(src))
    dest = os.path.join(folder, stem + ext)
    if os.path.abspath(src) == os.path.abspath(dest):
        return dest
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(folder, "%s (%d)%s" % (stem, n, ext))
        n += 1
    shutil.copy2(src, dest)
    return dest


def _gif_preview(cmd_input, out, data=None, h264=False):
    vf = ("fps=12,scale=%d:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];"
          "[b][p]paletteuse=dither=bayer" % PREVIEW_WIDTH)
    cmd = [media.ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y"]
    if h264:
        cmd += ["-f", "h264", "-r", "30"]
    fd, tmp = tempfile.mkstemp(suffix=".gif", dir=os.path.dirname(out))
    os.close(fd)
    try:
        cmd += ["-i", cmd_input, "-t", str(PREVIEW_SECONDS), "-vf", vf, "-loop", "0", tmp]
        r = media.run_ffmpeg(cmd, input=data, timeout=120)
        if r.returncode != 0 or not os.path.getsize(tmp):
            raise media.MediaError("couldn't make a preview: %s" % r.stderr.decode(errors="replace").strip()[-300:])
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return out


def prune_cache(limit=CACHE_LIMIT):
    """Delete the least recently used previews until the cache is under `limit` bytes."""
    try:
        entries = [os.path.join(CACHE_DIR, n) for n in os.listdir(CACHE_DIR)]
    except OSError:
        return
    stats = []
    for p in entries:
        try:
            st = os.stat(p)
        except OSError:
            continue
        stats.append((max(st.st_atime, st.st_mtime), st.st_size, p))
    total = sum(size for _t, size, _p in stats)
    for _t, size, p in sorted(stats):
        if total <= limit:
            break
        try:
            os.unlink(p)
            total -= size
        except OSError:
            pass


def preview_for_file(path):
    """Return a path to show for a local file: the image itself for stills, else a small cached GIF."""
    if path.lower().endswith(STILLS):
        return path
    st = os.stat(path)
    key = hashlib.sha1(("%s|%d|%d" % (os.path.abspath(path), st.st_size, int(st.st_mtime))).encode()).hexdigest()[:16]
    out = os.path.join(CACHE_DIR, "local-%s.gif" % key)
    if os.path.exists(out):
        os.utime(out)     # mark as recently used for prune_cache()
        return out
    os.makedirs(CACHE_DIR, exist_ok=True)
    return _gif_preview(path, out)


def safe_name(name):
    """A device-supplied file name made safe to use as a local file name: no path separators,
    no hidden files, nothing that looks like a command-line option."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name).lstrip(".-")
    return safe or "file"


def device_preview_path(name, size):
    return os.path.join(CACHE_DIR, "screen-%s-%d.gif" % (safe_name(name), size))


def preview_for_device_file(name, size, fetch):
    """fetch() returns the file's bytes from the screen; result cached by name and size."""
    out = device_preview_path(name, size)
    if os.path.exists(out):
        os.utime(out)
        return out
    os.makedirs(CACHE_DIR, exist_ok=True)
    return _gif_preview("pipe:0", out, data=fetch(), h264=True)
