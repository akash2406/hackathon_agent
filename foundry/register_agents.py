"""Register (create or update, by name) the CRIP agents in Azure AI Foundry Agent Service.

Thin CLI over ``crip_backend.agent_gateway.registration``, the same code the app
runs at startup when ``CRIP_REGISTER_AGENTS_ON_STARTUP=true``.

    az login
    python foundry/register_agents.py --endpoint <project endpoint> --model <deployment>
    python foundry/register_agents.py --dry-run      # print payloads, no Azure call

Endpoint/model default to CRIP_FOUNDRY_PROJECT_ENDPOINT / CRIP_FOUNDRY_MODEL_DEPLOYMENT.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

try:
    import crip_backend  # noqa: F401  (installed: container image or `pip install -e backend`)
except ImportError:  # running from a plain checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from crip_backend.agent_gateway.definitions import load_definitions  # noqa: E402
from crip_backend.agent_gateway.registration import build_agent_specs, upsert_agents  # noqa: E402

DEFINITIONS_DIR = Path(os.environ.get("CRIP_FOUNDRY_DEFINITIONS_DIR", Path(__file__).parent / "definitions"))


async def _register(endpoint: str, model: str) -> dict[str, str]:
    from azure.ai.agents.aio import AgentsClient
    from azure.identity.aio import DefaultAzureCredential

    async with DefaultAzureCredential() as credential, AgentsClient(endpoint=endpoint, credential=credential) as client:
        return await upsert_agents(client, build_agent_specs(load_definitions(DEFINITIONS_DIR)), model)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default=os.environ.get("CRIP_FOUNDRY_PROJECT_ENDPOINT"), help="Foundry project endpoint")
    parser.add_argument("--model", default=os.environ.get("CRIP_FOUNDRY_MODEL_DEPLOYMENT"), help="Model deployment name")
    parser.add_argument("--dry-run", action="store_true", help="Print the agent payloads without calling Foundry")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.dry_run:
        print(json.dumps(build_agent_specs(load_definitions(DEFINITIONS_DIR)), indent=2))
        return 0
    missing = [flag for flag, value in (("--endpoint", args.endpoint), ("--model", args.model)) if not value]
    if missing:
        parser.error(f"missing {', '.join(missing)} (Foundry project endpoint and model deployment name)")
    print(json.dumps(asyncio.run(_register(args.endpoint, args.model)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
