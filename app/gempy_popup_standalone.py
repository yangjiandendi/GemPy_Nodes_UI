#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path


def _patch_pyvista_add_mesh_positional_color() -> None:
    try:
        import pyvista as pv
        original = pv.Plotter.add_mesh
        if getattr(original, "_node_editor_color_patch", False):
            return

        def add_mesh_compat(self, mesh, *args, **kwargs):
            if args and isinstance(args[0], str) and "color" not in kwargs:
                kwargs["color"] = args[0]
                args = args[1:]
            return original(self, mesh, *args, **kwargs)

        add_mesh_compat._node_editor_color_patch = True
        pv.Plotter.add_mesh = add_mesh_compat
    except Exception:
        pass


def _load_model(path: str | Path):
    with open(path, "rb") as f:
        return pickle.load(f)


def _parse_styles(raw: str):
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def run_gempy_3d(cfg: dict) -> None:
    _patch_pyvista_add_mesh_positional_color()
    import gempy_viewer as gpv

    geo_model = _load_model(cfg["model_pickle"])
    kwargs = {
        "show_data": bool(cfg.get("show_data", True)),
        "show_lith": bool(cfg.get("show_lith", True)),
        "show_surfaces": bool(cfg.get("show_surfaces", True)),
        "show_topography": bool(cfg.get("show_topography", True)),
        "show_boundaries": bool(cfg.get("show_boundaries", True)),
    }

    # GemPy's documented plot_3d accepts show_* flags through **kwargs and
    # DataToShow.  Keep show_boundaries in the first and preferred call.
    #
    # The previous fallback could accidentally remove show_boundaries after an
    # unrelated TypeError, making the checkbox look ineffective.
    try:
        gpv.plot_3d(geo_model, **kwargs)
        return
    except TypeError as first_exc:
        # Compatibility fallback for older/variant gempy_viewer versions:
        # retry with a conservative set while still preserving show_boundaries.
        conservative = {
            "show_data": kwargs["show_data"],
            "show_lith": kwargs["show_lith"],
            "show_boundaries": kwargs["show_boundaries"],
        }
        try:
            gpv.plot_3d(geo_model, **conservative)
            return
        except TypeError:
            # Final fallback: only this loses the flag, and only for versions
            # whose plot_3d cannot accept show kwargs at all.
            try:
                gpv.plot_3d(geo_model)
                return
            except TypeError:
                raise first_exc


def run_clipped_layers(cfg: dict) -> None:
    _patch_pyvista_add_mesh_positional_color()
    import numpy as np
    import pyvista as pv
    import gempy_viewer as gpv

    geo_model = _load_model(cfg["model_pickle"])
    clip_path = Path(cfg["clip_path"]) if cfg.get("clip_path") else None
    top_path = Path(cfg["top_path"]) if cfg.get("top_path") else None
    cell_data_name = cfg.get("cell_data_name", "id")

    plot_obj = None
    errors = []
    candidate_kwargs = [
        {"show": False, "show_data": False, "show_lith": True, "show_surfaces": False, "show_topography": False},
        {"show_data": False, "show_lith": True, "show_surfaces": False, "show_topography": False},
        {},
    ]
    for kwargs in candidate_kwargs:
        try:
            plot_obj = gpv.plot_3d(geo_model, **kwargs)
            break
        except TypeError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(str(exc))
    if plot_obj is None:
        raise RuntimeError("Could not obtain GemPy 3D plot object. " + " | ".join(errors[-3:]))

    actor = getattr(plot_obj, "regular_grid_actor", None)
    if actor is None and hasattr(plot_obj, "plotter"):
        actor = getattr(plot_obj.plotter, "regular_grid_actor", None)
    if actor is None:
        raise RuntimeError("The GemPy 3D plot object has no regular_grid_actor.")

    box = pv.wrap(actor.GetMapper().GetInput())
    vol = box

    shell = None
    if clip_path is not None:
        shell = pv.read(str(clip_path))
        vol = vol.clip_surface(shell, invert=bool(cfg.get("invert", False)), crinkle=bool(cfg.get("crinkle", True)))

    top = None
    if top_path is not None:
        top = pv.read(str(top_path))
        if bool(cfg.get("topography_clip_enabled", True)):
            if bool(cfg.get("crop_to_topography_xy", True)):
                try:
                    b = top.bounds
                    centers = vol.cell_centers().points
                    ids_xy = np.where(
                        (centers[:, 0] >= float(b[0])) & (centers[:, 0] <= float(b[1])) &
                        (centers[:, 1] >= float(b[2])) & (centers[:, 1] <= float(b[3]))
                    )[0]
                    if ids_xy.size:
                        vol = vol.extract_cells(ids_xy)
                except Exception:
                    pass
            vol = vol.clip_surface(top, invert=bool(cfg.get("topography_invert", False)), crinkle=bool(cfg.get("crinkle", True)))

    if cell_data_name not in vol.cell_data:
        raise RuntimeError(f"Clipped volume has no cell_data '{cell_data_name}'. Available cell arrays: {list(vol.cell_data.keys())}")

    p = pv.Plotter(notebook=False)

    if bool(cfg.get("show_base_gempy", False)):
        p.add_mesh(box, opacity=0.08, show_edges=False)
    if bool(cfg.get("show_clip_mesh", False)) and shell is not None:
        p.add_mesh(shell, opacity=0.18, show_edges=False)
    if bool(cfg.get("show_topography_mesh", False)) and top is not None:
        p.add_mesh(top, opacity=0.22, show_edges=False)

    styles = _parse_styles(cfg.get("layer_styles", ""))
    if not styles:
        styles = [{"id": int(v), "label": f"layer_{int(v)}", "color": "", "opacity": 0.5}
                  for v in np.unique(vol.cell_data[cell_data_name])]

    for row in styles:
        try:
            layer_id = int(row.get("id"))
        except Exception:
            continue
        idx = np.where(vol.cell_data[cell_data_name] == layer_id)[0]
        if idx.size == 0:
            continue
        layer = vol.extract_cells(idx)
        kwargs = {"opacity": float(row.get("opacity", 0.5)), "show_edges": bool(cfg.get("show_edges", False))}
        color = str(row.get("color") or "").strip()
        if color:
            kwargs["color"] = color
        p.add_mesh(layer, **kwargs)

    p.add_axes()
    p.show_grid()
    p.add_title(f"Clipped GemPy layers | cell_data={cell_data_name}")
    p.show()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    mode = cfg.get("mode")
    if mode == "gempy_3d":
        run_gempy_3d(cfg)
    elif mode == "clipped_layers":
        run_clipped_layers(cfg)
    else:
        raise ValueError(f"Unknown popup mode: {mode}")


if __name__ == "__main__":
    main()
