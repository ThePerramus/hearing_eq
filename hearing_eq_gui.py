#!/usr/bin/env python3
"""Hearing EQ — single-executable front-end + dispatcher.

This file is the one entry point for the whole toolkit. Run with no arguments
it opens the GUI; the GUI re-invokes this same program with `--run <mode>` to
do the actual work, so everything lives in one PyInstaller .exe:

    (no args)              → GUI
    --run audiogram …      → audiogram.py   main (hearing test, own console)
    --run process  …       → hearing_eq.py  main (batch FLAC render)
    --run export   …       → export_eq.py   main (real-time IR + Equalizer APO)
"""
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk

# Where the user's files live: next to the .exe when frozen, next to this
# script otherwise. (Under PyInstaller onefile, __file__ points into a temp
# extraction dir, so we must use sys.executable's folder instead.)
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CREATE_NEW_CONSOLE = 0x00000010

JSON_TYPES = [("JSON", "*.json"), ("All", "*.*")]


def _self_cmd(mode: str, *args: str) -> list[str]:
    """Command that re-invokes this program in the given sub-mode."""
    if getattr(sys, "frozen", False):
        base = [sys.executable]                         # the .exe runs itself
    else:
        base = [sys.executable, "-u", os.path.abspath(__file__)]
    return base + ["--run", mode, *args]


def _ensure_console(title: str) -> None:
    """
    The frozen exe is a windowed (no-console) app, so CREATE_NEW_CONSOLE from
    the parent has no effect on it: the child starts with no console at all,
    input() dies and msvcrt has nothing to read. Allocate one explicitly and
    rebind stdio to it.
    """
    if not getattr(sys, "frozen", False):
        return                       # dev mode: python.exe already has a console
    import ctypes
    k32 = ctypes.windll.kernel32
    if k32.GetConsoleWindow():
        return                       # already attached somehow
    if not k32.AllocConsole():
        return
    sys.stdin  = open("CONIN$",  "r", encoding="utf-8", errors="replace")
    sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    try:
        k32.SetConsoleTitleW(title)
    except Exception:
        pass


def _dispatch() -> int:
    """Handle `--run <mode> [args…]`: forward to the right module's main()."""
    mode = sys.argv[2]
    sys.argv = [sys.argv[0]] + sys.argv[3:]   # hand clean args to the sub-main
    if mode == "audiogram":
        _ensure_console("Audiogram — Hearing Test")
        import audiogram
        return audiogram.main()
    if mode == "process":
        import hearing_eq
        return hearing_eq.main()
    if mode == "export":
        import export_eq
        return export_eq.main()
    print(f"unknown --run mode: {mode}", file=sys.stderr)
    return 2


def _default_audiogram() -> str:
    p = os.path.join(APP_DIR, "audiogram.json")
    return p if os.path.isfile(p) else ""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Hearing EQ")
        self.minsize(640, 560)
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        self._proc: subprocess.Popen | None = None
        self._actions: list[ttk.Button] = []
        self._build()
        self.geometry("720x680")

    # ─────────────────────────── Layout ──────────────────────────

    def _build(self):
        P = {"padx": 10, "pady": 4}

        # ── Audiograma ────────────────────────────────────────────
        fa = ttk.LabelFrame(self, text="Audiograma", padding=8)
        fa.pack(fill="x", **P)
        fa.columnconfigure(1, weight=1)

        self.v_audiogram = tk.StringVar(value=_default_audiogram())
        ttk.Label(fa, text="Archivo:").grid(row=0, column=0, sticky="w")
        ttk.Entry(fa, textvariable=self.v_audiogram).grid(
            row=0, column=1, sticky="ew", padx=(6, 4))
        ttk.Button(fa, text="Browse", width=8,
                   command=lambda: self._pick_file(self.v_audiogram, JSON_TYPES)
                   ).grid(row=0, column=2, padx=2)
        self._btn_test = ttk.Button(fa, text="Hacer test…", width=12,
                                    command=self._on_test)
        self._btn_test.grid(row=0, column=3, padx=(2, 0))

        self.v_fast = tk.BooleanVar(value=False)
        ttk.Checkbutton(fa, text="Test rápido (menos reversiones, ~mitad de tiempo)",
                        variable=self.v_fast).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Label(fa, text="El test corre en una ventana de consola aparte "
                  "(teclas ← → y audio).", foreground="gray"
                  ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(2, 0))

        # ── Parámetros (compartidos) ──────────────────────────────
        fp = ttk.LabelFrame(self, text="Parámetros de compensación", padding=8)
        fp.pack(fill="x", **P)
        fp.columnconfigure(1, weight=1)

        self.v_strength = tk.DoubleVar(value=0.5)
        self.v_maxboost = tk.StringVar(value="18")

        ttk.Label(fp, text="Strength:").grid(row=0, column=0, sticky="w")
        ttk.Scale(fp, from_=0.0, to=1.0, variable=self.v_strength,
                  orient="horizontal", command=self._on_strength
                  ).grid(row=0, column=1, sticky="ew", padx=(6, 6))
        self._lbl_strength = ttk.Label(fp, text="0.50", width=5)
        self._lbl_strength.grid(row=0, column=2, sticky="w")
        ttk.Label(fp, text="(0.5 = medio-gain clínico; más alto = más brillo)",
                  foreground="gray").grid(row=1, column=1, sticky="w", padx=(6, 0))

        ttk.Label(fp, text="Max boost (dB):").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(fp, from_=0, to=30, increment=1, width=6,
                    textvariable=self.v_maxboost).grid(
            row=2, column=1, sticky="w", padx=(6, 0), pady=(6, 0))

        # ── Procesar música → FLAC ────────────────────────────────
        fr = ttk.LabelFrame(self, text="Procesar música → FLAC", padding=8)
        fr.pack(fill="x", **P)
        fr.columnconfigure(1, weight=1)

        self.v_in   = tk.StringVar()
        self.v_out  = tk.StringVar()
        self.v_plot = tk.BooleanVar(value=False)
        self.v_simulate = tk.BooleanVar(value=False)

        ttk.Label(fr, text="Entrada:").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(fr, textvariable=self.v_in).grid(row=0, column=1, sticky="ew", padx=(6, 4))
        ttk.Button(fr, text="Carpeta", width=8,
                   command=lambda: self._pick_dir(self.v_in)).grid(row=0, column=2, padx=2)

        ttk.Label(fr, text="Salida:").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(fr, textvariable=self.v_out).grid(row=1, column=1, sticky="ew", padx=(6, 4))
        ttk.Button(fr, text="Carpeta", width=8,
                   command=lambda: self._pick_dir(self.v_out)).grid(row=1, column=2, padx=2)

        self._btn_process = ttk.Button(fr, text="Procesar", width=10,
                                       command=self._on_process)
        self._btn_process.grid(row=0, column=3, rowspan=2, padx=(2, 0), sticky="ns")
        ttk.Checkbutton(fr, text="Graficar curva EQ (--plot)", variable=self.v_plot
                        ).grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(4, 0))
        ttk.Checkbutton(
            fr, text="Simular mi audición (inverso, para que otro escuche como oigo yo)",
            variable=self.v_simulate
        ).grid(row=3, column=1, columnspan=2, sticky="w", padx=(6, 0), pady=(0, 2))

        # ── Exportar filtros (Windows, tiempo real) ───────────────
        fe = ttk.LabelFrame(self, text="Exportar filtros (Windows, tiempo real)", padding=8)
        fe.pack(fill="x", **P)
        fe.columnconfigure(1, weight=1)

        self.v_exportdir = tk.StringVar(value=os.path.join(APP_DIR, "windows_eq"))
        ttk.Label(fe, text="Destino:").grid(row=0, column=0, sticky="w")
        ttk.Entry(fe, textvariable=self.v_exportdir).grid(
            row=0, column=1, sticky="ew", padx=(6, 4))
        ttk.Button(fe, text="Carpeta", width=8,
                   command=lambda: self._pick_dir(self.v_exportdir)).grid(row=0, column=2, padx=2)
        self._btn_export = ttk.Button(fe, text="Exportar", width=10,
                                      command=self._on_export)
        self._btn_export.grid(row=0, column=3, padx=(2, 0))
        ttk.Button(fe, text="Abrir carpeta", command=self._open_exportdir
                   ).grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(4, 0))

        # ── Log ───────────────────────────────────────────────────
        flog = ttk.LabelFrame(self, text="Log", padding=4)
        flog.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.log = scrolledtext.ScrolledText(
            flog, wrap="word", height=9, font=("Consolas", 9), state="disabled",
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
        )
        self.log.pack(fill="both", expand=True)

        self._actions = [self._btn_test, self._btn_process, self._btn_export]

    # ─────────────────────────── Eventos ─────────────────────────

    def _on_strength(self, _evt=None):
        self._lbl_strength.config(text=f"{self.v_strength.get():.2f}")

    def _pick_dir(self, var: tk.StringVar):
        d = var.get().strip()
        init = d if os.path.isdir(d) else os.path.expanduser("~")
        path = filedialog.askdirectory(initialdir=init)
        if path:
            var.set(path)

    def _pick_file(self, var: tk.StringVar, filetypes: list):
        d = var.get().strip()
        init = (os.path.dirname(d) if os.path.isfile(d) else d) or APP_DIR
        path = filedialog.askopenfilename(initialdir=init, filetypes=filetypes)
        if path:
            var.set(path)

    def _open_exportdir(self):
        d = self.v_exportdir.get().strip()
        if d and os.path.isdir(d):
            try:
                os.startfile(d)  # type: ignore[attr-defined]
            except Exception as e:
                self._log(f"[ERROR] no se pudo abrir la carpeta: {e}\n")
        else:
            self._log("[info] la carpeta de destino todavía no existe.\n")

    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ── Test auditivo: consola propia (interactivo) ───────────────

    def _on_test(self):
        ag = self.v_audiogram.get().strip() or os.path.join(APP_DIR, "audiogram.json")
        extra = ["--fast"] if self.v_fast.get() else []
        cmd = _self_cmd("audiogram", "--output", ag, *extra)
        try:
            subprocess.Popen(cmd, cwd=APP_DIR, creationflags=CREATE_NEW_CONSOLE)
            self._log("Test auditivo lanzado en una ventana de consola aparte.\n"
                      f"Al terminar, el audiograma se guarda en:\n  {ag}\n\n")
        except Exception as e:
            self._log(f"[ERROR] no se pudo lanzar el test: {e}\n")

    # ── Procesar / Exportar: subprocess con log en vivo ───────────

    def _on_process(self):
        if self._running_toggle(self._btn_process):
            return
        ag  = self.v_audiogram.get().strip()
        inp = self.v_in.get().strip()
        out = self.v_out.get().strip()
        if not inp or not out:
            self._log("[ERROR] elegí carpeta de entrada y de salida.\n")
            return
        if not ag:
            self._log("[ERROR] falta el audiograma.\n")
            return
        extra = ["--plot"] if self.v_plot.get() else []
        if self.v_simulate.get():
            # Simulation uses its own defaults (full loss); don't pass strength/cap.
            cmd = _self_cmd("process", inp, out,
                            "--audiogram", ag, "--simulate", *extra)
        else:
            cmd = _self_cmd("process", inp, out,
                            "--audiogram", ag,
                            "--strength", f"{self.v_strength.get():.2f}",
                            "--max-boost", self.v_maxboost.get(), *extra)
        self._start(cmd, self._btn_process)

    def _on_export(self):
        if self._running_toggle(self._btn_export):
            return
        ag  = self.v_audiogram.get().strip()
        out = self.v_exportdir.get().strip()
        if not ag:
            self._log("[ERROR] falta el audiograma.\n")
            return
        if not out:
            self._log("[ERROR] elegí la carpeta de destino.\n")
            return
        cmd = _self_cmd("export",
                        "--audiogram", ag,
                        "--out-dir", out,
                        "--strength", f"{self.v_strength.get():.2f}",
                        "--max-boost", self.v_maxboost.get())
        self._start(cmd, self._btn_export)

    def _running_toggle(self, btn: ttk.Button) -> bool:
        """If a process is running on `btn`, cancel it and return True."""
        if self._proc and self._proc.poll() is None and btn["text"] == "Cancelar":
            self._proc.terminate()
            return True
        return False

    def _start(self, cmd: list[str], btn: ttk.Button):
        if self._proc and self._proc.poll() is None:
            self._log("[info] esperá a que termine la tarea en curso.\n")
            return
        self._clear_log()
        self._log("$ " + " ".join(f'"{a}"' if " " in a else a for a in cmd) + "\n\n")
        for b in self._actions:
            b.config(state="disabled")
        btn.config(text="Cancelar", state="normal")
        threading.Thread(target=self._run, args=(cmd, btn), daemon=True).start()

    def _run(self, cmd: list[str], btn: ttk.Button):
        labels = {self._btn_process: "Procesar", self._btn_export: "Exportar"}
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", encoding="utf-8", bufsize=1,
                env=env, cwd=APP_DIR,
            )
            assert self._proc.stdout
            for line in self._proc.stdout:
                self.after(0, self._log, line)
            self._proc.wait()
            rc = self._proc.returncode
            self.after(0, self._log,
                       "\n[Completado]\n" if rc == 0 else f"\n[Error: código {rc}]\n")
        except Exception as e:
            self.after(0, self._log, f"\n[ERROR] {e}\n")
        finally:
            self.after(0, self._finish, btn, labels.get(btn, "OK"))

    def _finish(self, btn: ttk.Button, label: str):
        btn.config(text=label)
        for b in self._actions:
            b.config(state="normal")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--run":
        sys.exit(_dispatch())
    App().mainloop()
