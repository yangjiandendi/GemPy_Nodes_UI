from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd


SURFACE_REQUIRED = ["X", "Y", "Z", "formation"]
ORIENTATION_REQUIRED_GRADIENT = ["X", "Y", "Z", "G_x", "G_y", "G_z", "formation"]
ORIENTATION_REQUIRED_AZIMUTH_DIP = ["X", "Y", "Z", "azimuth", "dip", "formation"]


def _find_col(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    lookup = {str(c).strip().lower(): str(c) for c in df.columns}
    for c in candidates:
        key = str(c).strip().lower()
        if key in lookup:
            return lookup[key]
    return None


def _missing(df: pd.DataFrame, cols: Sequence[str]) -> List[str]:
    return [c for c in cols if c not in df.columns]


def _orientation_column_mode(df: pd.DataFrame) -> Dict[str, Any]:
    gx = _find_col(df, ["G_x", "Gx", "gx", "g_x"])
    gy = _find_col(df, ["G_y", "Gy", "gy", "g_y"])
    gz = _find_col(df, ["G_z", "Gz", "gz", "g_z"])
    azimuth = _find_col(df, ["azimuth", "az", "dip_azimuth"])
    dip = _find_col(df, ["dip"])
    polarity = _find_col(df, ["polarity", "pole", "direction"])

    has_gradient = bool(gx and gy and gz)
    has_azimuth_dip = bool(azimuth and dip)

    if has_gradient:
        return {
            "mode": "gradient",
            "ok": True,
            "columns": {"G_x": gx, "G_y": gy, "G_z": gz},
            "missing_orientation_columns": [],
        }
    if has_azimuth_dip:
        return {
            "mode": "azimuth_dip",
            "ok": True,
            "columns": {"azimuth": azimuth, "dip": dip, "polarity": polarity},
            "missing_orientation_columns": [],
        }

    missing_gradient = [name for name, found in [("G_x", gx), ("G_y", gy), ("G_z", gz)] if not found]
    missing_azdip = [name for name, found in [("azimuth", azimuth), ("dip", dip)] if not found]
    return {
        "mode": "unknown",
        "ok": False,
        "columns": {},
        "missing_orientation_columns": {
            "gradient_format_missing": missing_gradient,
            "azimuth_dip_format_missing": missing_azdip,
        },
    }


def geological_table_report(df: pd.DataFrame, kind: str) -> Dict[str, Any]:
    kind_norm = str(kind or "").lower()
    is_orientation = kind_norm in {"orientations", "fault_orientations", "orientation"}

    required = SURFACE_REQUIRED if not is_orientation else ["X", "Y", "Z", "formation"]
    missing = _missing(df, required)

    report: Dict[str, Any] = {
        "kind": kind,
        "ok": not missing,
        "rows": int(len(df)),
        "columns": list(map(str, df.columns)),
        "required_columns": required,
        "missing_required_columns": missing,
        "warnings": [],
    }

    if is_orientation:
        orientation_mode = _orientation_column_mode(df)
        report["orientation_format"] = orientation_mode["mode"]
        report["orientation_columns"] = orientation_mode.get("columns", {})
        report["accepted_orientation_formats"] = [
            "X,Y,Z,formation,G_x,G_y,G_z",
            "X,Y,Z,formation,azimuth,dip,polarity(optional)",
        ]
        if not orientation_mode["ok"]:
            report["missing_orientation_columns"] = orientation_mode["missing_orientation_columns"]

    if missing:
        return report

    xyz = df[["X", "Y", "Z"]].apply(pd.to_numeric, errors="coerce")
    bad_xyz = int(xyz.isna().any(axis=1).sum())
    report["xyz_missing_or_non_numeric_rows"] = bad_xyz
    if bad_xyz:
        report["warnings"].append(f"{bad_xyz} row(s) have missing or non-numeric X/Y/Z values.")

    if "formation" in df.columns:
        empty_form = int(df["formation"].astype(str).str.strip().eq("").sum())
        report["empty_formation_rows"] = empty_form
        report["formation_counts"] = df["formation"].astype(str).value_counts().to_dict()
        if empty_form:
            report["warnings"].append(f"{empty_form} row(s) have empty formation names.")

    if is_orientation:
        if not report.get("orientation_format") or report.get("orientation_format") == "unknown":
            report["ok"] = False
            return report

        if report["orientation_format"] == "gradient":
            cols = report["orientation_columns"]
            grads = df[[cols["G_x"], cols["G_y"], cols["G_z"]]].apply(pd.to_numeric, errors="coerce")
            bad_grad = int(grads.isna().any(axis=1).sum())
            zero_grad = int((grads.fillna(0).abs().sum(axis=1) == 0).sum())
            report["gradient_missing_or_non_numeric_rows"] = bad_grad
            report["zero_gradient_rows"] = zero_grad
            if bad_grad:
                report["warnings"].append(f"{bad_grad} row(s) have missing or non-numeric G_x/G_y/G_z values.")
            if zero_grad:
                report["warnings"].append(f"{zero_grad} row(s) have zero orientation vectors.")
        elif report["orientation_format"] == "azimuth_dip":
            cols = report["orientation_columns"]
            ad = df[[cols["azimuth"], cols["dip"]]].apply(pd.to_numeric, errors="coerce")
            bad_ad = int(ad.isna().any(axis=1).sum())
            report["azimuth_dip_missing_or_non_numeric_rows"] = bad_ad
            if bad_ad:
                report["warnings"].append(f"{bad_ad} row(s) have missing or non-numeric azimuth/dip values.")
            if cols.get("polarity"):
                pol = pd.to_numeric(df[cols["polarity"]], errors="coerce")
                bad_pol = int(pol.isna().sum())
                report["polarity_missing_or_non_numeric_rows"] = bad_pol
                if bad_pol:
                    report["warnings"].append(f"{bad_pol} row(s) have missing or non-numeric polarity values; these will default to +1 during conversion.")

    report["ok"] = not missing and bad_xyz == 0
    if is_orientation:
        if report.get("orientation_format") == "gradient":
            report["ok"] = report["ok"] and report.get("gradient_missing_or_non_numeric_rows", 0) == 0
        elif report.get("orientation_format") == "azimuth_dip":
            report["ok"] = report["ok"] and report.get("azimuth_dip_missing_or_non_numeric_rows", 0) == 0
        else:
            report["ok"] = False
    return report


def validate_or_raise(df: pd.DataFrame, kind: str, error_prefix: str) -> Dict[str, Any]:
    from .models import NodeExecutionError

    report = geological_table_report(df, kind)
    errors = []
    if report.get("missing_required_columns"):
        errors.append("missing required columns: " + ", ".join(report["missing_required_columns"]))

    kind_norm = str(kind or "").lower()
    is_orientation = kind_norm in {"orientations", "fault_orientations", "orientation"}

    if is_orientation and report.get("orientation_format") == "unknown":
        errors.append(
            "missing orientation columns. Accepted formats are either "
            "G_x/G_y/G_z or azimuth/dip/polarity(optional)"
        )

    if report.get("xyz_missing_or_non_numeric_rows", 0):
        errors.append(f"{report['xyz_missing_or_non_numeric_rows']} row(s) have missing/non-numeric X/Y/Z")

    if is_orientation:
        if report.get("gradient_missing_or_non_numeric_rows", 0):
            errors.append(f"{report['gradient_missing_or_non_numeric_rows']} row(s) have missing/non-numeric G_x/G_y/G_z")
        if report.get("azimuth_dip_missing_or_non_numeric_rows", 0):
            errors.append(f"{report['azimuth_dip_missing_or_non_numeric_rows']} row(s) have missing/non-numeric azimuth/dip")

    if errors:
        raise NodeExecutionError(f"{error_prefix}: " + "; ".join(errors))
    return report
