import sys, traceback
sys.path.insert(0, r"c:\Users\SATISFY\Desktop\gis-app")
try:
    from api.routers import ai, aoi, dem, exports, jobs as jr, layers, projects
    print("all router imports OK")
    for m in (ai, aoi, dem, exports, jr, layers, projects):
        print(m.__name__, len(m.router.routes))
except Exception:
    traceback.print_exc()
