"""Platform-admin endpoints (``CRIP.PlatformAdmin`` only): usage log and settings / health.

Azure-facing admin views (access review, estate overview) are tools, so they
work both on the admin pages (via /api/tools) and in chat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query

from ..access.graph import GraphError
from ..access.model import UserAccess
from ..services import Services
from . import get_services
from .deps import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/usage")
async def usage(
    days: int = Query(default=7, ge=1, le=90),
    _admin: UserAccess = Depends(require_admin),
    services: Services = Depends(get_services),
) -> dict:
    report = await services.repository.usage(since=datetime.now(UTC) - timedelta(days=days), limit=200)
    return {"days": days, **report}


@router.get("/settings")
async def settings_view(
    admin: UserAccess = Depends(require_admin),
    services: Services = Depends(get_services),
) -> dict:
    s = services.settings
    graph_status = "not configured"
    if services.graph is not None:
        try:
            await services.graph.principals([admin.object_id])
            graph_status = "ok"
        except GraphError as exc:
            graph_status = f"error {exc.status}: grant Directory.Read.All to CRIP's identity"
    return {
        "access_mode": s.azure_access_mode.value,
        "management_group_id": s.management_group_id,
        "explicit_subscriptions": [x for x in s.subscription_ids.split(",") if x.strip()],
        "rbac_access_check": s.rbac_access_check,
        "group_mappings": {
            "platform_admin": bool(s.platform_admin_group_ids),
            "cost_reader": bool(s.cost_reader_group_ids),
            "reader": bool(s.reader_group_ids),
        },
        "access_cache_seconds": s.access_cache_seconds,
        "subscriptions_in_scope": len(admin.subscriptions),
        "agents": services.agent_names,
        "foundry_project_endpoint": s.foundry_project_endpoint,
        "foundry_model_deployment": s.foundry_model_deployment,
        "register_agents_on_startup": s.register_agents_on_startup,
        "graph": graph_status,
        "storage": "postgresql" if services.repository.__class__.__name__.startswith("Postgres") else "sqlite",
        "environment": s.environment,
    }
