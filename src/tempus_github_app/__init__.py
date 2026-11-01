"""Tempus Guard for GitHub: the credential-isolated GitHub executor of the Tempus protocol
and the "Verified by Tempus" check run that carries each action's evidence."""

from typing import Any

from .check_run import CheckRunPublisher, Evidence, build_check_run
from .credentials import GitHubAppCredentials
from .executor import GitHubAppActionAdapter, GitHubAppExecutorAdapter
from .guard import GuardError, GuardFlow, read_public_key
from .transport import GitHubRateLimitError, PermitContext
from .webhook import GitHubWebhookHandler, verify_webhook_signature

__version__ = "0.2.0"
__all__ = [
    "CheckRunPublisher",
    "Evidence",
    "GitHubAppActionAdapter",
    "GitHubAppCredentials",
    "GitHubAppExecutorAdapter",
    "GitHubRateLimitError",
    "GitHubWebhookHandler",
    "GuardError",
    "GuardFlow",
    "PermitContext",
    "build_check_run",
    "create_webhook_app",
    "read_public_key",
    "verify_webhook_signature",
]


def create_webhook_app(*args: Any, **kwargs: Any) -> Any:
    """Lazily load and create the FastAPI webhook application."""
    from .server import create_webhook_app as _create_webhook_app

    return _create_webhook_app(*args, **kwargs)
