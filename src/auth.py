"""Access control for the two audiences of the app.

Managers (approve payments, release escrow, read scores, edit the rules)
authenticate with a single bearer token, MANAGER_TOKEN. Requesters (people
asking for payments) never see decision internals and need no account; an
optional shared access code, REQUESTER_ACCESS_CODE, can gate who may submit.

Both are read from the environment on every request, never cached at import,
so a value from .env or a test's monkeypatch is always honoured.

Alpha scope, deliberately: one shared manager token, no per-user identity, no
rate limiting. See README ("Roles and access") for what that does not cover.
"""

import logging
import os
import secrets

from dotenv import load_dotenv
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

load_dotenv()

logger = logging.getLogger(__name__)

# Short tokens are guessable; refuse to treat one as configured.
MIN_TOKEN_LENGTH = 16

# The value shipped in .env.example. It is deliberately USABLE, so that a
# reviewer can open the dashboard without generating a secret first, and
# deliberately NOT SECRET: anyone with the repository has it. `manager_token`
# warns once while it is still in force, so a public deployment of the demo
# token is loud rather than silent.
EXAMPLE_MANAGER_TOKEN = "trust-me-im-the-manager-token"

_warned_about_example_token = False


def _warn_if_example_token_in_use(token: str) -> None:
    """Say so once -- not on every request -- while the demo token is set."""
    global _warned_about_example_token
    if _warned_about_example_token or token != EXAMPLE_MANAGER_TOKEN:
        return
    _warned_about_example_token = True
    logger.warning(
        "MANAGER_TOKEN is the demo value shipped in .env.example, so anyone with "
        "the repository can sign in as manager. Change it before exposing the app "
        "beyond localhost."
    )

_bearer = HTTPBearer(auto_error=False)


def manager_token() -> str | None:
    """The configured manager token, or None if absent or too short."""
    token = os.environ.get("MANAGER_TOKEN", "").strip()
    if len(token) < MIN_TOKEN_LENGTH:
        return None
    _warn_if_example_token_in_use(token)
    return token


def _matches(supplied: str, expected: str) -> bool:
    # Constant-time comparison: response time must not reveal how many
    # leading characters of a guess were right.
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def require_manager(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """FastAPI dependency guarding every manager route.

    Fails closed: with no usable MANAGER_TOKEN configured, manager routes
    are unavailable to everyone (503), rather than open.
    """
    expected = manager_token()
    if expected is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Manager access is not configured. Set MANAGER_TOKEN "
                f"(at least {MIN_TOKEN_LENGTH} characters) in .env."
            ),
        )
    supplied = credentials.credentials if credentials else ""
    if not _matches(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing manager token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def access_code_required() -> bool:
    return bool(os.environ.get("REQUESTER_ACCESS_CODE", "").strip())


def require_requester_access(x_access_code: str | None = Header(default=None)) -> None:
    """Gate submissions behind REQUESTER_ACCESS_CODE, if one is set.

    When unset, submission is open (the alpha default). Checking the status
    of an existing request never needs the code: the unguessable request id
    is itself the capability.
    """
    expected = os.environ.get("REQUESTER_ACCESS_CODE", "").strip()
    if not expected:
        return
    if not _matches(x_access_code or "", expected):
        raise HTTPException(status_code=401, detail="A valid access code is required.")
