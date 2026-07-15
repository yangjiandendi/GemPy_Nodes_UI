# GemPy Node Editor

**Version:** 0.1  
**Status:** research prototype / local workflow tool  
**Main purpose:** node-based geological modelling, GemPy workflow execution, mesh/voxel processing, visualization and export for GeoDT-style structural model workflows.

GemPy Node Editor is a local browser-based workflow editor for building, checking, computing, visualizing, clipping, voxelizing, merging and exporting geological models. It turns notebook-style workflows into a visual node graph where tables, meshes, GemPy models, PyVista objects, voxel grids, KADI files and exported outputs can be connected and reused.

The application is designed for reproducible geological modelling workflows rather than one-off manual processing. It is especially useful when a structural model has to pass through multiple steps: input table preparation, GemPy model construction, structural-frame configuration, computation, clipping, voxel-grid conversion, boundary extraction and export for simulation or visualization.

---

## Table of contents

- [Relationship to GemPy](#relationship-to-gempy)
- [Main features](#main-features)
- [Repository structure](#repository-structure)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the application](#running-the-application)
- [User interface overview](#user-interface-overview)
- [Data conventions](#data-conventions)
- [Recommended workflows](#recommended-workflows)
- [Execution, caching and reproducibility](#execution-caching-and-reproducibility)
- [File management and portable projects](#file-management-and-portable-projects)
- [KADI integration](#kadi-integration)
- [Node reference](#node-reference)
- [Input nodes](#input-nodes)
  - [Load Uploaded File](#load-uploaded-file-loaduploadedfile)
  - [Load GemPy Model JSON](#load-gempy-model-json-loadgempymodeljson)
  - [Load from Kadi](#load-from-kadi-loadkadifile)
- [Data nodes](#data-nodes)
  - [Merge Tables](#merge-tables-mergetables)
  - [Validate Geological Table](#validate-geological-table-validategeotable)
  - [Convert Orientation Angles](#convert-orientation-angles-convertorientationangles)
  - [Clean Geological Table](#clean-geological-table-cleangeotable)
  - [Sample Geological Data](#sample-geological-data-samplegeotable)
- [GemPy nodes](#gempy-nodes)
  - [Interactive GeoModel Data Editor](#interactive-geomodel-data-editor-interactivegempymodeldataeditor)
  - [Create GemPy Model](#create-gempy-model-creategempymodel)
  - [Configure Structural Frame](#configure-structural-frame-configurestructuralframe)
  - [Set Finite Fault](#set-finite-fault-setfinitefault)
  - [Add Surface Points](#add-surface-points-addsurfacepoints)
  - [Auto Orientations](#auto-orientations-autoorientations)
  - [Set / Preview Topography](#set--preview-topography-settopography)
  - [Set GemPy Grid](#set-gempy-grid-setgempygrid)
  - [Set Interpolation Options](#set-interpolation-options-setgempyoptions)
  - [Compute GemPy Model](#compute-gempy-model-computegempymodel)
  - [Extract GemPy Array](#extract-gempy-array-extractgempyarray)
- [View nodes](#view-nodes)
  - [Visualization](#visualization-visualization)
  - [Plot GemPy 2D](#plot-gempy-2d-plotgempy2d)
  - [Plot GemPy 3D](#plot-gempy-3d-plotgempy3d)
  - [Clipping Tool](#clipping-tool-pyvistaclippedlayerviewer)
  - [Thicken Mesh](#thicken-mesh-thickenmesh)
  - [Combine Meshes](#combine-meshes-combinemeshes)
- [Mesh / Voxel nodes](#mesh-voxel-nodes)
  - [Mesh to Voxel Model](#mesh-to-voxel-model-meshtovoxelmodel)
  - [Clip Voxel Model by Mask](#clip-voxel-model-by-mask-clipvoxelmodelbymask)
  - [Merge Voxel Models](#merge-voxel-models-mergevoxelmodels)
- [Output nodes](#output-nodes)
  - [Extract Voxel Boundaries](#extract-voxel-boundaries-extractvoxelboundaries)
  - [Hex Mesh to Voxel Grid](#hex-mesh-to-voxel-grid-hexmeshtovoxelgrid)
  - [Save Table CSV](#save-table-csv-savetablecsv)
  - [Save GemPy Model JSON](#save-gempy-model-json-savegempymodeljson)
  - [Save Array](#save-array-savearray)
  - [Upload GeoDT Structure Version to Kadi](#upload-geodt-structure-version-to-kadi-uploadgeodtstructureversiontokadi)
  - [Upload File to Kadi](#upload-file-to-kadi-uploadfiletokadi)
- [Troubleshooting](#troubleshooting)
- [Development notes](#development-notes)
- [License](#license)

---

## Relationship to GemPy

This project is **not a replacement for GemPy**. It is a workflow layer around GemPy and related Python tools.

GemPy is still responsible for the geological interpolation and the core `GeoModel` computation. GemPy Node Editor provides a browser interface and a graph executor around these tasks:

- pandas is used for table loading, cleaning, merging and sampling.
- GemPy is used for creating, configuring, editing, saving/loading and computing `GeoModel` objects.
- GemPy Viewer / PyVista are used for 2D/3D visualization when available.
- PyVista/VTK are used for mesh clipping, mesh thickening, voxel-grid conversion, voxel merging and boundary extraction.
- KADI integration is handled through `kadi_apy` where configured.

The node editor therefore helps with **workflow orchestration**:

```text
tables / meshes / KADI files
        ↓
preprocessing nodes
        ↓
GemPy GeoModel nodes
        ↓
GemPy compute_model
        ↓
visualization / clipping / voxelization
        ↓
boundary extraction / export / KADI upload
```

The editor stores intermediate Python objects only inside the running backend process. A `GeoModel` preview or PyVista popup may depend on a short-lived runtime token. If the server restarts, rerun the required upstream node chain.

The project follows GemPy's data model where possible. This means surface-point tables, orientation tables, structural groups, relations, topography and interpolation options should be understood in GemPy terms. When GemPy changes an API, the corresponding node may require adjustment.

---

## Main features

- Local FastAPI web application with browser-based node editor.
- Upload and organize files in user-defined categories.
- Drag uploaded files into the canvas as preconfigured `Load Uploaded File` nodes.
- Import/export portable projects with workflow graph and uploaded files.
- Run the full workflow or only the chain needed for a selected node.
- Visual execution states: queued, running, cached, done or failed.
- Stop button for cooperative cancellation between node executions.
- Node duplication via right click.
- Table workflows for geological surface points and orientations.
- Dip/azimuth to GemPy gradient-vector conversion.
- GemPy `GeoModel` creation, structural-frame mapping, topography, grids, finite faults, interpolation options and computation.
- 2D and 3D model visualization.
- Mesh clipping, mesh combination and mesh thickening.
- Mesh-to-voxel conversion for closed solids and distance-to-surface voxelization for faults/open sheet-like surfaces.
- True voxel-model merging with overlap priority, scalar reindexing and common-grid resampling.
- Voxel-model masking/clipping to reveal internal structures.
- Robust voxel boundary extraction using median-spacing quantized grid indexing.
- Save tables, arrays, GemPy JSON files and mesh/voxel outputs.
- KADI download/upload nodes for project data exchange.

---

## Repository structure

A typical repository layout is:

```text
gempy_node_editor/
├─ app/
│  ├─ main.py                       # FastAPI application and API endpoints
│  ├─ nodes.py                      # Node implementations
│  ├─ node_registry.py              # Node registry and UI metadata
│  ├─ graph_executor.py             # Dataflow execution, cache, progress and cancellation
│  ├─ storage.py                    # Upload/output workspace file handling
│  ├─ runtime_store.py              # Short-lived runtime object tokens
│  ├─ models.py                     # RuntimeValue and graph models
│  ├─ static/
│  │  ├─ index.html                 # Browser UI entry point
│  │  ├─ app.js                     # Node editor frontend
│  │  └─ style.css                  # UI styles
│  └─ workspace/                    # Created at runtime; uploads, outputs, tmp files
├─ scripts/
│  └─ make_minimal_tables.py
├─ requirements.txt                 # Core web/table dependencies
├─ requirements-gempy.txt           # Optional GemPy/PyVista/KADI dependencies
└─ README.md
```

`app/workspace/` is runtime data. It should usually not be committed to GitHub except for deliberately small example files.

---

## Requirements

### Core requirements

The core application requires Python and the packages in `requirements.txt`:

```text
fastapi
uvicorn[standard]
pydantic
python-multipart
pandas
numpy
openpyxl
scikit-learn
```

These packages are enough for the web app, table handling, project import/export, file upload and non-GemPy data nodes.

### Optional GemPy / PyVista / KADI requirements

Most geological modelling and mesh nodes require the optional packages in `requirements-gempy.txt`:

```text
gempy
kadi-apy
pyvista
```

PyTorch is used by GemPy when the `PYTORCH` backend is selected. Install the PyTorch build appropriate for your machine and CUDA setup. For CPU-only use, install a CPU-compatible PyTorch build.

Use a Python version supported by the GemPy version you install. When in doubt, create a clean virtual environment for this project.

---

## Installation

### 1. Clone the repository

```bash
git clone <your-repository-url>
cd gempy_node_editor
```

### 2. Create a virtual environment

Using `venv`:

```bash
python -m venv .venv
```

Activate it.

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Windows CMD:

```cmd
.venv\Scripts\activate.bat
```

Linux/macOS:

```bash
source .venv/bin/activate
```

Alternatively, using conda:

```bash
conda create -n gempy-node-editor python=3.11
conda activate gempy-node-editor
```

### 3. Upgrade pip

```bash
python -m pip install --upgrade pip
```

### 4. Install core dependencies

```bash
python -m pip install -r requirements.txt
```

### 5. Install GemPy/PyVista/KADI dependencies

```bash
python -m pip install -r requirements-gempy.txt
```

Install PyTorch separately if your GemPy installation or workflow requires it. For example, CPU-only PyTorch can be installed following the current PyTorch instructions for your operating system.

### 6. Verify imports

```bash
python - <<'PY'
import fastapi, pandas, numpy
print("Core dependencies OK")

try:
    import gempy
    print("GemPy OK:", getattr(gempy, "__version__", "unknown"))
except Exception as exc:
    print("GemPy not available:", exc)

try:
    import pyvista
    print("PyVista OK:", pyvista.__version__)
except Exception as exc:
    print("PyVista not available:", exc)
PY
```

---

## Running the application

From the repository root:

```bash
python -m uvicorn app.main:app --reload
```

Open the local web interface:

```text
http://127.0.0.1:8000
```

The server and browser run locally. PyVista popup windows are launched on the machine running the server.

If the browser still shows an older frontend after updating the code, refresh with:

```text
Ctrl + F5
```

---

## User interface overview

### Left panel: files

The left panel manages uploaded files. Files can be grouped into user-defined folders/categories.

Typical actions:

- Upload a file and optionally enter a folder/category.
- Click an uploaded file to create a `Load Uploaded File` node.
- Drag an uploaded file onto the canvas to create a `Load Uploaded File` node at that position.
- Use `Move folder` to reassign an uploaded file to a category.

### Center: canvas

The canvas contains workflow nodes and connections.

Common actions:

- Add a node from the node palette.
- Connect an output port to a compatible input port.
- Select a node to edit parameters.
- Right-click a node to duplicate it.
- Delete selected nodes with `Delete` or `Backspace`.
- Pan the canvas by dragging empty space.

### Right panel: inspector

The inspector shows parameters and results for the selected node.

### Top toolbar

Typical buttons include:

- `Run to Selected Node`: run only the upstream chain required by the selected node.
- `Run All`: run every node in the canvas.
- `Stop Run`: request cooperative cancellation.
- `Clear execution cache`: force recomputation.
- Project import/export controls.

---

## Data conventions

### Coordinate system

The node editor does not reproject coordinates. All tables and meshes in one workflow must already use the same coordinate reference system.

### Surface point table

A typical GemPy surface point table contains:

| Column | Meaning |
|---|---|
| `X` | Easting or x-coordinate |
| `Y` | Northing or y-coordinate |
| `Z` | Elevation/depth coordinate |
| `formation` | Formation or structural element name |

### Orientation table

GemPy orientation tables can use gradient vectors:

| Column | Meaning |
|---|---|
| `X`, `Y`, `Z` | Orientation location |
| `formation` | Formation or structural element name |
| `G_x`, `G_y`, `G_z` | Gradient vector components |

Alternatively, use `dip` and `azimuth` columns and convert them with `Convert Orientation Angles`.

### Topography table

A topography table should contain `X`, `Y`, `Z` columns.

### Mesh and voxel data

The mesh/voxel nodes work with PyVista-readable files such as:

```text
.vtk, .vtp, .vtu, .vti, .stl, .ply, .obj
```

Voxel-model workflows commonly use cell-data scalars such as:

```text
MaterialIDs
id
lith_block
layer_id
combined_element_id
merge_source_index
```

---

## Recommended workflows

### A. Table-based GemPy structural model

```text
Load Uploaded File(surface points)
Load Uploaded File(orientations)
        ↓
Clean Geological Table
        ↓
Validate Geological Table
        ↓
Sample Geological Data, optional
        ↓
Create GemPy Model
        ↓
Configure Structural Frame
        ↓
Set Topography, optional
        ↓
Set GemPy Grid, optional
        ↓
Set Interpolation Options, optional
        ↓
Compute GemPy Model
        ↓
Visualization / Plot GemPy 2D / Plot GemPy 3D
```

### B. Dip/azimuth orientation workflow

```text
Load Uploaded File(orientation table with dip + azimuth)
        ↓
Convert Orientation Angles
        ↓
Clean Geological Table
        ↓
Create GemPy Model.orientations
```

### C. Clipped GemPy volume to voxel boundaries

```text
Compute GemPy Model.geo_model
        ↓
Clipping Tool
        ↓
Hex Mesh to Voxel Grid or Mesh to Voxel Model
        ↓
Extract Voxel Boundaries
        ↓
Upload GeoDT Structure Version to Kadi, optional
```

### D. Fault or buffered surface to voxel model

```text
Load Uploaded File(fault surface / buffered fault mesh)
        ↓
Thicken Mesh, optional
        ↓
Mesh to Voxel Model
        ↓
Merge Voxel Models
```

For open or sheet-like fault surfaces, use:

```text
Voxelization mode = distance_to_surface
```

For closed solids, use:

```text
Voxelization mode = inside_surface
```

### E. Reveal internal voxel structures before merging

```text
large covering voxel model  → Clip Voxel Model by Mask.base_voxel_model
fault/small voxel model     → Clip Voxel Model by Mask.mask_voxel_model

Clip Voxel Model by Mask.voxel_model + fault/small voxel model
        ↓
Merge Voxel Models
```

### F. Reusable workflow blocks

Select connected nodes that form a repeated workflow, create a workflow/template block where supported by the frontend, and reuse it with different input nodes. This is useful when the same table-cleaning, GemPy or voxel-processing chain is needed several times.

---

## Execution, caching and reproducibility

The graph executor uses node IDs, node parameters and upstream inputs to decide whether a result can be reused.

- A node marked `queued` is waiting to run.
- A node marked `running` is currently executing.
- A node marked `cached` reused an existing result.
- A node marked `done` finished successfully.
- A node marked `failed` stopped with an error.

`Stop Run` is cooperative. It requests cancellation and stops before the next node starts. It cannot forcibly interrupt a long-running Python/GemPy function already executing inside a node.

Use `Clear execution cache` when:

- an upstream file was replaced,
- a runtime token is stale,
- you changed code and want fresh results,
- a result looks inconsistent with the current parameters.

---

## File management and portable projects

The runtime workspace stores uploaded files, output files and metadata:

```text
app/workspace/uploads/
app/workspace/outputs/
app/workspace/tmp/
app/workspace/files_index.json
app/workspace/file_categories.json
```

Uploaded-file categories are stored persistently in `file_categories.json`.

Project export creates a portable ZIP with:

- the workflow graph,
- node parameters,
- available uploaded input files.

Project import restores the workflow and imports bundled input files into the current workspace.

Generated outputs are usually accessed through the node result panel rather than through the upload list.

---

## KADI integration

KADI nodes require `kadi_apy` and a working KADI authentication setup.

Available KADI-related nodes:

- `Load from Kadi`
- `Upload File to Kadi`
- `Upload GeoDT Structure Version to Kadi`

Use `Load from Kadi` for table or mesh input files stored in a record. Use upload nodes for generated output files.

For authentication, configure `kadi_apy` in the way required by your KADI instance. Do not commit personal tokens or credentials to GitHub.

---

## Node reference

The following reference is generated from the current node registry and describes all nodes available in version 0.1.

### Port kinds

| Kind | Meaning |
|---|---|
| `table` | pandas DataFrame-like tabular data |
| `mesh` | PyVista/VTK mesh or voxel grid |
| `file` | File path managed by the backend workspace |
| `geo_model` | GemPy GeoModel object |
| `gempy_solution` | GemPy compute_model solution |
| `array` | NumPy array |
| `report` | JSON-like report or preview object |
| `any` | accepts multiple runtime value types |


## Input nodes

### Load Uploaded File (`LoadUploadedFile`)

**Purpose.** Read an uploaded table, mesh, raster or array file. Tables produce table output; meshes produce mesh output; all files also produce a file output.

**Typical use.**
- Use this for files that were uploaded through the left file panel.
- The node always returns a `file` output; it also returns `table` or `mesh` when the file can be parsed as that type.

**Inputs**

_None._

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | table |
| `mesh` | `mesh` | no | mesh |
| `file` | `file` | no | file |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `file_id` | `file_select` |  |  | Select an uploaded or generated file from the file manager. |
| `file_type` | `select` | `auto` | `auto, csv, xlsx, json, mesh, vtk, vtp, vtu, vti, stl, ply, obj, raster, tif, tiff, npy` | Controls how the selected file is interpreted. Use auto unless the extension is ambiguous. |
| `sheet_name` | `text` |  |  | Excel worksheet name. Leave empty to use the default or first sheet. |
### Load GemPy Model JSON (`LoadGemPyModelJson`)

**Purpose.** Load a complete GemPy GeoModel using GemPy JsonIO. This skips table import, structural-frame setup and interpolation-option setup.

**Typical use.**
- Use to restart a workflow from a saved GemPy JsonIO model instead of rebuilding it from tables.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `file` | `file` | no | GemPy model JSON, optional |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `file_id` | `file_select` |  |  | Optional if a file is connected. Use a JsonIO model JSON saved by Save GemPy Model JSON. |
### Load from Kadi (`LoadKadiFile`)

**Purpose.** Download one CSV/XLSX table or PyVista-readable mesh file from a Kadi record.

**Typical use.**
- Use when a required input file is stored in KADI and should be downloaded during the workflow.
- KADI authentication must already be configured for `kadi_apy`.

**Inputs**

_None._

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | table |
| `mesh` | `mesh` | no | mesh |
| `file` | `file` | no | downloaded file |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `record_id` | `number` | `49744` |  | KADI record identifier from which a file should be downloaded. |
| `file_name` | `text` | `local_geology.csv` |  | Output or target filename. The node may adjust the extension when required by the mesh type. |
| `file_type` | `select` | `auto` | `auto, csv, xlsx, json, mesh, vtk, vtp, vtu, vti, stl, ply, obj` | Controls how the selected file is interpreted. Use auto unless the extension is ambiguous. |
| `sheet_name` | `text` |  |  | Optional; only used when loading Excel files. |

## Data nodes

### Merge Tables (`MergeTables`)

**Purpose.** Concatenate multiple tables.

**Typical use.**
- Connect multiple table outputs to the `tables` input.
- Use `manual_order` when the order of connected inputs matters for later modelling.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `tables` | `table` | yes | tables |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | merged |
| `report` | `report` | no | merge report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `axis` | `number` | `0` |  | Concatenation axis. 0 appends rows; 1 appends columns. |
| `ignore_index` | `boolean` | `True` |  | Reset row indices after concatenation. |
| `add_source_column` | `boolean` | `False` |  | Add a column showing which input table each row came from. |
| `source_column` | `text` | `_merge_source` |  | Name of the source-tracking column. |
| `manual_order` | `text` |  |  | Optional comma-separated order used to reorder multi-input tables before merging. |
### Validate Geological Table (`ValidateGeoTable`)

**Purpose.** Check required GemPy columns and basic coordinate/orientation quality.

**Typical use.**
- Place this before `Create GemPy Model` to catch missing columns early.
- It does not change the table unless basic normalization is required; it mainly creates a report.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | table |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | table |
| `report` | `report` | no | report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `table_kind` | `select` | `auto` | `auto, surface_points, orientations, fault_points, fault_orientations` | Expected geological table type. Auto detects required columns when possible. |
### Convert Orientation Angles (`ConvertOrientationAngles`)

**Purpose.** Convert orientation tables with dip/azimuth columns into GemPy gradient-vector columns G_x, G_y, G_z before merging with existing G-vector orientation data.

**Typical use.**
- Use this when orientation data are stored as dip/azimuth rather than `G_x/G_y/G_z`.
- Run it before merging with orientation tables that already use GemPy gradient vectors.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | orientation table |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | converted orientation table |
| `report` | `report` | no | conversion report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `azimuth_col` | `text` | `auto` |  | auto detects azimuth, az, dip_direction, dipdirection, direction. |
| `dip_col` | `text` | `auto` |  | auto detects dip, dip_angle, dipangle, inclination. |
| `polarity_col` | `text` | `auto` |  | Optional. auto detects polarity/pole_polarity/sense. Missing polarity is treated as 1. |
| `formation_col` | `text` | `formation` |  | Column containing the formation, lithology, or structural element name. |
| `azimuth_is_dip_direction` | `boolean` | `True` |  | If unchecked, azimuth is interpreted as strike and converted to dip direction by adding 90 degrees. |
| `overwrite_existing_gradients` | `boolean` | `False` |  | If false, only rows missing G_x/G_y/G_z are filled from dip/azimuth. |
| `normalize_vectors` | `boolean` | `True` |  | Enable or disable: Normalize output vectors. |
| `formation_to_str` | `boolean` | `True` |  | Convert formation names to strings to avoid numeric-name issues in GemPy. |
| `drop_angle_columns` | `boolean` | `False` |  | Enable or disable: Drop dip/azimuth columns after conversion. |
| `drop_incomplete_gradient_rows` | `boolean` | `False` |  | Enable or disable: Drop rows still missing G values. |
### Clean Geological Table (`CleanGeoTable`)

**Purpose.** Convert coordinate/gradient columns to numbers and optionally remove invalid rows.

**Typical use.**
- Recommended before sampling and before `Create GemPy Model`.
- It converts coordinates and gradients to numeric values and removes invalid rows.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | table |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | cleaned |
| `report` | `report` | no | report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `coord_cols` | `text` | `X,Y,Z` |  | Comma-separated coordinate column names. |
| `grad_cols` | `text` | `G_x,G_y,G_z` |  | Comma-separated gradient-vector column names. |
| `formation_col` | `text` | `formation` |  | Column containing the formation, lithology, or structural element name. |
| `formation_to_str` | `boolean` | `True` |  | Recommended for GemPy. Prevents numeric formation names such as 0.0 from causing name_id_map KeyError. |
| `numeric_xyz` | `boolean` | `True` |  | Convert coordinate columns to numeric values. |
| `numeric_gradients` | `boolean` | `True` |  | Convert orientation vector columns to numeric values. |
| `drop_missing_xyz` | `boolean` | `True` |  | Remove rows with missing coordinate values. |
| `remove_zero_gradients` | `boolean` | `False` |  | Remove orientation rows whose gradient vector has zero length. |
### Sample Geological Data (`SampleGeoTable`)

**Purpose.** Responsive stratified formation-aware sampling. Default fast method avoids slow/hanging KMeans on large tables.

**Typical use.**
- Use for large surface-point or orientation tables.
- `fast` is the recommended method for large geological datasets.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | table |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | sampled |
| `report` | `report` | no | sampling report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `group_col` | `text` | `formation` |  | Column used for stratified sampling groups, usually formation. |
| `feature_cols` | `text` | `X,Y,Z` |  | Columns used to measure spatial spread during sampling. |
| `n` | `number` | `100` |  | Target number of rows to return. |
| `method` | `select` | `fast` | `fast, random, kmeans` | fast is recommended and avoids sklearn KMeans stalls; kmeans keeps the old behavior. |
| `allocation` | `select` | `equal` | `equal, proportional, min_distance` | Select the option used for Allocation. |
| `random_state` | `number` | `42` |  | Seed for reproducible sampling. |
| `na_strategy` | `select` | `median` | `median, drop` | How missing feature values are handled during sampling. |
| `scale` | `select` | `none` | `none, standard` | Whether numeric sampling features are standardized before representative selection. |

## GemPy nodes

### Interactive GeoModel Data Editor (`InteractiveGemPyModelDataEditor`)

**Purpose.** Interactively select, modify, delete and add points/orientations inside an already-created GemPy GeoModel using GemPy's add/modify/delete APIs.

**Typical use.**
- Use for interactive point/orientation edits after a GeoModel has been created.
- Queued edits are applied through GemPy APIs when the node runs.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | edited geo model |
| `report` | `report` | no | interactive editor report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `operations_json` | `geological_point_editor` | `[]` |  | Use the visual editor below. The queued edits are applied to the GeoModel through GemPy API calls when this node runs. |
| `auto_create_missing_element` | `boolean` | `False` |  | Advanced. If you type a new element name, the node will try to create a new structural element/group using the installed GemPy API before adding the point. |
| `new_element_relation` | `select` | `ERODE` | `ERODE, ONLAP, FAULT` | Used only when auto-creating a missing element is supported by the current GemPy version. |
| `fail_on_edit_error` | `boolean` | `True` |  | Recommended. The node will stop with a clear error instead of silently returning an unchanged model if a GemPy API edit fails. |
### Create GemPy Model (`CreateGemPyModel`)

**Purpose.** Create a GemPy GeoModel from surface points and orientations.

**Typical use.**
- This creates the initial GemPy `GeoModel` from surface-points and orientation tables.
- All input coordinate systems must already match; the node does not reproject coordinates.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `surface_points` | `table` | no | surface points |
| `orientations` | `table` | no | orientations |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `project_name` | `text` | `Hessian 3D` |  | Value for Project name. |
| `auto_extent_from_data` | `boolean` | `True` |  | When enabled, model extent is calculated from connected surface points and orientations using X/Y/Z min/max plus padding. |
| `extent_padding_percent` | `number` | `5.0` |  | Padding applied on each min/max side when auto extent is used. Default: 5%. |
| `extent_user_overridden` | `boolean` | `False` |  | Internal/state flag. Turns on automatically when you edit Model extent manually; disables auto extent until you click Fill extent from data. |
| `extent` | `extent6` | `[476621, 491762, 5487135, 5507123, -200, 700]` |  | Auto-filled from connected surface points/orientations when possible. Edit any value to switch to manual override. |
| `refinement` | `number` | `3` |  | GemPy octree refinement level. Lower values are faster; higher values are more detailed. |
| `resolution` | `resolution3` |  |  | When enabled, this overrides refinement with [nx, ny, nz]. |
| `apply_surface_point_nugget` | `boolean` | `False` |  | Optional. Calls gp.modify_surface_points(geo_model=geo_model, nugget=...) after gp.create_geomodel. Default is off to match GemPy examples. |
| `surface_point_nugget` | `number` | `0.01` |  | Numeric value for Surface point nugget. |
### Configure Structural Frame (`ConfigureStructuralFrame`)

**Purpose.** Use stack mapping for order/elements and structural groups for relations.

**Typical use.**
- Use this after `Create GemPy Model` and before `Compute GemPy Model`.
- The visual builder maps formations/elements to series/groups and relation types.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `anisotropy` | `select` | `NONE` | `unchanged, NONE` | GemPy anisotropy setting. |
| `mapping_json` | `mapping_builder` |  |  | Leave empty to auto-generate a generic one-element-per-series mapping from the connected GeoModel. Fill/edit rows to manually control series order and grouping. |
| `groups_json` | `structural_groups` | `[]` |  | Generic default is empty. When Stack mapping is empty and auto mode is enabled, groups are inferred from the connected GeoModel. When Stack mapping is filled, this only sets relation for each mapped series. |
| `auto_mapping_from_geo_model` | `boolean` | `True` |  | When Stack mapping is empty, infer series/elements from the incoming GeoModel instead of using project-specific defaults. |
| `auto_groups_from_mapping` | `boolean` | `True` |  | When Structural groups is empty, infer relation rows from the current mapping. Fault-looking names become FAULT, basement-looking names become BASEMENT, others ERODE. |
| `mapping_auto_initialized` | `boolean` | `False` |  | Internal/state flag. When true, automatic fill will not run again unless you click Fill from connected GeoModel. |
| `mapping_user_overridden` | `boolean` | `False` |  | Internal/state flag. Turns on automatically when you edit mapping rows; prevents auto-fill from overwriting your edits. |
| `groups_user_overridden` | `boolean` | `False` |  | Internal/state flag. Turns on when you clear/edit relation groups manually. |
| `fault_relations_json` | `fault_relations_builder` | `{<br>  "enabled": false,<br>  "relations": []<br>}` |  | Click checkboxes to define geo_model.structural_frame.fault_relations. Rows are faulting series; columns are affected series. |
| `remove_default_formation` | `boolean` | `True` |  | Remove GemPy's default formation if not needed. |
### Set Finite Fault (`SetFiniteFault`)

**Purpose.** Assign finite fault data to selected fault structural groups using GemPy ellipsoid_3d_factory and FaultsData.

**Typical use.**
- Use only when fault groups need finite-fault ellipsoid data.
- Requires the corresponding structural group to exist in the GeoModel.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `clear_existing` | `boolean` | `False` |  | Enable or disable: Clear existing finite fault data first. |
| `finite_faults_json` | `finite_fault_builder` | `<visual JSON default>` |  | Select a structural group and define center/radius/max slope/transform. This corresponds to assigning group.faults_input_data in your notebook. |
### Add Surface Points (`AddSurfacePoints`)

**Purpose.** Manually add single points or x-y grid points to an existing GemPy GeoModel before compute_model.

**Typical use.**
- Use for manual geological constraints such as additional fixed points or regular XY grids.
- Run it before `Compute GemPy Model`.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |
| `points_table` | `table` | no | points table, optional |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `points_json` | `surface_points_builder` | `<visual JSON default>` |  | Add single point rows or grid rows. Grid rows expand every x with every y at the chosen z, matching the gp.add_surface_points loops in your notebook. |
### Auto Orientations (`AutoOrientations`)

**Purpose.** Generate orientations from surface-point coordinates for selected elements.

**Typical use.**
- Generates simple orientations from available surface points for selected elements.
- Useful for quick tests but should be checked geologically.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `element_names` | `text` | `Quaternary,Granit` |  | Value for Element names. |
### Set / Preview Topography (`SetTopography`)

**Purpose.** Set topography from file, arrays/table, random topography within current model extent, or preview current topography.

**Typical use.**
- Adds or previews topography in the GemPy model.
- Topography can come from a file, connected table, inline JSON, or random generation.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |
| `topography_file` | `file` | no | topography file, optional |
| `topography_table` | `table` | no | topography XYZ table, optional |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |
| `report` | `report` | no | topography report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `mode` | `select` | `file` | `file, arrays, random, preview` | Operation mode for this node. |
| `topography_file_id` | `file_select` |  |  | Value for Uploaded topo file. |
| `filepath` | `text` |  |  | Value for Local filepath. |
| `topography_points_json` | `textarea` | `[]` |  | For arrays mode: list of [x,y,z] or objects {x,y,z}. A connected table with X/Y/Z can be used instead. |
| `topography_resolution` | `text` | `[50, 50]` |  | Used for random mode when supported by the installed GemPy version. |
| `random_z_fraction_min` | `number` | `0.6` |  | Fraction of the current model z-range. 0 is z_min, 1 is z_max. |
| `random_z_fraction_max` | `number` | `1.0` |  | Random topography is clipped to the model z-range. |
| `fractal_dimension` | `number` | `2.0` |  | Numeric value for Random fractal dimension. |
### Set GemPy Grid (`SetGemPyGrid`)

**Purpose.** Configure GemPy grid: section grid, custom grid, centered grid, or active grid selection.

**Typical use.**
- Use this to add section, custom, or centered grids, or to select active GemPy grids.
- For normal modelling, `activate` with `regular` is often sufficient.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |
| `grid_points` | `table` | no | grid/center points table, optional |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |
| `report` | `report` | no | grid report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `mode` | `select` | `activate` | `section, custom, centered, activate` | Operation mode for this node. |
| `section_name` | `text` | `section` |  | Value for Section name. |
| `section_start` | `text` | `[0, 0]` |  | Value for Section start [x,y]. |
| `section_end` | `text` | `[1000, 1000]` |  | Value for Section end [x,y]. |
| `section_resolution` | `text` | `[100, 80]` |  | Value for Section resolution [nx,ny]. |
| `custom_points_json` | `textarea` | `[]` |  | For custom mode: list of [x,y,z]. A connected X/Y/Z table can be used instead. |
| `centers_json` | `textarea` | `[[0, 0, 0]]` |  | For centered mode: list of center [x,y,z]. A connected X/Y/Z table can be used instead. |
| `centered_radius` | `text` | `[100, 100, 100]` |  | Value for Centered grid radius. |
| `centered_resolution` | `text` | `[10, 10, 10]` |  | Value for Centered grid resolution. |
| `active_grids` | `text` | `regular` |  | Comma-separated grid names, e.g. regular,topography,sections,custom,centered. |
| `reset_active_grids` | `boolean` | `True` |  | Enable or disable: Reset active grids first. |
### Set Interpolation Options (`SetGemPyOptions`)

**Purpose.** Set interpolation and engine-related options before compute_model.

**Typical use.**
- Use for advanced GemPy interpolation/engine parameters.
- Leave fields empty/unchanged unless you know which GemPy option you need.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `number_octree_levels_surface` | `number` |  |  | Leave empty to keep GemPy internal default. |
| `kernel_range` | `number` |  |  | Leave empty to keep GemPy internal default. |
| `octree_error_threshold` | `number` |  |  | Leave empty to keep GemPy internal default. |
| `evaluation_chunk_size` | `number` |  |  | Leave empty to keep GemPy internal default. |
| `verbose` | `select` | `unchanged` | `unchanged, true, false` | Leave unchanged to keep GemPy internal default. |
| `compute_condition_number` | `select` | `unchanged` | `unchanged, true, false` | Leave unchanged to keep GemPy internal default. |
| `uni_degree` | `number` |  |  | Leave empty to keep GemPy internal default. |
| `mesh_extraction` | `select` | `unchanged` | `unchanged, true, false` | Leave unchanged to keep GemPy internal default. Compute GemPy Model controls surface extraction for the run. |
| `kernel_function` | `text` |  |  | Leave empty to keep GemPy internal default. |
### Compute GemPy Model (`ComputeGemPyModel`)

**Purpose.** Run gp.compute_model and expose geo_model + solution outputs.

**Typical use.**
- Runs GemPy interpolation via `gp.compute_model`.
- If surface extraction fails, keep `retry_without_mesh_extraction` enabled.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |
| `solution` | `gempy_solution` | no | solution |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `backend` | `select` | `PYTORCH` | `PYTORCH, numpy` | GemPy compute backend. |
| `dtype` | `select` | `float64` | `float64, float32` | Numeric precision used during GemPy computation. |
| `mesh_extraction` | `boolean` | `True` |  | Default on so show_boundaries/surface plotting works. Disable this when GemPy fails in dual_contouring/mask_generation. |
| `retry_without_mesh_extraction` | `boolean` | `True` |  | Retry compute_model without mesh extraction if surface extraction fails. |
### Extract GemPy Array (`ExtractGemPyArray`)

**Purpose.** Extract lith_block or another raw_arrays attribute from GemPy solution.

**Typical use.**
- Use when you need raw solution arrays such as `lith_block` for export or custom analysis.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `solution` | `gempy_solution` | no | solution |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `array` | `array` | no | array |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `output_name` | `text` | `lith_block` |  | Name of the GemPy raw array to extract. |
| `reshape` | `shape3` | `[64, 64, 64]` |  | Shape used to reshape flat solution arrays for fallback visualization. |

## View nodes

### Visualization (`Visualization`)

**Purpose.** Universal visualization node with a single input. Connect any table, file, raster/TIF, VTK/VTU/VTP mesh, array, voxel output, report, GeoModel or solution.

**Typical use.**
- Universal inspection node for tables, arrays, reports, meshes, files, GeoModels and solutions.
- For meshes, choose the scalar used by the inline preview or PyVista popup.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `data` | `any` | no | data |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `preview` | `report` | no | preview report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `rows` | `number` | `15` |  | Number of table rows shown in the preview. |
| `mesh_scalars` | `text` | `auto` |  | Use auto to prefer MaterialIDs/id/lithology/layer. Or enter a cell/point data name. |
| `show_edges` | `boolean` | `False` |  | Show mesh/voxel cell edges in previews. |
| `generate_thumbnail` | `boolean` | `True` |  | Generate an inline lightweight preview thumbnail where supported. |
### Plot GemPy 2D (`PlotGemPy2D`)

**Purpose.** Create a 2D GemPy plot as a PNG. If gempy_viewer fails, it can fall back to a solution array slice.

**Typical use.**
- Creates a static PNG section through a GemPy model.
- Useful for documentation and quick model checks.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |
| `solution` | `gempy_solution` | no | solution, optional fallback |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `file` | `file` | no | PNG plot |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `direction` | `select` | `y` | `x, y, z` | Cross-section direction for 2D plotting. |
| `cell_number` | `number` |  |  | Index of the slice to plot. Leave empty for a default central slice. |
| `show_data` | `boolean` | `True` |  | Show input data points/orientations in plots. |
| `show_lith` | `boolean` | `True` |  | Show lithological block/model result. |
| `show_boundaries` | `boolean` | `True` |  | Show model boundaries when supported. |
| `show_scalar` | `boolean` | `False` |  | Show scalar field instead of lithology when supported. |
| `fallback_array` | `text` | `lith_block` |  | GemPy solution raw array used when the normal plotter fails. |
| `reshape` | `shape3` |  |  | Shape used to reshape flat solution arrays for fallback visualization. |
| `dpi` | `number` | `160` |  | Image resolution for exported PNG plots. |
| `file_name` | `text` | `gempy_2d_plot.png` |  | Output or target filename. The node may adjust the extension when required by the mesh type. |
### Plot GemPy 3D (`PlotGemPy3D`)

**Purpose.** Prepare a PyVista/GemPy 3D popup for the computed GeoModel.

**Typical use.**
- Creates a PyVista/GemPy 3D popup and can expose a surface mesh output.
- The popup requires PyVista and a local graphical environment.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `report` | `report` | no | viewer |
| `mesh` | `mesh` | no | surface mesh |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `show_data` | `boolean` | `True` |  | Show input data points/orientations in plots. |
| `show_lith` | `boolean` | `True` |  | Show lithological block/model result. |
| `show_surfaces` | `boolean` | `True` |  | Enable or disable: Show surfaces. |
| `show_topography` | `boolean` | `True` |  | Enable or disable: Show topography. |
| `show_boundaries` | `boolean` | `True` |  | Show model boundaries when supported. |
### Clipping Tool (`PyVistaClippedLayerViewer`)

**Purpose.** Clip either GemPy voxel/layer output or an existing PyVista/VTK mesh with optional shell and DEM/topography meshes, then export the clipped mesh.

**Typical use.**
- Despite the internal type name, this is the main `Clipping Tool`.
- It can clip either a computed GeoModel-derived voxel/layer volume or an existing mesh input.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | computed geo model, optional |
| `input_mesh` | `mesh` | no | mesh to clip, optional |
| `clip_mesh` | `mesh` | no | clipping shell mesh |
| `topography_mesh` | `mesh` | no | DEM/topography mesh |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `report` | `report` | no | viewer |
| `mesh` | `mesh` | no | clipped layer mesh |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `clip_mesh_file_id` | `file_select` |  |  | Optional shell_PraePerm.vtp or another PyVista-readable clipping surface. Leave empty to skip this clipping step. |
| `topography_mesh_file_id` | `file_select` |  |  | Optional dem.vtp. When provided, it is used for topographic clipping by default, not displayed as a separate mesh. |
| `cell_data_name` | `text` | `id` |  | For GeoModel mode this selects the layer id array. For mesh-input mode it is used for preview when present; otherwise the node auto-detects a scalar. |
| `mesh_scalars` | `text` | `auto` |  | Only used when clipping an existing mesh input and cell_data_name is not present. |
| `layer_styles_json` | `layer_styles` | `<visual JSON default>` |  | Visual style definitions for layer IDs. Usually edited with the visual builder. |
| `invert` | `boolean` | `False` |  | Invert the clipping operation. |
| `topography_clip_enabled` | `boolean` | `True` |  | Apply topographic clipping when a topography mesh is provided. |
| `topography_invert` | `boolean` | `False` |  | Invert the topography clipping direction. |
| `crop_to_topography_xy` | `boolean` | `True` |  | Crop to the XY extent of the topography mesh before topographic clipping. |
| `crinkle` | `boolean` | `True` |  | Use crinkle clipping where supported to preserve original cell boundaries. |
| `clean_output_mesh` | `boolean` | `True` |  | Clean the generated mesh after clipping. |
| `show_edges` | `boolean` | `False` |  | Show mesh/voxel cell edges in previews. |
| `show_base_gempy` | `boolean` | `False` |  | Enable or disable: Also show base GemPy 3D scene. |
| `show_clip_mesh` | `boolean` | `False` |  | Enable or disable: Show clipping shell mesh. |
| `show_topography_mesh` | `boolean` | `False` |  | Enable or disable: Show DEM/topography mesh. |
| `output_file_name` | `text` | `clipped_gempy_layers.vtu` |  | If the clipped result is a surface/PolyData but the name ends with .vtu, it is automatically saved as .vtp. |
### Thicken Mesh (`ThickenMesh`)

**Purpose.** Thicken a surface mesh along its normals by a user-defined buffer distance. Creates a shell surface with optional side walls for open meshes.

**Typical use.**
- Use to buffer surfaces, for example fault surfaces, before voxelization.
- Use `close_sides` for open surfaces when a closed shell is needed.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `mesh` | `mesh` | no | mesh |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `mesh` | `mesh` | no | thickened mesh |
| `file` | `file` | no | thickened mesh file |
| `report` | `report` | no | thickening report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `buffer_distance` | `number` | `10.0` |  | Thickness distance in the same unit as the mesh coordinates. |
| `mode` | `select` | `symmetric` | `symmetric, outward, inward` | symmetric offsets half the distance to both sides; outward/inward are one-sided along/opposite the normals. |
| `close_sides` | `boolean` | `True` |  | Adds side-wall faces along boundary edges of open surfaces. |
| `triangulate_input` | `boolean` | `True` |  | Enable or disable: Triangulate input surface. |
| `consistent_normals` | `boolean` | `True` |  | Enable or disable: Consistent normals. |
| `auto_orient_normals` | `boolean` | `True` |  | Enable or disable: Auto-orient normals. |
| `flip_normals` | `boolean` | `False` |  | Enable or disable: Flip normals. |
| `clean_input` | `boolean` | `True` |  | Enable or disable: Clean input surface. |
| `clean_output` | `boolean` | `True` |  | Clean the final output mesh/grid. |
| `mesh_scalars` | `text` | `auto` |  | Cell or point scalar used for mesh preview. Use auto to choose a likely material/layer scalar. |
| `show_edges` | `boolean` | `True` |  | Show mesh/voxel cell edges in previews. |
| `output_file_name` | `text` | `thickened_mesh.vtp` |  | Requested output filename for the generated mesh. |
### Combine Meshes (`CombineMeshes`)

**Purpose.** Combine multiple PyVista/VTK mesh outputs into one mesh. Useful for combining clipped results from several GemPy models into a single exportable mesh.

**Typical use.**
- Use for visualization-style mesh aggregation.
- This does not resolve voxel overlaps like `Merge Voxel Models`; it simply combines mesh geometry.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `meshes` | `mesh` | yes | meshes |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `mesh` | `mesh` | no | combined mesh |
| `file` | `file` | no | combined mesh file |
| `report` | `report` | no | combine report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `mesh_file_ids` | `text` |  |  | Optional comma-separated file ids from uploaded/output VTK meshes. |
| `combine_mode` | `select` | `merge` | `merge, multiblock_combine` | How to combine input meshes: direct merge or multiblock combine. |
| `extract_surface` | `boolean` | `False` |  | Useful when combining volumetric VTU grids as surface-only output. |
| `merge_points` | `boolean` | `False` |  | Usually false for combining separate clipped model parts. Set true to weld coincident points. |
| `tolerance` | `number` | `0.0` |  | Geometric tolerance used for point merging or cleaning. |
| `clean_inputs` | `boolean` | `False` |  | Clean individual input meshes before combining. |
| `clean_output` | `boolean` | `True` |  | Clean the final output mesh/grid. |
| `add_source_id` | `boolean` | `True` |  | Adds source_id to cell and point data so parts can be distinguished after combining. |
| `create_reindexed_element_id` | `boolean` | `True` |  | Creates a new cell-data scalar where each input mesh's original element/material ids are remapped to globally unique ids. |
| `reindex_source_scalar` | `text` | `auto` |  | Use auto to detect existing combined_element_id first, then element_id/id/MaterialIDs/lith_block. Or enter the exact scalar name to remap. |
| `reindexed_scalar_name` | `text` | `combined_element_id` |  | Name of the newly created reindexed scalar. |
| `reindex_scope` | `select` | `source_and_element` | `source_and_element, global_element` | source_and_element makes element_id=1 from different input meshes become different ids. global_element keeps same original values together. |
| `reindex_start_id` | `number` | `1` |  | First integer ID used for newly reindexed values. |
| `reindex_missing_value` | `number` | `-1` |  | Value assigned when the source scalar is missing or invalid. |
| `preserve_numeric_arrays` | `boolean` | `True` |  | Adds NaN-filled missing numeric arrays before merging so cell-data columns are not dropped. |
| `mesh_scalars` | `text` | `auto` |  | Cell or point scalar used for mesh preview. Use auto to choose a likely material/layer scalar. |
| `show_edges` | `boolean` | `False` |  | Show mesh/voxel cell edges in previews. |
| `output_file_name` | `text` | `combined_mesh.vtu` |  | In auto mode, surface/PolyData outputs requested as .vtu are saved as .vtp because PyVista cannot save PolyData as .vtu. |
| `auto_extract_surface_for_vtp` | `boolean` | `True` |  | If output is .vtp and the combined mesh is volumetric, extract its surface before saving. |

## Mesh / Voxel nodes

### Mesh to Voxel Model (`MeshToVoxelModel`)

**Purpose.** Voxelize PyVista-readable meshes into a regular voxel-grid model. Use inside-surface mode for closed solids or distance-to-surface mode for open fault/sheet meshes.

**Typical use.**
- Use for converting arbitrary PyVista-readable meshes into regular voxel models.
- Use `distance_to_surface` for fault/open-sheet surfaces and `inside_surface` for closed solids.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `mesh` | `mesh` | no | mesh |
| `reference_geo_model` | `geo_model` | no | reference geo model, optional |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `voxel_model` | `mesh` | no | voxel model |
| `voxel_grid` | `mesh` | no | voxel grid |
| `file` | `file` | no | voxel model file |
| `report` | `report` | no | voxelization report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `mesh_file_id` | `file_select` |  |  | Optional if a mesh is connected. Supports PyVista-readable mesh files. |
| `voxel_size` | `text` |  |  | Leave empty/auto to infer. If reference_geo_model is connected, uses the same regular-grid voxel size as the Clipping Tool GeoModel volume. |
| `voxel_size_y` | `text` |  |  | Optional manual voxel size in Y. |
| `voxel_size_z` | `text` |  |  | Optional manual voxel size in Z. |
| `target_cells_longest_axis` | `number` | `80` |  | Automatic voxel size target: number of candidate cells along the longest axis. |
| `max_voxels` | `number` | `2000000` |  | Safety limit for candidate voxel generation. |
| `padding` | `number` | `0.0` |  | Additional padding around the input mesh bounds. |
| `voxelization_mode` | `select` | `inside_surface` | `inside_surface, distance_to_surface` | inside_surface is for closed solids. distance_to_surface is recommended for open fault/sheet meshes and avoids ray-casting fringe artifacts. |
| `distance_buffer` | `text` |  |  | Only used for distance_to_surface. Leave empty to use ~0.75×min voxel size, or set the desired fault buffer thickness. |
| `distance_chunk_size` | `number` | `200000` |  | Chunk size reserved for distance calculations. |
| `source_scalar` | `text` | `auto` |  | Cell/point scalar from the mesh to transfer to voxel cells. |
| `output_scalar_name` | `text` | `MaterialIDs` |  | Name of the scalar created on the output voxel cells. |
| `inside_tolerance` | `number` | `1e-06` |  | Tolerance used by inside-surface classification. |
| `check_surface` | `boolean` | `False` |  | Enable to make PyVista validate closed surface. For open surfaces, use Thicken Mesh first. |
| `invert_inside` | `boolean` | `False` |  | Invert inside/outside classification. |
| `clean_output` | `boolean` | `True` |  | Clean the final output mesh/grid. |
| `show_edges` | `boolean` | `True` |  | Show mesh/voxel cell edges in previews. |
| `file_name` | `text` | `mesh_voxel_model.vtu` |  | Output or target filename. The node may adjust the extension when required by the mesh type. |
### Clip Voxel Model by Mask (`ClipVoxelModelByMask`)

**Purpose.** Remove voxels from a base voxel model using another voxel model as a mask/occluder. Useful for clipping away the covering layer so the mask model becomes visible on the outer surface after merging.

**Typical use.**
- Use when one voxel model should cut a window through another voxel model.
- Typical use: reveal a fault voxel model inside a larger lithology voxel model before final merge.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `base_voxel_model` | `mesh` | no | base voxel model to clip |
| `mask_voxel_model` | `mesh` | no | mask / model to reveal |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `voxel_model` | `mesh` | no | clipped voxel model |
| `voxel_grid` | `mesh` | no | clipped voxel grid |
| `removed_voxels` | `mesh` | no | removed voxels, optional |
| `file` | `file` | no | clipped voxel file |
| `report` | `report` | no | clip report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `clip_mode` | `select` | `same_xy_column_all_z` | `<many>` | exact_overlap removes only identical voxel positions. same_xy_column_all_z cuts a vertical window through the base model where the mask exists. above/below remove only one side in Z. |
| `xy_expand_cells` | `number` | `0` |  | Expands the mask columns by this many cells in X/Y, useful for making the revealed window slightly wider. |
| `z_margin_cells` | `number` | `0` |  | Add a vertical margin in voxel layers for Z-based mask clipping. |
| `resample_mask_to_base_grid` | `boolean` | `True` |  | When mask and base voxel sizes differ, expand mask voxels to covered base-grid cells. |
| `voxel_size` | `text` |  |  | Leave empty to infer from the base voxel model. |
| `voxel_size_y` | `text` |  |  | Optional manual voxel size in Y. |
| `voxel_size_z` | `text` |  |  | Optional manual voxel size in Z. |
| `preview_scalar` | `text` | `auto` |  | Scalar shown in preview after clipping. |
| `output_removed_voxels` | `boolean` | `False` |  | Also output the removed voxels for debugging. |
| `clean_output` | `boolean` | `True` |  | Clean the final output mesh/grid. |
| `show_edges` | `boolean` | `True` |  | Show mesh/voxel cell edges in previews. |
| `file_name` | `text` | `clipped_voxel_model.vtu` |  | Output or target filename. The node may adjust the extension when required by the mesh type. |
### Merge Voxel Models (`MergeVoxelModels`)

**Purpose.** Merge several voxel-grid models into one true voxel grid. Inputs with different grid sizes are resampled onto a common target voxel grid before overlap resolution and source-aware reindexing.

**Typical use.**
- Use for true voxel-grid merging with overlap priority and output scalar reindexing.
- Use `target_voxel_size_mode` and `resample_to_target_grid` when input voxel sizes differ.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `voxel_models` | `mesh` | yes | voxel models |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `voxel_model` | `mesh` | no | merged voxel model |
| `voxel_grid` | `mesh` | no | merged voxel grid |
| `file` | `file` | no | merged voxel file |
| `report` | `report` | no | merge report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `input_scalar_map_json` | `voxel_scalar_map` | `[]` |  | After connecting/running input voxel models, choose which cell-data scalar from each model should be merged. |
| `cell_data_name` | `text` | `auto` |  | Used when no per-input scalar is selected. Use auto to choose a reasonable scalar from each input. |
| `output_scalar_name` | `text` | `MaterialIDs` |  | The selected input scalar values are remapped into this new scalar as consecutive IDs starting at 1. |
| `reindex_scope` | `select` | `source_and_value` | `source_and_value, global_value` | source_and_value keeps source-based ID separation. global_value keeps equal original values together across inputs. |
| `reindex_start_id` | `number` | `1` |  | First integer ID used for newly reindexed values. |
| `first_input_wins` | `boolean` | `True` |  | Recommended. If voxel models overlap, the first connected input has priority. |
| `target_voxel_size_mode` | `select` | `smallest_input` | `smallest_input, first_input, largest_input, median_input` | smallest_input preserves detail when voxel models have different grid sizes. first_input keeps the old behavior. |
| `resample_to_target_grid` | `boolean` | `True` |  | When enabled, large source voxels are expanded to all target voxels they cover instead of being represented by one center point. |
| `max_merged_voxels` | `number` | `2000000` |  | Safety limit for the merged voxel grid. |
| `voxel_size` | `text` |  |  | Overrides Target voxel size mode when set. |
| `voxel_size_y` | `text` |  |  | Optional manual voxel size in Y. |
| `voxel_size_z` | `text` |  |  | Optional manual voxel size in Z. |
| `default_value` | `number` | `1` |  | Value used when the selected input scalar is missing. |
| `clean_output` | `boolean` | `True` |  | Clean the final output mesh/grid. |
| `show_edges` | `boolean` | `True` |  | Show mesh/voxel cell edges in previews. |
| `file_name` | `text` | `merged_voxel_model.vtu` |  | Output or target filename. The node may adjust the extension when required by the mesh type. |

## Output nodes

### Extract Voxel Boundaries (`ExtractVoxelBoundaries`)

**Purpose.** Extract top, bottom, north, south, east and west boundary meshes from a voxel grid. Uses median-spacing quantized indexing to avoid fake boundary columns from tiny floating-point coordinate noise.

**Typical use.**
- Use after voxelization to create simulation boundary meshes.
- It extracts east, west, south, north, top, bottom, side mesh and/or full shell outputs.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `mesh` | `mesh` | no | voxel grid / mesh |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `report` | `report` | no | boundary report |
| `top` | `file` | no | top boundary .vtp |
| `bottom` | `file` | no | bottom boundary .vtp |
| `north` | `file` | no | north boundary .vtp |
| `south` | `file` | no | south boundary .vtp |
| `east` | `file` | no | east boundary .vtp |
| `west` | `file` | no | west boundary .vtp |
| `side_mesh` | `file` | no | side mesh .vtp |
| `full_shell` | `file` | no | full shell .vtp |
| `side_mesh_obj` | `mesh` | no | side mesh object |
| `full_shell_obj` | `mesh` | no | full shell object |
| `selected_preview` | `file` | no | selected boundary preview manifest |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `mesh_file_id` | `file_select` |  |  | Optional .vtu/.vtk voxel grid. Leave empty when connecting Hex Mesh to Voxel Grid.voxel_grid. |
| `cell_data_name` | `text` | `MaterialIDs` |  | Input cell-data scalar name used as material/layer/value array. |
| `decimals` | `number` | `8` |  | Used only for robust spacing/min-center inference. Grid indices are quantized by median voxel spacing, so tiny coordinate noise will not create fake grid columns. |
| `add_top_risers` | `boolean` | `True` |  | Enable or disable: Add vertical risers on stepped top. |
| `triangulate_shells` | `boolean` | `True` |  | Enable or disable: Triangulate side/full shell. |
| `clean_tolerance` | `number` | `1e-09` |  | Numeric value for Clean tolerance for side/full shell. |
| `output_prefix` | `text` | `voxel` |  | Prefix for output boundary filenames. |
| `show_edges` | `boolean` | `True` |  | Show mesh/voxel cell edges in previews. |
| `show_top` | `boolean` | `True` |  | Enable or disable: Show top. |
| `show_bottom` | `boolean` | `True` |  | Enable or disable: Show bottom. |
| `show_north` | `boolean` | `True` |  | Enable or disable: Show north. |
| `show_south` | `boolean` | `True` |  | Enable or disable: Show south. |
| `show_east` | `boolean` | `True` |  | Enable or disable: Show east. |
| `show_west` | `boolean` | `True` |  | Enable or disable: Show west. |
| `color_top` | `text` | `red` |  | Value for Top color. |
| `color_bottom` | `text` | `blue` |  | Value for Bottom color. |
| `color_north` | `text` | `green` |  | Value for North color. |
| `color_south` | `text` | `yellow` |  | Value for South color. |
| `color_east` | `text` | `orange` |  | Value for East color. |
| `color_west` | `text` | `purple` |  | Value for West color. |
### Hex Mesh to Voxel Grid (`HexMeshToVoxelGrid`)

**Purpose.** Convert an 8-node hexahedral mesh to a VTK VOXEL unstructured grid, preserving cell_data, following your original notebook method.

**Typical use.**
- Use for converting clipped hexahedral meshes into explicit VTK voxel-grid cells.
- Newer general mesh workflows should often use `Mesh to Voxel Model` instead.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `mesh` | `mesh` | no | mesh, optional |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `voxel_grid` | `mesh` | no | voxel grid |
| `file` | `file` | no | VTU file |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `mesh_file_id` | `file_select` |  |  | Optional .vtu/.vtk mesh file. Leave empty when connecting a mesh output from PyVista Clipped Layer Viewer. |
| `skip_non_hex` | `boolean` | `False` |  | Enable or disable: Skip non-hex cells. |
| `clean_after` | `boolean` | `True` |  | Enable or disable: Clean output grid. |
| `tolerance` | `number` | `1e-08` |  | Geometric tolerance used for point merging or cleaning. |
| `material_scalar` | `text` | `auto` |  | Use 'auto' to prefer MaterialIDs/id/lithology. Do not use cell_ids unless you want an index-colored grid. |
| `output_material_name` | `text` | `MaterialIDs` |  | The chosen material scalar is copied to this cell-data name so the PyVista preview uses the same style as your original notebook. |
| `show_edges` | `boolean` | `True` |  | Show mesh/voxel cell edges in previews. |
| `file_name` | `text` | `gempy_volume_with_topo_voxel_grid.vtu` |  | Output or target filename. The node may adjust the extension when required by the mesh type. |
### Save Table CSV (`SaveTableCsv`)

**Purpose.** Export a table as CSV and expose a download link.

**Typical use.**
- Exports a table output as CSV.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `table` | `table` | no | table |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `file` | `file` | no | file |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `file_name` | `text` | `table.csv` |  | Output or target filename. The node may adjust the extension when required by the mesh type. |
### Save GemPy Model JSON (`SaveGemPyModelJson`)

**Purpose.** Save a complete GemPy GeoModel using GemPy JsonIO.save_model_to_json.

**Typical use.**
- Use for preserving a complete GemPy model state via GemPy JsonIO.
- Best used when the model has an explicit `resolution`.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `geo_model` | `geo_model` | no | geo model |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `file` | `file` | no | GemPy model JSON |
| `report` | `report` | no | save report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `file_name` | `text` | `gempy_model.json` |  | Output or target filename. The node may adjust the extension when required by the mesh type. |
| `require_explicit_resolution` | `boolean` | `True` |  | Recommended. JsonIO save/load requires a model created with Resolution, not only Refinement. |
### Save Array (`SaveArray`)

**Purpose.** Export a NumPy array as .npy or .csv.

**Typical use.**
- Exports a NumPy array output as `.npy` or `.csv`.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `array` | `array` | no | array |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `file` | `file` | no | file |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `format` | `select` | `npy` | `npy, csv` | Export format. |
| `file_name` | `text` | `array.npy` |  | Output or target filename. The node may adjust the extension when required by the mesh type. |
### Upload GeoDT Structure Version to Kadi (`UploadGeoDTStructureVersionToKadi`)

**Purpose.** Create a new GeoDT structure-model version record in Kadi, update the current-version description record, and upload the voxel volume plus all extracted boundary meshes with fixed simulation names.

**Typical use.**
- Project-specific KADI upload node for GeoDT structure-model releases.
- Requires volume and boundary file inputs with fixed semantic roles.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `volume` | `file` | no | Hex Mesh to Voxel Grid.file |
| `south` | `file` | no | south boundary file |
| `north` | `file` | no | north boundary file |
| `bottom` | `file` | no | bottom boundary file |
| `top` | `file` | no | top boundary file |
| `west` | `file` | no | west boundary file |
| `east` | `file` | no | east boundary file |
| `side_mesh` | `file` | no | side_mesh file |
| `full_shell` | `file` | no | full_shell file |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `report` | `report` | no | Kadi upload report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `title` | `text` | `GeoDT Input Structure Model` |  | Value for Record title. |
| `description` | `textarea` | `Data from the structural model workflow, including the structural model, the total surfaces ...` |  | Text/JSON content for Record description. |
| `subject` | `text` | `Structure model in voxel grid from GemPy` |  | Value for Subject metadata. |
| `collection_id` | `number` | `7238` |  | Numeric value for Collection ID. |
| `description_record_id` | `number` | `80295` |  | This record receives/updates the metadatum 'Record ID of the current version'. |
| `version` | `text` | `0.1` |  | Value for Version. |
| `tag` | `text` | `geolab` |  | Value for Tag. |
| `update_description_record` | `boolean` | `True` |  | Enable or disable: Update current-version description record. |
| `add_group_roles` | `boolean` | `True` |  | Adds group roles 143/Admin, 302/Editor and 122/Member. |
| `force_upload` | `boolean` | `True` |  | Enable or disable: Overwrite files if names exist. |
| `dry_run` | `boolean` | `False` |  | Prepare the correctly named files and report what would be uploaded, without creating a Kadi record. |
### Upload File to Kadi (`UploadFileToKadi`)

**Purpose.** Upload an exported file to a Kadi record.

**Typical use.**
- Uploads one generated file to an existing KADI record.

**Inputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `file` | `file` | no | file |

**Outputs**

| Port | Kind | Multiple | Meaning |
|---|---:|:---:|---|
| `report` | `report` | no | report |

**Parameters**

| Parameter | Type | Default | Options | Meaning |
|---|---:|---:|---|---|
| `record_id` | `number` | `49742` |  | KADI record identifier from which a file should be downloaded. |
| `file_name` | `text` |  |  | Output or target filename. The node may adjust the extension when required by the mesh type. |
| `force` | `boolean` | `True` |  | Overwrite target file if it already exists. |

---

## Troubleshooting

### The server starts, but the browser still shows an old version

Use a hard refresh:

```text
Ctrl + F5
```

Then reload the page and clear the execution cache.

### `Runtime object token not found`

Runtime tokens are stored only in backend memory. They disappear after a server restart or cache cleanup. Rerun the upstream node chain that creates the GeoModel or mesh preview.

### GemPy import or compute errors

Check that GemPy and its numerical dependencies are installed in the same environment used to launch the server. If using the PyTorch backend, check the PyTorch installation.

If GemPy surface extraction fails during computation, keep:

```text
Retry without surface meshes = True
```

in `Compute GemPy Model`.

### PyVista popup does not open

PyVista popups require PyVista and a graphical environment on the machine running the backend server. On headless servers, use inline previews or export mesh files and inspect them elsewhere.

### Mesh to Voxel creates fringe artifacts for faults

Use:

```text
Voxelization mode = distance_to_surface
```

for fault surfaces and open sheet-like meshes. Use `inside_surface` only for genuinely closed solids.

### Merge Voxel Models looks blocky or loses detail

Use:

```text
Target voxel size mode = smallest_input
Resample inputs to target grid = True
```

when merging voxel models with different grid sizes. Increase `Max merged voxels` if needed, or choose a coarser manual voxel size.

### Extracted voxel boundaries have artificial internal strips

`Extract Voxel Boundaries` uses quantized grid indexing based on inferred median voxel spacing. If artifacts remain, inspect the report fields:

```text
grid_shape
voxel_spacing
duplicate_quantized_centers
occupied_voxels
input_cells
```

and verify that the input really is a regular voxel grid.

### KADI upload/download fails

Check:

- `kadi_apy` is installed.
- Authentication is configured.
- The record ID exists.
- The target filename is correct.
- You have permission to read/write the record.

---

## Development notes

### Adding a new node

1. Implement a subclass of `BaseNode` in `app/nodes.py`.
2. Give it a unique `type_name`.
3. Implement `run(self, inputs, params, context)`.
4. Return outputs as `RuntimeValue` objects.
5. Import the class in `app/node_registry.py`.
6. Add it to `NODE_REGISTRY`.
7. Add its UI metadata to `NODE_TYPES`.
8. If a new custom parameter editor is needed, implement the frontend editor in `app/static/app.js`.

### Runtime values

Nodes exchange typed `RuntimeValue` objects. Common kinds are:

```text
table
mesh
file
geo_model
gempy_solution
array
report
```

Use these kinds consistently so the frontend can enforce compatible connections.

### Custom frontend parameter kinds

Some nodes use visual builders instead of plain text inputs. Examples include:

```text
layer_styles
voxel_scalar_map
structural_frame_builder
fault_relations_builder
finite_fault_builder
surface_points_builder
geological_point_editor
```

These store JSON in the node parameters, but the UI exposes a specialized editor.

### Generated files

Use the backend storage helpers for generated outputs. Do not hard-code paths into node results. Generated outputs should be registered so the frontend can download them.

---

## License

MIT License

Copyright (c) 2026 JIAN YANG

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## Acknowledgements

This editor is designed around GemPy-based structural geological modelling workflows and uses GemPy, PyVista/VTK, pandas, NumPy, FastAPI and related Python tools. The application is a workflow interface around these libraries; geological interpolation remains the responsibility of GemPy.
