"""
Seed command robustness. The reset must survive pre-existing rows that
RESTRICT-reference Customer — notably IdempotencyKey rows created by ops credit
issuance. Regression: `seed --reset` used to fail once any credit had been
issued, because it deleted Customer before clearing IdempotencyKey.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.audit.models import IdempotencyKey
from apps.tenancy.models import Customer


@pytest.mark.django_db
def test_seed_reset_succeeds_with_existing_idempotency_keys():
    out = StringIO()
    call_command("seed", "--customers=1", "--days=1", "--reset", stdout=out)
    customer = Customer.objects.first()

    # Simulate an ops credit having left an idempotency key (RESTRICT FK).
    IdempotencyKey.objects.create(
        customer=customer, staff_id="ops", key="k1", method="POST",
        path="/ops/customers/x/credits", request_hash=b"x",
        response_status=201, response_body={}, expires_at=timezone.now(),
    )

    # Re-seeding with --reset must not raise; it clears idempotency keys first.
    call_command("seed", "--customers=1", "--days=1", "--reset", stdout=out)
    assert Customer.objects.count() == 1
    assert IdempotencyKey.objects.count() == 0
