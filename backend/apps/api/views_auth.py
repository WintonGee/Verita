"""
Customer dashboard auth: login / logout / me.

Login verifies email+password (argon2id), creates a CustomerSession (storing
only the token hash), and sets an httpOnly, SameSite=Lax cookie. The raw token
lives only in the cookie. Logout deletes the session row and clears the cookie.

Rate-limited to blunt credential stuffing.
"""

import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from django_ratelimit.core import is_ratelimited
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.auth import (
    CUSTOMER_SESSION_COOKIE,
    CustomerSessionAuthentication,
    hash_session_token,
)
from apps.api.permissions import HasCustomerScope
from apps.tenancy.models import CustomerSession, CustomerUser

SESSION_TTL = timedelta(hours=12)

# A real argon2 hash, computed once, used to keep login timing uniform whether
# or not the email exists (defeats user-enumeration via response time).
_DUMMY_PASSWORD_HASH = make_password("timing-uniformity-dummy")


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class MeSerializer(serializers.Serializer):
    user = serializers.DictField()
    customer = serializers.DictField()


def _me_payload(customer_user):
    c = customer_user.customer
    return {
        "user": {
            "id": str(customer_user.id),
            "email": customer_user.email,
            "last_login_at": customer_user.last_login_at,
        },
        "customer": {
            "id": str(c.id),
            "name": c.name,
            "status": c.status,
            "price_plan": {"id": str(c.price_plan_id), "name": c.price_plan.name},
        },
    }


class LoginView(APIView):
    authentication_classes = []
    permission_classes = []

    @extend_schema(request=LoginSerializer, responses={200: MeSerializer})
    def post(self, request):
        # Rate-limit: 5 attempts/min/IP. Called manually (not as a decorator)
        # so it composes with DRF's class-based view and returns our standard
        # error envelope instead of django-ratelimit's default 403.
        limited = is_ratelimited(
            request._request, group="customer-login", key="ip",
            rate="5/m", method="POST", increment=True,
        )
        if limited:
            return Response(
                {"error": {"code": "rate_limited", "message": "Too many attempts."}},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]

        user = CustomerUser.objects.select_related("customer").filter(
            email=email, is_active=True).first()
        # Always run check_password to keep timing uniform (avoid user enumeration).
        valid = check_password(password, user.password_hash if user else _DUMMY_PASSWORD_HASH)
        if not user or not valid:
            return Response(
                {"error": {"code": "unauthenticated", "message": "Invalid credentials."}},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Create the session — store only the hash of the token.
        raw_token = secrets.token_urlsafe(32)
        CustomerSession.objects.create(
            customer_user=user,
            token_hash=hash_session_token(raw_token),
            expires_at=timezone.now() + SESSION_TTL,
        )
        user.last_login_at = timezone.now()
        user.save(update_fields=["last_login_at"])

        resp = Response(_me_payload(user), status=status.HTTP_200_OK)
        resp.set_cookie(
            CUSTOMER_SESSION_COOKIE, raw_token,
            httponly=True, samesite="Lax", secure=not _is_debug(),
            max_age=int(SESSION_TTL.total_seconds()),
        )
        return resp


class LogoutView(APIView):
    authentication_classes = [CustomerSessionAuthentication]
    permission_classes = [HasCustomerScope]

    def post(self, request):
        token = request.COOKIES.get(CUSTOMER_SESSION_COOKIE)
        if token:
            CustomerSession.objects.filter(
                token_hash=hash_session_token(token)).delete()
        resp = Response(status=status.HTTP_204_NO_CONTENT)
        resp.delete_cookie(CUSTOMER_SESSION_COOKIE)
        return resp


class MeView(APIView):
    # API key OR session cookie both work here.
    from apps.api.auth import ApiKeyAuthentication
    authentication_classes = [ApiKeyAuthentication, CustomerSessionAuthentication]
    permission_classes = [HasCustomerScope]

    @extend_schema(responses={200: MeSerializer})
    def get(self, request):
        # If authed by API key there's no customer_user; synthesize a minimal me.
        user = getattr(request, "customer_user", None)
        if user is not None:
            return Response(_me_payload(user))
        c = request.customer
        return Response({
            "user": None,
            "customer": {
                "id": str(c.id), "name": c.name, "status": c.status,
                "price_plan": {"id": str(c.price_plan_id), "name": c.price_plan.name},
            },
        })


def _is_debug():
    from django.conf import settings
    return settings.DEBUG
