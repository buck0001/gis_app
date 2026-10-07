export const API = "";

async function req(path: string, init?: RequestInit) {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      msg = (j as any)?.detail || JSON.stringify(j);
    } catch {
      try { msg = await res.text(); } catch { /* ignore */ }
    }
    throw new Error(msg);
  }
  return res.json();
}

export const api = {
  listProjects: () => req(`/api/projects`),
  createProject: (body: any) =>
    req(`/api/projects`, { method: "POST", body: JSON.stringify(body) }),
  getProject: (id: string) => req(`/api/projects/${id}`),
  setAoi: (id: string, body: any) =>
    req(`/api/projects/${id}/aoi`, { method: "POST", body: JSON.stringify(body) }),
  setBbox: (id: string, body: any) =>
    req(`/api/projects/${id}/aoi/bbox`, { method: "POST", body: JSON.stringify(body) }),
  setPoint: (id: string, body: any) =>
    req(`/api/projects/${id}/aoi/point`, { method: "POST", body: JSON.stringify(body) }),
  uploadAoi: async (id: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`/api/projects/${id}/aoi/upload`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  },
  searchPlaces: (q: string) => req(`/api/aoi/search?q=${encodeURIComponent(q)}`),
  uploadDem: async (id: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`/api/projects/${id}/dem/upload`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(await res.text());
    return res.json();
  },
  confirmDem: (id: string, body: any) =>
    req(`/api/projects/${id}/dem/confirm`, { method: "POST", body: JSON.stringify(body) }),
  demFootprint: (id: string) => req(`/api/projects/${id}/dem/footprint`),
  demCoverage: (id: string) => req(`/api/projects/${id}/dem/coverage`),
  listLayers: (id: string) => req(`/api/projects/${id}/layers`),
  layerDetail: (pid: string, lid: string) => req(`/api/projects/${pid}/layers/${lid}`),
  layerGeojson: (pid: string, lid: string) => req(`/api/projects/${pid}/layers/${lid}/geojson`),
  aiPlan: (id: string, request: string) =>
    req(`/api/projects/${id}/ai/plan`, { method: "POST", body: JSON.stringify({ request }) }),
  aiTools: (id: string) => req(`/api/projects/${id}/ai/tools`),
  quickBasic: (id: string) =>
    req(`/api/projects/${id}/ai/quick/basic-terrain-drainage`, { method: "POST" }),
  runJob: (id: string, body: any) =>
    req(`/api/projects/${id}/jobs`, { method: "POST", body: JSON.stringify(body) }),
  generateContours: (id: string, interval_m: number) =>
    req(`/api/projects/${id}/jobs`, {
      method: "POST",
      body: JSON.stringify({
        title: `Generate contours (${interval_m} m)`,
        plan: [{ tool: "generate_contours", params: { interval_m } }],
      }),
    }),
  listJobs: (id: string) => req(`/api/projects/${id}/jobs`),
  jobDetail: (pid: string, jid: string) => req(`/api/projects/${pid}/jobs/${jid}`),
  cancelJob: (pid: string, jid: string) =>
    req(`/api/projects/${pid}/jobs/${jid}/cancel`, { method: "POST" }),
  makeMap: (id: string, body: any) =>
    req(`/api/projects/${id}/exports/maps`, { method: "POST", body: JSON.stringify(body) }),
  listMaps: (id: string) => req(`/api/projects/${id}/exports/maps`),
  makePackage: (id: string) =>
    req(`/api/projects/${id}/exports/package`, { method: "POST" }),
  exportConfig: (id: string) => req(`/api/projects/${id}/exports/config`),
};

export function fmtArea(km2: number | undefined | null) {
  if (km2 === undefined || km2 === null) return "—";
  return `${km2.toLocaleString(undefined, { maximumFractionDigits: 2 })} km²`;
}
