"""
factory_boy factories for the test suite. Keep these small — they exist to
remove boilerplate, not to encode product behavior.
"""

import hashlib
import secrets

import factory
from factory.django import DjangoModelFactory

from apps.billing.models import PricePlan, PriceTier
from apps.tenancy.models import ApiKey, Customer, CustomerUser


class PricePlanFactory(DjangoModelFactory):
    class Meta:
        model = PricePlan
        skip_postgeneration_save = True

    name = factory.Sequence(lambda n: f"Standard Plan {n}")
    currency = "USD"

    @factory.post_generation
    def with_default_tiers(self, create, extracted, **kwargs):
        if not create or extracted is False:
            return
        # Default tiered structure from the brief:
        # 0–10k free, 10k–100k @ $0.001/unit, 100k+ @ $0.0005/unit
        # In micro-cents: $0.001 = 100,000 μ¢; $0.0005 = 50,000 μ¢
        PriceTier.objects.bulk_create([
            PriceTier(price_plan=self, start_unit=0, end_unit=10_000,
                      unit_price_micro_cents=0, ordinality=1),
            PriceTier(price_plan=self, start_unit=10_000, end_unit=100_000,
                      unit_price_micro_cents=100_000, ordinality=2),
            PriceTier(price_plan=self, start_unit=100_000, end_unit=None,
                      unit_price_micro_cents=50_000, ordinality=3),
        ])


class CustomerFactory(DjangoModelFactory):
    class Meta:
        model = Customer

    name = factory.Sequence(lambda n: f"Tenant {n}")
    billing_email = factory.LazyAttribute(lambda o: f"billing+{o.name.lower().replace(' ', '')}@example.com")
    status = Customer.Status.ACTIVE
    price_plan = factory.SubFactory(PricePlanFactory)


class CustomerUserFactory(DjangoModelFactory):
    class Meta:
        model = CustomerUser

    customer = factory.SubFactory(CustomerFactory)
    email = factory.Sequence(lambda n: f"user{n}@example.com")
    password_hash = "stub-hash"  # tests that exercise login set this properly
    is_active = True


class ApiKeyFactory(DjangoModelFactory):
    """
    Creates an ApiKey with a real salt + hash. The plaintext key is returned
    via the `_plaintext_key` attribute on the instance for test convenience.
    """
    class Meta:
        model = ApiKey
        skip_postgeneration_save = True

    customer = factory.SubFactory(CustomerFactory)
    name = "test key"

    @factory.lazy_attribute
    def salt(self):
        return secrets.token_bytes(16)

    @factory.lazy_attribute
    def key_hash(self):
        # Stash the plaintext on the factory call site via a thread-local hack
        # would be over-engineered. Instead we just precompute here and the
        # caller can ask for `plaintext_key` via the post_generation hook below.
        return b""  # filled in by post_generation

    @factory.post_generation
    def _generate_secret(self, create, extracted, **kwargs):
        if not create:
            return
        secret = secrets.token_hex(16)  # 32 chars, 128 bits — enough for tests
        self.plaintext_key = f"vk_live_{self.prefix}_{secret}"
        h = hashlib.sha256()
        h.update(bytes(self.salt))
        h.update(secret.encode())
        self.key_hash = h.digest()
        self.save(update_fields=["key_hash"])
