"""
Seed/generator command: produces realistic demo data for the metered-billing
stack. Creates a shared "Standard" price plan, N customers (each with a login
user and 1-2 API keys), and a few thousand usage events per customer spread
across the last D days.

The seeded events flow through the real pipeline:
    python manage.py aggregate_events   # events -> usage_windows
    python manage.py issue_invoices     # usage_windows -> invoices

All money is integer micro-cents (1 unit = $1e-8). Never floats for money.

Usage:
    python manage.py seed --customers=N --days=D [--reset]
    Defaults: --customers=5 --days=30
"""

import hashlib
import secrets
import uuid
from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.billing.models import (
    Credit,
    CronState,
    Event,
    Invoice,
    LineItem,
    PricePlan,
    PriceTier,
    UsageWindow,
)
from apps.tenancy.models import ApiKey, Customer, CustomerUser

# Default password for every seeded login (demo data only).
DEMO_PASSWORD = "password123"

# A few endpoints to spread events across, for realism.
ENDPOINTS = [
    "/v1/completions",
    "/v1/embeddings",
    "/v1/chat",
    "/v1/rerank",
]

# bulk_create batch size — big enough to be fast, small enough to stay friendly.
BATCH_SIZE = 1000

# Fraction of events that arrive "late" (older event_timestamp). We only vary
# the timestamp; the pipeline decides whether is_late actually gets set.
LATE_FRACTION = 0.01
# Fraction of events that reuse a prior request_id to exercise dedup. These are
# silently dropped by ON CONFLICT DO NOTHING (ignore_conflicts=True).
DUPLICATE_FRACTION = 0.005


class Command(BaseCommand):
    help = "Seed realistic demo data (customers, API keys, usage events)."

    def add_arguments(self, parser):
        parser.add_argument("--customers", type=int, default=5,
                            help="Number of customers to create (default 5).")
        parser.add_argument("--days", type=int, default=30,
                            help="Spread events across the last D days (default 30).")
        parser.add_argument("--reset", action="store_true",
                            help="Delete existing billing/tenancy rows first "
                                 "(FK-safe order) for a clean re-run. Never "
                                 "touches audit_log (append-only).")

    def handle(self, *args, **options):
        n_customers = options["customers"]
        n_days = options["days"]
        now = timezone.now()

        if options["reset"]:
            self._reset()

        self._ensure_demo_staff()
        plan = self._create_price_plan()

        total_events = 0
        # Collect what we need to print at the end (plaintext keys are shown once).
        summaries = []

        for i in range(1, n_customers + 1):
            customer, login_email, plaintext_keys, api_keys = \
                self._create_customer(i, plan)
            inserted = self._seed_events(customer, api_keys, n_days, now)
            total_events += inserted
            summaries.append({
                "name": customer.name,
                "login_email": login_email,
                "plaintext_keys": plaintext_keys,
                "events": inserted,
            })

        # Seeded events are historical, so their ingestion was historical too.
        # Backdate ingested_at to event_timestamp; otherwise the aggregator's
        # 5-min edge-delay (which filters on ingested_at) would skip every
        # freshly-seeded row, and the demo flow (seed -> aggregate -> invoice)
        # would produce nothing until 5 minutes elapsed.
        Event.objects.unsafe_all_tenants().update(ingested_at=F("event_timestamp"))

        self._print_summary(n_customers, total_events, summaries)

    # --- Reset ---------------------------------------------------------------

    def _reset(self):
        """
        Delete existing rows in FK-safe order so re-running is clean.

        Tenant-scoped models (CustomerScopedManager) raise on a bare query, so
        we delete those via .unsafe_all_tenants(). The rest use default managers.

        We deliberately do NOT touch audit_log (append-only; a DB trigger and
        revoked grants reject deletes) or cron_state (infra watermarks).
        """
        self.stdout.write("resetting existing data...")
        with transaction.atomic():
            # Children of Invoice first (both RESTRICT-reference it).
            LineItem.objects.all().delete()
            Credit.objects.unsafe_all_tenants().delete()
            Invoice.objects.unsafe_all_tenants().delete()
            # Events / windows reference Customer + ApiKey via RESTRICT.
            Event.objects.unsafe_all_tenants().delete()
            UsageWindow.objects.unsafe_all_tenants().delete()
            # Tenancy: keys + users before the customer they belong to.
            ApiKey.objects.all().delete()
            CustomerUser.objects.all().delete()
            Customer.objects.all().delete()
            # Pricing last (Customer.price_plan is RESTRICT). PriceTier cascades
            # from PricePlan, but we delete it explicitly for clarity.
            PriceTier.objects.all().delete()
            PricePlan.objects.all().delete()
            # Clear the aggregator watermark: seeded events have backdated
            # ingested_at, so a stale watermark would make the incremental scan
            # skip them.
            CronState.objects.all().delete()

    # --- Demo staff ----------------------------------------------------------

    def _ensure_demo_staff(self):
        """Create a demo ops staff user so the ops console is usable immediately.
        Demo-only credentials — replace in any real environment."""
        from django.contrib.auth.models import User
        if not User.objects.filter(username="ops").exists():
            User.objects.create_superuser("ops", "ops@verita.local", "ops-pass-123")
        self.stdout.write("ops staff login: ops / ops-pass-123")

    # --- Pricing -------------------------------------------------------------

    def _create_price_plan(self):
        """
        One shared "Standard" plan with the three tiers from the brief:
            [0, 10k)      @ 0          (free)
            [10k, 100k)   @ 100,000 μ¢ ($0.001/unit)
            [100k, ∞)     @  50,000 μ¢ ($0.0005/unit)
        """
        plan = PricePlan.objects.create(name="Standard", currency="USD")
        PriceTier.objects.bulk_create([
            PriceTier(price_plan=plan, start_unit=0, end_unit=10_000,
                      unit_price_micro_cents=0, ordinality=1),
            PriceTier(price_plan=plan, start_unit=10_000, end_unit=100_000,
                      unit_price_micro_cents=100_000, ordinality=2),
            PriceTier(price_plan=plan, start_unit=100_000, end_unit=None,
                      unit_price_micro_cents=50_000, ordinality=3),
        ])
        return plan

    # --- Customer / users / keys --------------------------------------------

    def _create_customer(self, index, plan):
        """Create a Customer + one login user + 1-2 API keys."""
        customer = Customer.objects.create(
            name=f"Acme Corp {index}",
            billing_email=f"billing{index}@example.com",
            status=Customer.Status.ACTIVE,
            price_plan=plan,
        )

        login_email = f"user{index}@example.com"
        CustomerUser.objects.create(
            customer=customer,
            email=login_email,
            password_hash=make_password(DEMO_PASSWORD),
            is_active=True,
        )

        n_keys = secrets.choice([1, 2])
        api_keys = []
        plaintext_keys = []
        for k in range(n_keys):
            api_key, plaintext = self._make_api_key(customer, name=f"key {k + 1}")
            api_keys.append(api_key)
            plaintext_keys.append(plaintext)

        return customer, login_email, plaintext_keys, api_keys

    def _make_api_key(self, customer, name):
        """
        Generate an API key EXACTLY as tests/factories.py does:
            prefix    = token_hex(4)               # 8 hex chars
            secret    = token_hex(16)              # 32 hex chars
            plaintext = vk_live_<prefix>_<secret>
            salt      = token_bytes(16)
            key_hash  = sha256(salt || secret.encode())
        Returns (saved ApiKey, plaintext).
        """
        prefix = secrets.token_hex(4)
        secret = secrets.token_hex(16)
        salt = secrets.token_bytes(16)
        plaintext = f"vk_live_{prefix}_{secret}"

        h = hashlib.sha256()
        h.update(salt)
        h.update(secret.encode())
        key_hash = h.digest()

        api_key = ApiKey.objects.create(
            customer=customer,
            prefix=prefix,
            key_hash=key_hash,
            salt=salt,
            name=name,
        )
        return api_key, plaintext

    # --- Events --------------------------------------------------------------

    def _seed_events(self, customer, api_keys, n_days, now):
        """
        Generate a substantial-but-fast batch of events for one customer,
        spread across the last n_days. Inserts via bulk_create with
        ignore_conflicts=True (matches the ON CONFLICT DO NOTHING ingest path),
        so duplicate request_ids are silently skipped.

        Returns the number of rows actually inserted.
        """
        # Per-customer baseline: how many events/day on average. Varied so
        # different customers land in different price tiers.
        baseline_per_day = secrets.choice([80, 120, 160, 220, 280])
        # Some customers spike early in the month (front-loaded usage).
        spikes_early = secrets.choice([True, False])

        target = baseline_per_day * max(n_days, 1)
        window = timedelta(days=n_days)

        recent_request_ids = []  # pool to draw duplicates from
        inserted = 0
        batch = []

        for _ in range(target):
            api_key = secrets.choice(api_keys)

            # Place the event somewhere in the last n_days. Front-loaded
            # customers bias toward the start of the window.
            frac = secrets.randbelow(10_000) / 10_000.0
            if spikes_early:
                frac = frac * frac  # squash toward 0 (earlier in the window)
            offset = window * frac
            ts = now - window + offset

            # ~1% late arrivals: nudge the timestamp 1-2 hours into the past
            # relative to its neighbours. We only move the timestamp; the
            # pipeline computes is_late itself.
            if secrets.randbelow(10_000) < int(LATE_FRACTION * 10_000):
                ts = ts - timedelta(hours=secrets.choice([1, 2]))

            # ~0.5% duplicates: reuse a prior request_id to exercise dedup.
            if recent_request_ids and \
                    secrets.randbelow(10_000) < int(DUPLICATE_FRACTION * 10_000):
                request_id = secrets.choice(recent_request_ids)
            else:
                request_id = uuid.uuid4().hex
                recent_request_ids.append(request_id)
                # Keep the dedup pool bounded.
                if len(recent_request_ids) > 256:
                    recent_request_ids.pop(0)

            batch.append(Event(
                customer=customer,
                api_key=api_key,
                request_id=request_id,
                endpoint=secrets.choice(ENDPOINTS),
                units_consumed=secrets.choice(range(1, 51)),  # 1-50
                event_timestamp=ts,
                # ingested_at is auto_now_add; is_late defaults False (pipeline
                # decides). Do NOT set is_late here.
            ))

            if len(batch) >= BATCH_SIZE:
                inserted += self._flush(customer, batch)
                batch = []

        if batch:
            inserted += self._flush(customer, batch)

        return inserted

    def _flush(self, customer, batch):
        """
        bulk_create one batch via the CustomerScopedManager (which passes
        through to the default queryset). ignore_conflicts=True mirrors the
        ingest path's ON CONFLICT DO NOTHING, so duplicate request_ids are
        dropped.

        With ignore_conflicts=True on Postgres, Django does NOT populate PKs on
        the returned objects (it can't tell inserted from skipped), so we count
        actually-inserted rows via a row-count delta for this customer.
        """
        before = Event.objects.for_customer(customer).count()
        Event.objects.bulk_create(batch, ignore_conflicts=True)
        after = Event.objects.for_customer(customer).count()
        return after - before

    # --- Summary -------------------------------------------------------------

    def _print_summary(self, n_customers, total_events, summaries):
        w = self.stdout.write
        w("")
        w(self.style.SUCCESS(
            f"Seeded {n_customers} customers, {total_events} events inserted."
        ))
        w("")
        for s in summaries:
            w(f"  {s['name']}  ({s['events']} events)")
            w(f"    login: {s['login_email']}  /  {DEMO_PASSWORD}")
            for key in s["plaintext_keys"]:
                w(f"    api key: {key}")
            w("")
        w("Run: python manage.py aggregate_events && python manage.py issue_invoices")
