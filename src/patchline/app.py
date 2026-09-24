"""FastAPI application factory (§6: src/patchline/app.py)."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import APP_VERSION, config
from .adapters.engine import create_adapter
from .adapters.scm.mock_github import FixtureGitHubClient
from .auth import SessionMiddleware
from .core import config_store
from .db import session_scope
from .routes import ALL_ROUTERS

log = logging.getLogger("patchline.app")

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(_PACKAGE_DIR, "templates")
STATIC_DIR = os.path.join(_PACKAGE_DIR, "static")


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # First-startup seeding (CP-6/FR-026, FR-038): no-op when tables are
        # absent (healthz reports disconnected until `migrate` runs).
        try:
            with session_scope() as sess:
                config_store.seed_default_language(sess)
                config_store.instance_id(sess)
        except Exception as exc:  # noqa: BLE001
            log.warning("config seeding skipped (database not ready): %s", exc)
        yield

    app = FastAPI(title="Patchline", version=APP_VERSION, lifespan=lifespan)

    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from fastapi.templating import Jinja2Templates
    import json as _json

    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    )
    # tojson is NOT a stock Jinja filter (it is Flask-provided); register it.
    env.filters["tojson"] = lambda value, **kwargs: _json.dumps(value, **kwargs)
    app.state.templates = Jinja2Templates(env=env)
    app.state.engine = create_adapter()
    app.state.app_version = APP_VERSION

    _fixture_scm = {"client": None}

    def get_scm(real: bool = False):
        """Demo mode → fixture-backed client (FR-060b); otherwise the real
        GitHub client. ``real=True`` is for the OAuth leg only."""
        if config.demo_mode() and not real:
            if _fixture_scm["client"] is None:
                _fixture_scm["client"] = FixtureGitHubClient()
            return _fixture_scm["client"]
        from .adapters.scm.github import GitHubClient

        return GitHubClient()

    app.state.get_scm = get_scm

    for router in ALL_ROUTERS:
        app.include_router(router)

    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Outermost: session enforcement (FR-003).
    app.add_middleware(SessionMiddleware)
    return app


app = create_app()
