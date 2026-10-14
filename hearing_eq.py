#!/usr/bin/env python3
"""
hearing_eq.py  —  Audiogram-based hearing-compensation EQ for music.

Restores the high frequencies you've lost by boosting each band by a fraction
of your measured hearing loss — a per-ear, linear-phase equaliser derived from
the JSON produced by audiogram.py. Pitches, instruments and tuning are left
exactly as the mix intends; only the spectral balance is corrected so the
content you can barely hear comes back into an audible, pleasant level.

This is the right tool for mild / moderate loss with usable residual hearing.
(Frequency-lowering, the previous approach, is for severe loss / cochlear dead
regions and is intentionally no longer here.)

With --simulate the EQ runs in reverse: instead of boosting your lost bands it
attenuates them, so a normal-hearing person hears the music the way you do
(dull, missing highs) — useful for showing someone how your hearing works.

Requirements:
    pip install numpy scipy soundfile mutagen   (matplotlib for --plot)
    ffmpeg must be in PATH (used to decode MP3 / M4A)

Packaging: the whole toolkit ships as one exe — see HearingEQ.spec
    pyinstaller HearingEQ.spec

Usage:
    python hearing_eq.py <input_dir> <output_dir> [options]
    python hearing_eq.py music/ out/                       # uses ./audiogram.json
    python hearing_eq.py music/ out/ --strength 0.6 --max-boost 20
    python hearing_eq.py music/ out/ --audiogram me.json --plot
    python hearing_eq.py music/ out/ --simulate            # hear how I hear it
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import traceback
import warnings
from pathlib import Path

import numpy as np
from scipy.signal import firwin2, fftconvolve
import soundfile as sf
from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture

# ── defaults ──────────────────────────────────────────────────────────────────

SUPPORTED_EXT = {".mp3", ".m4a", ".wav", ".flac"}

DEF_STRENGTH  = 0.5    # fraction of measured loss to compensate (half-gain rule)
DEF_MAX_BOOST = 18.0   # dB — hard ceiling on per-band boost
DEF_TAPS      = 4095   # FIR length (odd → Type-I linear phase, exact at Nyquist)

SIM_STRENGTH  = 1.0    # --simulate default: reproduce the full measured loss
SIM_MAX_ATTEN = 60.0   # --simulate default ceiling on per-band attenuation

NA_LOSS_DB    = 60.0   # loss assigned to frequencies marked not-audible (null)
BASELINE_MAX  = 2000   # baseline = median threshold of freqs ≤ this (Hz)
CEIL_DB       = -1.0   # output peak ceiling

_EASY_KEYS = [
    "title", "artist", "album", "albumartist",
    "date", "genre", "tracknumber", "discnumber",
    "composer", "comment", "bpm", "copyright", "isrc",
]


# ── audiogram → per-ear loss curves ───────────────────────────────────────────

def load_loss_curves(audiogram_path: Path) -> tuple[dict[str, dict[int, float]], float]:
    """
    Read audiogram.py JSON and return per-ear hearing loss in dB.

    Returns ({ear: {hz: loss_db}}, baseline_dbfs) where loss is the threshold
    elevation relative to the listener's good (low/mid) hearing. null thresholds
    (frequencies that were not audible at all) map to NA_LOSS_DB.
    """
    data = json.loads(audiogram_path.read_text(encoding="utf-8"))
    thr  = data.get("thresholds", data)

    base_vals = [
        v
        for ear in ("left", "right")
        for f, v in (thr.get(ear) or {}).items()
        if v is not None and int(f) <= BASELINE_MAX
    ]
    if not base_vals:
        raise ValueError(
            f"no audible thresholds at or below {BASELINE_MAX} Hz — "
            "cannot establish a baseline"
        )
    baseline = float(np.median(base_vals))

    curves: dict[str, dict[int, float]] = {}
    for ear in ("left", "right"):
        d = thr.get(ear) or {}
        if not d:
            continue
        curves[ear] = {
            int(f): (NA_LOSS_DB if v is None else float(v) - baseline)
            for f, v in d.items()
        }
    if not curves:
        raise ValueError(f"no per-ear thresholds found in {audiogram_path}")
    return curves, baseline


def _merge_curves(*loss_dicts: dict[int, float] | None) -> dict[int, float]:
    """Average loss across the given (non-empty) ear curves, per frequency."""
    present = [d for d in loss_dicts if d]
    freqs   = sorted({f for d in present for f in d})
    out: dict[int, float] = {}
    for f in freqs:
        vals = [d[f] for d in present if f in d]
        out[f] = float(np.mean(vals))
    return out


def boost_curve(
    loss_by_hz: dict[int, float],
    strength: float,
    max_boost: float,
) -> dict[int, float]:
    """Convert loss → applied boost (dB): clip(loss·strength, 0, max_boost)."""
    return {
        f: float(np.clip(loss * strength, 0.0, max_boost))
        for f, loss in loss_by_hz.items()
    }


# ── FIR design + application ──────────────────────────────────────────────────

def design_eq_fir(
    loss_by_hz: dict[int, float],
    sr: int,
    strength: float,
    max_boost: float,
    numtaps: int,
    invert: bool = False,
) -> np.ndarray:
    """
    Linear-phase FIR whose magnitude response boosts each band by
    clip(loss·strength, 0, max_boost) dB. Below the lowest / above the highest
    measured frequency the boost is held flat.

    invert=True negates the curve: instead of compensating the loss it
    *attenuates* each band by the same amount — a simulation of how the
    listener hears (dull, missing highs) for a normal-hearing person.
    """
    nyq = sr / 2.0
    boost = boost_curve(loss_by_hz, strength, max_boost)
    if invert:
        boost = {f: -v for f, v in boost.items()}

    pts = sorted(f for f in boost if 0 < f < nyq)
    if not pts:
        # nothing to do — return a unit impulse (identity filter)
        imp = np.zeros(numtaps | 1)
        imp[len(imp) // 2] = 1.0
        return imp

    g_db = [boost[f] for f in pts]
    f_hz = [0.0] + pts + [nyq]
    g_db = [g_db[0]] + g_db + [g_db[-1]]

    freq = np.clip(np.asarray(f_hz) / nyq, 0.0, 1.0)
    freq[0], freq[-1] = 0.0, 1.0
    gain = 10.0 ** (np.asarray(g_db) / 20.0)

    numtaps |= 1  # force odd: Type-I, arbitrary gain allowed at Nyquist
    return firwin2(numtaps, freq, gain)


def apply_fir(sig: np.ndarray, fir: np.ndarray) -> np.ndarray:
    """Zero-delay (centered) linear-phase filtering of one channel."""
    return fftconvolve(sig, fir, mode="same")


def process(
    audio: np.ndarray,
    sr: int,
    curves: dict[str, dict[int, float]],
    strength: float = DEF_STRENGTH,
    max_boost: float = DEF_MAX_BOOST,
    numtaps: int = DEF_TAPS,
    invert: bool = False,
) -> np.ndarray:
    """
    Apply the per-ear EQ. invert=False compensates the loss (default);
    invert=True simulates the listener's hearing for someone else.

    Stereo: channel 0 ← left-ear curve, channel 1 ← right-ear curve.
    Mono / extra channels: averaged curve of whatever ears are available.
    """
    left  = curves.get("left")
    right = curves.get("right")
    merged = _merge_curves(left, right)

    def fir_for(c: dict[int, float] | None) -> np.ndarray:
        return design_eq_fir(c or merged, sr, strength, max_boost, numtaps, invert)

    if audio.ndim == 1:
        return apply_fir(audio, fir_for(merged)).astype(np.float32)

    n_ch = audio.shape[1]
    if n_ch == 1:
        return apply_fir(audio[:, 0], fir_for(merged))[:, None].astype(np.float32)

    # First two channels treated as L / R; any further channels get merged curve.
    firs = [fir_for(left), fir_for(right)] + [fir_for(merged)] * (n_ch - 2)
    return np.stack(
        [apply_fir(audio[:, c], firs[c]) for c in range(n_ch)],
        axis=1,
    ).astype(np.float32)


def peak_limit(a: np.ndarray, ceil_db: float = CEIL_DB) -> tuple[np.ndarray, float]:
    """
    Scale down to the ceiling only if the peak exceeds it (never boosts).
    Returns (audio, applied_gain_db).
    """
    ceil = 10 ** (ceil_db / 20.0)
    pk   = float(np.max(np.abs(a)))
    if pk > ceil:
        g = ceil / pk
        return a * g, 20.0 * np.log10(g)
    return a, 0.0


# ── I/O ───────────────────────────────────────────────────────────────────────

def load_audio(path: Path) -> tuple[np.ndarray, int]:
    """
    Decode any supported format to float32.
    WAV/FLAC: read directly with soundfile.
    MP3/M4A:  ffmpeg → float32 temp WAV (no 16-bit intermediate), then soundfile.
    """
    if path.suffix.lower() in (".wav", ".flac"):
        return sf.read(str(path), dtype="float32", always_2d=False)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    try:
        # CREATE_NO_WINDOW: don't flash a console when running from the GUI exe
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(path),
                 "-acodec", "pcm_f32le", tmp],
                capture_output=True, creationflags=flags,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "ffmpeg not found — install it and make sure it's on PATH "
                "(needed to decode MP3/M4A)"
            ) from None
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"ffmpeg could not decode {path.name}: {err}")
        return sf.read(tmp, dtype="float32", always_2d=False)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def save_flac_24(audio: np.ndarray, sr: int, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), audio, sr, subtype="PCM_24", format="FLAC")


# ── metadata ──────────────────────────────────────────────────────────────────

def copy_tags(src: Path, dst_flac: Path) -> None:
    """
    Copy text tags and embedded artwork from any supported format to FLAC.
    Everything is written in one save() call to avoid partial-write issues.
    """
    out = FLAC(str(dst_flac))

    # Text tags — mutagen's easy=True normalises key names across ID3/MP4/Vorbis
    try:
        easy = MutagenFile(str(src), easy=True)
        if easy is not None:
            for k in _EASY_KEYS:
                v = easy.get(k)
                if v:
                    try:
                        out[k] = v
                    except Exception:
                        pass
    except Exception as e:
        print(f"  [warn] text tags: {e}", file=sys.stderr)

    # Embedded artwork
    try:
        raw = MutagenFile(str(src))
        if raw is not None:
            if hasattr(raw, "pictures"):        # FLAC source
                for pic in raw.pictures:
                    out.add_picture(pic)
            elif raw.tags:
                for k in list(raw.tags.keys()):  # ID3 cover (MP3 / WAV)
                    if k.startswith("APIC"):
                        f = raw.tags[k]
                        p = Picture()
                        p.type, p.mime, p.desc, p.data = f.type, f.mime, f.desc, f.data
                        out.add_picture(p)
                try:                            # MP4 cover (M4A)
                    from mutagen.mp4 import MP4Cover
                    for cov in (raw.tags.get("covr") or []):
                        p = Picture()
                        p.type = 3  # front cover
                        p.mime = (
                            "image/jpeg"
                            if cov.imageformat == MP4Cover.FORMAT_JPEG
                            else "image/png"
                        )
                        p.desc, p.data = "", bytes(cov)
                        out.add_picture(p)
                except Exception:
                    pass
    except Exception as e:
        print(f"  [warn] artwork: {e}", file=sys.stderr)

    try:
        out.save()
    except Exception as e:
        print(f"  [warn] save tags: {e}", file=sys.stderr)


# ── optional EQ-curve plot ────────────────────────────────────────────────────

def plot_curves(
    curves: dict[str, dict[int, float]],
    strength: float,
    max_boost: float,
    save_path: Path,
    simulate: bool = False,
) -> None:
    try:
        import matplotlib.pyplot as plt
        from matplotlib.ticker import ScalarFormatter
    except ImportError:
        print("  [warn] matplotlib not installed — skipping --plot")
        return

    sign = -1.0 if simulate else 1.0
    colors = {"left": "#2979FF", "right": "#D50000"}
    fig, ax = plt.subplots(figsize=(11, 5))
    for ear in ("left", "right"):
        if ear not in curves:
            continue
        b = boost_curve(curves[ear], strength, max_boost)
        xs = sorted(b)
        ax.plot(xs, [sign * b[f] for f in xs], marker="o", color=colors[ear],
                label=f"{ear} ear", linewidth=2)
    ax.set_xscale("log")
    ax.set_xlim(300, 20_000)
    ax.set_xticks([500, 1000, 2000, 4000, 8000, 16000])
    ax.get_xaxis().set_major_formatter(ScalarFormatter())
    limit_label = ("max attenuation" if simulate else "max boost")
    ax.axhline(sign * max_boost, ls="--", color="gray", alpha=0.6,
               label=f"{limit_label} {max_boost:.0f} dB")
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Applied attenuation (dB)" if simulate else "Applied boost (dB)")
    ax.set_title(
        f"Hearing-loss SIMULATION  (strength {strength:g})" if simulate
        else f"Hearing-compensation EQ  (strength {strength:g})"
    )
    ax.grid(True, which="both", alpha=0.25)
    ax.legend()
    plt.tight_layout()
    plt.savefig(str(save_path), dpi=150)
    print(f"  EQ curve image   -> {save_path}")
    plt.show()


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Audiogram-based hearing-compensation EQ.\n"
            "Boosts each band by a fraction of your measured hearing loss,\n"
            "per ear. Outputs 24-bit FLAC with original metadata preserved."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s music/ out/\n"
            "  %(prog)s music/ out/ --strength 0.6 --max-boost 20\n"
            "  %(prog)s music/ out/ --audiogram me.json --plot\n"
            "  %(prog)s music/ out/ --simulate     # hear how I hear it\n"
        ),
    )
    ap.add_argument("input_dir",  type=Path, help="Folder with source audio files")
    ap.add_argument("output_dir", type=Path, help="Destination folder for FLAC output")
    ap.add_argument(
        "--audiogram", type=Path, default=Path("audiogram.json"), metavar="FILE",
        help="JSON from audiogram.py (default: ./audiogram.json)",
    )
    ap.add_argument(
        "--simulate", action="store_true",
        help="Inverse mode: ATTENUATE the lost bands instead of boosting them, "
             "so a normal-hearing person hears the music as the listener does "
             "(dull, missing highs). Defaults to full loss (strength 1.0).",
    )
    ap.add_argument(
        "--strength", type=float, default=None, metavar="F",
        help=f"Fraction of measured loss to apply (default: {DEF_STRENGTH} "
             "compensating, 1.0 simulating). 0.5 = clinical half-gain; "
             "higher = brighter when compensating / duller when simulating",
    )
    ap.add_argument(
        "--max-boost", type=float, default=None, metavar="DB",
        help=f"Ceiling on per-band gain in dB (default: {DEF_MAX_BOOST} "
             f"compensating, {SIM_MAX_ATTEN:.0f} simulating)",
    )
    ap.add_argument(
        "--taps", type=int, default=DEF_TAPS, metavar="N",
        help=f"FIR filter length (default: {DEF_TAPS}; higher = sharper)",
    )
    ap.add_argument(
        "--plot", action="store_true",
        help="Save/show the applied EQ curve as a PNG next to the output folder",
    )
    return ap.parse_args()


def main() -> int:
    warnings.filterwarnings("ignore")
    # Legacy Windows consoles (cp1252) can't encode some characters we print;
    # degrade instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    args = parse_args()

    in_dir  = args.input_dir.resolve()
    out_dir = args.output_dir.resolve()

    if not in_dir.is_dir():
        print(f"error: not a directory: {in_dir}", file=sys.stderr)
        return 1

    ag_path = args.audiogram.resolve()
    if not ag_path.is_file():
        print(f"error: audiogram not found: {ag_path}\n"
              "       run audiogram.py first, or pass --audiogram <file>",
              file=sys.stderr)
        return 1
    try:
        curves, baseline = load_loss_curves(ag_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"error: cannot read audiogram: {e}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(
        p for p in in_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT
    )
    if not files:
        print(f"No supported audio files (.mp3 .m4a .wav .flac) in {in_dir}")
        return 0

    # Resolve strength / ceiling: simulate and compensate have different defaults
    simulate  = args.simulate
    strength  = args.strength  if args.strength  is not None else (
        SIM_STRENGTH if simulate else DEF_STRENGTH)
    max_boost = args.max_boost if args.max_boost is not None else (
        SIM_MAX_ATTEN if simulate else DEF_MAX_BOOST)

    # Report the gain that will be applied, per ear
    verb = "attenuation (SIMULATE)" if simulate else "boost"
    sign = "-" if simulate else "+"
    print(f"Audiogram: {ag_path.name}  |  baseline {baseline:.1f} dBFS  |  "
          f"strength {strength:g}  max {sign}{max_boost:.0f} dB")
    for ear in ("left", "right"):
        if ear in curves:
            b = boost_curve(curves[ear], strength, max_boost)
            line = "  ".join(f"{f}:{sign}{v:.0f}" for f, v in sorted(b.items()))
            print(f"  {ear:>5} {verb} (dB):  {line}")
    print(f"\nFound {len(files)} file(s)\n")

    if args.plot:
        plot_curves(curves, strength, max_boost, out_dir / "eq_curve.png",
                    simulate=simulate)

    failed: list[str] = []

    for i, src in enumerate(files, 1):
        dst = out_dir / src.with_suffix(".flac").name
        print(f"[{i}/{len(files)}] {src.name}")
        try:
            print("  loading...")
            audio, sr = load_audio(src)

            shape_str = (
                f"{len(audio)} samples, mono"
                if audio.ndim == 1
                else f"{audio.shape[0]} samples, {audio.shape[1]} ch"
            )
            print(f"  filtering  ({shape_str} @ {sr} Hz)...")

            out = process(
                audio, sr, curves,
                strength=strength,
                max_boost=max_boost,
                numtaps=args.taps,
                invert=simulate,
            )
            out, scaled = peak_limit(out)
            if scaled < -0.05:
                print(f"  (peak-limited {scaled:+.1f} dB to avoid clipping)")

            print(f"  saving  -> {dst.name}...")
            save_flac_24(out, sr, dst)

            print("  copying tags...")
            copy_tags(src, dst)
            print("  done.")

        except Exception as e:
            print(f"  [ERROR] {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            failed.append(src.name)

    n_ok = len(files) - len(failed)
    suffix = f"  —  failed: {', '.join(failed)}" if failed else ""
    print(f"\nResult: {n_ok}/{len(files)} OK{suffix}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
