"""
Shared pytest fixtures. Imports models lazily inside fixtures so Django apps
are ready by the time we hit the DB.
"""

import pytest


@pytest.fixture
def price_plan(db):
    from tests.factories import PricePlanFactory
    return PricePlanFactory(with_default_tiers=True)


@pytest.fixture
def customer_a(db, price_plan):
    from tests.factories import CustomerFactory
    return CustomerFactory(price_plan=price_plan, name="Acme")


@pytest.fixture
def customer_b(db, price_plan):
    from tests.factories import CustomerFactory
    return CustomerFactory(price_plan=price_plan, name="Beta")


@pytest.fixture
def api_key_a(db, customer_a):
    from tests.factories import ApiKeyFactory
    return ApiKeyFactory(customer=customer_a)


@pytest.fixture
def api_key_b(db, customer_b):
    from tests.factories import ApiKeyFactory
    return ApiKeyFactory(customer=customer_b)


@pytest.fixture
def customer_user_a(db, customer_a):
    from django.contrib.auth.hashers import make_password
    from apps.tenancy.models import CustomerUser
    return CustomerUser.objects.create(
        customer=customer_a, email="alice@acme.com",
        password_hash=make_password("s3cret-pass"), is_active=True,
    )


@pytest.fixture
def staff_user(db):
    from django.contrib.auth.models import User
    return User.objects.create_user(
        username="ops", email="ops@verita.com", password="ops-pass", is_staff=True,
    )
