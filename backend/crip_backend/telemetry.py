"""Logging and Azure Monitor (Application Insights) setup.

Called once at startup. The Application Insights connection string is read
from the Key Vault CSI mount (allocation #8 via #5), never from an environment
variable. Missing telemetry degrades to local logging with a warning instead of
failing startup, because observability should not block answering questions.
"""

from __future__ import annotations

import logging

from .secrets import SecretStore

log = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # The Azure SDK logs request headers at DEBUG; keep it quiet so tokens never reach logs.
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def configure_telemetry(secrets: SecretStore, secret_name: str) -> bool:
    connection_string = secrets.get_optional(secret_name)
    if not connection_string:
        log.warning("Application Insights secret '%s' not mounted; telemetry export disabled", secret_name)
        return False
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(connection_string=connection_string, logger_name="crip_backend")
    return True
