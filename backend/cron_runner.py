"""
Lightweight cron dispatcher. No external broker, no Celery — just a small
polling loop that invokes Django management commands at their scheduled times.

Each scheduled task is responsible for its own concurrency safety via
`pg_try_advisory_lock`. The runner dispatches each job on a daemon thread and
logs a non-zero exit, so a failed job (notably the daily reconcile, which is the
thing that *detects* drift) is observable rather than silently lost.

Schedule (see SCHEDULES below): aggregate hourly at :15, issue invoices on the
1st at 00:30, reconcile daily at 03:00, clean up idempotency keys at 04:00.
"""

import logging
import subprocess
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("cron")
logger.setLevel(logging.INFO)
logging.basicConfig(format="[cron] %(asctime)s %(message)s")

# (name, predicate(dt: datetime) -> bool, [manage.py args])
SCHEDULES: list = [
    ("aggregator", lambda dt: dt.minute == 15, ["aggregate_events"]),
    ("invoicer", lambda dt: dt.day == 1 and dt.hour == 0 and dt.minute == 30, ["issue_invoices"]),
    ("reconcile", lambda dt: dt.hour == 3 and dt.minute == 0, ["run_reconciliation"]),
    ("cleanup", lambda dt: dt.hour == 4 and dt.minute == 0, ["cleanup_idempotency_keys"]),
]


def _run_job(name, args):
    """Run one management command, logging a non-zero exit as an error so a
    failed cron job (e.g. the drift-detecting reconcile) is observable."""
    try:
        result = subprocess.run(["python", "manage.py", *args],
                                capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("job %s exited %d: %s", name, result.returncode,
                         (result.stderr or "").strip()[:500])
        else:
            logger.info("job %s completed ok", name)
    except Exception:  # noqa: BLE001 - never let a job crash the scheduler loop
        logger.exception("job %s failed to run", name)


def main():
    logger.info("cron runner started; %d schedules registered", len(SCHEDULES))
    last_minute_key = None
    while True:
        now = datetime.now(timezone.utc)
        current_key = (now.year, now.month, now.day, now.hour, now.minute)
        if current_key != last_minute_key:
            last_minute_key = current_key
            for name, predicate, args in SCHEDULES:
                if predicate(now):
                    logger.info("dispatching %s at %s", name, now.isoformat())
                    # Daemon thread so a long job doesn't block the scheduler,
                    # but its exit code is still captured and logged.
                    threading.Thread(target=_run_job, args=(name, args),
                                     daemon=True).start()
        time.sleep(15)


if __name__ == "__main__":
    main()
