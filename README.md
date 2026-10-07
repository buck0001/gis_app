# GIS Mapper by buck0001

A local web app for preparing an area of interest (AOI), acquiring or uploading a digital elevation model (DEM), running terrain and hydrology analyses, viewing result layers, and exporting maps and project data.

This guide is written for a anyone setting up the app on Windows. The application has a Next.js frontend and a Python/FastAPI GIS backend. The backend stores projects in a local SQLite database and writes project files under `data\storage`.

## Features

- Create and reopen GIS projects.
- Define an AOI by drawing a polygon or rectangle, uploading a boundary file, or using coordinate search to move the map.
- Automatically acquire Copernicus DEM GLO-30 data for an AOI, or upload a GeoTIFF DEM.
- Run terrain and hydrology processing using WhiteboxTools.
- Display result layers and download raster/vector data.
- Export individual maps as PNG or PDF, with a QA report alongside each map.
- Download a project package containing available data, processing intermediates, maps, and reports.

## Requirements

Install these before starting:

- Windows 10/11, 64-bit.
- Python 3.11 recommended.
- Node.js 20 LTS or newer, including npm.
- Internet access for installing dependencies, downloading WhiteboxTools on first use, acquiring Copernicus DEM tiles, and loading the OpenStreetMap basemap.

The app does not require API keys for its configured DEM source. DEM acquisition and map tiles do require an internet connection.

## Install

Open PowerShell in the project folder (the folder containing `README.md`).

### 1. Set up the Python backend

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell prevents activation, allow it only for the current PowerShell process:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Then activate the environment. Alternatively, skip activation and replace `python` in the commands below with `.\.venv\Scripts\python.exe`.

### 2. Set up the frontend

Open a **second** PowerShell window in the project folder:

```powershell
Set-Location .\frontend
npm ci
```

`npm ci` installs the exact dependency versions from `frontend\package-lock.json`.

## Run the app

Run the backend and frontend in separate PowerShell windows. Keep both windows open while using the app.

### Backend window

From the project folder, with the Python environment activated:

```powershell
python -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```

The API health endpoint is <http://127.0.0.1:8000/api/health>. A healthy backend returns JSON with `"status": "ok"`.

### Frontend window

```powershell
Set-Location .\frontend
npm run dev
```

Open <http://localhost:3000> in a browser. The frontend proxies API and file requests to the backend at `127.0.0.1:8000`.

To stop either service, focus its PowerShell window and press **Ctrl+C**.

## Use the app

### Create a project

1. Open <http://localhost:3000>.
2. Enter a project name and optional country, region, or district.
3. Choose a DEM source:
   - **Get one automatically**: the first analysis run downloads and clips Copernicus DEM GLO-30 for the saved AOI.
   - **Upload your DEM**: use a GeoTIFF (`.tif` or `.tiff`) already on your computer.
4. Select **Create project**.

### Define the AOI

For automatic DEM acquisition, save an AOI before running analysis. On the project page, you can:

- Enter longitude and latitude and select **Search coordinates** to move the map.
- Draw a polygon (click vertices, then double-click to finish) or a rectangle.
- Upload an AOI boundary in GeoJSON, zipped shapefile, KML/KMZ, or GeoPackage format.

The saved AOI is used for clipping and analysis. Drawing another boundary replaces the saved AOI.

### Upload and validate a DEM

1. In an upload-mode project, choose a GeoTIFF and select **Inspect DEM**.
2. Review the validation details, including CRS, coverage, resolution, and NoData warnings.
3. Confirm the DEM before running analysis. If NoData needs handling, choose **Fill NoData during processing** only when appropriate for the dataset and analysis.
4. If the raster has no CRS, use **Assign CRS** only when you know its actual CRS. Assigning a CRS labels coordinates; it does not reproject the raster.

An uploaded DEM that has been confirmed can be analyzed without drawing an AOI. In that case the analysis uses the full DEM footprint. You may draw or upload an AOI to restrict analysis to a smaller area.

### Plan and run an analysis

1. In **Analysis**, describe the terrain or hydrology products you need and select **Preview plan**, or use **Basic terrain + drainage**.
2. Review the proposed steps and estimated processing time.
3. If the plan includes stream extraction or a dependent hydrology product, provide a positive **Stream threshold (flow-accumulation cells)** unless the plan already specifies one. The app does not choose a universal threshold; select it based on your study design and document the method.
4. Select **Run analysis** and keep the project page open. Job status and progress update while processing.
5. Completed layers appear under **Result layers**. Select a layer checkbox to show or hide it on the map.

To create a contour layer, set the **Contour interval (m)** under **Result layers** and select **Generate contour layer**. The generated vector layer is listed with the other results and can be toggled on the map or downloaded.

Hydrology processing uses depression breaching before D8 flow analysis by default. Depression filling is available as a separate explicit operation; it is not silently substituted if breaching fails.

### Download maps and data

- Use **Download** beside a result layer to download its underlying raster or vector file.
- Choose PNG or PDF in **Export format**, then select **Export map** beside a layer. The map downloads when the export is ready; the entry also remains available in the exports list.
- Each map export includes a `.qa.json` report. The report lists checks and any actions needed.
- Use **Download project package** to create a ZIP of the project data, maps, QA reports, processing intermediates, and reports. A saved AOI is required for this export.

An export marked **NOT CLIENT-READY** may still be downloaded, but its QA report identifies unresolved requirements. In particular, administrative boundaries/locator maps and manual cartographic review are not fabricated or automatically claimed as complete.

## Data and outputs

- All project data is stored **locally** and is excluded from GitHub by `.gitignore`.
- SQLite database: `data\gis.db`
- Downloaded Copernicus DEM tiles: `data\cache\dem\`
- Project files: `data\storage\<project-id>\`
  - Uploaded/acquired inputs and AOI files: `source\`
  - Intermediate processing rasters and vectors: `processed\`
  - Final GIS result layers: `outputs\`
  - Browser map previews: `previews\`
  - Exported PNG/PDF maps and their `.qa.json` reports: `maps\`
  - Project ZIP packages and other exports: `exports\`
- Frontend dependencies: `frontend\node_modules\` (created by `npm ci`)
- Python environment: `.venv\` (created during installation)

Generated maps, uploaded DEMs, AOIs, result rasters/vectors, and project packages remain available in the local folders above; they are not included when the source code is pushed to GitHub. Back up the database and relevant project folders if the data needs to be retained or transferred. Project files are stored locally and are not automatically synchronized to another computer.

## Troubleshooting

- **The page says the backend is unreachable:** make sure the backend window is running, then check <http://127.0.0.1:8000/api/health>.
- **DEM acquisition fails:** confirm internet access and retry. Automatic acquisition uses public Copernicus DEM GLO-30 tiles.
- **WhiteboxTools is unavailable:** confirm the Python `whitebox` dependency installed successfully and allow internet access on the first run so its executable can be obtained.
- **A job fails or produces no layers:** open the project page's **Failed jobs** section and read the reported error. Confirm AOI/DEM validation and the stream threshold where required.
- **A map basemap is blank:** the OpenStreetMap basemap requires internet access; result overlays and local processing files may still be available.
- **A DEM is rejected for missing CRS:** verify the source dataset's documented CRS before assigning one. Do not guess.
- **Port 3000 or 8000 is already in use:** stop the other process using that port, or coordinate with whoever is already running the app.

## Development checks

From the project folder:

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest tests.test_map_rendering tests.test_hydrology_spec tests.test_terrain tests.test_uploaded_dem_no_aoi tests.test_job_layer_storage
python tests\smoke_e2e.py
```

From `frontend`:

```powershell
npm audit
npm run build
```

## External data and attribution

- Copernicus DEM GLO-30 is provided by the Copernicus Programme / ESA via AWS Open Data. Check the current source terms and attribution requirements when redistributing data.
- The interactive basemap uses OpenStreetMap tiles and displays OpenStreetMap attribution. Follow the tile usage policy and attribution requirements.
- Generated map exports are branded **GIS Mapper by buck0001**.

## Sharing with a colleague

Share the application source and this guide, but do not send `frontend\node_modules\` or `.venv\`; they can be recreated using the installation steps. Share `data\gis.db` and the corresponding `data\storage\` project directory only when you intend to transfer saved projects and have checked that you are permitted to share the included inputs and outputs.

The development server is intended for trusted local use, not direct exposure to the public internet. This setup does not provide user authentication or access control.
#   g i s - a p p 
 
 #   g i s _ a p p  
 