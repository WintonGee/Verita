import json

from django.core.management.base import BaseCommand, CommandError

from apps.billing.reconciliation import run_reconciliation


class Command(BaseCommand):
    help = "Run drift reconciliation checks (read-only); prints findings as JSON."

    def handle(self, *args, **options):
        result = run_reconciliation()
        self.stdout.write(json.dumps(result, indent=2))
        if not result["clean"]:
            # Raise so the process exits non-zero (CommandError -> exit 1):
            # a cron wrapper checking $? will actually alert on drift. Printing
            # to stderr alone left exit status 0, silently swallowing drift.
            raise CommandError("reconciliation found drift")
