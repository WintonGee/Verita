"""
Customer dashboard auth: login / session / logout / me.

Security boundaries:
  - wrong password / unknown email → 401 (no user enumeration difference)
  - session cookie authenticates /v1/me; API key also works
  - logout invalidates the session
  - expired session → 401
  - login is rate-limited
"""

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone
from datetime import timedelta

from apps.api.auth import CUSTOMER_SESSION_COOKIE, hash_session_token
from apps.tenancy.models import CustomerSession


@pytest.fixture
def client():
    return Client()


@pytest.fixture(autouse=True)
def _clear_ratelimit_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_login_success_sets_cookie(client, customer_user_a):
    resp = client.post("/v1/auth/login",
                       {"email": "alice@acme.com", "password": "s3cret-pass"},
                       content_type="application/json")
    assert resp.status_code == 200
    assert CUSTOMER_SESSION_COOKIE in resp.cookies
    assert resp.json()["customer"]["name"] == "Acme"
    assert CustomerSession.objects.count() == 1


@pytest.mark.django_db
def test_login_wrong_password_401(client, customer_user_a):
    resp = client.post("/v1/auth/login",
                       {"email": "alice@acme.com", "password": "wrong"},
                       content_type="application/json")
    assert resp.status_code == 401
    assert CustomerSession.objects.count() == 0


@pytest.mark.django_db
def test_login_unknown_email_401(client, db):
    resp = client.post("/v1/auth/login",
                       {"email": "nobody@nowhere.com", "password": "whatever"},
                       content_type="application/json")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_me_with_session_cookie(client, customer_user_a):
    login = client.post("/v1/auth/login",
                        {"email": "alice@acme.com", "password": "s3cret-pass"},
                        content_type="application/json")
    token = login.cookies[CUSTOMER_SESSION_COOKIE].value
    client.cookies[CUSTOMER_SESSION_COOKIE] = token

    resp = client.get("/v1/me")
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == "alice@acme.com"


@pytest.mark.django_db
def test_me_with_api_key(client, api_key_a):
    resp = client.get("/v1/me", HTTP_AUTHORIZATION=f"Bearer {api_key_a.plaintext_key}")
    assert resp.status_code == 200
    assert resp.json()["customer"]["id"] == str(api_key_a.customer_id)


@pytest.mark.django_db
def test_me_unauthenticated_401(client, db):
    resp = client.get("/v1/me")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_logout_invalidates_session(client, customer_user_a):
    login = client.post("/v1/auth/login",
                        {"email": "alice@acme.com", "password": "s3cret-pass"},
                        content_type="application/json")
    token = login.cookies[CUSTOMER_SESSION_COOKIE].value
    client.cookies[CUSTOMER_SESSION_COOKIE] = token

    out = client.post("/v1/auth/logout")
    assert out.status_code == 204
    assert CustomerSession.objects.count() == 0

    # Old cookie no longer authenticates
    client.cookies[CUSTOMER_SESSION_COOKIE] = token
    me = client.get("/v1/me")
    assert me.status_code == 401


@pytest.mark.django_db
def test_expired_session_rejected(client, customer_user_a):
    raw = "expired-token-xyz"
    CustomerSession.objects.create(
        customer_user=customer_user_a,
        token_hash=hash_session_token(raw),
        expires_at=timezone.now() - timedelta(hours=1),
    )
    client.cookies[CUSTOMER_SESSION_COOKIE] = raw
    resp = client.get("/v1/me")
    assert resp.status_code == 401
    # expired session is cleaned up on access
    assert CustomerSession.objects.count() == 0


@pytest.mark.django_db
def test_login_rate_limited(client, customer_user_a):
    # 5/min/IP; the 6th attempt should be throttled.
    codes = []
    for _ in range(6):
        r = client.post("/v1/auth/login",
                        {"email": "alice@acme.com", "password": "wrong"},
                        content_type="application/json")
        codes.append(r.status_code)
    assert codes[-1] == 429
    assert codes[:5] == [401] * 5
