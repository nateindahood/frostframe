"""Convert images/GIFs/videos into the screen's media format with ffmpeg.

On-device format: uint32 LE header length + media header + raw H.264 (Annex-B).
The stream mirrors what the device is known to play: H.264 Main profile, level 4.1,
yuv420p, every frame a keyframe with SPS/PPS repeated, access-unit delimiters, 30 fps.
"""

import os
import re
import shutil
import struct
import subprocess
import tempfile

from . import protocol as P
from . import wire

FPS = 30
TYPES = {".gif": "GIF", ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".mp4": "MP4"}
IMAGE_SECONDS = 2  # still images become a short looping clip
# Videos are cut to this length unless asked otherwise: every frame is a keyframe, so the
# device format takes about 3 MB per second, all of it held in memory while uploading.
DEFAULT_MAX_SECONDS = 60


class MediaError(Exception):
    pass


def ffmpeg_path():
    path = shutil.which("ffmpeg")
    if not path:
        raise MediaError("ffmpeg is required for converting media. Install it with your package "
                         "manager (e.g. `sudo apt install ffmpeg`).")
    return path


def device_name(src, width, height):
    """File name on the device: <stem>.<ext>.h264_<W>x<H> (lowercase ASCII, no spaces).

    Lowercase because an upload with capital letters in its name once disappeared from the
    device after a power cut while lowercase ones survived (under investigation)."""
    stem, ext = os.path.splitext(os.path.basename(src))
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")[:48] or "media"
    ext = (ext.lstrip(".").lower() or "bin")[:5]
    return ("%s.%s.h264_%dx%d" % (stem, ext, width, height)).lower()


def _video_filter(mode, width, height):
    if mode == "fill":   # scale to cover, crop the overflow
        return ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1"
                % (width, height, width, height))
    if mode == "fit":    # scale to fit, pad with black
        return ("scale=%d:%d:force_original_aspect_ratio=decrease,"
                "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1" % (width, height, width, height))
    if mode == "stretch":
        return "scale=%d:%d,setsar=1" % (width, height)
    raise MediaError("unknown fit mode %r (use fill, fit or stretch)" % mode)


def convert(src, width, height, mode="fill", max_seconds=DEFAULT_MAX_SECONDS, quality=23):
    """Return (device_file_name, file_bytes) ready for upload. max_seconds=0/None: no limit."""
    if not os.path.isfile(src):
        raise MediaError("file not found: %s" % src)
    ext = os.path.splitext(src)[1].lower()
    original = TYPES.get(ext, "MP4")
    still = ext in (".png", ".jpg", ".jpeg")

    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y"]
    if still:
        cmd += ["-loop", "1", "-t", str(IMAGE_SECONDS)]
    cmd += ["-i", src]
    if max_seconds and not still:
        cmd += ["-t", str(max_seconds)]
    vf = _video_filter(mode, width, height) + ",fps=%d,format=yuv420p" % FPS
    cmd += ["-an", "-vf", vf,
            "-c:v", "libx264", "-profile:v", "main", "-level", "4.1", "-pix_fmt", "yuv420p",
            "-g", "1", "-keyint_min", "1", "-bf", "0", "-crf", str(quality),
            "-x264-params", "aud=1:repeat-headers=1",
            "-f", "h264"]
    with tempfile.TemporaryDirectory(prefix="frostframe-") as tmp:
        out = os.path.join(tmp, "out.h264")
        r = subprocess.run(cmd + [out], capture_output=True, text=True)
        if r.returncode != 0:
            raise MediaError("ffmpeg failed: %s" % (r.stderr.strip()[-500:] or r.returncode))
        with open(out, "rb") as f:
            payload = f.read()
    frames = count_frames(payload)
    if frames == 0:
        raise MediaError("conversion produced no video frames")
    header = wire.encode(P.MEDIA_HEADER, {
        "magic": P.MEDIA_MAGIC,
        # The device's own files use exactly this description format.
        "description": "Tryx media header v1, fps=%d, size=%dx%d" % (FPS, width, height),
        "original_type": original,
        "payload_format": "H264Raw",
        "frame_rate": FPS,
        "width": width,
        "height": height,
        "frame_count": frames,
    })
    return device_name(src, width, height), struct.pack("<I", len(header)) + header + payload


def count_frames(stream):
    """Frames in an H.264 Annex-B stream: AUDs if present, else slices that start a picture."""
    aud = slices = 0
    i = stream.find(b"\x00\x00\x01")
    while i >= 0 and i + 4 < len(stream):
        t = stream[i + 3] & 0x1F
        if t == 9:
            aud += 1
        elif t in (1, 5) and stream[i + 4] & 0x80:   # first_mb_in_slice == 0
            slices += 1
        i = stream.find(b"\x00\x00\x01", i + 3)
    return aud or slices


def extract_mp4(device_file, out):
    """Turn a device media file (header + raw H.264) into a playable .mp4 (no re-encode)."""
    out = os.path.abspath(out)    # never let a name starting with "-" look like an option
    r = subprocess.run([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-f", "h264", "-r", str(FPS),
                        "-i", "pipe:0", "-c", "copy", out], input=device_file, capture_output=True)
    if r.returncode != 0:
        raise MediaError("ffmpeg failed: %s" % r.stderr.decode(errors="replace").strip()[-300:])
    return out


def read_header(data):
    """Parse the media header of a device file (for `frostframe inspect`)."""
    n = struct.unpack("<I", data[:4])[0]
    return wire.decode(P.MEDIA_HEADER, data[4:4 + n])
