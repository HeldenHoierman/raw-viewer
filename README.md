# Raw Viewer — Developer Handoff

A single-file Python/Tkinter raw photo viewer and editor.

## Running

```
pip install rawpy Pillow numpy
python raw_viewer.py
```

Tested on Python 3.14, Windows 11.

## File structure

Everything lives in `raw_viewer.py`. No other source files.

```
raw_viewer.py
├── ToneCurveWidget   — standalone interactive curve editor widget
├── GalleryView       — scrollable thumbnail grid (ttk.Frame)
├── EditorView        — raw editor (ttk.Frame)
└── App               — coordinates the two views, owns the window/menu/status bar
```

## Architecture

`App` stacks `GalleryView` and `EditorView` in the same grid cell (`row=0, col=0`) and toggles between them using `grid_remove()` / `grid()`. The widget not currently shown is hidden but retains its state.

```python
self._show_gallery()   # editor.grid_remove(), gallery.grid()
self._open_in_editor() # gallery.grid_remove(), editor.grid()
```

Gallery thumbnails persist across editor round-trips — the GalleryView is never rebuilt.

## Image processing pipeline

`EditorView` holds two arrays:
- `raw_linear` — full-resolution float32 [0,1] linear-light RGB, decoded once on file open with `gamma=(1,1)` via rawpy.
- `preview_linear` — `raw_linear` downsampled to the current canvas size (per-channel float32 via PIL mode "F"). All edits operate on this.

Pipeline in `_apply_and_display` (all ops are on `preview_linear.copy()`):

1. **White balance** — per-channel multipliers in linear light. Temp ±100 → R×(1±0.4), B×(1∓0.4). Tint ±100 → G×(1±0.15).
2. **Exposure** — `img *= 2 ** stops` in linear light.
3. **Clip** to [0,1].
4. **sRGB gamma** — piecewise formula, exact IEC 61966-2-1.
5. **Tone curve** — 256-entry LUT via `np.interp`, applied in gamma space.
6. **Saturation** — luma-preserving: `luma + (1 + sat) * (img - luma)` using Rec.709 weights.
7. **Clip** and blit.

The "before" toggle skips steps 1–6 and just applies gamma to `preview_linear`.

## Tone curve (ToneCurveWidget)

Control points stored as sorted `(x, y)` tuples in [0,1]×[0,1]. Interpolated with a **PCHIP monotone cubic spline** (Fritsch-Carlson) implemented in `_build_lut`, producing a 256-entry float32 LUT. With ≤2 points, falls back to `np.interp` (linear).

- Left-click empty area → add point (x clamped to [0.01, 0.99])
- Drag → move point (endpoints locked to x=0 and x=1)
- Right-click non-endpoint → remove

`_PAD = 5` (equals `_R`, the dot radius) puts endpoint dots flush with the widget edge.

## Gallery thumbnail loading

Thumbnails load on a **thread pool** (`concurrent.futures.ThreadPoolExecutor`, workers = CPU count). The main thread polls a `queue.Queue` every 80 ms via `after()` and adds cells to the grid as they arrive.

Each raw file is decoded via `raw.postprocess(half_size=True, gamma=(1,1))` — the same linear decode the editor uses — followed by the identical piecewise sRGB gamma formula. This means an unedited gallery thumbnail is pixel-identical to what you see when you first open the photo, making edits immediately visible when returning to the gallery.

Results are emitted in filename-sorted order: as futures complete out of order, they are held in a `pending` dict and flushed as soon as all earlier indices are ready.

`PhotoImage` objects are kept in `self._photos` (a list on `GalleryView`) to prevent garbage collection — Tkinter drops images that have no Python reference.

Hover highlight uses `highlightbackground` on the cell Frame. The leave handler checks pointer coordinates against the cell bounds to avoid flicker when the mouse crosses into child widgets.

## Per-photo edit state

Each photo's adjustments (exposure, temperature, tint, saturation, curve control points) are stored in `EditorView._edit_states`, a dict keyed by file path.

- `_save_state()` is called at the top of `load()`, capturing the outgoing photo's live `DoubleVar` values and curve points.
- `_restore_state(path)` is called after the new raw is decoded. It writes values directly into the `DoubleVar`s and redraws the curve widget without firing `on_change`, avoiding a stale-preview flash.
- On `_show_gallery()`, `App` calls `editor.render_thumbnail(THUMB_W, THUMB_H)` and pushes the result to `gallery.update_thumbnail(path, pil)`, replacing the cell's `PhotoImage` in-place.

`render_thumbnail` runs the full edit pipeline on a freshly downsampled copy of `raw_linear`, independent of the canvas-sized `preview_linear`.

## Key bindings

| Key | Action |
|-----|--------|
| `Ctrl+O` | Open single raw file → editor |
| `Ctrl+Shift+O` | Open folder → gallery |
| `\` | Toggle before/after (editor only) |

`\` is bound at the root level in `App._on_backslash` and only fires `toggle_before_after()` when the editor is mapped.

## Known issues / history

- Shadow slider strength reduced from 0.15 → 0.08 (user found original too aggressive).
- There is no export. Editing is preview-only.
- Opening a file from within the editor via "Open Raw File…" does not update the window title (only gallery → editor navigations do).
- Tone curve operates in gamma space (after sRGB encoding), not linear light.
- Gallery thumbnail decode is slower than embedded-JPEG extraction; large folders take longer to populate.

## What's not built yet

- Export to JPEG / TIFF
- Keyboard navigation in gallery (arrow keys, Enter to open)
- Zoom / pan in editor canvas
- Remembering the last-opened folder across sessions
- Persisting edit states to disk across sessions
