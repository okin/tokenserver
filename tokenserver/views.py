# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
import json
import os
import re
import time
import logging

from cornice import Service
from mozsvc.metrics import metrics_timer
from pyramid import httpexceptions


import tokenlib

from tokenserver.verifiers import (
    ComponentLookupError,
    ConnectionError,
    get_browserid_verifier,
    get_oauth_verifier
)
from tokenserver.assignment import INodeAssignment
from tokenserver.util import json_error, fxa_metrics_hash

import fxa.errors
import browserid.errors
import browserid.utils


logger = logging.getLogger("tokenserver")

# A GET on / returns the discovery API

discovery = Service(name='discovery', path='/')
token = Service(name='token', path='/1.0/{application}/{version}')

DEFAULT_TOKEN_DURATION = 5 * 60


def get_service_name(application, version):
    return "%s-%s" % (application, version)


@discovery.get()
def _discovery(request):
    """Returns a JSON file listing the services supported by the server."""
    services = request.registry.settings['tokenserver.applications']
    discovery = {}
    discovery["services"] = services
    discovery["auth"] = request.url.rstrip("/")
    try:
        verifier = get_browserid_verifier(request.registry)
    except ComponentLookupError:
        pass
    else:
        discovery["browserid"] = {
          "allowed_issuers": verifier.allowed_issuers,
          "trusted_issuers": verifier.trusted_issuers,
        }
    try:
        verifier = get_oauth_verifier(request.registry)
    except ComponentLookupError:
        pass
    else:
        discovery["oauth"] = {
          "default_issuer": verifier.default_issuer,
          "scope": verifier.scope,
          "server_url": verifier.server_url,
        }
    return discovery


def _unauthorized(status_message='error', **kw):
    kw.setdefault('description', 'Unauthorized')
    return json_error(401, status_message, **kw)


def _invalid_client_state(reason, **kw):
    kw.setdefault('location', 'header')
    kw.setdefault('name', 'X-Client-State')
    description = 'Unacceptable client-state value %s' % (reason,)
    kw.setdefault('description', description)
    return _unauthorized('invalid-client-state', **kw)


# validators

def valid_authorization(request, **kwargs):
    """Validate that the Authorization on the request is correct.

    If not, add errors in the response so that the client can know what
    happened.
    """
    authz = request.headers.get('Authorization')
    if authz is None:
        raise _unauthorized()

    authz = authz.split(None, 1)
    if len(authz) != 2:
        raise _unauthorized()
    name, token = authz

    if name.lower() == 'browserid':
        return _valid_browserid_assertion(request, token)

    if name.lower() == 'bearer':
        return _valid_oauth_token(request, token)

    resp = _unauthorized(description='Unsupported')
    resp.www_authenticate = ('BrowserID', {})
    raise resp


def _valid_browserid_assertion(request, assertion):
    try:
        verifier = get_browserid_verifier(request.registry)
    except ComponentLookupError:
        raise _unauthorized(description='Unsupported')
    try:
        with metrics_timer('tokenserver.assertion.verify', request):
            assertion = verifier.verify(assertion)
    except browserid.errors.Error as e:
        # Convert CamelCase to under_scores for reporting.
        error_type = e.__class__.__name__
        error_type = re.sub('(?<=.)([A-Z])', r'_\1', error_type).lower()
        request.metrics['token.assertion.verify_failure'] = 1
        request.metrics['token.assertion.%s' % error_type] = 1
        # Log a full traceback for errors that are not a simple
        # "your assertion was bad and we dont trust it".
        if not isinstance(e, browserid.errors.TrustError):
            logger.exception("Unexpected verification error")
        # Report an appropriate error code.
        if isinstance(e, browserid.errors.ConnectionError):
            raise json_error(503, description="Resource is not available")
        if isinstance(e, browserid.errors.ExpiredSignatureError):
            raise _unauthorized("invalid-timestamp")
        raise _unauthorized("invalid-credentials")

    # FxA sign-in confirmation introduced the notion of unverified tokens.
    # The default value is True to preserve backwards compatibility.
    try:
        tokenVerified = assertion['idpClaims']['fxa-tokenVerified']
    except KeyError:
        tokenVerified = True
    if not tokenVerified:
        raise _unauthorized("invalid-credentials")

    # everything sounds good, add the assertion to the list of validated fields
    # and continue
    request.metrics['token.assertion.verify_success'] = 1
    request.validated['authorization'] = assertion

    email = assertion['email']
    request.validated['fxa_uid'] = email.split("@", 1)[0]

    id_key = request.registry.settings.get("fxa.metrics_uid_secret_key")
    if id_key is None:
        id_key = 'insecure'
    hashed_fxa_uid_full = fxa_metrics_hash(email, id_key)
    # "legacy" key used by heka active_counts.lua
    request.metrics['uid'] = hashed_fxa_uid_full
    request.metrics['email'] = email

    # "new" keys use shorter values
    hashed_fxa_uid = hashed_fxa_uid_full[:32]
    request.validated['hashed_fxa_uid'] = hashed_fxa_uid
    request.metrics['hashed_fxa_uid'] = hashed_fxa_uid

    try:
        device = assertion['idpClaims']['fxa-deviceId']
        if device is None:
            device = 'none'
    except KeyError:
        device = 'none'
    hashed_device_id = fxa_metrics_hash(hashed_fxa_uid + device, id_key)[:32]
    request.validated['hashed_device_id'] = hashed_device_id
    request.metrics['device_id'] = hashed_device_id


def _valid_oauth_token(request, token):
    try:
        verifier = get_oauth_verifier(request.registry)
    except ComponentLookupError:
        raise _unauthorized(description='Unsupported')
    try:
        with metrics_timer('tokenserver.oauth.verify', request):
            token = verifier.verify(token)
    except (fxa.errors.Error, ConnectionError) as e:
        request.metrics['token.oauth.verify_failure'] = 1
        if isinstance(e, fxa.errors.InProtocolError):
            request.metrics['token.oauth.errno.%s' % e.errno] = 1
        # Log a full traceback for errors that are not a simple
        # "your token was bad and we dont trust it".
        if not isinstance(e, fxa.errors.TrustError):
            logger.exception("Unexpected verification error")
        # Report an appropriate error code.
        if isinstance(e, ConnectionError):
            request.metrics['token.oauth.connection_error'] = 1
            raise json_error(503, description="Resource is not available")
        raise _unauthorized("invalid-credentials")

    request.metrics['token.oauth.verify_success'] = 1
    request.validated['authorization'] = token

    # OAuth clients should send the scoped-key kid in lieu of X-Client-State.
    # A future enhancement might allow us to learn this from the OAuth
    # verification response rather than requiring a separate header.
    kid = request.headers.get('X-KeyID')
    if kid:
        try:
            # The kid combines a timestamp and a hash of the key material,
            # so we can decode it into equivalent information to what we
            # get out of a BrowserID assertion.
            generation, client_state = kid.split("-", 1)
            generation = int(generation)
            idpClaims = request.validated['authorization']['idpClaims']
            idpClaims['fxa-generation'] = generation
            client_state = browserid.utils.decode_bytes(client_state)
            client_state = client_state.encode('hex')
            if not 1 <= len(client_state) <= 32:
                raise json_error(400, location='header', name='X-Client-State',
                                 description='Invalid client state value')
            # Sanity-check in case the client sent *both* headers.
            # If they don't match, the client is definitely confused.
            if 'X-Client-State' in request.headers:
                if request.headers['X-Client-State'] != client_state:
                    raise _unauthorized("invalid-client-state")
            request.validated['client-state'] = client_state
        except (IndexError, ValueError):
            raise _unauthorized("invalid-credentials")

    email = token['email']
    request.validated['fxa_uid'] = email.split("@", 1)[0]

    id_key = request.registry.settings.get("fxa.metrics_uid_secret_key")
    if id_key is None:
        id_key = 'insecure'
    hashed_fxa_uid_full = fxa_metrics_hash(email, id_key)
    # "legacy" key used by heka active_counts.lua
    request.metrics['uid'] = hashed_fxa_uid_full
    request.metrics['email'] = email

    # "new" keys use shorter values
    hashed_fxa_uid = hashed_fxa_uid_full[:32]
    request.validated['hashed_fxa_uid'] = hashed_fxa_uid
    request.metrics['fxa_uid'] = hashed_fxa_uid

    # There's currently no notion of a "device id" in OAuth.
    # In future we might be able to use e.g. the refresh token
    # or some derivative of it here.
    device = 'none'
    hashed_device_id = fxa_metrics_hash(hashed_fxa_uid + device, id_key)[:32]
    request.validated['hashed_device_id'] = hashed_device_id
    request.metrics['device_id'] = hashed_device_id


def valid_app(request, **kwargs):
    """Checks that the given application is one of the compatible ones.

    If it's not the case, a 404 is issued with the appropriate information.
    """
    supported = request.registry.settings['tokenserver.applications']
    application = request.matchdict.get('application')
    version = request.matchdict.get('version')

    if application not in supported:
        raise json_error(404, location='url', name='application',
                         description='Unsupported application')
    else:
        request.validated['application'] = application

    supported_versions = supported[application]

    if version not in supported_versions:
        raise json_error(404, location='url', name=version,
                         description='Unsupported application version')
    else:
        request.validated['version'] = version


def valid_client_state(request, **kwargs):
    """Checks for and validates the X-Client-State header."""
    client_state = request.headers.get('X-Client-State', '')
    if client_state:
        if not re.match("^[a-zA-Z0-9._-]{1,32}$", client_state):
            raise json_error(400, location='header', name='X-Client-State',
                             description='Invalid client state value')
    request.validated['client-state'] = client_state


def pattern_exists(request, **kwargs):
    """Checks that the given service do have an associated pattern in the db or
    in the configuration file.

    If not, raises a 503 error.
    """
    application = request.validated['application']
    version = request.validated['version']
    defined_patterns = request.registry['endpoints_patterns']
    service = get_service_name(application, version)
    try:
        pattern = defined_patterns[service]
    except KeyError:
        description = "The api_endpoint pattern for %r is not known" % service
        raise json_error(503, description=description)

    request.validated['pattern'] = pattern


VALIDATORS = (
    valid_app,
    valid_client_state,
    valid_authorization,
    pattern_exists
)


@token.get(validators=VALIDATORS)
def return_token(request):
    """This service does the following process:

    - validates the BrowserID assertion provided on the Authorization header
    - allocates when necessary a node to the user for the required service
    - checks generation numbers and x-client-state header
    - returns a JSON mapping containing the following values:

        - **id** -- a signed authorization token, containing the
          user's id for hthe application and the node.
        - **secret** -- a secret derived from the shared secret
        - **uid** -- the user id for this servic
        - **api_endpoint** -- the root URL for the user for the service.
    """
    # at this stage, we are sure that the assertion, application and version
    # number were valid, so let's build the authentication token and return it.
    backend = request.registry.getUtility(INodeAssignment)
    settings = request.registry.settings
    email = request.validated['authorization']['email']
    try:
        idp_claims = request.validated['authorization']['idpClaims']
        generation = idp_claims['fxa-generation']
        if not isinstance(generation, (int, long)):
            raise _unauthorized("invalid-generation")
    except KeyError:
        generation = 0
    application = request.validated['application']
    version = request.validated['version']
    pattern = request.validated['pattern']
    service = get_service_name(application, version)
    client_state = request.validated['client-state']

    with metrics_timer('tokenserver.backend.get_user', request):
        user = backend.get_user(service, email)
    if not user:
        allowed = settings.get('tokenserver.allow_new_users', True)
        if not allowed:
            raise _unauthorized('new-users-disabled')
        with metrics_timer('tokenserver.backend.allocate_user', request):
            user = backend.allocate_user(service, email, generation,
                                         client_state)

    # Update if this client is ahead of previously-seen clients.
    updates = {}
    if generation > user['generation']:
        updates['generation'] = generation
    if client_state != user['client_state']:
        # Don't revert from some-client-state to no-client-state.
        if not client_state:
            raise _invalid_client_state('empty string')
        # Don't revert to a previous client-state.
        if client_state in user['old_client_states']:
            raise _invalid_client_state('stale value')
        # If the IdP has been sending generation numbers, then
        # don't update client-state without a change in generation number.
        if user['generation'] > 0 and 'generation' not in updates:
            raise _invalid_client_state('new value with no generation change')
        updates['client_state'] = client_state
    if updates:
        with metrics_timer('tokenserver.backend.update_user', request):
            backend.update_user(service, user, **updates)

    # Error out if this client is behind some previously-seen client.
    # This is done after the updates because some other, even more up-to-date
    # client may have raced with a concurrent update.
    if user['generation'] > generation:
        raise _unauthorized("invalid-generation")

    secrets = settings['tokenserver.secrets']
    node_secrets = secrets.get(user['node'])
    if not node_secrets:
        raise Exception("The specified node does not have any shared secret")
    secret = node_secrets[-1]  # the last one is the most recent one

    # Clients can request a smaller token duration via an undocumented
    # query parameter, for testing purposes.
    token_duration = settings.get(
        'tokenserver.token_duration', DEFAULT_TOKEN_DURATION
    )
    try:
        requested_duration = int(request.params["duration"])
    except (KeyError, ValueError):
        pass
    else:
        if 0 < requested_duration < token_duration:
            token_duration = requested_duration

    token_data = {
        'uid': user['uid'],
        'node': user['node'],
        'expires': int(time.time()) + token_duration,
        'fxa_uid': request.validated['fxa_uid'],
        'fxa_kid': request.validated['client-state'],
        'hashed_fxa_uid': request.validated['hashed_fxa_uid'],
        'hashed_device_id': request.validated['hashed_device_id']
    }
    token = tokenlib.make_token(token_data, secret=secret)
    secret = tokenlib.get_derived_secret(token, secret=secret)

    endpoint = pattern.format(
        uid=user['uid'],
        service=service,
        node=user['node']
    )

    # To help measure user retention, include the timestamp at which we
    # first saw this user as part of the logs.
    request.metrics['uid.first_seen_at'] = user['first_seen_at']

    return {
        'id': token,
        'key': secret,
        'uid': user['uid'],
        'hashed_fxa_uid': request.validated['fxa_uid'],
        'api_endpoint': endpoint,
        'duration': token_duration,
        'hashalg': tokenlib.DEFAULT_HASHMOD
    }


# Heartbeat

lbheartbeat = Service(name="lbheartbeat", path='/__lbheartbeat__',
                      description="Web head health")


@lbheartbeat.get()
def get_lbheartbeat(request):
    """Return successful healthy response.

    If the load-balancer tries to access this URL and fails, this means the
    Web head is not operational and should be dropped.
    """
    return {}


version = Service(name="version", path='/__version__', description="Version")
HERE = os.path.dirname(os.path.abspath(__file__))
ORIGIN = os.path.dirname(os.path.dirname(HERE))


@version.get()
def version_view(request):
    try:
        return version_view.__json__
    except AttributeError:
        pass

    files = [
        './version.json',  # Default is current working dir.
        os.path.join(ORIGIN, 'version.json'),  # Relative to the package root.
    ]
    for version_file in files:
        file_path = os.path.abspath(version_file)
        if os.path.exists(file_path):
            with open(file_path) as f:
                version_view.__json__ = json.load(f)
                return version_view.__json__  # First one wins.

    raise httpexceptions.HTTPNotFound()
