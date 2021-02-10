"""
Custom Authenticator to use OpenShift OAuth with JupyterHub.

Derived from the GitHub OAuth authenticator.
"""


import json
import os
import warnings

import requests
from jupyterhub.auth import LocalAuthenticator
from tornado import web
from tornado.auth import OAuth2Mixin
from tornado.httpclient import AsyncHTTPClient, HTTPClient, HTTPRequest
from tornado.httputil import url_concat

from oauthenticator.oauth2 import OAuthenticator
from traitlets import Bool, Set, Unicode, default

try:
    import pycurl
    AsyncHTTPClient.configure("tornado.curl_httpclient.CurlAsyncHTTPClient")
except ModuleNotFoundError:
    warnings.warn('pycurl not installed. defaulting to simple_httpclient')


class OpenShiftOAuthenticator(OAuthenticator):

    login_service = "OpenShift"

    scope = ['user:info']

    openshift_url = Unicode(
        os.environ.get('OPENSHIFT_URL')
        or 'https://openshift.default.svc.cluster.local',
        config=True,
    )

    validate_cert = Bool(
        True, config=True, help="Set to False to disable certificate validation"
    )

    ca_certs = Unicode(config=True)

    allowed_groups = Set(
        config=True,
        help="Set of OpenShift groups that should be allowed to access the hub.",
    )

    admin_groups = Set(
        config=True,
        help="Set of OpenShift groups that should be given admin access to the hub.",
    )

    @default("ca_certs")
    def _ca_certs_default(self):
        ca_cert_file = "/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        if self.validate_cert and os.path.exists(ca_cert_file):
            return ca_cert_file

        return ''

    openshift_auth_api_url = Unicode(config=True)

    @default("openshift_auth_api_url")
    def _openshift_auth_api_url_default(self):
        auth_info_url = '%s/.well-known/oauth-authorization-server' % self.openshift_url

        resp = requests.get(auth_info_url, verify=self.ca_certs or self.validate_cert)
        resp_json = resp.json()

        return resp_json.get('issuer')

    openshift_rest_api_url = Unicode(
        os.environ.get('OPENSHIFT_REST_API_URL')
        or 'https://openshift.default.svc.cluster.local',
        config=True,
    )

    @default("openshift_rest_api_url")
    def _openshift_rest_api_url_default(self):
        return self.openshift_url

    @default("authorize_url")
    def _authorize_url_default(self):
        return "%s/oauth/authorize" % self.openshift_auth_api_url

    @default("token_url")
    def _token_url_default(self):
        return "%s/oauth/token" % self.openshift_auth_api_url

    @default("userdata_url")
    def _userdata_url_default(self):
        return "%s/apis/user.openshift.io/v1/users/~" % self.openshift_rest_api_url

    @staticmethod
    def user_in_groups(user_groups: set, allowed_groups: set):
        return any(user_groups.intersection(allowed_groups))

    async def authenticate(self, handler, data=None):
        code = handler.get_argument("code")
        # TODO: Configure the curl_httpclient for tornado

        http_client = AsyncHTTPClient()

        # Exchange the OAuth code for a OpenShift Access Token
        #
        # See: https://docs.openshift.org/latest/architecture/additional_concepts/authentication.html#api-authentication

        params = dict(
            client_id=self.client_id,
            client_secret=self.client_secret,
            grant_type="authorization_code",
            code=code,
        )

        url = url_concat(self.token_url, params)

        req = HTTPRequest(
            url,
            method="POST",
            validate_cert=self.validate_cert,
            ca_certs=self.ca_certs,
            headers={"Accept": "application/json"},
            body='',  # Body is required for a POST...
        )

        resp = await http_client.fetch(req)

        resp_json = json.loads(resp.body.decode('utf8', 'replace'))

        access_token = resp_json['access_token']

        # Determine who the logged in user is
        headers = {
            "Accept": "application/json",
            "User-Agent": "JupyterHub",
            "Authorization": "Bearer {}".format(access_token),
        }

        req = HTTPRequest(
            self.userdata_url,
            method="GET",
            validate_cert=self.validate_cert,
            ca_certs=self.ca_certs,
            headers=headers,
        )

        resp = await http_client.fetch(req)
        ocp_user = json.loads(resp.body.decode('utf8', 'replace'))

        username = ocp_user['metadata']['name']

        user_info = {
            'name': username,
            'auth_state': {'access_token': access_token, 'openshift_user': ocp_user},
        }

        if self.allowed_groups or self.admin_groups:
            user_info = await self._add_openshift_group_info(user_info)

        return user_info

    async def _add_openshift_group_info(self, user_info: dict):
        """
        Use the group info stored on the OpenShift User object to determine if a user
        is authenticated based on groups, an admin, or both.
        """
        user_groups = set(user_info['auth_state']['openshift_user']['groups'])

        if self.admin_groups:
            is_admin = self.user_in_groups(user_groups, self.admin_groups)

        user_in_allowed_group = self.user_in_groups(user_groups, self.allowed_groups)

        if self.admin_groups and (is_admin or user_in_allowed_group):
            user_info['admin'] = is_admin
            return user_info
        elif user_in_allowed_group:
            return user_info
        else:
            msg = "username:{username} User not in any of the allowed/admin groups"
            self.log.warning(msg.format(username=user_info['name']))
            return None


class LocalOpenShiftOAuthenticator(LocalAuthenticator, OpenShiftOAuthenticator):

    """A version that mixes in local system user creation"""

    pass
