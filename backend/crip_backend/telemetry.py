"""Logging and Azure Monitor (Application Insights) setup.

Called once at startup. The connection string comes from the standard App
Service setting ``APPLICATIONINSIGHTS_CONNECTION_STRING`` (set by Bicep) or the
secret store. Missing telemetry degrades to local logging with a warning instead
of failing startup, because observability should not block answering questions.
"""

from __future__ import annotations

import logging
import os

from .secrets import SecretStore

log = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # The Azure SDK logs request headers at DEBUG; keep it quiet so tokens never reach logs.
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def configure_telemetry(secrets: SecretStore, secret_name: str) -> bool:
    # App Service convention first (set by Bicep from the App Insights resource), then the secret store.
    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING") or secrets.get_optional(secret_name)
    if not connection_string:
        log.warning("Application Insights not configured (APPLICATIONINSIGHTS_CONNECTION_STRING / secret '%s'); telemetry export disabled", secret_name)
        return False
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(connection_string=connection_string, logger_name="crip_backend")
    return True
