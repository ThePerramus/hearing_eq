# PyInstaller spec for hearing_eq.py
# Build: pyinstaller hearing_eq.spec
#
# The resulting .exe requires ffmpeg.exe in the same folder or in PATH.

block_cipher = None

a = Analysis(
    ['hearing_eq.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'soundfile',
        'scipy.signal',
        'scipy.fft',
        'pydub',
        'mutagen',
        'mutagen.flac',
        'mutagen.mp3',
        'mutagen.mp4',
        'mutagen.id3',
        'mutagen.wave',
    ],
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
    name='hearing_eq',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,       # keep console visible so progress is shown
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
