"""OAuth client authentication shared by every client of an OIDC issuer.

The console's sign-in (`admin.oidc.provider`) and the account-provisioning
adapter (`adapters.accounts.cyberdyneauth`) authenticate to the same kind of
token endpoint the same way; keeping the rule here means a fix reaches both.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote


def token_auth_methods(discovery: Mapping[str, Any]) -> tuple[str, ...]:
    """`token_endpoint_auth_methods_supported`, keeping only the strings."""
    methods = discovery.get("token_endpoint_auth_methods_supported")
    if not isinstance(methods, list):
        return ()
    return tuple(m for m in methods if isinstance(m, str))


def client_auth(
    form: Mapping[str, str], client_id: str, client_secret: str, methods: tuple[str, ...]
) -> tuple[dict[str, str], dict[str, str]]:
    """The token request's form and headers: client_secret_basic unless the
    issuer offers only client_secret_post."""
    if methods and "client_secret_basic" not in methods and "client_secret_post" in methods:
        return {**form, "client_id": client_id, "client_secret": client_secret}, {}
    # RFC 6749 2.3.1: form-encode each half before the Basic encoding.
    pair = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"
    return dict(form), {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}
