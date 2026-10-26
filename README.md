# frostframe

**Control AIO cooler LCD screens natively on Linux.** Upload your own GIFs, videos and images,
pick what plays, show live CPU/GPU temperatures and usage on the screen, set brightness, and
change fan/pump settings. No Wine, no vendor app, no internet connection.

```console
$ frostframe upload ~/Pictures/cat.gif --play
Converting cat.gif to 2240x1080 (fill)...
  cat.gif.h264_2240x1080, 6.8 MB, 73 frames
  uploading 100% (6650/6650 KiB)
Uploaded as cat.gif.h264_2240x1080
Now playing.

$ frostframe labels set cpu-temp cpu-load gpu-temp --color cyan
$ frostframe monitor        # or: ./install.sh --service
```

Prefer clicking? `frostframe gui` (or **frostframe** in your app menu) opens a GTK window with
the same features, plus:

- **Your library**: a folder of GIFs, videos and images (`~/Pictures/frostframe` by default).
  Drag files onto the window, use *Add files…*, or copy them in with your file manager; then
  **Send to screen** in one click.
- **Preview**: select any file, in your library *or already on the screen* (built-in clips
  too), to see it animate in the preview box.
- Media on the screen with play / dual-screen left & right / delete, brightness, on-screen
  readings with any text color and all four built-in fonts, the background-service switch,
  and cooler settings.

> **Unofficial, independent project.** frostframe is not affiliated with, endorsed by or
> supported by the cooler's manufacturer. Product and company names mentioned here belong to
> their owners and are used only to describe compatibility. It talks to the device using the
> same messages the manufacturer's software sends; use it at your own risk.

## Compatible hardware

AIO coolers whose screen appears on USB as vendor **`391a`** with a USB *printer-class*
interface: for example the **TRYX Panorama** series (6.5" AMOLED, 2240×1080) on its current
(Linux-based) firmware. Check with:

```console
$ lsusb | grep 391a
Bus 001 Device 007: ID 391a:1011 RK PANO
```

**Tested:** Panorama 360 ARGB (`391a:1011`), screen firmware v2.0.6 / app v2.0.4.
Other models with the same vendor ID very likely work; reports welcome (include
`frostframe doctor` and `frostframe info` output).

**Old firmware:** early units ran an Android-based screen firmware (USB `18d1:2d03`,
"Android Open Accessory"). That firmware uses a different protocol and is not supported; it
needs a one-time update with the manufacturer's Windows app first. See the companion project
[tryx-kanali-linux](https://github.com/nateindahood/tryx-kanali-linux), which also has a guide for
doing that update from Linux via a Windows VM.

## Install

Requirements: Linux with udev, **Python 3.8+**, **ffmpeg** (for uploads). For the GUI:
GTK 3 for Python (`python3-gi` + `gir1.2-gtk-3.0`, preinstalled on most desktops). Optional:
`nvidia-smi` (comes with the NVIDIA driver) for NVIDIA GPU readings. AMD GPUs are read
through the `amdgpu` driver.

```bash
sudo apt install ffmpeg          # Fedora: sudo dnf install ffmpeg · Arch: sudo pacman -S ffmpeg
git clone https://github.com/nateindahood/frostframe.git
cd frostframe
./install.sh                     # installs to ~/.local, adds a udev rule (asks for sudo)
frostframe doctor
```

`./install.sh --service` also enables a background service that keeps the on-screen readings
live. `./install.sh --uninstall` removes everything except your settings.

frostframe has no Python dependencies. It can also be installed with `pipx install .`, but
you still need the udev rule from `packaging/`.

## Usage

| Command | What it does |
|---|---|
| `frostframe gui [--page media\|display\|cooler]` | Graphical interface |
| `frostframe info` | Model, firmware, serial, screen size |
| `frostframe files` | Media on the screen (▶ = playing) |
| `frostframe upload FILE [--play]` | Convert (ffmpeg) and upload a GIF, MP4, PNG or JPEG (path or library file name) |
| `frostframe library [add FILE... \| path]` | Your local media folder |
| `frostframe pull NAME [--video]` | Download a file from the screen (`--video`: as a playable .mp4) |
| `frostframe upload FILE --half --play --side right` | Half-width clip for dual-screen mode |
| `frostframe play NAME` | Play a file (full name or unique prefix, e.g. `default_03`) |
| `frostframe play --left A --right B` | Dual-screen mode, two clips side by side |
| `frostframe play NAME --loop all\|random` | Loop through your files |
| `frostframe rm NAME` | Delete one of your uploads |
| `frostframe brightness 0-100` · `frostframe screen on\|off` | Display |
| `frostframe labels set cpu-temp gpu-temp ...` | On-screen readings (1-4), see `labels list` |
| `frostframe labels clear` | Remove on-screen readings |
| `frostframe monitor` | Keep readings live (or use the service) |
| `frostframe fan fixed 60` · `frostframe fan smart` · `frostframe pump 80` | Cooler settings |
| `frostframe cooler` | Fan/pump settings and RPM |
| `frostframe dump` | All device settings as JSON |

Upload options: `--fit fill` (crop to fill, default), `--fit fit` (letterbox), `--fit stretch`,
`--seconds N` (trim long videos), `--quality N` (x264 CRF, default 23; lower is sharper and larger).

Label options: `--color white|cyan|#ff8800|...` (any RGB color), `--font roboto-regular|BitcountSingle-Regular|Orbitron-Regular|RubikGlitch-Regular`,
`--side left|right|full`, `--size 160`. Metrics: `cpu-temp cpu-load cpu-clock gpu-temp gpu-load
gpu-clock gpu-power ram net-down net-up time date`.

## Good to know

- **One program at a time.** Linux's printer driver lets only one program open the screen. Quit
  the vendor app (or Wine) before using frostframe. frostframe's own commands take turns, so you
  can run `frostframe play ...` while the monitor service is running.
- **Fan and pump settings only matter if the fans/pump are wired to the cooler.** If
  `frostframe cooler` shows 0 RPM, they're probably on motherboard headers; control them in the
  BIOS or with your motherboard's Linux tools instead.
- **Firmware updates** aren't supported. They use the manufacturer's tools.
- **Black screen?** The screen is set to play a file that's no longer on it. `frostframe files`
  (or the GUI) points this out; pick another file to play.
- **Never send the device's "Reboot" system command** (not exposed by frostframe): on current
  firmware it leaves the screen in its flashing mode until the PC's power supply is switched off.
- **Store content** from the manufacturer's online library (delivered encrypted) isn't supported.
  Your own images and videos are.

## How it works

The screen is a small Linux board that shows up as a USB printer-class device. Messages are
protobuf-encoded, framed with a 4-byte marker and a length, and sent over the printer's bulk
endpoints. Media is stored on the device as raw H.264 with a small header. The protocol is
documented in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Status

| Feature | Status |
|---|---|
| Device info, file list, settings dump | ✅ tested |
| Brightness, screen on/off | ✅ tested |
| Play / switch media, dual-screen mode | ✅ tested |
| On-screen live readings (labels + monitor) | ✅ tested |
| Upload (GIF/MP4/PNG/JPEG → H.264) | ✅ tested |
| GUI (GTK 3), library, previews | ✅ tested on Cinnamon |
| Download files from the screen | ✅ tested |
| Fan/pump settings | ⚠️ accepted by the device; effect untested (fans on motherboard on the test system) |
| RGB light settings, live frame streaming | 🔜 in the protocol, not implemented yet |

## Contributing

Bug reports and device reports are very welcome. `python3 -m unittest discover -s tests` runs
the offline tests.

## License

MIT, see [LICENSE](LICENSE).
