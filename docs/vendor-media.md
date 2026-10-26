# Using the clips that came with your cooler

**Can frostframe include the manufacturer's animations?** No. The built-in clips, the videos
inside the manufacturer's app (KANALI) and the content in its online library belong to the
manufacturer, so frostframe can't ship them, and you shouldn't share copies either.

**Can you use them yourself?** Yes: they're already on your hardware and your computer, and you
can make your own copies for personal use with your own cooler. Here's how.

## 1. Built-in clips on the screen (best quality)

The screen comes with several clips (`default_01` … `default_06`, `screensaver`, `start`) in full
2240×1080. frostframe can read them back off the screen and save them into your library as
normal `.mp4` files:

```bash
frostframe pull --presets --library          # all built-in clips
frostframe pull default_03 --library         # just one
```

In the GUI: **Media → On your screen**, click the **save** icon next to a clip.

From the library you can trim them, edit them, combine them with your own clips, or send them
back to the screen at any time, even after the screen's storage has been reset.

## 2. Clips from the manufacturer's online library

The manufacturer's app downloads its online clips **encrypted**; only the screen decrypts them.
frostframe doesn't (and won't) decrypt that content itself. But once you've sent a clip to your
screen with the manufacturer's app, it's stored on the screen like any other file, and you can
save it the same way:

1. In the manufacturer's app, send the clip to your screen.
2. Quit the app (only one program can use the screen at a time).
3. `frostframe files` to find its name, then `frostframe pull NAME --library`
   (or the **save** icon in the GUI).

*Not yet confirmed on real hardware. Please report whether this works for you.*

## 3. Preview videos inside the manufacturer's app (lower quality)

The manufacturer's Windows app (KANALI) contains short preview videos of its clips (e.g. Cosmos,
Erebos, Festival, Robot arm) at **lower resolution** (544×360 to 1280×720). If you have the app
installed (for example with [tryx-kanali-linux](https://github.com/nateindahood/tryx-kanali-linux)),
you can copy them out of your own installation. They're inside `resources/app.asar`, a simple
archive. This snippet copies every video in it into a folder:

```bash
python3 - "$HOME/.wine-kanali/drive_c/Program Files/KANALI/resources/app.asar" ~/Videos/kanali-previews <<'EOF'
import json, os, struct, sys
src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
f = open(src, "rb")
head = f.read(16)
index = json.loads(f.read(struct.unpack("<IIII", head)[3]))
base = 8 + struct.unpack("<I", head[4:8])[0]
def walk(node):
    for name, entry in node.get("files", {}).items():
        if "files" in entry:
            walk(entry)
        elif name.lower().endswith((".mp4", ".webm", ".gif")) and "offset" in entry:
            f.seek(base + int(entry["offset"]))
            with open(os.path.join(out, name), "wb") as o:
                o.write(f.read(entry["size"]))
            print(name)
walk(index)
EOF
```

(Adjust the first path if your installation lives elsewhere; on Windows it's
`C:\Program Files\KANALI\resources\app.asar`.) Then add the ones you like to your library: drag
them onto the frostframe window, or `frostframe library add FILE...`.

Because these are small previews, they'll look softer on the 2240×1080 screen than method 1.

## 4. GIPHY

The manufacturer's app searches GIPHY. You can do the same in your browser at
[giphy.com](https://giphy.com), download the GIF, and drop it into your library.
