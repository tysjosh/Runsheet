"""Portal API routers. ``main.py`` mounts every router in :data:`routers`
unconditionally; the flag answers 404 per request, so route inventories stay
stable whatever the flag says."""
from portal.api.admin_endpoints import router as admin_router
from portal.api.invoice_endpoints import router as invoice_router
from portal.api.me_endpoints import router as me_router
from portal.api.order_endpoints import router as order_router
from portal.api.tank_endpoints import router as tank_router

#: Every portal router, in mount order. Later FEATs append theirs here.
routers = (me_router, admin_router, order_router, tank_router, invoice_router)

__all__ = ["routers"]
