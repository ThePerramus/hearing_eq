# PyInstaller spec — single-file build of the whole Hearing EQ toolkit.
#
# Build:   pyinstaller HearingEQ.spec
# Result:  dist/HearingEQ.exe   (one file; GUI + audiogram + processor + export)
#
# The .exe re-invokes itself with `--run <mode>` (see hearing_eq_gui.py), so the
# three tools ship inside this one executable. ffmpeg.exe must still be on PATH
# for decoding MP3 / M4A (pydub uses it at runtime).

from PyInstaller.utils.hooks import (
    collect_dynamic_libs, collect_data_files, collect_submodules,
)

# Native libraries that travel inside their packages (libsndfile, PortAudio)
binaries  = collect_dynamic_libs("soundfile") + collect_dynamic_libs("sounddevice")
datas     = collect_data_files("soundfile")  + collect_data_files("sounddevice")

hiddenimports = [
    # the toolkit's own modules, imported lazily by the --run dispatcher
    "audiogram", "hearing_eq", "export_eq",
    # scientific stack
    "scipy.signal", "scipy.fft",
    # audio I/O (MP3/M4A decoding calls the external ffmpeg binary directly)
    "soundfile", "sounddevice",
    # tag formats
    "mutagen.flac", "mutagen.mp3", "mutagen.mp4", "mutagen.id3", "mutagen.wave",
    # plotting (optional at runtime, but bundle the Tk backend it uses)
    "matplotlib.backends.backend_tkagg",
] + collect_submodules("scipy.signal")

block_cipher = None

a = Analysis(
    ["hearing_eq_gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="HearingEQ",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI app; the hearing test opens its own console
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
