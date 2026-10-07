"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Search } from "lucide-react";
import "leaflet/dist/leaflet.css";

export type Overlay = {
  id: string;
  kind: "raster" | "vector";
  url?: string;
  bounds?: [[number, number], [number, number]];
  geojson?: any;
  opacity?: number;
  style?: Record<string, string | number>;
};

/** Interactive map: OSM basemap, AOI + DEM footprint + result overlays, polygon/rectangle draw. */
export default function MapView(props: {
  aoi: any | null;
  draftAoi: any | null;
  footprint: any | null;
  overlays: Overlay[];
  onDrawn: (geometry: any) => void;
  drawSignal: number;
}) {
  const divRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<any>(null);
  const overlayRef = useRef<Record<string, { layer: any; signature: string }>>({});
  const aoiLayerRef = useRef<any>(null);
  const draftAoiLayerRef = useRef<any>(null);
  const fpLayerRef = useRef<any>(null);
  const onDrawnRef = useRef(props.onDrawn);
  const [mapReady, setMapReady] = useState(false);
  const [drawMode, setDrawMode] = useState<"polygon" | "rectangle">("polygon");
  const [drawRequest, setDrawRequest] = useState(0);
  const [longitude, setLongitude] = useState("");
  const [latitude, setLatitude] = useState("");
  const [coordinateError, setCoordinateError] = useState("");
  onDrawnRef.current = props.onDrawn;

  useEffect(() => {
    let cancelled = false;
    let map: any = null;

    (async () => {
      const L = (await import("leaflet")).default;
      if (cancelled || !divRef.current) return;

      map = L.map(divRef.current).setView([7.07, 6.27], 11);
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "© OpenStreetMap contributors",
      }).addTo(map);
      map._L = L;
      mapRef.current = map;
      setMapReady(true);
    })();

    return () => {
      cancelled = true;
      if (map) map.remove();
      if (mapRef.current === map) mapRef.current = null;
      setMapReady(false);
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || (!props.drawSignal && drawRequest === 0)) return;
    const L = map._L;
    const points: any[] = [];
    let preview: any = null;
    const restoreDoubleClickZoom = map.doubleClickZoom.enabled();
    map.doubleClickZoom.disable();

    const cleanup = () => {
      map.off("click", onClick);
      map.off("dblclick", onDoubleClick);
      if (preview) map.removeLayer(preview);
      if (restoreDoubleClickZoom) map.doubleClickZoom.enable();
    };

    const onClick = (event: any) => {
      if (drawMode === "rectangle" && points.length === 0) {
        points.push(event.latlng);
        return;
      }
      if (drawMode === "rectangle" && points.length === 1) {
        points.push(event.latlng);
        const bounds = L.latLngBounds(points[0], points[1]);
        const geometry = {
          type: "Polygon",
          coordinates: [[
            [bounds.getWest(), bounds.getSouth()],
            [bounds.getEast(), bounds.getSouth()],
            [bounds.getEast(), bounds.getNorth()],
            [bounds.getWest(), bounds.getNorth()],
            [bounds.getWest(), bounds.getSouth()],
          ]],
        };
        cleanup();
        onDrawnRef.current(geometry);
        return;
      }

      points.push(event.latlng);
      if (preview) map.removeLayer(preview);
      preview = L.polyline([...points, points[0]], { color: "#e53e3e" }).addTo(map);
    };

    const onDoubleClick = (event: any) => {
      if (drawMode !== "polygon" || points.length < 3) return;
      event.originalEvent?.preventDefault();
      const ring = points.map((point: any) => [point.lng, point.lat]);
      if (ring.length > 1 && ring[ring.length - 1][0] === ring[ring.length - 2][0]
        && ring[ring.length - 1][1] === ring[ring.length - 2][1]) {
        ring.pop();
      }
      ring.push(ring[0]);
      cleanup();
      onDrawnRef.current({ type: "Polygon", coordinates: [ring] });
    };

    map.on("click", onClick);
    map.on("dblclick", onDoubleClick);
    return cleanup;
  }, [drawMode, drawRequest, mapReady, props.drawSignal]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const L = map._L;

    if (aoiLayerRef.current) {
      map.removeLayer(aoiLayerRef.current);
      aoiLayerRef.current = null;
    }
    if (props.aoi?.geometry) {
      aoiLayerRef.current = L.geoJSON(props.aoi.geometry, {
        style: { color: "#e53e3e", weight: 2, dashArray: "6 4", fillOpacity: 0.06 },
      }).addTo(map);
    }
    if (draftAoiLayerRef.current) {
      map.removeLayer(draftAoiLayerRef.current);
      draftAoiLayerRef.current = null;
    }
    if (props.draftAoi) {
      draftAoiLayerRef.current = L.geoJSON(props.draftAoi, {
        style: { color: "#d97706", weight: 3, fillOpacity: 0.12 },
      }).addTo(map);
    }

    if (fpLayerRef.current) {
      map.removeLayer(fpLayerRef.current);
      fpLayerRef.current = null;
    }
    if (props.footprint) {
      fpLayerRef.current = L.geoJSON(props.footprint, {
        style: { color: "#38bdf8", weight: 1.5, fillOpacity: 0.03 },
      }).addTo(map);
    }

    const focusLayer = aoiLayerRef.current || fpLayerRef.current;
    if (focusLayer) {
      const bounds = focusLayer.getBounds();
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20] });
    }
  }, [mapReady, props.aoi, props.draftAoi, props.footprint]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const L = map._L;
    const seen = new Set(props.overlays.map((overlay) => overlay.id));

    Object.keys(overlayRef.current).forEach((id) => {
      if (!seen.has(id)) {
        map.removeLayer(overlayRef.current[id].layer);
        delete overlayRef.current[id];
      }
    });

    for (const overlay of props.overlays) {
      const signature = JSON.stringify([
        overlay.kind, overlay.url, overlay.bounds, overlay.geojson, overlay.style,
      ]);
      const existing = overlayRef.current[overlay.id];
      if (existing?.signature === signature) {
        existing.layer.setOpacity?.(overlay.opacity ?? 0.85);
        continue;
      }
      if (existing) {
        map.removeLayer(existing.layer);
        delete overlayRef.current[overlay.id];
      }

      let layer: any = null;
      if (overlay.kind === "raster" && overlay.url && overlay.bounds) {
        layer = L.imageOverlay(overlay.url, overlay.bounds, {
          opacity: overlay.opacity ?? 0.85,
        }).addTo(map);
      } else if (overlay.kind === "vector" && overlay.geojson) {
        layer = L.geoJSON(overlay.geojson, {
          style: overlay.style || { color: "#2b6cb0", weight: 2 },
          pointToLayer: (_feature: any, latlng: any) =>
            L.circleMarker(latlng, { radius: 4, color: "#2b6cb0" }),
        }).addTo(map);
        const bounds = layer.getBounds();
        if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20] });
      }
      if (layer) overlayRef.current[overlay.id] = { layer, signature };
    }
  }, [mapReady, props.overlays]);

  const startDrawing = (mode: "polygon" | "rectangle") => {
    setDrawMode(mode);
    setDrawRequest((request) => request + 1);
  };

  const searchCoordinates = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const lon = Number(longitude);
    const lat = Number(latitude);
    if (!longitude.trim() || !latitude.trim() || !Number.isFinite(lon)
      || !Number.isFinite(lat) || lon < -180 || lon > 180 || lat < -90 || lat > 90) {
      setCoordinateError("Enter a valid longitude (-180 to 180) and latitude (-90 to 90).");
      return;
    }
    const map = mapRef.current;
    if (!map) {
      setCoordinateError("The map is still loading. Try again shortly.");
      return;
    }
    setCoordinateError("");
    map.setView([lat, lon], Math.max(map.getZoom(), 14));
  };

  return (
    <div>
      <form className="row" onSubmit={searchCoordinates} style={{ marginBottom: 8 }}>
        <label htmlFor="map-longitude" style={{ margin: 0 }}>Longitude</label>
        <input
          id="map-longitude"
          type="number"
          step="any"
          min="-180"
          max="180"
          value={longitude}
          onChange={(event) => setLongitude(event.target.value)}
          placeholder="e.g. 3.3792"
          style={{ maxWidth: 150 }}
        />
        <label htmlFor="map-latitude" style={{ margin: 0 }}>Latitude</label>
        <input
          id="map-latitude"
          type="number"
          step="any"
          min="-90"
          max="90"
          value={latitude}
          onChange={(event) => setLatitude(event.target.value)}
          placeholder="e.g. 6.5244"
          style={{ maxWidth: 150 }}
        />
        <button type="submit" className="secondary">
          <Search size={16} aria-hidden="true" /> Search coordinates
        </button>
      </form>
      {coordinateError && <p className="issue error" role="alert">{coordinateError}</p>}
      <div className="row" style={{ marginBottom: 8 }}>
        <button type="button" className="secondary" onClick={() => startDrawing("polygon")}>
          Draw polygon (click, double-click to finish)
        </button>
        <button type="button" className="secondary" onClick={() => startDrawing("rectangle")}>
          Draw rectangle (2 clicks)
        </button>
      </div>
      <p className="muted" style={{ margin: "0 0 8px" }}>
        Finish a polygon with a double-click, or a rectangle with two opposite-corner clicks.
        Then choose <strong>Save AOI</strong> below the map before running analysis.
      </p>
      <div
        ref={divRef}
        style={{ height: 480, borderRadius: 12, overflow: "hidden", border: "1px solid var(--line)" }}
      />
      <p className="muted">
        Basemap: OpenStreetMap. Raster results render as styled previews; vectors overlay from GeoJSON.
        {props.draftAoi && " Unsaved AOI shown in amber; save it below to use it for analysis."}{" "}
        <Link href="/">← projects</Link>
      </p>
    </div>
  );
}
