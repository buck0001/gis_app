"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Download, Map as MapIcon } from "lucide-react";
import { api, fmtArea } from "../../../lib/api";
import MapView, { type Overlay } from "./MapView";

type Project = {
  id: string;
  name: string;
  country?: string;
  region?: string;
  lga?: string;
  dem_mode?: string;
  dem_source?: string | null;
  dem_validation?: any;
  dem_confirm?: any;
  analysis_crs?: string;
  aoi?: any;
};

type Layer = {
  id: string;
  layer_key: string;
  name: string;
  type: "raster" | "vector";
  layer_group?: string;
  units?: string;
  crs?: string;
  resolution?: number | string;
  hidden?: boolean;
  stats?: any;
  preview?: {
    file?: string;
    bounds_4326?: [number, number, number, number];
    legend?: any[];
    error?: string;
  };
  download_url?: string;
};

type Job = {
  id: string;
  title: string;
  status: string;
  progress?: number;
  message?: string;
  error?: string;
  plan?: { tool: string }[];
};

export default function ProjectClient({ id }: { id: string }) {
  const [project, setProject] = useState<Project | null>(null);
  const [layers, setLayers] = useState<Layer[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [footprint, setFootprint] = useState<any | null>(null);
  const [visibleLayerIds, setVisibleLayerIds] = useState<string[]>([]);
  const [overlays, setOverlays] = useState<Overlay[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<React.ReactNode>("");
  const [busy, setBusy] = useState("");
  const [savingAoi, setSavingAoi] = useState(false);
  const [drawSignal, setDrawSignal] = useState(0);
  const [draftAoi, setDraftAoi] = useState<any | null>(null);
  const [aoiName, setAoiName] = useState("AOI");
  const [planRequest, setPlanRequest] = useState("Generate the basic terrain and drainage analysis.");
  const [plan, setPlan] = useState<any | null>(null);
  const [uploadValidation, setUploadValidation] = useState<any | null>(null);
  const [epsg, setEpsg] = useState("");
  const [maps, setMaps] = useState<any[]>([]);
  const [streamThreshold, setStreamThreshold] = useState("");
  const [contourInterval, setContourInterval] = useState("10");
  const [mapFormat, setMapFormat] = useState<"png" | "pdf">("png");
  const [flowDirectionArrows, setFlowDirectionArrows] = useState(false);

  const refresh = useCallback(async () => {
    const [projectData, layerData, jobData, mapData] = await Promise.all([
      api.getProject(id),
      api.listLayers(id),
      api.listJobs(id),
      api.listMaps(id),
    ]);
    setProject(projectData);
    setLayers(layerData);
    setJobs(jobData);
    setMaps(mapData);
    if (projectData.aoi?.name) setAoiName(projectData.aoi.name);
    if (projectData.dem_validation) setUploadValidation(projectData.dem_validation);
    if (projectData.dem_source) {
      try {
        setFootprint(await api.demFootprint(id));
      } catch {
        setFootprint(null);
      }
    } else {
      setFootprint(null);
    }
  }, [id]);

  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        await refresh();
        if (mounted) setError("");
      } catch (reason) {
        if (mounted) setError(reason instanceof Error ? reason.message : String(reason));
      } finally {
        if (mounted) setLoading(false);
      }
    })();
    return () => { mounted = false; };
  }, [refresh]);

  const hasRunningJob = jobs.some((job) => job.status === "QUEUED" || job.status === "RUNNING");
  useEffect(() => {
    if (!hasRunningJob) return;
    const timer = window.setInterval(() => {
      refresh().catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
    }, 2500);
    return () => window.clearInterval(timer);
  }, [hasRunningJob, refresh]);

  useEffect(() => {
    let cancelled = false;
    const selected = layers.filter((layer) => visibleLayerIds.includes(layer.id) && !layer.hidden);

    Promise.all(selected.map(async (layer): Promise<Overlay | null> => {
      if (layer.type === "raster") {
        const bounds = layer.preview?.bounds_4326;
        const file = layer.preview?.file;
        if (!bounds || !file) return null;
        const [west, south, east, north] = bounds;
        return {
          id: layer.id,
          kind: "raster",
          url: `/files/${id}/${file}`,
          bounds: [[south, west], [north, east]],
        };
      }
      const geojson = await api.layerGeojson(id, layer.id);
      return {
        id: layer.id,
        kind: "vector",
        geojson,
        style: layer.layer_key === "contours"
          ? { color: "#15803d", weight: 1.5 }
          : undefined,
      };
    })).then((items) => {
      if (!cancelled) setOverlays(items.filter((item): item is Overlay => item !== null));
    }).catch((reason) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason));
    });

    return () => { cancelled = true; };
  }, [id, layers, visibleLayerIds]);

  const activeJobs = useMemo(
    () => jobs.filter((job) => job.status === "QUEUED" || job.status === "RUNNING"),
    [jobs],
  );
  const canRunWithoutAoi = project?.dem_mode === "upload"
    && Boolean(project.dem_source)
    && ["continue", "fill_nodata", "assign_crs"].includes(project.dem_confirm?.action);

  async function perform(label: string, action: () => Promise<any>, success?: string) {
    setBusy(label);
    setError("");
    setNotice("");
    try {
      const result = await action();
      if (success) setNotice(success);
      return result;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      return null;
    } finally {
      setBusy("");
    }
  }

  async function stageAoi(geometry: any) {
    setDraftAoi(geometry);
    setNotice("");
    setError("");
  }

  async function saveAoi() {
    if (!draftAoi || savingAoi) return;
    setSavingAoi(true);
    setError("");
    setNotice("");
    try {
      await api.setAoi(id, {
        geometry: draftAoi,
        name: aoiName || "AOI",
        input_method: "draw",
      });
      setDraftAoi(null);
      setNotice("AOI saved.");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSavingAoi(false);
    }
  }

  function discardAoi() {
    if (savingAoi || !draftAoi) return;
    setDraftAoi(null);
    setNotice("Unsaved AOI discarded. The saved AOI is unchanged.");
    setError("");
  }

  async function uploadAoi(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const file = new FormData(form).get("aoi-file");
    if (!(file instanceof File) || !file.size) {
      setError("Choose an AOI file to upload.");
      return;
    }
    const result = await perform("Uploading AOI", () => api.uploadAoi(id, file), "AOI uploaded.");
    if (result) {
      setDraftAoi(null);
      await refresh();
    }
    form.reset();
  }

  async function uploadDem(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const file = new FormData(form).get("dem-file");
    if (!(file instanceof File) || !file.size) {
      setError("Choose a GeoTIFF DEM to upload.");
      return;
    }
    const result = await perform("Inspecting DEM", () => api.uploadDem(id, file));
    if (result) {
      setUploadValidation(result);
      setNotice("DEM inspected. Review validation and confirm before running analysis.");
      await refresh();
    }
    form.reset();
  }

  async function confirmDem(action: string) {
    const result = await perform(
      "Confirming DEM",
      () => api.confirmDem(id, { action, epsg: epsg || undefined }),
      "DEM confirmation saved.",
    );
    if (result) {
      if (action === "assign_crs") setUploadValidation(result);
      await refresh();
    }
  }

  async function createPlan() {
    const result = await perform("Planning analysis", () => api.aiPlan(id, planRequest));
    if (result) setPlan(result);
  }

  async function useQuickPlan() {
    const result = await perform("Planning analysis", () => api.quickBasic(id));
    if (result) setPlan(result);
  }

  async function runAnalysis() {
    if (!plan?.plan?.length) {
      setError("Create an analysis plan before running.");
      return;
    }
    const streamTools = new Set([
      "extract_streams", "calculate_stream_order",
      "extract_drainage_network", "delineate_watershed",
      "calculate_drainage_density", "calculate_distance_to_drainage",
    ]);
    const needsThreshold = plan.plan.some((item: any) => streamTools.has(item.tool));
    const plannedThreshold = plan.plan.find((item: any) =>
      item.tool === "extract_streams" && item.params?.threshold_cells != null)
      ?.params?.threshold_cells;
    const threshold = plannedThreshold ?? (
      streamThreshold.trim() ? Number(streamThreshold) : undefined
    );
    if (needsThreshold && (!Number.isInteger(threshold) || threshold < 1)) {
      setError("Enter a positive integer stream threshold in cells, or include one in your plan request.");
      return;
    }
    const result = await perform(
      "Starting analysis",
      () => api.runJob(id, {
        title: plan.request || "GIS analysis",
        plan: plan.plan,
        stream_threshold: plannedThreshold == null ? threshold : undefined,
      }),
      "Analysis queued.",
    );
    if (result) await refresh();
  }

  async function generateContours() {
    const interval = Number(contourInterval);
    if (!Number.isFinite(interval) || interval <= 0) {
      setError("Enter a positive contour interval in metres.");
      return;
    }
    const result = await perform(
      "Generating contours",
      () => api.generateContours(id, interval),
      "Contour generation queued.",
    );
    if (result) await refresh();
  }

  async function checkCoverage() {
    const result = await perform("Checking DEM coverage", () => api.demCoverage(id));
    if (result) setNotice(JSON.stringify(result, null, 2));
  }

  async function exportMap(layer: Layer) {
    const result = await perform(
      `Exporting ${layer.name}`,
      () => api.makeMap(id, {
        layer_id: layer.id,
        fmt: mapFormat,
        flow_direction_arrows:
          layer.layer_key === "flow_direction" && flowDirectionArrows,
      }),
      "Map export created.",
    );
    if (result) {
      const mapDownload = { ...result, layer_id: layer.id };
      setMaps((current) => [
        ...current.filter((item) => item.layer_id !== layer.id),
        mapDownload,
      ]);
      setNotice(
        <span>
          Map export created. Client-readiness QA:{" "}
          <strong>{result.qa_report?.status || "report unavailable"}</strong>.{" "}
          {result.qa_download_url && (
            <a href={result.qa_download_url} download>
              Download QA report
            </a>
          )}
          {result.qa_report?.status === "NOT CLIENT-READY"
            && " Resolve the failed checks before client delivery."}
        </span>,
      );
      const link = document.createElement("a");
      link.href = result.download_url;
      link.download = `${layer.layer_key}.${mapFormat}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      await refresh();
    }
  }

  async function exportPackage() {
    const result = await perform("Creating project package", () => api.makePackage(id));
    if (result) setNotice(<a href={result.download_url}>Download project package</a>);
  }

  if (loading) return <p className="muted">Loading project…</p>;
  if (!project) {
    return (
      <section className="card">
        <p className="issue error">{error || "Project not found."}</p>
        <Link href="/">← Projects</Link>
      </section>
    );
  }

  return (
    <>
      <header className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
        <div>
          <Link href="/">← Projects</Link>
          <h1 style={{ margin: "8px 0" }}>{project.name}</h1>
          <p className="muted">
            {[project.country, project.region, project.lga].filter(Boolean).join(" · ") || "GIS project"}
            {" · "}{project.analysis_crs || "Analysis CRS is set when the AOI is saved"}
          </p>
        </div>
        <button type="button" className="secondary" onClick={() => refresh().catch((e) => setError(String(e)))}>
          Refresh
        </button>
      </header>

      {error && <p className="issue error" role="alert">{error}</p>}
      {notice && <p className="issue info" role="status">{notice}</p>}

      <div className="grid2">
        <section className="card">
          <h2 style={{ marginTop: 4 }}>Map and analysis layers</h2>
          <MapView
            aoi={project.aoi}
            draftAoi={draftAoi}
            footprint={footprint}
            overlays={overlays}
            onDrawn={stageAoi}
            drawSignal={drawSignal}
          />
          <div className="row" style={{ marginTop: 8 }}>
            <label htmlFor="aoi-name" style={{ margin: 0 }}>AOI name</label>
            <input
              id="aoi-name"
              value={aoiName}
              onChange={(event) => setAoiName(event.target.value)}
              style={{ maxWidth: 280 }}
            />
            <button type="button" onClick={() => setDrawSignal((signal) => signal + 1)}>
              Start drawing AOI
            </button>
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <span className="muted" role="status">
              {draftAoi
                  ? "AOI drawing is ready. Save it to enable analysis."
                : project.aoi
                  ? "Saved AOI is ready for analysis."
                    : "Draw an AOI on the map, then save it before analysis."}
            </span>
            <button
              type="button"
              onClick={saveAoi}
              disabled={savingAoi || !draftAoi}
            >
              {savingAoi ? "Saving AOI…" : "Save AOI"}
            </button>
            <button
              type="button"
              className="secondary"
              onClick={discardAoi}
              disabled={savingAoi || !draftAoi}
            >
              Discard AOI
            </button>
          </div>
          {project.aoi && (
            <p className="muted">
              {project.aoi.name} · {fmtArea(project.aoi.area_km2)} · {project.aoi.analysis_crs}
            </p>
          )}
          <form className="row" onSubmit={uploadAoi} style={{ marginTop: 8 }}>
            <label htmlFor="aoi-file" style={{ margin: 0 }}>Upload AOI</label>
            <input id="aoi-file" name="aoi-file" type="file" accept=".geojson,.json,.zip,.kml,.kmz,.gpkg" />
            <button type="submit" className="secondary" disabled={!!busy}>Upload</button>
          </form>
          <div style={{ marginTop: 16 }}>
            <h3>Result layers</h3>
            <div className="row" style={{ marginBottom: 12 }}>
              <label htmlFor="contour-interval" style={{ margin: 0 }}>
                Contour interval (m)
              </label>
              <input
                id="contour-interval"
                type="number"
                min="0.01"
                step="any"
                value={contourInterval}
                onChange={(event) => setContourInterval(event.target.value)}
                style={{ maxWidth: 140 }}
              />
              <button
                type="button"
                className="secondary"
                onClick={generateContours}
                disabled={!!busy || activeJobs.length > 0
                  || (!project.aoi && !canRunWithoutAoi)}
              >
                Generate contour layer
              </button>
            </div>
            <label htmlFor="map-format">Export format</label>
            <select
              id="map-format"
              value={mapFormat}
              onChange={(event) => setMapFormat(event.target.value as "png" | "pdf")}
              style={{ maxWidth: 160, marginBottom: 10 }}
            >
              <option value="png">PNG</option>
              <option value="pdf">PDF</option>
            </select>
            {!layers.length && <p className="muted">Analysis results will appear here when a job completes.</p>}
            {layers.map((layer) => (
              <div className="checkline" key={layer.id}>
                <input
                  id={`layer-${layer.id}`}
                  type="checkbox"
                  checked={visibleLayerIds.includes(layer.id)}
                  onChange={(event) => setVisibleLayerIds((current) =>
                    event.target.checked
                      ? [...current, layer.id]
                      : current.filter((layerId) => layerId !== layer.id))}
                />
                <label htmlFor={`layer-${layer.id}`} style={{ margin: 0 }}>
                  <strong>{layer.name}</strong>
                  <span className="muted"> · {layer.layer_group || layer.type}{layer.units ? ` · ${layer.units}` : ""}</span>
                  {layer.preview?.error
                    && <span className="issue error">Map preview unavailable: {layer.preview.error}</span>}
                </label>
                <div className="row" style={{ marginLeft: "auto" }}>
                  {layer.layer_key === "flow_direction" && (
                    <label className="row" style={{ margin: 0 }}>
                      <input
                        type="checkbox"
                        checked={flowDirectionArrows}
                        onChange={(event) =>
                          setFlowDirectionArrows(event.target.checked)}
                      />
                      Arrow map
                    </label>
                  )}
                  {layer.download_url && <a href={layer.download_url}>Download</a>}
                  {maps.find((map) => map.layer_id === layer.id)?.download_url
                    && <a href={maps.find((map) => map.layer_id === layer.id)?.download_url}
                      download>
                      <Download size={14} aria-hidden="true" /> Download map
                    </a>}
                  {maps.find((map) => map.layer_id === layer.id)?.qa_download_url
                    && <a href={maps.find((map) => map.layer_id === layer.id)?.qa_download_url}
                      download>QA report</a>}
                  <button type="button" className="secondary" onClick={() => exportMap(layer)} disabled={!!busy || !project.aoi}>
                    <MapIcon size={16} aria-hidden="true" /> Export map
                  </button>
                </div>
              </div>
            ))}
          </div>
        </section>

        <div>
          <section className="card" style={{ marginBottom: 12 }}>
            <h2 style={{ marginTop: 4 }}>Area of interest</h2>
            {project.aoi
              ? <p className="muted">The saved AOI is shown on the map. Draw a new polygon or rectangle to replace it.</p>
              : canRunWithoutAoi
                ? <p className="muted">No AOI is needed for this upload. Analysis will use the full uploaded DEM footprint; you can still draw an AOI to limit the area.</p>
                : <p className="muted">Draw a polygon or rectangle on the map, or upload a supported boundary file.</p>}
            {project.aoi?.warnings?.map((warning: string, index: number) =>
              <p className="issue" key={`${warning}-${index}`}>{warning}</p>)}
          </section>

          <section className="card" style={{ marginBottom: 12 }}>
            <h2 style={{ marginTop: 4 }}>DEM</h2>
            <p className="muted">
              Source: {project.dem_mode === "upload" ? "Uploaded GeoTIFF" : "Copernicus GLO-30 (automatic acquisition)"}
            </p>
            {project.dem_mode === "upload" && (
              <>
                <form className="row" onSubmit={uploadDem}>
                  <input name="dem-file" type="file" accept=".tif,.tiff" aria-label="Upload DEM GeoTIFF" />
                  <button type="submit" className="secondary" disabled={!!busy}>Inspect DEM</button>
                </form>
                {uploadValidation && (
                  <div>
                    <h3>DEM validation</h3>
                    <pre className="scroll">{JSON.stringify(uploadValidation, null, 2)}</pre>
                    <div className="row">
                      <button type="button" onClick={() => confirmDem("continue")} disabled={!!busy}>
                        Confirm DEM
                      </button>
                      <button type="button" className="secondary" onClick={() => confirmDem("fill_nodata")} disabled={!!busy}>
                        Fill NoData during processing
                      </button>
                    </div>
                    {uploadValidation?.issues?.some((issue: any) => issue.code === "missing_crs" || issue.code === "crs_missing" || issue.rule === "crs_missing") && (
                      <div className="row" style={{ marginTop: 8 }}>
                        <input value={epsg} onChange={(event) => setEpsg(event.target.value)} placeholder="EPSG:32632" aria-label="DEM CRS EPSG code" />
                        <button type="button" className="secondary" onClick={() => confirmDem("assign_crs")} disabled={!!busy || !epsg}>
                          Assign CRS
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
            {project.dem_mode === "upload" && project.dem_source && (
              <button type="button" className="secondary" onClick={checkCoverage} disabled={!!busy || !project.aoi}>
                Check AOI coverage
              </button>
            )}
            {project.dem_mode !== "upload" && (
              <p className="muted">The DEM is acquired automatically when the first analysis plan runs.</p>
            )}
          </section>

          <section className="card" style={{ marginBottom: 12 }}>
            <h2 style={{ marginTop: 4 }}>Analysis</h2>
            <label htmlFor="plan-request">Describe the products you need</label>
            <textarea
              id="plan-request"
              rows={3}
              value={planRequest}
              onChange={(event) => setPlanRequest(event.target.value)}
            />
            {plan?.plan?.some((item: any) => [
              "extract_streams", "calculate_stream_order",
              "extract_drainage_network", "delineate_watershed",
              "calculate_drainage_density", "calculate_distance_to_drainage",
            ].includes(item.tool))
              && !plan.plan.some((item: any) =>
                item.tool === "extract_streams"
                && item.params?.threshold_cells != null)
              && (
                <div>
                  <label htmlFor="stream-threshold">Stream threshold (flow-accumulation cells)</label>
                  <input
                    id="stream-threshold"
                    type="number"
                    min="1"
                    step="1"
                    value={streamThreshold}
                    onChange={(event) => setStreamThreshold(event.target.value)}
                    placeholder="Set from your method or study design"
                  />
                  <p className="muted">No threshold is assumed. The chosen value will be recorded in the processing metadata.</p>
                </div>
              )}
            <div className="row" style={{ marginTop: 8 }}>
              <button type="button" className="secondary" onClick={createPlan} disabled={!!busy || !planRequest.trim()}>
                Preview plan
              </button>
              <button type="button" className="secondary" onClick={useQuickPlan} disabled={!!busy}>
                Basic terrain + drainage
              </button>
            </div>
            {plan && (
              <div style={{ marginTop: 12 }}>
                <strong>Plan for {plan.dem_line}</strong>
                <p className="muted">Estimated processing time: {plan.estimated}</p>
                <ul>
                  {(plan.checklist || []).map((step: string, index: number) => <li key={`${step}-${index}`}>{step}</li>)}
                </ul>
                {(plan.notes || []).map((note: string, index: number) =>
                  <p className="issue" key={`${note}-${index}`}>{note}</p>)}
                <button type="button" onClick={runAnalysis} disabled={!!busy || (!project.aoi && !canRunWithoutAoi) || activeJobs.length > 0}>
                  Run analysis
                </button>
                {!project.aoi && !canRunWithoutAoi && <p className="muted">Save an AOI before starting analysis, or confirm an uploaded DEM to use its footprint.</p>}
              </div>
            )}
            {!!activeJobs.length && (
              <div style={{ marginTop: 14 }}>
                <h3>Processing</h3>
                {activeJobs.map((job) => (
                  <div key={job.id} style={{ marginBottom: 10 }}>
                    <p><span className={`status ${job.status}`}>{job.status}</span> · {job.title}</p>
                    <div className="progress"><div style={{ width: `${Math.min(100, job.progress || 0)}%` }} /></div>
                    <p className="muted">{job.message || "Waiting for a worker…"}</p>
                    <button type="button" className="secondary" onClick={() =>
                      perform("Cancelling job", () => api.cancelJob(id, job.id), "Cancellation requested.")}
                    >
                      Cancel
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="card">
            <h2 style={{ marginTop: 4 }}>Exports</h2>
            <button type="button" className="secondary" onClick={exportPackage} disabled={!!busy || !layers.length || !project.aoi}>
              Download project package
            </button>
            {!!maps.length && (
              <ul>
                {maps.map((map) => (
                  <li key={map.id}>
                    <a href={map.download_url} download>{map.title || "Map export"}</a>
                    {" · QA: "}
                    <strong>{map.qa_report?.status || "not run"}</strong>
                    {map.qa_download_url
                      && <> · <a href={map.qa_download_url} download>Report</a></>}
                  </li>
                ))}
              </ul>
            )}
            <p className="muted">Map exports are available per result layer above. Package exports include completed project data.</p>
          </section>
        </div>
      </div>
      {busy && <p className="muted" role="status">{busy}…</p>}
      {jobs.some((job) => job.status === "FAILED") && (
        <section className="card" style={{ marginTop: 12 }}>
          <h2>Failed jobs</h2>
          {jobs.filter((job) => job.status === "FAILED").map((job) => (
            <p className="issue error" key={job.id}>{job.title}: {job.error || job.message || "Processing failed."}</p>
          ))}
        </section>
      )}
    </>
  );
}
