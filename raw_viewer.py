import base64
import concurrent.futures
import io
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
from PIL import Image, ImageTk
import rawpy


THUMB_W  = 200
THUMB_H  = 150
CELL_PAD = 10
RAW_EXTS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".arq", ".dng", ".rw2",
            ".orf", ".raf", ".pef", ".srw", ".x3f", ".gpr", ".iiq", ".3fr",
            ".fff", ".mrw"}
RAW_GLOB = ("*.CR2 *.CR3 *.NEF *.NRW *.ARW *.ARQ *.DNG *.RW2 *.ORF "
            "*.RAF *.PEF *.SRW *.X3F *.GPR *.IIQ *.3FR *.FFF *.MRW")

_APPDATA     = os.environ.get("APPDATA") or os.path.expanduser("~")
_SESSION_DIR = os.path.join(_APPDATA, "RawViewer")
_SESSION_PATH = os.path.join(_SESSION_DIR, "session.json")


def _load_session():
    try:
        with open(_SESSION_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_session(data):
    try:
        os.makedirs(_SESSION_DIR, exist_ok=True)
        with open(_SESSION_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ── Tone-curve widget ─────────────────────────────────────────────────────────

class ToneCurveWidget(ttk.Frame):
    _PAD = 5
    _R   = 5
    _HIT = 9

    def __init__(self, parent, on_change, **kwargs):
        super().__init__(parent, **kwargs)
        self.on_change = on_change
        self.points = [(0.0, 0.0), (1.0, 1.0)]
        self._drag = None
        self._lut  = np.linspace(0.0, 1.0, 256)

        self.cv = tk.Canvas(self, bg="#222", highlightthickness=1,
                            highlightbackground="#555", width=188, height=188)
        self.cv.pack(fill=tk.BOTH, expand=True)
        self.cv.bind("<ButtonPress-1>",   self._press)
        self.cv.bind("<B1-Motion>",       self._drag_move)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<ButtonPress-3>",   self._right_click)
        self.cv.bind("<Configure>",       lambda _: self._draw())

    def get_lut(self):
        return self._lut

    def reset(self):
        self.points = [(0.0, 0.0), (1.0, 1.0)]
        self._update()

    def _to_norm(self, cx, cy):
        w, h, p = self.cv.winfo_width(), self.cv.winfo_height(), self._PAD
        return (float(np.clip((cx - p) / (w - 2*p), 0, 1)),
                float(np.clip(1.0 - (cy - p) / (h - 2*p), 0, 1)))

    def _to_canvas(self, x, y):
        w, h, p = self.cv.winfo_width(), self.cv.winfo_height(), self._PAD
        return p + x * (w - 2*p), p + (1.0 - y) * (h - 2*p)

    def _build_lut(self):
        xs = np.array([p[0] for p in self.points])
        ys = np.array([p[1] for p in self.points])
        t  = np.linspace(0.0, 1.0, 256)

        if len(self.points) <= 2:
            self._lut = np.clip(np.interp(t, xs, ys), 0.0, 1.0)
            return

        dx    = np.diff(xs)
        delta = np.diff(ys) / np.where(dx > 1e-12, dx, 1e-12)
        n     = len(xs)
        m     = np.zeros(n)
        m[0], m[-1] = delta[0], delta[-1]
        for i in range(1, n - 1):
            if delta[i-1] * delta[i] <= 0:
                m[i] = 0.0
            else:
                w1, w2 = 2*dx[i] + dx[i-1], dx[i] + 2*dx[i-1]
                m[i]   = (w1 + w2) / (w1/delta[i-1] + w2/delta[i])
        for i in range(n - 1):
            if abs(delta[i]) < 1e-12:
                m[i] = m[i+1] = 0.0
            else:
                a, b = m[i]/delta[i], m[i+1]/delta[i]
                if a**2 + b**2 > 9:
                    tau = 3.0 / np.sqrt(a**2 + b**2)
                    m[i], m[i+1] = tau*a*delta[i], tau*b*delta[i]

        j   = np.clip(np.searchsorted(xs, t, side="right") - 1, 0, n-2)
        h   = dx[j]
        s   = (t - xs[j]) / np.where(h > 1e-12, h, 1e-12)
        lut = ((2*s**3 - 3*s**2 + 1) * ys[j]
               + (s**3 - 2*s**2 + s) * h * m[j]
               + (-2*s**3 + 3*s**2)  * ys[j+1]
               + (s**3 - s**2)       * h * m[j+1])
        self._lut = np.clip(lut, 0.0, 1.0)

    def _draw(self):
        self._build_lut()
        cv = self.cv
        cv.delete("all")
        w, h, p = cv.winfo_width(), cv.winfo_height(), self._PAD
        if w < 2*p + 4 or h < 2*p + 4:
            return
        for v in (0.25, 0.5, 0.75):
            gx, _ = self._to_canvas(v, 0)
            _, gy = self._to_canvas(0, v)
            cv.create_line(gx, p, gx, h-p, fill="#363636")
            cv.create_line(p, gy, w-p, gy, fill="#363636")
        ax, ay = self._to_canvas(0, 0)
        bx, by = self._to_canvas(1, 1)
        cv.create_line(ax, ay, bx, by, fill="#3a3a3a", dash=(4, 4))
        steps  = max(int(w - 2*p), 64)
        t_vals = np.linspace(0.0, 1.0, steps)
        y_vals = np.interp(t_vals, np.linspace(0.0, 1.0, 256), self._lut)
        pts    = []
        for tx, ty in zip(t_vals, y_vals):
            cx, cy = self._to_canvas(float(tx), float(ty))
            pts   += [cx, cy]
        if len(pts) >= 4:
            cv.create_line(*pts, fill="#d8d8d8", width=1.5)
        r = self._R
        for i, (x, y) in enumerate(self.points):
            cx, cy = self._to_canvas(x, y)
            fill   = "#aaaaaa" if (i == 0 or i == len(self.points)-1) else "#ffffff"
            cv.create_oval(cx-r, cy-r, cx+r, cy+r, fill=fill, outline="#777", width=1)

    def _hit(self, cx, cy):
        for i, (x, y) in enumerate(self.points):
            px, py = self._to_canvas(x, y)
            if (cx-px)**2 + (cy-py)**2 <= self._HIT**2:
                return i
        return None

    def _press(self, e):
        idx = self._hit(e.x, e.y)
        if idx is not None:
            self._drag = idx
        else:
            x, y   = self._to_norm(e.x, e.y)
            new_pt = (float(np.clip(x, 0.01, 0.99)), float(np.clip(y, 0.0, 1.0)))
            self.points.append(new_pt)
            self.points.sort()
            self._drag = self.points.index(new_pt)
            self._update()

    def _drag_move(self, e):
        if self._drag is None:
            return
        i    = self._drag
        x, y = self._to_norm(e.x, e.y)
        lo   = self.points[i-1][0] + 0.005 if i > 0                  else 0.0
        hi   = self.points[i+1][0] - 0.005 if i < len(self.points)-1 else 1.0
        x    = 0.0 if i == 0 else (1.0 if i == len(self.points)-1 else float(np.clip(x, lo, hi)))
        self.points[i] = (x, float(np.clip(y, 0.0, 1.0)))
        self._update()

    def _release(self, _e):
        self._drag = None

    def _right_click(self, e):
        idx = self._hit(e.x, e.y)
        if idx is not None and 0 < idx < len(self.points)-1:
            self.points.pop(idx)
            self._drag = None
            self._update()

    def _update(self):
        self._draw()
        self.on_change()


# ── Gallery view ──────────────────────────────────────────────────────────────

class GalleryView(ttk.Frame):
    def __init__(self, parent, open_cb, set_status, **kwargs):
        super().__init__(parent, **kwargs)
        self._open_cb    = open_cb
        self._set_status = set_status
        self._cells      = []
        self._photos     = []
        self._img_lbls   = []
        self._path_to_idx = {}
        self._ncols      = 0
        self._n_total    = 0
        self._n_loaded   = 0
        self._queue      = queue.Queue()
        self._resize_job     = None
        self._current_folder = None
        self._load_done_cb   = None
        self._build_ui()

    def _build_ui(self):
        bar = ttk.Frame(self, padding=(8, 8, 8, 4))
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="Open Folder…", command=self.open_folder).pack(side=tk.LEFT)
        self._folder_lbl = ttk.Label(bar, text="", foreground="#777", anchor="w")
        self._folder_lbl.pack(side=tk.LEFT, padx=(8, 0))

        wrap = ttk.Frame(self)
        wrap.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        vbar = ttk.Scrollbar(wrap, orient="vertical")
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._cv = tk.Canvas(wrap, bg="#1a1a1a", highlightthickness=0,
                             yscrollcommand=vbar.set)
        self._cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vbar.configure(command=self._cv.yview)

        self._gf = tk.Frame(self._cv, bg="#1a1a1a")
        self._cv_win = self._cv.create_window((0, 0), window=self._gf, anchor="nw")
        self._gf.bind("<Configure>",
                      lambda _e: self._cv.configure(scrollregion=self._cv.bbox("all")))
        self._cv.bind("<Configure>", self._on_cv_resize)
        self._cv.bind("<MouseWheel>", self._scroll)
        self._gf.bind("<MouseWheel>", self._scroll)

        self._hint = tk.Label(
            self._gf,
            text="Open a folder to browse your raw files",
            bg="#1a1a1a", fg="#4a4a4a", font=("", 12),
        )
        self._hint.grid(row=0, column=0, padx=60, pady=120)

    def _scroll(self, e):
        self._cv.yview_scroll(int(-1 * (e.delta / 120)), "units")

    def _on_cv_resize(self, e):
        self._cv.itemconfig(self._cv_win, width=e.width)
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(120, self._relayout)

    def open_folder(self):
        d = filedialog.askdirectory(title="Open Folder")
        if d:
            self._load_folder(d)

    def _load_folder(self, folder):
        try:
            entries = sorted(os.listdir(folder))
        except OSError as exc:
            messagebox.showerror("Error", str(exc))
            return
        self._current_folder = folder
        paths = [os.path.join(folder, f) for f in entries
                 if os.path.splitext(f)[1].lower() in RAW_EXTS]
        if not paths:
            self._set_status("No raw files found in this folder")
            return
        self._folder_lbl.config(text=folder)
        self._clear()
        self._n_total  = len(paths)
        self._n_loaded = 0
        self._set_status(f"Loading 0 / {self._n_total}…")
        threading.Thread(target=self._load_thread, args=(paths,), daemon=True).start()
        self._poll()

    def _load_thread(self, paths):
        workers = min(os.cpu_count() or 4, len(paths))
        idx_of   = {p: i for i, p in enumerate(paths)}
        pending  = {}
        next_out = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._extract_thumb, p): p for p in paths}
            for future in concurrent.futures.as_completed(futures):
                p = futures[future]
                try:
                    img = future.result()
                except Exception:
                    img = None
                pending[idx_of[p]] = (p, img)
                while next_out in pending:
                    self._queue.put(pending.pop(next_out))
                    next_out += 1
        self._queue.put(None)

    def _extract_thumb(self, path):
        with rawpy.imread(path) as raw:
            rgb16 = raw.postprocess(
                half_size=True, use_camera_wb=True, no_auto_bright=True,
                output_color=rawpy.ColorSpace.sRGB, output_bps=16, gamma=(1, 1),
            )
        img = rgb16.astype(np.float32) / 65535.0
        img = np.where(img <= 0.0031308, img * 12.92,
                       1.055 * np.power(np.maximum(img, 1e-9), 1/2.4) - 0.055)
        img = (np.clip(img, 0.0, 1.0) * 255).astype(np.uint8)
        pil = Image.fromarray(img)
        pil.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
        return pil

    def _poll(self):
        try:
            while True:
                item = self._queue.get_nowait()
                if item is None:
                    self._set_status(f"{len(self._cells)} photos")
                    cb, self._load_done_cb = self._load_done_cb, None
                    if cb:
                        cb()
                    return
                path, pil = item
                self._n_loaded += 1
                if pil is not None:
                    self._add_cell(path, pil)
                self._set_status(f"Loading {self._n_loaded} / {self._n_total}…")
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _add_cell(self, path, pil):
        if self._hint.winfo_manager():
            self._hint.grid_remove()

        photo = ImageTk.PhotoImage(pil)
        self._photos.append(photo)

        cell     = tk.Frame(self._gf, bg="#272727", cursor="hand2",
                            highlightbackground="#272727", highlightthickness=2)
        img_lbl  = tk.Label(cell, image=photo, bg="#272727",
                            width=THUMB_W, height=THUMB_H)
        name_lbl = tk.Label(cell,
                            text=os.path.splitext(os.path.basename(path))[0],
                            bg="#272727", fg="#999", font=("", 8),
                            wraplength=THUMB_W - 8)
        img_lbl.pack(pady=(6, 2), padx=6)
        name_lbl.pack(pady=(0, 6))

        def on_enter(_e, c=cell):
            c.config(highlightbackground="#5a90c8")

        def on_leave(_e, c=cell):
            rx, ry = c.winfo_rootx(), c.winfo_rooty()
            if not (rx <= c.winfo_pointerx() <= rx + c.winfo_width()
                    and ry <= c.winfo_pointery() <= ry + c.winfo_height()):
                c.config(highlightbackground="#272727")

        for w in (cell, img_lbl, name_lbl):
            w.bind("<Enter>",           on_enter)
            w.bind("<Leave>",           on_leave)
            w.bind("<Double-Button-1>", lambda _e, p=path: self._open_cb(p))
            w.bind("<MouseWheel>",      self._scroll)

        cw    = self._cv.winfo_width()
        ncols = max(1, cw // (THUMB_W + CELL_PAD * 2 + 4))
        r, c  = divmod(len(self._cells), ncols)
        cell.grid(row=r, column=c, padx=CELL_PAD, pady=CELL_PAD, sticky="n")
        self._path_to_idx[path] = len(self._cells)
        self._cells.append(cell)
        self._img_lbls.append(img_lbl)

    def update_thumbnail(self, path, pil):
        idx = self._path_to_idx.get(path)
        if idx is None:
            return
        photo = ImageTk.PhotoImage(pil)
        self._photos[idx] = photo
        self._img_lbls[idx].config(image=photo)

    def _relayout(self):
        cw    = self._cv.winfo_width()
        ncols = max(1, cw // (THUMB_W + CELL_PAD * 2 + 4))
        if ncols == self._ncols:
            return
        self._ncols = ncols
        for i, cell in enumerate(self._cells):
            r, c = divmod(i, ncols)
            cell.grid(row=r, column=c, padx=CELL_PAD, pady=CELL_PAD, sticky="n")

    def _clear(self):
        for c in self._cells:
            c.destroy()
        self._cells.clear()
        self._photos.clear()
        self._img_lbls.clear()
        self._path_to_idx.clear()
        self._ncols = 0
        self._hint.grid(row=0, column=0, padx=60, pady=120)


# ── Editor view ───────────────────────────────────────────────────────────────

class EditorView(ttk.Frame):
    def __init__(self, parent, gallery_cb, set_status, **kwargs):
        super().__init__(parent, **kwargs)
        self._gallery_cb     = gallery_cb
        self._set_status     = set_status
        self.raw_linear      = None
        self.preview_linear  = None
        self.photo_image     = None
        self._update_pending = False
        self._resize_job     = None
        self._placeholder    = None
        self._show_before    = False
        self._disp_vars      = {}
        self._current_path   = None
        self._edit_states    = {}

        self.adjustments = {
            "exposure":    tk.DoubleVar(value=0.0),
            "temperature": tk.DoubleVar(value=0.0),
            "tint":        tk.DoubleVar(value=0.0),
            "saturation":  tk.DoubleVar(value=0.0),
        }
        self._build_ui()

    def _build_ui(self):
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=0, minsize=220)

        canvas_bg = ttk.Frame(self)
        canvas_bg.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=(8, 4))
        self.canvas = tk.Canvas(canvas_bg, bg="#1b1b1b", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self._placeholder = self.canvas.create_text(
            400, 300, text="Open a raw file to begin",
            fill="#4a4a4a", font=("", 13), justify="center",
        )

        ctrl = ttk.Frame(self, padding=(0, 8, 8, 4))
        ctrl.grid(row=0, column=1, sticky="ns")

        ttk.Button(ctrl, text="← Gallery",
                   command=self._gallery_cb).pack(fill=tk.X, pady=(0, 4))
        ttk.Button(ctrl, text="Open Raw File…",
                   command=self._open_file).pack(fill=tk.X, pady=(0, 12))

        for args in [
            ("Exposure",    "exposure",    -3.0, 3.0,  "%.2f"),
            ("Temperature", "temperature", -100, 100,  "%+.0f"),
            ("Tint",        "tint",        -100, 100,  "%+.0f"),
            ("Saturation",  "saturation",  -100, 100,  "%+.0f"),
        ]:
            self._add_slider(ctrl, *args)

        ttk.Separator(ctrl, orient="horizontal").pack(fill=tk.X, pady=(10, 6))
        ttk.Label(ctrl, text="Tone Curve", anchor="w").pack(fill=tk.X)
        self.curve = ToneCurveWidget(ctrl, on_change=self._schedule_update)
        self.curve.pack(fill=tk.X, pady=(4, 2))
        ttk.Label(ctrl, text="click add  •  right-click remove",
                  foreground="#666", font=("", 8), anchor="center").pack(fill=tk.X)

        ttk.Separator(ctrl, orient="horizontal").pack(fill=tk.X, pady=10)
        self._ba_btn = ttk.Button(ctrl, text="Before  [\\]",
                                  command=self.toggle_before_after)
        self._ba_btn.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(ctrl, text="Reset All",
                   command=self.reset_adjustments).pack(fill=tk.X)

    def _add_slider(self, parent, label, key, lo, hi, fmt):
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=3)
        header = ttk.Frame(frame)
        header.pack(fill=tk.X)
        disp = tk.StringVar(value=fmt % 0.0)
        self._disp_vars[key] = (disp, fmt)
        ttk.Label(header, text=label, width=12, anchor="w").pack(side=tk.LEFT)
        ttk.Label(header, textvariable=disp, width=7, anchor="e",
                  font=("Courier", 9)).pack(side=tk.RIGHT)
        slider = ttk.Scale(frame, variable=self.adjustments[key],
                           from_=lo, to=hi, orient=tk.HORIZONTAL)
        slider.pack(fill=tk.X)
        slider.configure(command=lambda v, _d=disp, _f=fmt: (
            _d.set(_f % float(v)), self._schedule_update()))

    def _open_file(self):
        path = filedialog.askopenfilename(
            title="Open Raw File",
            filetypes=[("Raw images", RAW_GLOB), ("All files", "*.*")],
        )
        if path:
            self.load(path)

    def load(self, path):
        self._save_state()
        self._set_status(f"Loading {os.path.basename(path)}…")
        self.update_idletasks()
        try:
            with rawpy.imread(path) as raw:
                rgb16 = raw.postprocess(
                    use_camera_wb=True, no_auto_bright=True,
                    output_color=rawpy.ColorSpace.sRGB,
                    output_bps=16, gamma=(1, 1),
                )
            self.raw_linear = rgb16.astype(np.float32) / 65535.0
            h, w = self.raw_linear.shape[:2]
            self._set_status(f"{os.path.basename(path)}  —  {w} × {h}")
            if self._placeholder is not None:
                self.canvas.delete(self._placeholder)
                self._placeholder = None
            self._current_path = path
            self._restore_state(path)
            if self._show_before:
                self._show_before = False
                self._ba_btn.config(text="Before  [\\]")
            self._rebuild_preview()
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))
            self._set_status("Error loading file")

    def _save_state(self):
        if self._current_path is None:
            return
        self._edit_states[self._current_path] = {
            "exposure":     self.adjustments["exposure"].get(),
            "temperature":  self.adjustments["temperature"].get(),
            "tint":         self.adjustments["tint"].get(),
            "saturation":   self.adjustments["saturation"].get(),
            "curve_points": list(self.curve.points),
        }

    def _restore_state(self, path):
        state = self._edit_states.get(path)
        vals = state if state else {"exposure": 0.0, "temperature": 0.0,
                                    "tint": 0.0, "saturation": 0.0}
        for key in ("exposure", "temperature", "tint", "saturation"):
            val = vals[key]
            self.adjustments[key].set(val)
            disp, fmt = self._disp_vars[key]
            disp.set(fmt % val)
        self.curve.points = list(state["curve_points"]) if state else [(0.0, 0.0), (1.0, 1.0)]
        self.curve._build_lut()
        self.curve._draw()

    def render_thumbnail(self, tw, th):
        if self.raw_linear is None:
            return None
        h, w  = self.raw_linear.shape[:2]
        scale = min(tw / w, th / h)
        pw    = max(1, int(w * scale))
        ph    = max(1, int(h * scale))
        channels = [
            np.array(
                Image.fromarray(self.raw_linear[:, :, c], mode="F")
                     .resize((pw, ph), Image.LANCZOS),
                dtype=np.float32,
            )
            for c in range(3)
        ]
        img  = np.stack(channels, axis=2)
        temp = self.adjustments["temperature"].get() / 100.0
        tint = self.adjustments["tint"].get() / 100.0
        if temp or tint:
            img[:, :, 0] *= 1.0 + temp * 0.40
            img[:, :, 2] *= 1.0 - temp * 0.40
            img[:, :, 1] *= 1.0 + tint * 0.15
        exp = self.adjustments["exposure"].get()
        if exp:
            img *= 2.0 ** exp
        np.clip(img, 0.0, 1.0, out=img)
        img = np.where(img <= 0.0031308, img * 12.92,
                       1.055 * np.power(np.maximum(img, 1e-9), 1/2.4) - 0.055)
        lut = self.curve.get_lut()
        img = np.interp(img.ravel(), np.linspace(0.0, 1.0, 256), lut) \
                .reshape(img.shape).astype(np.float32)
        sat = self.adjustments["saturation"].get() / 100.0
        if sat:
            luma = (0.2126 * img[:, :, 0]
                    + 0.7152 * img[:, :, 1]
                    + 0.0722 * img[:, :, 2])[:, :, np.newaxis]
            img = luma + (1.0 + sat) * (img - luma)
        np.clip(img, 0.0, 1.0, out=img)
        return Image.fromarray((img * 255).astype(np.uint8))

    def render_full(self):
        if self.raw_linear is None:
            return None
        img  = self.raw_linear.copy()
        temp = self.adjustments["temperature"].get() / 100.0
        tint = self.adjustments["tint"].get() / 100.0
        if temp or tint:
            img[:, :, 0] *= 1.0 + temp * 0.40
            img[:, :, 2] *= 1.0 - temp * 0.40
            img[:, :, 1] *= 1.0 + tint * 0.15
        exp = self.adjustments["exposure"].get()
        if exp:
            img *= 2.0 ** exp
        np.clip(img, 0.0, 1.0, out=img)
        img = np.where(img <= 0.0031308, img * 12.92,
                       1.055 * np.power(np.maximum(img, 1e-9), 1/2.4) - 0.055)
        lut = self.curve.get_lut()
        img = np.interp(img.ravel(), np.linspace(0.0, 1.0, 256), lut) \
                .reshape(img.shape).astype(np.float32)
        sat = self.adjustments["saturation"].get() / 100.0
        if sat:
            luma = (0.2126 * img[:, :, 0]
                    + 0.7152 * img[:, :, 1]
                    + 0.0722 * img[:, :, 2])[:, :, np.newaxis]
            img = luma + (1.0 + sat) * (img - luma)
        np.clip(img, 0.0, 1.0, out=img)
        return Image.fromarray((img * 255).astype(np.uint8))

    def _rebuild_preview(self):
        if self.raw_linear is None:
            return
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw < 2 or ch < 2:
            return
        h, w  = self.raw_linear.shape[:2]
        scale = min(cw / w, ch / h)
        pw    = max(1, int(w * scale))
        ph    = max(1, int(h * scale))
        channels = [
            np.array(
                Image.fromarray(self.raw_linear[:, :, c], mode="F")
                     .resize((pw, ph), Image.LANCZOS),
                dtype=np.float32,
            )
            for c in range(3)
        ]
        self.preview_linear = np.stack(channels, axis=2)
        self._apply_and_display()

    def toggle_before_after(self):
        self._show_before = not self._show_before
        self._ba_btn.config(
            text="After  [\\]" if self._show_before else "Before  [\\]")
        self._apply_and_display()

    def _apply_and_display(self):
        self._update_pending = False
        if self.preview_linear is None:
            return

        if self._show_before:
            img = np.clip(self.preview_linear, 0.0, 1.0)
            img = np.where(img <= 0.0031308, img * 12.92,
                           1.055 * np.power(np.maximum(img, 1e-9), 1/2.4) - 0.055)
            self._blit(np.clip(img, 0.0, 1.0))
            return

        img = self.preview_linear.copy()

        temp = self.adjustments["temperature"].get() / 100.0
        tint = self.adjustments["tint"].get() / 100.0
        if temp or tint:
            img[:, :, 0] *= 1.0 + temp * 0.40
            img[:, :, 2] *= 1.0 - temp * 0.40
            img[:, :, 1] *= 1.0 + tint * 0.15

        exp = self.adjustments["exposure"].get()
        if exp:
            img *= 2.0 ** exp

        np.clip(img, 0.0, 1.0, out=img)

        img = np.where(img <= 0.0031308, img * 12.92,
                       1.055 * np.power(np.maximum(img, 1e-9), 1/2.4) - 0.055)

        lut = self.curve.get_lut()
        img = np.interp(img.ravel(), np.linspace(0.0, 1.0, 256), lut) \
                .reshape(img.shape).astype(np.float32)

        sat = self.adjustments["saturation"].get() / 100.0
        if sat:
            luma = (0.2126 * img[:, :, 0]
                    + 0.7152 * img[:, :, 1]
                    + 0.0722 * img[:, :, 2])[:, :, np.newaxis]
            img  = luma + (1.0 + sat) * (img - luma)

        self._blit(np.clip(img, 0.0, 1.0))

    def _blit(self, img):
        pil = Image.fromarray((img * 255).astype(np.uint8))
        self.photo_image = ImageTk.PhotoImage(pil)
        self.canvas.delete("img")
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.create_image(cw // 2, ch // 2,
                                 image=self.photo_image, anchor="center", tags="img")

    def _schedule_update(self):
        if not self._update_pending and self.preview_linear is not None:
            self._update_pending = True
            self.after(20, self._apply_and_display)

    def _on_canvas_resize(self, _event):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(150, self._rebuild_preview)

    def reset_adjustments(self):
        for key, var in self.adjustments.items():
            var.set(0.0)
            disp, fmt = self._disp_vars[key]
            disp.set(fmt % 0.0)
        self.curve.reset()
        if self._show_before:
            self._show_before = False
            self._ba_btn.config(text="Before  [\\]")
        self._apply_and_display()


# ── App coordinator ───────────────────────────────────────────────────────────

class App:
    def __init__(self, root):
        self.root = root
        root.title("Raw Viewer")
        root.geometry("1280x800")
        root.minsize(800, 550)

        menubar   = tk.Menu(root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Open Folder…", command=self._cmd_open_folder,
                              accelerator="Ctrl+Shift+O")
        file_menu.add_command(label="Open File…",   command=self._cmd_open_file,
                              accelerator="Ctrl+O")
        file_menu.add_separator()
        file_menu.add_command(label="Export…",      command=self._cmd_export,
                              accelerator="Ctrl+E")
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)
        root.config(menu=menubar)

        root.rowconfigure(0, weight=1)
        root.rowconfigure(1, weight=0)
        root.columnconfigure(0, weight=1)

        self._status = tk.StringVar(value="Open a folder to browse, or a file to edit directly")
        ttk.Label(root, textvariable=self._status, anchor="w",
                  padding=(8, 2), relief="sunken").grid(row=1, column=0, sticky="ew")

        self._gallery = GalleryView(root, open_cb=self._open_in_editor,
                                    set_status=self._status.set)
        self._editor  = EditorView(root, gallery_cb=self._show_gallery,
                                   set_status=self._status.set)

        # Stack both in the same cell; toggle visibility to switch views
        self._gallery.grid(row=0, column=0, sticky="nsew")
        self._editor.grid(row=0, column=0, sticky="nsew")

        root.bind("<Control-o>",       lambda _e: self._cmd_open_file())
        root.bind("<Control-O>",       lambda _e: self._cmd_open_folder())
        root.bind("<Control-e>",       lambda _e: self._cmd_export())
        root.bind("<backslash>",       self._on_backslash)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        session = _load_session()
        if "edit_states" in session:
            self._editor._edit_states = {
                path: {**s, "curve_points": [tuple(p) for p in s["curve_points"]]}
                for path, s in session["edit_states"].items()
            }

        self._thumb_cache: dict = {}
        for path, b64 in session.get("thumbs", {}).items():
            try:
                self._thumb_cache[path] = Image.open(io.BytesIO(base64.b64decode(b64)))
            except Exception:
                pass

        self._show_gallery()

        last = session.get("last_folder")
        if last and os.path.isdir(last):
            self._gallery._load_done_cb = self._apply_cached_thumbs
            root.after(100, lambda: self._gallery._load_folder(last))

    def _apply_cached_thumbs(self):
        for path, pil in self._thumb_cache.items():
            self._gallery.update_thumbnail(path, pil)

    def _save_session_now(self):
        if self._editor._current_path:
            self._editor._save_state()
        states = {}
        for path, s in self._editor._edit_states.items():
            states[path] = {**s, "curve_points": [list(p) for p in s["curve_points"]]}
        thumbs = {}
        for path, pil in self._thumb_cache.items():
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            thumbs[path] = base64.b64encode(buf.getvalue()).decode()
        _write_session({
            "last_folder": self._gallery._current_folder,
            "edit_states": states,
            "thumbs":      thumbs,
        })

    def _on_close(self):
        self._save_session_now()
        self.root.destroy()

    def _show_gallery(self):
        path = self._editor._current_path
        if path is not None:
            thumb = self._editor.render_thumbnail(THUMB_W, THUMB_H)
            if thumb is not None:
                self._gallery.update_thumbnail(path, thumb)
                self._thumb_cache[path] = thumb
        self._save_session_now()
        self._editor.grid_remove()
        self._gallery.grid()
        self.root.title("Raw Viewer — Gallery")

    def _open_in_editor(self, path):
        self._gallery.grid_remove()
        self._editor.grid()
        self._editor.load(path)
        self.root.title(f"Raw Viewer — {os.path.basename(path)}")

    def _cmd_open_folder(self):
        self._show_gallery()
        self._gallery._load_done_cb = self._apply_cached_thumbs
        self._gallery.open_folder()
        if self._gallery._current_folder:
            self._save_session_now()

    def _cmd_open_file(self):
        path = filedialog.askopenfilename(
            title="Open Raw File",
            filetypes=[("Raw images", RAW_GLOB), ("All files", "*.*")],
        )
        if path:
            self._open_in_editor(path)

    def _cmd_export(self):
        if self._editor.raw_linear is None:
            messagebox.showinfo("Export", "Open a raw file first.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Export")
        dlg.resizable(False, False)
        dlg.grab_set()
        dlg.transient(self.root)

        outer = ttk.Frame(dlg, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)

        # Format
        fmt_var = tk.StringVar(value="JPEG")
        fmt_row = ttk.Frame(outer)
        fmt_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(fmt_row, text="Format", width=10, anchor="w").pack(side=tk.LEFT)
        ttk.Radiobutton(fmt_row, text="JPEG", variable=fmt_var,
                        value="JPEG", command=lambda: _on_fmt()).pack(side=tk.LEFT)
        ttk.Radiobutton(fmt_row, text="PNG",  variable=fmt_var,
                        value="PNG",  command=lambda: _on_fmt()).pack(side=tk.LEFT, padx=(8, 0))

        # Quality
        quality_var = tk.IntVar(value=92)
        q_row = ttk.Frame(outer)
        q_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(q_row, text="Quality", width=10, anchor="w").pack(side=tk.LEFT)
        q_disp = ttk.Label(q_row, text="92", width=4, anchor="e")
        q_disp.pack(side=tk.RIGHT)
        ttk.Scale(q_row, variable=quality_var, from_=1, to=100, orient=tk.HORIZONTAL,
                  command=lambda v: q_disp.config(text=str(int(float(v))))).pack(
                      side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        def _on_fmt():
            if fmt_var.get() == "PNG":
                q_row.pack_forget()
            else:
                q_row.pack(fill=tk.X, pady=(0, 10), before=btn_row)

        ttk.Separator(outer, orient="horizontal").pack(fill=tk.X, pady=(0, 10))

        btn_row = ttk.Frame(outer)
        btn_row.pack(fill=tk.X)
        ttk.Button(btn_row, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT)

        def do_export():
            fmt = fmt_var.get()
            ext = ".jpg" if fmt == "JPEG" else ".png"
            stem = os.path.splitext(os.path.basename(self._editor._current_path))[0]
            out = filedialog.asksaveasfilename(
                parent=dlg,
                title="Export",
                defaultextension=ext,
                initialfile=stem + ext,
                filetypes=[("JPEG", "*.jpg *.jpeg"), ("PNG", "*.png")],
            )
            if not out:
                return
            dlg.destroy()
            self._status.set("Exporting…")
            self.root.update_idletasks()
            pil = self._editor.render_full()
            kw  = {"quality": quality_var.get(), "optimize": True} if fmt == "JPEG" else {}
            pil.save(out, format=fmt, **kw)
            self._status.set(f"Exported → {os.path.basename(out)}")

        ttk.Button(btn_row, text="Export", command=do_export).pack(side=tk.RIGHT)

    def _on_backslash(self, _e):
        if self._editor.winfo_ismapped():
            self._editor.toggle_before_after()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
