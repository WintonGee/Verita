import json

from django.core.management.base import BaseCommand

from apps.billing.reconciliation import run_reconciliation


class Command(BaseCommand):
    help = "Run drift reconciliation checks (read-only); prints findings as JSON."

    def handle(self, *args, **options):
        result = run_reconciliation()
        self.stdout.write(json.dumps(result, indent=2))
        if not result["clean"]:
            # Non-zero exit so a cron wrapper / CI can alert.
            self.stderr.write("reconciliation found drift")
