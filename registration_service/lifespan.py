"""FastAPI lifespan handler — startup and shutdown sequence."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from registration_service.db import close_db, init_db
from registration_service.supervisor import get_supervisor

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info("registration_service_starting")

    await init_db()

    supervisor = get_supervisor()

    logger.info("recovery_starting")
    await supervisor.recover_live_agents()
    logger.info("recovery_complete")

    await supervisor.start_health_loop()
    logger.info("health_loop_started")

    await supervisor.start_build_poll_loop()
    logger.info("build_poll_loop_started")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("registration_service_stopping")

    await supervisor.stop_health_loop()
    await supervisor.stop_build_poll_loop()
    await close_db()

    logger.info("registration_service_stopped")
