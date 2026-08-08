"""Dependency injection providers.

These are the composition root for every route — settings, clients, services.
The app factory wires the real implementations; tests override via
app.state or dependency_overrides.
"""

from typing import Annotated, cast

from fastapi import Depends, Request

from app.core.config import Settings


async def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


SettingsDep = Annotated[Settings, Depends(get_settings)]
