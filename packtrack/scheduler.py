"""APScheduler — Zoho sync + push retries.

In-process scheduler. Single instance. No queue, no workers, no Redis.
"""
from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from packtrack.config import settings
from packtrack.db import engine
from packtrack.models import SyncRun
from sqlmodel import Session

logger = logging.getLogger("packtrack.scheduler")
_scheduler: BackgroundScheduler | None = None


def _zoho_sync_job() -> None:
    if not settings.gateway_configured:
        return
    from packtrack import zoho

    with Session(engine) as session:
        run = SyncRun(started_at=datetime.utcnow())
        session.add(run)
        session.commit()
        session.refresh(run)
        try:
            updated, created = zoho.sync_items(session)
            mirrored = zoho.sync_open_pos(session)
            run.items_updated = updated
            run.items_created = created
            run.po_mirrored = mirrored
            run.status = "ok"
        except Exception as e:
            logger.exception("Zoho sync failed")
            run.status = "error"
            run.error_message = str(e)[:1000]

        # Catch-up: backfill BoxReceipts for lines already received in Zoho.
        try:
            from packtrack.services.receive_catchup import catchup_zoho_receives
            catchup = catchup_zoho_receives(session)
            if catchup["receipts_created"]:
                logger.info("Receive catch-up: %s", catchup)
        except Exception:
            logger.exception("Receive catch-up failed")
        run.finished_at = datetime.utcnow()
        session.add(run)
        session.commit()


def _push_retry_job() -> None:
    if not settings.zoho_configured:
        return
    from packtrack import zoho

    with Session(engine) as session:
        result = zoho.retry_unpushed(session)
        if result["tried"]:
            logger.info("Push retry: %s", result)


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True, timezone="UTC")
    _scheduler.add_job(
        _zoho_sync_job,
        trigger=IntervalTrigger(minutes=settings.SYNC_INTERVAL_MINUTES),
        id="zoho-sync",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.utcnow(),  # run once at boot
    )
    _scheduler.add_job(
        _push_retry_job,
        trigger=IntervalTrigger(minutes=settings.PUSH_RETRY_INTERVAL_MINUTES),
        id="push-retry",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    _scheduler.start()
    logger.info("Scheduler started (sync every %s min, push-retry every %s min)",
                settings.SYNC_INTERVAL_MINUTES, settings.PUSH_RETRY_INTERVAL_MINUTES)


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def trigger_sync_now() -> None:
    """Kick off a sync immediately (used by Admin → Sync button)."""
    if _scheduler is None:
        _zoho_sync_job()
        return
    _scheduler.add_job(_zoho_sync_job, id=f"manual-sync-{datetime.utcnow().timestamp()}")
