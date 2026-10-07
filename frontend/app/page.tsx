import Link from "next/link";
import HomeClient from "./HomeClient";

export default function Home() {
  return (
    <main style={{ maxWidth: 1100, margin: "0 auto", padding: 20 }}>
      <header className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1 style={{ margin: "6px 0" }}>GIS Mapper by buck0001</h1>
          <p className="muted" style={{ maxWidth: 720 }}>
            Automated GIS analysis — define an AOI, acquire a DEM (or upload your own),
            run terrain + hydrology processing, inspect metadata, and download layers,
            maps and a full project package.
          </p>
        </div>
        <Link href="#new"><button>New GIS Project</button></Link>
      </header>
      <HomeClient />
      <footer className="muted" style={{ marginTop: 24 }}>
        GIS Mapper by buck0001 ·{" "}
        <a href="https://github.com/buck0001/">GitHub</a> ·
        Copernicus GLO-30 DEM · WhiteboxTools hydrology · Leaflet map ·
        FastAPI backend. GEE / LULC / susceptibility arrive in later versions.
      </footer>
    </main>
  );
}
