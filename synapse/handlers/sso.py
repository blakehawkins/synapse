# -*- coding: utf-8 -*-
# Copyright 2020 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import abc
import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, List, Mapping, Optional
from urllib.parse import urlencode

import attr
from typing_extensions import NoReturn, Protocol

from twisted.web.http import Request

from synapse.api.errors import Codes, RedirectException, SynapseError
from synapse.http.server import respond_with_html
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict, UserID, contains_invalid_mxid_characters
from synapse.util.async_helpers import Linearizer
from synapse.util.stringutils import random_string

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class MappingException(Exception):
    """Used to catch errors when mapping an SSO response to user attributes.

    Note that the msg that is raised is shown to end-users.
    """


class SsoIdentityProvider(Protocol):
    """Abstract base class to be implemented by SSO Identity Providers

    An Identity Provider, or IdP, is an external HTTP service which authenticates a user
    to say whether they should be allowed to log in, or perform a given action.

    Synapse supports various implementations of IdPs, including OpenID Connect, SAML,
    and CAS.

    The main entry point is `handle_redirect_request`, which should return a URI to
    redirect the user's browser to the IdP's authentication page.

    Each IdP should be registered with the SsoHandler via
    `hs.get_sso_handler().register_identity_provider()`, so that requests to
    `/_matrix/client/r0/login/sso/redirect` can be correctly dispatched.
    """

    @property
    @abc.abstractmethod
    def idp_id(self) -> str:
        """A unique identifier for this SSO provider

        Eg, "saml", "cas", "github"
        """

    @property
    @abc.abstractmethod
    def idp_name(self) -> str:
        """User-facing name for this provider"""

    @abc.abstractmethod
    async def handle_redirect_request(
        self,
        request: SynapseRequest,
        client_redirect_url: Optional[bytes],
        ui_auth_session_id: Optional[str] = None,
    ) -> str:
        """Handle an incoming request to /login/sso/redirect

        Args:
            request: the incoming HTTP request
            client_redirect_url: the URL that we should redirect the
                client to after login (or None for UI Auth).
            ui_auth_session_id: The session ID of the ongoing UI Auth (or
                None if this is a login).

        Returns:
            URL to redirect to
        """
        raise NotImplementedError()


@attr.s
class UserAttributes:
    # the localpart of the mxid that the mapper has assigned to the user.
    # if `None`, the mapper has not picked a userid, and the user should be prompted to
    # enter one.
    localpart = attr.ib(type=Optional[str])
    display_name = attr.ib(type=Optional[str], default=None)
    emails = attr.ib(type=List[str], default=attr.Factory(list))


@attr.s(slots=True)
class UsernameMappingSession:
    """Data we track about SSO sessions"""

    # A unique identifier for this SSO provider, e.g.  "oidc" or "saml".
    auth_provider_id = attr.ib(type=str)

    # user ID on the IdP server
    remote_user_id = attr.ib(type=str)

    # attributes returned by the ID mapper
    display_name = attr.ib(type=Optional[str])
    emails = attr.ib(type=List[str])

    # An optional dictionary of extra attributes to be provided to the client in the
    # login response.
    extra_login_attributes = attr.ib(type=Optional[JsonDict])

    # where to redirect the client back to
    client_redirect_url = attr.ib(type=str)

    # expiry time for the session, in milliseconds
    expiry_time_ms = attr.ib(type=int)


# the HTTP cookie used to track the mapping session id
USERNAME_MAPPING_SESSION_COOKIE_NAME = b"username_mapping_session"


class SsoHandler:
    # The number of attempts to ask the mapping provider for when generating an MXID.
    _MAP_USERNAME_RETRIES = 1000

    # the time a UsernameMappingSession remains valid for
    _MAPPING_SESSION_VALIDITY_PERIOD_MS = 15 * 60 * 1000

    def __init__(self, hs: "HomeServer"):
        self._clock = hs.get_clock()
        self._store = hs.get_datastore()
        self._server_name = hs.hostname
        self._registration_handler = hs.get_registration_handler()
        self._error_template = hs.config.sso_error_template
        self._auth_handler = hs.get_auth_handler()

        # a lock on the mappings
        self._mapping_lock = Linearizer(name="sso_user_mapping", clock=hs.get_clock())

        # a map from session id to session data
        self._username_mapping_sessions = {}  # type: Dict[str, UsernameMappingSession]

        # map from idp_id to SsoIdentityProvider
        self._identity_providers = {}  # type: Dict[str, SsoIdentityProvider]

    def register_identity_provider(self, p: SsoIdentityProvider):
        p_id = p.idp_id
        assert p_id not in self._identity_providers
        self._identity_providers[p_id] = p

    def get_identity_providers(self) -> Mapping[str, SsoIdentityProvider]:
        """Get the configured identity providers"""
        return self._identity_providers

    def render_error(
        self,
        request: Request,
        error: str,
        error_description: Optional[str] = None,
        code: int = 400,
    ) -> None:
        """Renders the error template and responds with it.

        This is used to show errors to the user. The template of this page can
        be found under `synapse/res/templates/sso_error.html`.

        Args:
            request: The incoming request from the browser.
                We'll respond with an HTML page describing the error.
            error: A technical identifier for this error.
            error_description: A human-readable description of the error.
            code: The integer error code (an HTTP response code)
        """
        html = self._error_template.render(
            error=error, error_description=error_description
        )
        respond_with_html(request, code, html)

    async def handle_redirect_request(
        self, request: SynapseRequest, client_redirect_url: bytes,
    ) -> str:
        """Handle a request to /login/sso/redirect

        Args:
            request: incoming HTTP request
            client_redirect_url: the URL that we should redirect the
                client to after login.

        Returns:
             the URI to redirect to
        """
        if not self._identity_providers:
            raise SynapseError(
                400, "Homeserver not configured for SSO.", errcode=Codes.UNRECOGNIZED
            )

        # if we only have one auth provider, redirect to it directly
        if len(self._identity_providers) == 1:
            ap = next(iter(self._identity_providers.values()))
            return await ap.handle_redirect_request(request, client_redirect_url)

        # otherwise, redirect to the IDP picker
        return "/_synapse/client/pick_idp?" + urlencode(
            (("redirectUrl", client_redirect_url),)
        )

    async def get_sso_user_by_remote_user_id(
        self, auth_provider_id: str, remote_user_id: str
    ) -> Optional[str]:
        """
        Maps the user ID of a remote IdP to a mxid for a previously seen user.

        If the user has not been seen yet, this will return None.

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".
            remote_user_id: The user ID according to the remote IdP. This might
                be an e-mail address, a GUID, or some other form. It must be
                unique and immutable.

        Returns:
            The mxid of a previously seen user.
        """
        logger.debug(
            "Looking for existing mapping for user %s:%s",
            auth_provider_id,
            remote_user_id,
        )

        # Check if we already have a mapping for this user.
        previously_registered_user_id = await self._store.get_user_by_external_id(
            auth_provider_id, remote_user_id,
        )

        # A match was found, return the user ID.
        if previously_registered_user_id is not None:
            logger.info(
                "Found existing mapping for IdP '%s' and remote_user_id '%s': %s",
                auth_provider_id,
                remote_user_id,
                previously_registered_user_id,
            )
            return previously_registered_user_id

        # No match.
        return None

    async def complete_sso_login_request(
        self,
        auth_provider_id: str,
        remote_user_id: str,
        request: SynapseRequest,
        client_redirect_url: str,
        sso_to_matrix_id_mapper: Callable[[int], Awaitable[UserAttributes]],
        grandfather_existing_users: Callable[[], Awaitable[Optional[str]]],
        extra_login_attributes: Optional[JsonDict] = None,
    ) -> None:
        """
        Given an SSO ID, retrieve the user ID for it and possibly register the user.

        This first checks if the SSO ID has previously been linked to a matrix ID,
        if it has that matrix ID is returned regardless of the current mapping
        logic.

        If a callable is provided for grandfathering users, it is called and can
        potentially return a matrix ID to use. If it does, the SSO ID is linked to
        this matrix ID for subsequent calls.

        The mapping function is called (potentially multiple times) to generate
        a localpart for the user.

        If an unused localpart is generated, the user is registered from the
        given user-agent and IP address and the SSO ID is linked to this matrix
        ID for subsequent calls.

        Finally, we generate a redirect to the supplied redirect uri, with a login token

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".

            remote_user_id: The unique identifier from the SSO provider.

            request: The request to respond to

            client_redirect_url: The redirect URL passed in by the client.

            sso_to_matrix_id_mapper: A callable to generate the user attributes.
                The only parameter is an integer which represents the amount of
                times the returned mxid localpart mapping has failed.

                It is expected that the mapper can raise two exceptions, which
                will get passed through to the caller:

                    MappingException if there was a problem mapping the response
                        to the user.
                    RedirectException to redirect to an additional page (e.g.
                        to prompt the user for more information).

            grandfather_existing_users: A callable which can return an previously
                existing matrix ID. The SSO ID is then linked to the returned
                matrix ID.

            extra_login_attributes: An optional dictionary of extra
                attributes to be provided to the client in the login response.

        Raises:
            MappingException if there was a problem mapping the response to a user.
            RedirectException: if the mapping provider needs to redirect the user
                to an additional page. (e.g. to prompt for more information)

        """
        # grab a lock while we try to find a mapping for this user. This seems...
        # optimistic, especially for implementations that end up redirecting to
        # interstitial pages.
        with await self._mapping_lock.queue(auth_provider_id):
            # first of all, check if we already have a mapping for this user
            user_id = await self.get_sso_user_by_remote_user_id(
                auth_provider_id, remote_user_id,
            )

            # Check for grandfathering of users.
            if not user_id:
                user_id = await grandfather_existing_users()
                if user_id:
                    # Future logins should also match this user ID.
                    await self._store.record_user_external_id(
                        auth_provider_id, remote_user_id, user_id
                    )

            # Otherwise, generate a new user.
            if not user_id:
                attributes = await self._call_attribute_mapper(sso_to_matrix_id_mapper)

                if attributes.localpart is None:
                    # the mapper doesn't return a username. bail out with a redirect to
                    # the username picker.
                    await self._redirect_to_username_picker(
                        auth_provider_id,
                        remote_user_id,
                        attributes,
                        client_redirect_url,
                        extra_login_attributes,
                    )

                user_id = await self._register_mapped_user(
                    attributes,
                    auth_provider_id,
                    remote_user_id,
                    request.get_user_agent(""),
                    request.getClientIP(),
                )

        await self._auth_handler.complete_sso_login(
            user_id, request, client_redirect_url, extra_login_attributes
        )

    async def _call_attribute_mapper(
        self, sso_to_matrix_id_mapper: Callable[[int], Awaitable[UserAttributes]],
    ) -> UserAttributes:
        """Call the attribute mapper function in a loop, until we get a unique userid"""
        for i in range(self._MAP_USERNAME_RETRIES):
            try:
                attributes = await sso_to_matrix_id_mapper(i)
            except (RedirectException, MappingException):
                # Mapping providers are allowed to issue a redirect (e.g. to ask
                # the user for more information) and can issue a mapping exception
                # if a name cannot be generated.
                raise
            except Exception as e:
                # Any other exception is unexpected.
                raise MappingException(
                    "Could not extract user attributes from SSO response."
                ) from e

            logger.debug(
                "Retrieved user attributes from user mapping provider: %r (attempt %d)",
                attributes,
                i,
            )

            if not attributes.localpart:
                # the mapper has not picked a localpart
                return attributes

            # Check if this mxid already exists
            user_id = UserID(attributes.localpart, self._server_name).to_string()
            if not await self._store.get_users_by_id_case_insensitive(user_id):
                # This mxid is free
                break
        else:
            # Unable to generate a username in 1000 iterations
            # Break and return error to the user
            raise MappingException(
                "Unable to generate a Matrix ID from the SSO response"
            )
        return attributes

    async def _redirect_to_username_picker(
        self,
        auth_provider_id: str,
        remote_user_id: str,
        attributes: UserAttributes,
        client_redirect_url: str,
        extra_login_attributes: Optional[JsonDict],
    ) -> NoReturn:
        """Creates a UsernameMappingSession and redirects the browser

        Called if the user mapping provider doesn't return a localpart for a new user.
        Raises a RedirectException which redirects the browser to the username picker.

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".

            remote_user_id: The unique identifier from the SSO provider.

            attributes: the user attributes returned by the user mapping provider.

            client_redirect_url: The redirect URL passed in by the client, which we
                will eventually redirect back to.

            extra_login_attributes: An optional dictionary of extra
                attributes to be provided to the client in the login response.

        Raises:
            RedirectException
        """
        session_id = random_string(16)
        now = self._clock.time_msec()
        session = UsernameMappingSession(
            auth_provider_id=auth_provider_id,
            remote_user_id=remote_user_id,
            display_name=attributes.display_name,
            emails=attributes.emails,
            client_redirect_url=client_redirect_url,
            expiry_time_ms=now + self._MAPPING_SESSION_VALIDITY_PERIOD_MS,
            extra_login_attributes=extra_login_attributes,
        )

        self._username_mapping_sessions[session_id] = session
        logger.info("Recorded registration session id %s", session_id)

        # Set the cookie and redirect to the username picker
        e = RedirectException(b"/_synapse/client/pick_username")
        e.cookies.append(
            b"%s=%s; path=/"
            % (USERNAME_MAPPING_SESSION_COOKIE_NAME, session_id.encode("ascii"))
        )
        raise e

    async def _register_mapped_user(
        self,
        attributes: UserAttributes,
        auth_provider_id: str,
        remote_user_id: str,
        user_agent: str,
        ip_address: str,
    ) -> str:
        """Register a new SSO user.

        This is called once we have successfully mapped the remote user id onto a local
        user id, one way or another.

        Args:
             attributes: user attributes returned by the user mapping provider,
                including a non-empty localpart.

            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".

            remote_user_id: The unique identifier from the SSO provider.

            user_agent: The user-agent in the HTTP request (used for potential
                shadow-banning.)

            ip_address: The IP address of the requester (used for potential
                shadow-banning.)

        Raises:
            a MappingException if the localpart is invalid.

            a SynapseError with code 400 and errcode Codes.USER_IN_USE if the localpart
            is already taken.
        """

        # Since the localpart is provided via a potentially untrusted module,
        # ensure the MXID is valid before registering.
        if not attributes.localpart or contains_invalid_mxid_characters(
            attributes.localpart
        ):
            raise MappingException("localpart is invalid: %s" % (attributes.localpart,))

        logger.debug("Mapped SSO user to local part %s", attributes.localpart)
        registered_user_id = await self._registration_handler.register_user(
            localpart=attributes.localpart,
            default_display_name=attributes.display_name,
            bind_emails=attributes.emails,
            user_agent_ips=[(user_agent, ip_address)],
        )

        await self._store.record_user_external_id(
            auth_provider_id, remote_user_id, registered_user_id
        )
        return registered_user_id

    async def complete_sso_ui_auth_request(
        self,
        auth_provider_id: str,
        remote_user_id: str,
        ui_auth_session_id: str,
        request: Request,
    ) -> None:
        """
        Given an SSO ID, retrieve the user ID for it and complete UIA.

        Note that this requires that the user is mapped in the "user_external_ids"
        table. This will be the case if they have ever logged in via SAML or OIDC in
        recentish synapse versions, but may not be for older users.

        Args:
            auth_provider_id: A unique identifier for this SSO provider, e.g.
                "oidc" or "saml".
            remote_user_id: The unique identifier from the SSO provider.
            ui_auth_session_id: The ID of the user-interactive auth session.
            request: The request to complete.
        """

        user_id = await self.get_sso_user_by_remote_user_id(
            auth_provider_id, remote_user_id,
        )

        if not user_id:
            logger.warning(
                "Remote user %s/%s has not previously logged in here: UIA will fail",
                auth_provider_id,
                remote_user_id,
            )
            # Let the UIA flow handle this the same as if they presented creds for a
            # different user.
            user_id = ""

        await self._auth_handler.complete_sso_ui_auth(
            user_id, ui_auth_session_id, request
        )

    async def check_username_availability(
        self, localpart: str, session_id: str,
    ) -> bool:
        """Handle an "is username available" callback check

        Args:
            localpart: desired localpart
            session_id: the session id for the username picker
        Returns:
            True if the username is available
        Raises:
            SynapseError if the localpart is invalid or the session is unknown
        """

        # make sure that there is a valid mapping session, to stop people dictionary-
        # scanning for accounts

        self._expire_old_sessions()
        session = self._username_mapping_sessions.get(session_id)
        if not session:
            logger.info("Couldn't find session id %s", session_id)
            raise SynapseError(400, "unknown session")

        logger.info(
            "[session %s] Checking for availability of username %s",
            session_id,
            localpart,
        )

        if contains_invalid_mxid_characters(localpart):
            raise SynapseError(400, "localpart is invalid: %s" % (localpart,))
        user_id = UserID(localpart, self._server_name).to_string()
        user_infos = await self._store.get_users_by_id_case_insensitive(user_id)

        logger.info("[session %s] users: %s", session_id, user_infos)
        return not user_infos

    async def handle_submit_username_request(
        self, request: SynapseRequest, localpart: str, session_id: str
    ) -> None:
        """Handle a request to the username-picker 'submit' endpoint

        Will serve an HTTP response to the request.

        Args:
            request: HTTP request
            localpart: localpart requested by the user
            session_id: ID of the username mapping session, extracted from a cookie
        """
        self._expire_old_sessions()
        session = self._username_mapping_sessions.get(session_id)
        if not session:
            logger.info("Couldn't find session id %s", session_id)
            raise SynapseError(400, "unknown session")

        logger.info("[session %s] Registering localpart %s", session_id, localpart)

        attributes = UserAttributes(
            localpart=localpart,
            display_name=session.display_name,
            emails=session.emails,
        )

        # the following will raise a 400 error if the username has been taken in the
        # meantime.
        user_id = await self._register_mapped_user(
            attributes,
            session.auth_provider_id,
            session.remote_user_id,
            request.get_user_agent(""),
            request.getClientIP(),
        )

        logger.info("[session %s] Registered userid %s", session_id, user_id)

        # delete the mapping session and the cookie
        del self._username_mapping_sessions[session_id]

        # delete the cookie
        request.addCookie(
            USERNAME_MAPPING_SESSION_COOKIE_NAME,
            b"",
            expires=b"Thu, 01 Jan 1970 00:00:00 GMT",
            path=b"/",
        )

        await self._auth_handler.complete_sso_login(
            user_id,
            request,
            session.client_redirect_url,
            session.extra_login_attributes,
        )

    def _expire_old_sessions(self):
        to_expire = []
        now = int(self._clock.time_msec())

        for session_id, session in self._username_mapping_sessions.items():
            if session.expiry_time_ms <= now:
                to_expire.append(session_id)

        for session_id in to_expire:
            logger.info("Expiring mapping session %s", session_id)
            del self._username_mapping_sessions[session_id]
