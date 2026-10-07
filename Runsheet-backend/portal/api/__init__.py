"""Portal API routers. ``main.py`` mounts every router in :data:`routers`
unconditionally; the flag answers 404 per request, so route inventories stay
stable whatever the flag says."""
from portal.api.me_endpoints import router as me_router

#: Every portal router, in mount order. Later FEATs append theirs here.
routers = (me_router,)

__all__ = ["routers"]
