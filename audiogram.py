#!/usr/bin/env python3
"""
audiogram.py  —  Personal hearing threshold test (adaptive pure-tone audiometry).

Plays pure tones at random (frequency, level, ear) combinations using an
interleaved adaptive staircase (Hughson-Westlake: 10 dB down / 5 dB up).
Saves thresholds as JSON for use by hearing_eq.py, and plots an audiogram.

Requirements:
    pip install numpy sounddevice matplotlib

Usage:
    python audiogram.py                          # full test, all ears
    python audiogram.py --freqs 2000 4000 8000   # specific frequencies
    python audiogram.py --ear left               # one ear only
    python audiogram.py --output my_map.json     # custom output path
    python audiogram.py --fast                   # fewer reversals, quicker test
"""
from __future__ import annotations

import argparse
import json
import msvcrt
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sys.exit("sounddevice not installed.\n  Run:  pip install sounddevice")

try:
    import matplotlib.pyplot as plt
    from matplotlib.ticker import ScalarFormatter
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# ── test parameters ────────────────────────────────────────────────────────────

# Standard audiometric + extended high-frequency set.
# 3000 Hz is omitted by default: it's redundant between 2000 and 4000 and
# trimming it shaves ~2 min off the test. Add it back via --freqs if wanted.
DEFAULT_FREQS = [500, 1000, 2000, 4000, 6000, 8000, 10_000, 12_000, 16_000]
EARS          = ["left", "right"]
SAMPLE_RATE   = 44_100

TONE_DURATION   = 1.5    # seconds of tone
RESPONSE_WINDOW = 2.0    # extra wait after tone ends (seconds)
FADE_MS         = 50     # cosine fade in/out to avoid clicks (ms)
PRE_SILENCE     = 0.4    # silence before each tone (seconds)

START_DB    = -25        # dBFS starting level for each staircase
MIN_DB      = -78        # quietest level tested
MAX_DB      = -5         # loudest level tested
STEP_DOWN   = 10         # dB drop after heard  (Hughson-Westlake standard)
STEP_UP     = 5          # dB rise after missed
N_REVERSALS = 5          # stop after this many direction reversals
FAST_REVERSALS = 3       # reversals in --fast mode (quick, slightly noisier)
AVG_LAST    = 4          # threshold = mean of last N reversals
CATCH_PROB  = 0.15       # fraction of trials that are silent catch trials

LIMIT_REPEATS    = 3     # misses at MAX_DB (or hits at MIN_DB) before giving up
MAX_TRIALS_STAIR = 25    # hard cap per staircase — never loops forever
SEED_OFFSET_DB   = 10    # start level = neighbor threshold + this margin


# ── tone generation ────────────────────────────────────────────────────────────

def make_tone(
    freq: float,
    level_dbfs: float,
    ear: str,
    duration: float = TONE_DURATION,
    sr: int = SAMPLE_RATE,
) -> np.ndarray:
    """Return (samples × 2) float32 stereo array: pure tone in one ear only."""
    n    = int(sr * duration)
    t    = np.linspace(0, duration, n, endpoint=False)
    mono = np.sin(2.0 * np.pi * freq * t)

    # Cosine-squared ramp — removes all spectral splatter at onset/offset
    fade = int(sr * FADE_MS / 1_000)
    ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade) / fade))
    mono[:fade]  *= ramp
    mono[-fade:] *= ramp[::-1]

    mono *= 10.0 ** (level_dbfs / 20.0)

    stereo = np.zeros((n, 2), dtype=np.float32)
    stereo[:, 0 if ear == "left" else 1] = mono.astype(np.float32)
    return stereo


def make_silence(duration: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    return np.zeros((int(sr * duration), 2), dtype=np.float32)


# ── keyboard input (Windows msvcrt) ───────────────────────────────────────────

ABORT_KEY   = b"\x1b"             # ESC
_EXT_PREFIX = {b"\xe0", b"\x00"}  # Windows extended-key prefix byte
_SCAN_LEFT  = b"K"                # left arrow  (scan code 75)
_SCAN_RIGHT = b"M"                # right arrow (scan code 77)


def _flush_kb() -> None:
    while msvcrt.kbhit():
        msvcrt.getch()


def wait_response(timeout: float, expected_ear: str) -> str:
    """
    Poll keyboard for up to `timeout` seconds.
    Only LEFT / RIGHT arrow keys register as responses; all other keys are
    ignored (prevents accidental spacebar triggers).

    expected_ear: 'left', 'right', or 'catch' (any arrow = false positive).
    Returns: 'correct', 'wrong', 'abort', or 'timeout'.
    """
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if msvcrt.kbhit():
            key = msvcrt.getch()
            if key == ABORT_KEY:
                return "abort"
            if key in _EXT_PREFIX:
                scan = msvcrt.getch()
                if scan == _SCAN_LEFT:
                    pressed = "left"
                elif scan == _SCAN_RIGHT:
                    pressed = "right"
                else:
                    continue      # other extended key (up/down/F-keys) — ignore
                if expected_ear == "catch":
                    return "wrong"   # any arrow during silence = false positive
                return "correct" if pressed == expected_ear else "wrong"
            # all other non-arrow keys are silently ignored
        time.sleep(0.01)
    return "timeout"


# ── adaptive staircase ─────────────────────────────────────────────────────────

@dataclass
class Staircase:
    freq:    float
    ear:     str

    # mutable state
    level:         float          = field(default=START_DB)
    _prev_heard:   Optional[bool] = field(default=None, repr=False)
    reversals:     list[float]    = field(default_factory=list)
    levels_seen:   list[float]    = field(default_factory=list)
    responses:     list[bool]     = field(default_factory=list)
    n_wrong:       int            = 0     # correct tone heard but wrong arrow pressed
    done:          bool           = False
    not_audible:   bool           = False # never heard, even at MAX_DB
    threshold:     Optional[float] = None
    _ceil_misses:  int            = field(default=0, repr=False)
    _floor_hits:   int            = field(default=0, repr=False)

    # configurable
    step_down:      float = STEP_DOWN
    step_up:        float = STEP_UP
    n_reversals:    int   = N_REVERSALS
    avg_last:       int   = AVG_LAST

    def update(self, heard: bool) -> None:
        """Record one response and advance the staircase."""
        self.levels_seen.append(self.level)
        self.responses.append(heard)

        # Ceiling: repeatedly missed at max level → frequency is not audible.
        # Without this the staircase can bounce at MAX_DB forever (no reversals).
        if not heard and self.level >= MAX_DB:
            self._ceil_misses += 1
            if self._ceil_misses >= LIMIT_REPEATS:
                self.done        = True
                self.not_audible = True
                self.threshold   = None
                return
        else:
            self._ceil_misses = 0

        # Floor: repeatedly heard at quietest level → threshold is at/below MIN_DB.
        if heard and self.level <= MIN_DB:
            self._floor_hits += 1
            if self._floor_hits >= LIMIT_REPEATS:
                self.done      = True
                self.threshold = float(MIN_DB)
                return
        else:
            self._floor_hits = 0

        # Detect reversal: direction changed from previous trial
        if self._prev_heard is not None and heard != self._prev_heard:
            self.reversals.append(self.level)

        self._prev_heard = heard

        if len(self.reversals) >= self.n_reversals:
            self.done      = True
            self.threshold = float(np.mean(self.reversals[-self.avg_last:]))
            return

        # Hard cap: converge on whatever we have rather than run forever
        if len(self.levels_seen) >= MAX_TRIALS_STAIR:
            self.done      = True
            self.threshold = self.best_estimate
            return

        step  = self.step_down if heard else self.step_up
        delta = -step if heard else +step
        self.level = float(np.clip(self.level + delta, MIN_DB, MAX_DB))

    @property
    def best_estimate(self) -> Optional[float]:
        """Partial threshold estimate even if not yet converged."""
        if self.threshold is not None:
            return self.threshold
        if len(self.reversals) >= 2:
            return float(np.mean(self.reversals))
        if self.levels_seen:
            return float(np.mean(self.levels_seen[-4:]))
        return None


# ── test runner ────────────────────────────────────────────────────────────────

def _ear_tag(ear: str) -> str:
    return "[ ← L ]" if ear == "left" else "[ R → ]"


def _seed_level(stair: Staircase, staircases: list[Staircase]) -> None:
    """
    Set the starting level of an unstarted staircase from the converged
    threshold of the nearest frequency in the same ear (+ safety margin).
    Cuts ~4-5 descent trials per staircase.
    """
    done = [
        s for s in staircases
        if s.ear == stair.ear and s.done and s.threshold is not None
    ]
    if not done:
        return
    nearest = min(done, key=lambda s: abs(np.log(s.freq / stair.freq)))
    stair.level = float(np.clip(nearest.threshold + SEED_OFFSET_DB, MIN_DB, MAX_DB))


def run_test(
    freqs: list[float],
    ears:  list[str],
    n_reversals: int = N_REVERSALS,
) -> tuple[dict, int, int, int]:
    """
    Run the full interleaved adaptive staircase test.
    Returns (thresholds_dict, n_false_positives, n_catch_trials, n_wrong_ear).
    """
    staircases = [
        Staircase(freq=f, ear=e, n_reversals=n_reversals)
        for f in freqs
        for e in ears
    ]
    active = list(staircases)

    n_catch = 0
    n_fp    = 0
    trial   = 0

    print()
    print("  ┌─────────────────────────────────────────────────────┐")
    print("  │  ← left arrow  = heard the tone in LEFT ear        │")
    print("  │  → right arrow = heard the tone in RIGHT ear       │")
    print("  │  No key        = did not hear anything             │")
    print("  │  ESC           = abort                             │")
    print("  └─────────────────────────────────────────────────────┘")
    print()
    time.sleep(1.5)

    aborted = False

    while active and not aborted:
        is_catch = random.random() < CATCH_PROB and trial > 0

        # Pick target staircase (catch trials use a random one just for ear info)
        stair = random.choice(active)
        trial += 1
        progress = f"[trial {trial:3d}  |  {len(active):2d} left]"

        # Pre-tone silence with random jitter so tone onset can't be anticipated
        sd.play(make_silence(PRE_SILENCE + random.uniform(0.0, 0.6)), SAMPLE_RATE)
        sd.wait()
        _flush_kb()

        if is_catch:
            n_catch += 1
            # Display looks identical to a real tone — revealing "[silence]"
            # on screen would tell the user not to press, defeating the check.
            print(
                f"  {progress}  {_ear_tag(stair.ear)}  ♪ {stair.freq:>6.0f} Hz   ",
                end="\r", flush=True,
            )
            sd.play(make_silence(TONE_DURATION), SAMPLE_RATE)
            resp = wait_response(TONE_DURATION + RESPONSE_WINDOW, expected_ear="catch")
            sd.stop()
            if resp == "abort":
                aborted = True
                break
            fp = resp == "wrong"   # any arrow during silence = false positive
            if fp:
                n_fp += 1
            mark = "✗ FP" if fp else "·"
            print(
                f"  {progress}  {_ear_tag(stair.ear)}  [catch: silence]  {mark}      "
            )
            # catch trials don't update any staircase
            continue

        # Real tone — seed start level from a converged neighbor frequency
        if not stair.levels_seen:
            _seed_level(stair, staircases)

        print(
            f"  {progress}  {_ear_tag(stair.ear)}  ♪ {stair.freq:>6.0f} Hz   ",
            end="\r", flush=True,
        )
        audio = make_tone(stair.freq, stair.level, stair.ear)
        sd.play(audio, SAMPLE_RATE)

        # Accept response during playback
        resp = wait_response(TONE_DURATION, stair.ear)
        sd.stop()

        if resp == "abort":
            aborted = True
            break

        if resp == "timeout":
            # No key pressed during the tone — short extra window after it ends.
            # (A wrong-arrow press already counts as an answer; don't wait again.)
            resp2 = wait_response(RESPONSE_WINDOW, stair.ear)
            if resp2 == "abort":
                aborted = True
                break
            resp = resp2

        heard = resp == "correct"
        if resp == "wrong":
            stair.n_wrong += 1   # heard something but pressed the wrong-ear arrow

        stair.update(heard)
        mark = "✔" if heard else ("✗" if resp == "wrong" else "·")
        print(
            f"  {progress}  {_ear_tag(stair.ear)}  ♪ {stair.freq:>6.0f} Hz  "
            f"{stair.level:+5.0f} dBFS  {mark}  "
        )

        if stair.done:
            active.remove(stair)
            if stair.not_audible:
                print(
                    f"            └→ {stair.freq:.0f} Hz {stair.ear}:  "
                    f"NOT AUDIBLE (missed at max level)"
                )
            else:
                print(
                    f"            └→ threshold {stair.freq:.0f} Hz {stair.ear}:  "
                    f"{stair.threshold:+.1f} dBFS"
                )

    if aborted:
        print("\n  [Test aborted — partial results saved]")

    # Build results dict from all staircases (complete or partial).
    # null = frequency not audible even at max level.
    results: dict = {"left": {}, "right": {}}
    for s in staircases:
        if s.not_audible:
            results[s.ear][str(int(s.freq))] = None
            continue
        est = s.best_estimate
        if est is not None:
            results[s.ear][str(int(s.freq))] = round(est, 1)

    n_wrong_total = sum(s.n_wrong for s in staircases)
    return results, n_fp, n_catch, n_wrong_total


# ── calibration step ───────────────────────────────────────────────────────────

def calibration_step() -> None:
    print()
    print("══════════════════════════════════════════════════════════════")
    print("   AUDIOGRAM  —  Personal Hearing Threshold Test")
    print("══════════════════════════════════════════════════════════════")
    print()
    print("  Before starting:")
    print("  • Use headphones (not speakers) for accurate L/R separation.")
    print("  • Find a quiet environment.")
    print("  • A 1 kHz reference tone will play in the LEFT ear.")
    print("  • Adjust your headphone volume until it is clearly audible")
    print("    but comfortable — like a normal speaking voice.")
    print("  • DO NOT change the volume after this step.")
    print()
    print("  Press ENTER to play the reference tone…")
    input()

    # Loop so the tone never runs out while the user adjusts volume
    ref = make_tone(1_000, -25, "left", duration=10.0)
    print("  ♪ Reference tone playing…")
    print("  (Adjust volume now, then press ENTER when ready)")
    sd.play(ref, SAMPLE_RATE, loop=True)
    input()
    sd.stop()

    print()
    print("  Volume set. Keep it fixed for the entire test.")
    print()
    print("  During the test, when you hear a tone press the arrow key")
    print("  that matches the ear where you heard it:")
    print("    ← left arrow   if heard in the LEFT ear")
    print("    → right arrow  if heard in the RIGHT ear")
    print("  If you hear nothing, press no key and wait.")
    print("  Pressing the wrong arrow does NOT count as a correct response.")
    print()
    print("  Press ENTER to begin…")
    input()


# ── audiogram plot ─────────────────────────────────────────────────────────────

_COLORS  = {"left": "#2979FF", "right": "#D50000"}
_MARKERS = {"left": "x",       "right": "o"}
_LABELS  = {"left": "Left ear", "right": "Right ear"}


def plot_audiogram(results: dict, save_path: Path) -> None:
    if not HAS_MPL:
        print("  [warn] matplotlib not installed — skipping plot")
        return

    all_freqs = sorted(
        {int(f) for ear_data in results.values() for f in ear_data},
        key=int,
    )

    fig, ax = plt.subplots(figsize=(12, 6))

    plotted_any = False
    for ear in ("left", "right"):
        data = results.get(ear, {})
        xs   = [f for f in all_freqs if data.get(str(f)) is not None]
        ys   = [data[str(f)] for f in xs]
        if xs:
            ax.plot(
                xs, ys,
                marker=_MARKERS[ear],
                color=_COLORS[ear],
                label=_LABELS[ear],
                linewidth=2,
                markersize=11,
                markeredgewidth=2.5,
            )
            plotted_any = True
        # Not-audible frequencies: downward arrow pinned at the loud end
        na = [f for f in all_freqs if str(f) in data and data[str(f)] is None]
        if na:
            ax.scatter(
                na, [MAX_DB] * len(na),
                marker="v", s=120, color=_COLORS[ear], zorder=5,
                label=f"{_LABELS[ear]} — not audible",
            )
            plotted_any = True

    if not plotted_any:
        print("  [warn] No results to plot")
        return

    # ── axes ──────────────────────────────────────────────────────────────────
    ax.set_xscale("log")
    ax.set_xlim(300, 20_000)

    all_thr = [v for d in results.values() for v in d.values() if v is not None]
    has_na  = any(v is None for d in results.values() for v in d.values())
    if has_na:
        all_thr.append(MAX_DB)   # keep not-audible markers inside the plot
    all_thr = all_thr or [MAX_DB]
    y_lo = min(all_thr) - 10
    y_hi = max(all_thr) + 10
    ax.set_ylim(y_hi, y_lo)   # inverted: lower dBFS (better hearing) at top

    # Frequency tick marks at the frequencies actually tested
    ax.set_xticks(all_freqs)
    ax.get_xaxis().set_major_formatter(ScalarFormatter())
    ax.tick_params(axis="x", which="minor", bottom=False)

    # ── labels ─────────────────────────────────────────────────────────────────
    ax.set_xlabel("Frequency (Hz)", fontsize=12)
    ax.set_ylabel(
        "Hearing threshold (dBFS)\n← better                      worse →",
        fontsize=11,
    )
    ax.set_title("Personal Audiogram", fontsize=14, fontweight="bold", pad=12)
    ax.legend(fontsize=11)
    ax.grid(True, which="both", alpha=0.25)

    # Annotate each point with its dBFS value
    for ear in ("left", "right"):
        data = results.get(ear, {})
        for f in all_freqs:
            if data.get(str(f)) is not None:
                v = data[str(f)]
                ax.annotate(
                    f"{v:+.0f}",
                    xy=(f, v),
                    textcoords="offset points",
                    xytext=(0, -14 if ear == "left" else +6),
                    fontsize=7.5,
                    color=_COLORS[ear],
                    ha="center",
                )

    plt.tight_layout()
    plt.savefig(str(save_path), dpi=150)
    print(f"  Audiogram image  → {save_path}")
    plt.show()


# ── results table ──────────────────────────────────────────────────────────────

def print_table(results: dict) -> None:
    left  = results.get("left",  {})
    right = results.get("right", {})
    freqs = sorted({int(f) for d in results.values() for f in d}, key=int)

    def _fmt(d: dict, f: int) -> str:
        if str(f) not in d:
            return f"{'—':>8}"          # not tested
        v = d[str(f)]
        if v is None:
            return f"{'inaud.':>8}"     # not audible at max level
        return f"{v:>8}"

    print()
    print("  ─────────────────────────────────────────────────────")
    print("  THRESHOLDS  (dBFS — lower value = better hearing)")
    print("  ─────────────────────────────────────────────────────")
    print(f"  {'Freq (Hz)':>10}   {'Left':>8}   {'Right':>8}")
    print(f"  {'─'*10}   {'─'*8}   {'─'*8}")
    for f in freqs:
        print(f"  {f:>10}   {_fmt(left, f)}   {_fmt(right, f)}")
    print()


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Adaptive hearing threshold test — outputs JSON audiogram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s\n"
            "  %(prog)s --freqs 1000 2000 4000 8000 --ear left\n"
            "  %(prog)s --fast --output quick_map.json\n"
        ),
    )
    ap.add_argument(
        "--freqs", nargs="+", type=float, default=DEFAULT_FREQS, metavar="HZ",
        help="Test frequencies in Hz (default: standard audiometric set)",
    )
    ap.add_argument(
        "--ear", choices=["left", "right", "both"], default="both",
        help="Which ear(s) to test (default: both)",
    )
    ap.add_argument(
        "--output", type=Path, default=Path("audiogram.json"), metavar="FILE",
        help="Output JSON file (default: audiogram.json)",
    )
    ap.add_argument(
        "--fast", action="store_true",
        help=f"Use fewer reversals ({FAST_REVERSALS}) for a quicker test",
    )
    ap.add_argument(
        "--no-calibration", action="store_true",
        help="Skip the reference tone / volume-setting step",
    )
    ap.add_argument(
        "--no-plot", action="store_true",
        help="Skip audiogram plot",
    )
    return ap.parse_args()


def main() -> int:
    # Legacy Windows consoles (cp1252) can't encode some characters we print;
    # degrade to '?' instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    args = parse_args()

    freqs = sorted(set(args.freqs))
    ears  = EARS if args.ear == "both" else [args.ear]
    n_rev = FAST_REVERSALS if args.fast else N_REVERSALS

    # Realistic estimate: ~(2·reversals + 2) tone trials per staircase plus
    # catch trials; 2.8–3.8 s per trial (heard tones end early, misses wait out
    # the full response window).
    n_combos   = len(freqs) * len(ears)
    est_trials = round(n_combos * (2 * n_rev + 2) * (1 + CATCH_PROB))
    est_min    = round(est_trials * 2.8 / 60)
    est_max    = round(est_trials * 3.8 / 60)
    print(f"\n  {n_combos} staircase(s)  |  ~{est_trials} trials  |  "
          f"estimated {est_min}–{est_max} minutes\n")

    if not args.no_calibration:
        calibration_step()

    # ── run test ───────────────────────────────────────────────────────────────
    results, n_fp, n_catch, n_wrong = run_test(freqs, ears, n_reversals=n_rev)

    # ── print results ──────────────────────────────────────────────────────────
    print_table(results)

    if n_catch > 0:
        fp_pct = 100 * n_fp / n_catch
        status = "OK" if fp_pct <= 25 else "HIGH — consider re-testing"
        print(f"  Catch trials:        {n_fp}/{n_catch} false positives ({fp_pct:.0f}%)  [{status}]")
    if n_wrong > 0:
        print(f"  Wrong-ear responses: {n_wrong}  (heard but pressed wrong arrow — not counted)")
    print()

    # ── save JSON ──────────────────────────────────────────────────────────────
    out_path = args.output.resolve()
    device_name = ""
    try:
        device_name = str(sd.query_devices(sd.default.device[1], "output")["name"])
    except Exception:
        pass

    payload = {
        "metadata": {
            "date":        datetime.now().isoformat(timespec="seconds"),
            "device":      device_name,
            "n_reversals": n_rev,
            "note": (
                "Thresholds in dBFS (decibels below full scale). "
                "null = frequency not audible even at maximum test level. "
                "This is a RELATIVE test — results depend on your system volume. "
                "Keep the same volume setting when running hearing_eq.py."
            ),
        },
        "thresholds": results,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"  JSON results     → {out_path}")

    # ── plot ───────────────────────────────────────────────────────────────────
    if not args.no_plot:
        png_path = out_path.with_suffix(".png")
        plot_audiogram(results, png_path)

    # Keep the console open so the results table doesn't vanish (the test is
    # often launched in its own console window, which closes on exit).
    try:
        input("\n  Done. Press ENTER to close…")
    except (EOFError, RuntimeError):
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
