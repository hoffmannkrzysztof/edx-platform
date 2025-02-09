"""
Views that dispatch processing of OAuth requests to django-oauth2-provider or
django-oauth-toolkit as appropriate.
"""


import json

from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.generic import View
from edx_django_utils import monitoring as monitoring_utils
from oauth2_provider import views as dot_views
from ratelimit import ALL
from ratelimit.decorators import ratelimit

from openedx.core.djangoapps.auth_exchange import views as auth_exchange_views
from openedx.core.djangoapps.oauth_dispatch import adapters
from openedx.core.djangoapps.oauth_dispatch.dot_overrides import views as dot_overrides_views
from openedx.core.djangoapps.oauth_dispatch.jwt import create_jwt_from_token, get_jwt_access_token_expire_seconds


class _DispatchingView(View):
    """
    Base class that route views to the appropriate provider view.  The default
    behavior routes based on client_id, but this can be overridden by redefining
    `select_backend()` if particular views need different behavior.
    """

    dot_adapter = adapters.DOTAdapter()

    def get_adapter(self, request):
        """
        Returns the appropriate adapter based on the OAuth client linked to the request.
        """
        client_id = self._get_client_id(request)
        monitoring_utils.set_custom_attribute('oauth_client_id', client_id)

        return self.dot_adapter

    def dispatch(self, request, *args, **kwargs):
        """
        Dispatch the request to the selected backend's view.
        """
        backend = self.select_backend(request)
        view = self.get_view_for_backend(backend)
        return view(request, *args, **kwargs)

    def select_backend(self, request):
        """
        Given a request that specifies an oauth `client_id`, return the adapter
        for the appropriate OAuth handling library.  If the client_id is found
        in a django-oauth-toolkit (DOT) Application, use the DOT adapter,
        otherwise use the django-oauth2-provider (DOP) adapter, and allow the
        calls to fail normally if the client does not exist.
        """
        return self.get_adapter(request).backend

    def get_view_for_backend(self, backend):
        """
        Return the appropriate view from the requested backend.
        """
        if backend == self.dot_adapter.backend:
            return self.dot_view.as_view()  # lint-amnesty, pylint: disable=no-member
        else:
            raise KeyError(f'Failed to dispatch view. Invalid backend {backend}')

    def _get_client_id(self, request):
        """
        Return the client_id from the provided request
        """
        if request.method == 'GET':
            return request.GET.get('client_id')
        else:
            return request.POST.get('client_id')


def _get_token_type(request):
    """
    Get the token_type for the request.

    - Respects the HTTP_X_TOKEN_TYPE header if the token_type parameter is not supplied.
    - Adds `oauth_token_type` custom attribute for monitoring.
    """
    default_token_type = request.META.get('HTTP_X_TOKEN_TYPE', 'no_token_type_supplied')
    token_type = request.POST.get('token_type', default_token_type).lower()
    monitoring_utils.set_custom_attribute('oauth_token_type', token_type)
    return token_type


def _get_jwt_dict_from_access_token_dict(token_dict, oauth_adapter):
    """
    Returns a JWT token dict from the provided original (opaque) access token dict.

    Creates the new JWT, and then overrides various values in a copy of the
        token dict with the JWT specific values.
    """
    jwt_dict = token_dict.copy()
    # TODO: It would be safer if create_jwt_from_token returned this
    #   dict directly, so it would not be possible for the dict and JWT
    #   to get out of sync, but that is a larger refactor to think through.
    jwt = create_jwt_from_token(jwt_dict, oauth_adapter)
    jwt_dict.update({
        'access_token': jwt,
        'token_type': 'JWT',
        'expires_in': get_jwt_access_token_expire_seconds(),
    })
    return jwt_dict


@method_decorator(
    ratelimit(
        key='openedx.core.djangoapps.util.ratelimit.real_ip', rate=settings.RATELIMIT_RATE,
        method=ALL, block=True
    ), name='dispatch'
)
class AccessTokenView(_DispatchingView):
    """
    Handle access token requests.
    """
    dot_view = dot_views.TokenView

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        monitoring_utils.set_custom_attribute('oauth_grant_type', request.POST.get('grant_type', 'not-supplied'))
        token_type = _get_token_type(request)

        if response.status_code == 200 and token_type == 'jwt':
            response.content = self._get_jwt_content_from_access_token_content(request, response)

        return response

    def _get_jwt_content_from_access_token_content(self, request, response):
        """
        Gets the JWT response content from the original (opaque) token response content.

        Includes the JWT token and token type in the response.
        """
        opaque_token_dict = json.loads(response.content.decode('utf-8'))
        jwt_token_dict = _get_jwt_dict_from_access_token_dict(
            opaque_token_dict, self.get_adapter(request)
        )
        return json.dumps(jwt_token_dict)


class AuthorizationView(_DispatchingView):
    """
    Part of the authorization flow.
    """
    dot_view = dot_overrides_views.EdxOAuth2AuthorizationView


class AccessTokenExchangeView(_DispatchingView):
    """
    Exchange a third party auth token.
    """
    dot_view = auth_exchange_views.DOTAccessTokenExchangeView


class RevokeTokenView(_DispatchingView):
    """
    Dispatch to the RevokeTokenView of django-oauth-toolkit

    Note: JWT access tokens are non-revocable, but you could still revoke
        its associated refresh_token.
    """
    dot_view = dot_views.RevokeTokenView
