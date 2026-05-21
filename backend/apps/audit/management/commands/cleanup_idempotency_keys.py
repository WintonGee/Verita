from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.audit.models import IdempotencyKey


class Command(BaseCommand):
    help = "Delete expired idempotency keys (the one table where deletion is allowed)."

    def handle(self, *args, **options):
        deleted, _ = IdempotencyKey.objects.filter(
            expires_at__lt=timezone.now()).delete()
        self.stdout.write(f"deleted {deleted} expired idempotency keys")
