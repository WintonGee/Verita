"""
Lightweight cron dispatcher. No external broker, no Celery — just a small
polling loop that invokes Django management commands at their scheduled times.

Each scheduled task is responsible for its own concurrency safety via
`pg_try_advisory_lock`. The runner just fires-and-forgets.

Schedule (see SCHEDULES below): aggregate hourly at :15, issue invoices on the
1st at 00:30, reconcile daily at 03:00, clean up idempotency keys at 04:00.
"""

import logging
import subprocess
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
                    subprocess.Popen(["python", "manage.py", *args])
        time.sleep(15)


if __name__ == "__main__":
    main()
