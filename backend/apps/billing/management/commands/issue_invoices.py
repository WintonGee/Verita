from django.core.management.base import BaseCommand

from apps.billing.invoicer import run_invoicing


class Command(BaseCommand):
    help = "Issue monthly invoices (defaults to the previous calendar month)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--customer", dest="customer_ids", action="append",
            help="Limit to specific customer id(s). Repeatable.",
        )

    def handle(self, *args, **options):
        result = run_invoicing(customer_ids=options.get("customer_ids"))
        self.stdout.write(
            f"issued invoices for {result['customers']} customers, "
            f"period {result['period_start'].date()}–{result['period_end'].date()}"
        )
