"""
The API router, assembled from one module per resource.

Every sub-router is built with
:class:`~wasm.web.api.deps.WASMErrorRoute`, so a manager error becomes an HTTP
response with a status that matches what went wrong, instead of the ``500`` a
per-handler ``except Exception`` used to produce.

:func:`~wasm.web.api.deps.install_error_handlers` is re-exported here so the
application can register the same translation for anything raised outside a
route, such as in a dependency.
"""

from fastapi import APIRouter

from wasm.web.api.apps import router as apps_router
from wasm.web.api.auth import router as auth_router
from wasm.web.api.backup_schedules import router as backup_schedules_router
from wasm.web.api.backups import router as backups_router
from wasm.web.api.certs import router as certs_router
from wasm.web.api.config import router as config_router
from wasm.web.api.cron import router as cron_router
from wasm.web.api.databases import router as databases_router
from wasm.web.api.deps import install_error_handlers
from wasm.web.api.jobs import router as jobs_router
from wasm.web.api.metrics import router as metrics_router
from wasm.web.api.monitor import router as monitor_router
from wasm.web.api.services import router as services_router
from wasm.web.api.sites import router as sites_router
from wasm.web.api.system import router as system_router

__all__ = ["install_error_handlers", "router"]

router = APIRouter()

router.include_router(auth_router, prefix="/auth", tags=["Authentication"])
router.include_router(apps_router, prefix="/apps", tags=["Applications"])
router.include_router(services_router, prefix="/services", tags=["Services"])
router.include_router(sites_router, prefix="/sites", tags=["Sites"])
router.include_router(certs_router, prefix="/certs", tags=["Certificates"])
router.include_router(system_router, prefix="/system", tags=["System"])
router.include_router(monitor_router, prefix="/monitor", tags=["Monitor"])
router.include_router(metrics_router, prefix="/metrics", tags=["Metrics"])
# The jobs router carries its own "/jobs" prefix.
router.include_router(jobs_router, tags=["Jobs"])
router.include_router(config_router, prefix="/config", tags=["Configuration"])
router.include_router(backups_router, prefix="/backups", tags=["Backups"])
router.include_router(
    backup_schedules_router, prefix="/backup-schedules", tags=["Backup Schedules"]
)
router.include_router(databases_router, prefix="/databases", tags=["Databases"])
router.include_router(cron_router, prefix="/cron", tags=["Cron Jobs"])
