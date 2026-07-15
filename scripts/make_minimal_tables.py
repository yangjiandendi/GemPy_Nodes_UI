from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
out = ROOT / "minimal_data"
out.mkdir(exist_ok=True)

rng = np.random.default_rng(42)
formations = ["LayerA", "LayerB", "LayerC"]
rows = []
oris = []
for i, f in enumerate(formations):
    x = rng.uniform(0, 1000, 60)
    y = rng.uniform(0, 1000, 60)
    z = 100 - i * 120 + 0.03 * x - 0.01 * y + rng.normal(0, 8, 60)
    for xi, yi, zi in zip(x, y, z):
        rows.append({"X": xi, "Y": yi, "Z": zi, "formation": f})
    for xi, yi, zi in zip(x[::12], y[::12], z[::12]):
        oris.append({"X": xi, "Y": yi, "Z": zi, "G_x": -0.03, "G_y": 0.01, "G_z": 1.0, "formation": f})

pd.DataFrame(rows).to_csv(out / "surface_points.csv", index=False)
pd.DataFrame(oris).to_csv(out / "orientations.csv", index=False)
print(f"Wrote {out / 'surface_points.csv'}")
print(f"Wrote {out / 'orientations.csv'}")
