import base64
import functools
import hashlib

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.utils.encoding import force_bytes, smart_text
from rest_framework import exceptions
from rest_framework.authentication import get_authorization_header

from . import models


class OIDCAuthenticationMiddleware(object):
    """GraphQL middleware to authenticate against open id connect provider.

    This middle enforces authentication for all graphs.
    """

    def __init__(self):
        self._keys = None

    def get_bearer_token(self, request):
        # TODO: status code of AuthenticationFailed error should be 401
        # https://github.com/graphql-python/graphene-django/issues/252
        auth = get_authorization_header(request).split()
        header_prefix = "Bearer"

        if not auth:
            return None

        if smart_text(auth[0].lower()) != header_prefix.lower():
            raise exceptions.AuthenticationFailed("No Bearer Authorization header")

        if len(auth) == 1:
            msg = "Invalid Authorization header. No credentials provided"
            raise exceptions.AuthenticationFailed(msg)
        elif len(auth) > 2:
            msg = (
                "Invalid Authorization header. Credentials string should "
                "not contain spaces."
            )
            raise exceptions.AuthenticationFailed(msg)

        return auth[1]

    def get_userinfo(self, token):
        response = requests.get(
            settings.OIDC_USERINFO_ENDPOINT,
            verify=settings.OIDC_VERIFY_SSL,
            headers={"Authorization": f"Bearer {smart_text(token)}"},
        )
        response.raise_for_status()
        return response.json()

    def get_introspection(self, token):
        basic = base64.b64encode(
            f"{settings.OIDC_INTROSPECT_CLIENT_ID}:{settings.OIDC_INTROSPECT_CLIENT_SECRET}".encode(
                "utf-8"
            )
        ).decode()
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        response = requests.post(
            settings.OIDC_INTROSPECT_ENDPOINT,
            verify=settings.OIDC_VERIFY_SSL,
            headers=headers,
            data={"token": token},
        )
        response.raise_for_status()
        return response.json()

    def resolve(self, next, root, info, **args):
        request = info.context
        token = self.get_bearer_token(request)

        if token is None:
            request.user = models.AnonymousUser()
            return next(root, info, **args)

        if not settings.OIDC_USERINFO_ENDPOINT:
            raise ImproperlyConfigured(
                'Token was provided, but "OIDC_USERINFO_ENDPOINT" is not set.'
            )

        userinfo_method = functools.partial(self.get_userinfo, token=token)
        # token might be too long for key so we use hash sum instead.
        hashsum_token = hashlib.sha256(force_bytes(token)).hexdigest()

        try:
            userinfo = cache.get_or_set(
                f"authentication.userinfo.{hashsum_token}",
                userinfo_method,
                timeout=settings.OIDC_BEARER_TOKEN_REVALIDATION_TIME,
            )
            request.user = models.OIDCUser(token, userinfo)
        except requests.HTTPError as e:
            if (
                e.response.status_code in [401, 403]
                and settings.OIDC_INTROSPECT_ENDPOINT
            ):
                introspect_method = functools.partial(
                    self.get_introspection, token=token
                )
                introspection = cache.get_or_set(
                    f"authentication.introspect.{hashsum_token}",
                    introspect_method,
                    timeout=settings.OIDC_BEARER_TOKEN_REVALIDATION_TIME,
                )
                request.user = models.OIDCClient(token, introspection)
            else:
                raise e

        return next(root, info, **args)
