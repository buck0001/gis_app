"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { api, fmtArea } from "../lib/api";

export default function HomeClient() {
  const [projects, setProjects] = useState<any[]>([]);
  const [err, setErr] = useState("");
  async function load() {
    try { setProjects(await api.listProjects()); }
    catch (e: any) { setErr(e.message); }
  }
  useEffect(() => { load(); }, []);
  return (
    <>
      <section className="card" id="new" style={{ marginTop: 16 }}>
        <h2 style={{ margin: "4px 0 10px" }}>New GIS Project</h2>
        <NewProjectForm onCreated={load} />
      </section>
      <section style={{ marginTop: 16 }}>
        <h2>Projects</h2>
        {err && <p className="muted">Backend not reachable: {err}</p>}
        {projects.length === 0 && !err && <p className="muted">No projects yet — create one above.</p>}
        <div className="grid3">
          {projects.map((p) => (
            <div className="card" key={p.id}>
              <strong>{p.name}</strong>
              <p className="muted">{[p.country, p.region, p.lga].filter(Boolean).join(" · ") || "—"}</p>
              <p className="muted mono">DEM: {p.dem_mode} · {p.analysis_crs || "CRS set with AOI"}</p>
              <Link href={`/projects/${p.id}`}><button className="secondary">Open</button></Link>
            </div>
          ))}
        </div>
      </section>
    </>
  );
}

function NewProjectForm({ onCreated }: { onCreated: () => void }) {
  const [f, setF] = useState({ name: "", country: "Nigeria", region: "", lga: "", dem_mode: "acquire" });
  const [msg, setMsg] = useState("");
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  async function submit(e: any) {
    e.preventDefault();
    setMsg("");
    if (!f.name.trim()) { setMsg("Project name is required."); return; }
    try {
      const p = await api.createProject(f);
      setMsg(`Created “${p.name}”. Opening…`);
      onCreated();
      window.location.href = `/projects/${p.id}`;
    } catch (e: any) { setMsg(e.message); }
  }
  return (
    <form onSubmit={submit} className="grid3">
      <div><label>Project name</label><input value={f.name} onChange={set("name")} placeholder="Auchi catchment" /></div>
      <div><label>Country</label><input value={f.country} onChange={set("country")} /></div>
      <div><label>State / region</label><input value={f.region} onChange={set("region")} placeholder="Edo State" /></div>
      <div><label>LGA / district</label><input value={f.lga} onChange={set("lga")} placeholder="Etsako West" /></div>
      <div>
        <label>DEM source</label>
        <select value={f.dem_mode} onChange={set("dem_mode")}>
          <option value="acquire">Don’t have a DEM? Get one automatically</option>
          <option value="upload">Upload your DEM (.tif)</option>
        </select>
      </div>
      <div style={{ alignSelf: "end" }}><button type="submit">Create project</button>
        {msg && <p className="muted">{msg}</p>}</div>
    </form>
  );
}

export { fmtArea };
