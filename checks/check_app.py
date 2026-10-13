import sys, traceback
sys.path.insert(0, r"c:\Users\SATISFY\Desktop\gis-app")
try:
    from api.main import app
    print("total routes:", len(app.routes))
    for r in app.routes:
        if hasattr(r, "path") and r.path.startswith("/api/"):
            print(" ", r.path)
except Exception:
    traceback.print_exc()
