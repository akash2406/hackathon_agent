"""HTTP routes: chat, direct tool invocation, health probes."""

from fastapi import Request

from ..services import Services


def get_services(request: Request) -> Services:
    return request.app.state.services
