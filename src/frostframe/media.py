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


def convert(src, width, height, mode="fill", max_seconds=None, quality=23):
    """Return (device_file_name, file_bytes) ready for upload."""
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
        payload = open(out, "rb").read()
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
    """Count access units (AUD NAL units) in an Annex-B stream."""
    n = 0
    i = stream.find(b"\x00\x00\x01")
    while i >= 0:
        if i + 3 < len(stream) and stream[i + 3] & 0x1F == 9:
            n += 1
        i = stream.find(b"\x00\x00\x01", i + 3)
    return n


def read_header(data):
    """Parse the media header of a device file (for `frostframe inspect`)."""
    n = struct.unpack("<I", data[:4])[0]
    return wire.decode(P.MEDIA_HEADER, data[4:4 + n])
