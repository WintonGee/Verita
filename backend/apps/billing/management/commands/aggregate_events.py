from django.core.management.base import BaseCommand

from apps.billing.aggregator import run_aggregation


class Command(BaseCommand):
    help = "Roll usage events into hourly usage_windows (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--catch-up", action="store_true",
            help="Ignore the 5-min edge delay; process all events up to now. "
                 "Use for forced re-aggregation or right after seeding.",
        )

    def handle(self, *args, **options):
        result = run_aggregation(catch_up=options["catch_up"])
        if not result["locked"]:
            self.stdout.write("aggregator already running; skipped")
            return
        self.stdout.write(
            f"aggregated {result['windows_upserted']} windows "
            f"from {result['candidates']} candidates"
        )
