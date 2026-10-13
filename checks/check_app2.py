import sys
sys.path.insert(0, r"c:\Users\SATISFY\Desktop\gis-app")
import api.main as m
print("module file:", m.__file__)
import inspect
src = inspect.getsource(m.create_app)
print("has include_router:", "include_router" in src)
print("routers line:", [l for l in src.splitlines() if "include_router" in l])
print("app id in module:", id(m.app), "routes:", len(m.app.routes))
