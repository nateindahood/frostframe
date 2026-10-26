"""Copies of the fonts built into the screen firmware, used only to preview them in the GUI.

All four are from Google Fonts under the SIL Open Font License 1.1 (see the OFL-*.txt files).
They are registered for the frostframe process only (fontconfig "application fonts"), not
installed system-wide.
"""

import ctypes
import ctypes.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# device font id -> (font file, family name to use in Pango markup)
FILES = {
    "roboto-regular": ("Roboto-Variable.ttf", "Roboto"),
    "BitcountSingle-Regular": ("BitcountSingle-Variable.ttf", "Bitcount Single"),
    "Orbitron-Regular": ("Orbitron-Variable.ttf", "Orbitron"),
    "RubikGlitch-Regular": ("RubikGlitch-Regular.ttf", "Rubik Glitch"),
}

_loaded = False


def register():
    """Make the bundled fonts available to GTK in this process. Returns True on success.
    Must run before GTK creates its first text layout."""
    global _loaded
    if _loaded:
        return True
    lib = ctypes.util.find_library("fontconfig")
    if not lib:
        return False
    fc = ctypes.CDLL(lib)
    fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    fc.FcConfigAppFontAddFile.restype = ctypes.c_int
    ok = True
    for fname, _family in FILES.values():
        ok &= bool(fc.FcConfigAppFontAddFile(None, os.path.join(HERE, fname).encode()))
    _loaded = ok
    return ok


def family(font_id):
    return FILES.get(font_id, (None, "Sans"))[1]
