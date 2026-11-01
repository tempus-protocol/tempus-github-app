"""The "Verified by Tempus" check run: the face of an action's evidence on a pull request.

After the executor has acted and the gate has signed the execution receipt, Guard
exports the evidence bundle, verifies it against the pinned gate key and publishes one
check run on the commit the action produced. The check carries the action identifier,
the outcome, the verification result and a link to the public verifier, and embeds the
bundle so a reviewer can verify it with no Tempus install.

Publishing the check never changes the evidence: the bundle is complete and signed
before this module runs, and a failed publish is reported, not raised.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib import parse

from tempus_ddb.executor_runtime import AmbiguousTransportError

from .errors import BINDING_REJECTED, TRANSPORT_AMBIGUOUS, http_status, server_response
from .transport import (
    RESOURCE_PATTERN,
    GitHubAPIError,
    GitHubExecutorError,
    GitHubTransport,
    PermitContext,
    UrllibGitHubTransport,
    validate_github_api_url,
)

# The installation-token scope the check run needs (`checks: write`); it is not a Tempus
# action type and never appears in an intent.
CHECK_RUN_SCOPE = "tempus.guard.check_run"
DEFAULT_CHECK_NAME = "Verified by Tempus"
# GitHub limits for check run output fields.
MAX_OUTPUT_TEXT = 65_535
MAX_OUTPUT_SUMMARY = 65_535
MAX_OUTPUT_TITLE = 1_024
# Room left in `text` for the bundle once the fixed prose is written.
_BUNDLE_BUDGET = MAX_OUTPUT_TEXT - 4_096

_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_HEX_KEY_PATTERN = re.compile(r"[0-9a-f]{64}")

# Which field of a sanitized outcome names the commit the check attaches to.
_HEAD_SHA_FIELDS = {
    "github.push_branch": "sha",
    "github.merge_pull_request": "sha",
    "github.create_pull_request": "head_sha",
}

_NOT_CHECKED_TEXT = {
    "AGENT_REGISTRATION_AT_ISSUE": "whether the agent was registered when the permit was issued (the gate attests it)",
    "SINGLE_USE_CONSUMPTION": "that this permit produced exactly one outcome (only the gate ledger shows that)",
    "EXECUTOR_IDENTITY": "which executor acted (pin the executor key in the verifier to check it)",
    "LEDGER_INCLUSION": "that these events are in the gate's signed ledger (no checkpoint in the bundle)",
    "COMPLETENESS": "that nothing else happened: other actions, retries or outcomes",
}


@dataclass(frozen=True)
class Evidence:
    """What the check run publishes. Every value comes from signed documents or the verifier."""

    action_id: str
    action_type: str
    resource: str
    head_sha: str
    outcome_status: str
    verification: dict[str, Any]
    bundle_json: str
    gate_public_key: str
    executor_public_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not _HEX_KEY_PATTERN.fullmatch(self.action_id):
            raise GitHubExecutorError("action_id must be 64 lowercase hexadecimal characters")
        if not isinstance(self.resource, str) or not RESOURCE_PATTERN.fullmatch(self.resource):
            raise GitHubExecutorError("resource must be an exact 'owner/repository' value")
        if not isinstance(self.head_sha, str) or not _SHA_PATTERN.fullmatch(self.head_sha):
            raise GitHubExecutorError("head_sha must be 40 lowercase hexadecimal characters")
        if self.outcome_status not in {"SUCCEEDED", "FAILED"}:
            raise GitHubExecutorError("outcome_status must be SUCCEEDED or FAILED")
        if not isinstance(self.verification, dict):
            raise GitHubExecutorError("verification must be the verifier's result object")
        if not _HEX_KEY_PATTERN.fullmatch(self.gate_public_key or ""):
            raise GitHubExecutorError("gate_public_key must be 64 lowercase hexadecimal characters")
        if self.executor_public_key is not None and not _HEX_KEY_PATTERN.fullmatch(
            self.executor_public_key
        ):
            raise GitHubExecutorError("executor_public_key must be 64 lowercase hexadecimal characters")

    @property
    def verification_status(self) -> str:
        status = self.verification.get("status")
        return status if status in {"VERIFIED", "INVALID"} else "INVALID"

    @property
    def verify_url(self) -> str | None:
        url = self.verification.get("verify_url")
        return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None

    @property
    def bundle_digest(self) -> str | None:
        digest = self.verification.get("bundle_digest")
        return digest if isinstance(digest, str) and _HEX_KEY_PATTERN.fullmatch(digest) else None


def head_sha_for(action_type: str, output: Any) -> str | None:
    """The commit a sanitized outcome names, if the action produced one."""
    field = _HEAD_SHA_FIELDS.get(action_type)
    if field is None or not isinstance(output, dict):
        return None
    sha = output.get(field)
    if isinstance(sha, str) and _SHA_PATTERN.fullmatch(sha.lower()):
        return sha.lower()
    return None


def conclusion_for(verification_status: str, outcome_status: str) -> str:
    """A check is green only when the evidence verifies and the action succeeded."""
    if verification_status == "VERIFIED" and outcome_status == "SUCCEEDED":
        return "success"
    return "failure"


def details_url_for(evidence: Evidence) -> str | None:
    """The bundle's verify_url plus the gate key as a hint the verifier page pre-fills.

    The hint is a convenience: an independent reader obtains the key out of band.
    """
    base = evidence.verify_url
    if base is None:
        return None
    url = parse.urlsplit(base)
    if url.fragment and "gate=" not in url.fragment:
        return base + "&gate=" + evidence.gate_public_key
    return base


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_output(evidence: Evidence) -> dict[str, str]:
    """The check run output a reviewer reads; the bundle is embedded when it fits."""
    status = evidence.verification_status
    trust = evidence.verification.get("trust") or {}
    independent = trust.get("independent") is True
    title = f"{status} · {evidence.outcome_status} · {evidence.action_type}"
    summary = (
        f"Tempus evidence for action `{evidence.action_id[:16]}…` "
        f"{'verifies' if status == 'VERIFIED' else 'does NOT verify'} against the pinned gate key; "
        f"the action {'succeeded' if evidence.outcome_status == 'SUCCEEDED' else 'failed'} on `{evidence.resource}`."
    )
    if status == "VERIFIED" and not independent:
        summary += " The verification was not independent (no pinned key)."

    lines = [
        f"**Action** `{evidence.action_id}`",
        f"**Resource** `{evidence.resource}` · **Commit** `{evidence.head_sha}`",
        f"**Outcome** {evidence.outcome_status} · **Evidence** {status}"
        + (" (independent: gate key pinned)" if independent else ""),
        f"**Gate public key** `{evidence.gate_public_key}`",
    ]
    if evidence.executor_public_key:
        lines.append(f"**Executor public key** `{evidence.executor_public_key}`")
    if evidence.bundle_digest:
        lines.append(f"**Bundle digest** `{evidence.bundle_digest}`")
    errors = evidence.verification.get("errors") or []
    if errors:
        lines += ["", "**Verification errors**"] + [f"- `{code}`" for code in errors]
    not_checked = trust.get("not_checked") or []
    if not_checked:
        lines += ["", "**What this check does not show**"]
        for entry in not_checked:
            code = str(entry).split(" ")[0]
            lines.append(f"- `{code}`: {_NOT_CHECKED_TEXT.get(code, str(entry))}")

    verify_url = evidence.verify_url
    lines += ["", "### Verify it yourself"]
    if verify_url:
        lines.append(
            f"1. Open the verifier: {verify_url} (it runs offline in your browser; nothing is sent)."
        )
    else:
        lines.append("1. Open the Tempus verifier page.")
    lines += [
        (
            "2. Paste the evidence bundle below and the gate public key. For an independent "
            "result, obtain the key from the operator through a channel you trust, not from this check."
        ),
        f"3. Expect **{status}**"
        + (f" with bundle digest `{evidence.bundle_digest}`." if evidence.bundle_digest else "."),
        "",
        "CLI: `tempus verify-bundle --bundle evidence.json --gate-public-key <hex>`",
    ]

    bundle = evidence.bundle_json.strip()
    if len(bundle) <= _BUNDLE_BUDGET:
        lines += [
            "",
            "<details>",
            f"<summary>Evidence bundle (tempus.evidence-bundle.v1, {len(bundle.encode('utf-8'))} bytes)</summary>",
            "",
            "```json",
            bundle,
            "```",
            "</details>",
        ]
    else:
        lines += [
            "",
            f"The evidence bundle ({len(bundle.encode('utf-8'))} bytes) exceeds GitHub's check output limit. "
            f"Ask the operator for the bundle of action `{evidence.action_id}`"
            + (f" (digest `{evidence.bundle_digest}`)" if evidence.bundle_digest else "")
            + ".",
        ]
    return {
        "title": _truncate(title, MAX_OUTPUT_TITLE),
        "summary": _truncate(summary, MAX_OUTPUT_SUMMARY),
        "text": _truncate("\n".join(lines), MAX_OUTPUT_TEXT),
    }


def build_check_run(evidence: Evidence, name: str = DEFAULT_CHECK_NAME) -> dict[str, Any]:
    """The Checks API payload for one completed check run."""
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise GitHubExecutorError("check run name must be 1 to 255 characters")
    payload: dict[str, Any] = {
        "name": name,
        "head_sha": evidence.head_sha,
        "external_id": evidence.action_id,
        "status": "completed",
        "conclusion": conclusion_for(evidence.verification_status, evidence.outcome_status),
        "output": build_output(evidence),
    }
    details_url = details_url_for(evidence)
    if details_url:
        payload["details_url"] = details_url
    return payload


class CheckRunPublisher:
    """Create the check run with a `checks: write` installation token scoped to the repository."""

    def __init__(
        self,
        credentials: Any,
        api_url: str = "https://api.github.com",
        transport: GitHubTransport | None = None,
        *,
        name: str = DEFAULT_CHECK_NAME,
        user_agent: str = "tempus-github-app-guard/0.2.0",
    ):
        self._credentials = credentials
        self._api_url = validate_github_api_url(api_url)
        self._transport = transport or UrllibGitHubTransport()
        self._name = name
        self._user_agent = user_agent
        build_check_run(_placeholder_evidence(), name)  # validate the name once

    @property
    def name(self) -> str:
        return self._name

    def publish(self, evidence: Evidence, *, context: PermitContext | None = None) -> dict[str, Any]:
        """Publish one check run. Returns a small status object and never raises for GitHub errors."""
        try:
            payload = build_check_run(evidence, self._name)
        except GitHubExecutorError as exc:
            return {"status": "FAILED", "error_code": BINDING_REJECTED, "message": str(exc)}
        try:
            token = self._credentials.token_for(evidence.resource, CHECK_RUN_SCOPE)
        except Exception:  # noqa: BLE001 - never leak credential details
            return {"status": "FAILED", "error_code": BINDING_REJECTED}
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{self._api_url}/repos/{evidence.resource}/check-runs"
        try:
            response = self._transport.request("POST", url, headers, payload)
        except GitHubAPIError as exc:
            code = server_response(exc.status_code) if exc.status_code >= 500 else http_status(exc.status_code)
            return {"status": "UNKNOWN" if exc.status_code >= 500 else "FAILED", "error_code": code}
        except AmbiguousTransportError:
            return {"status": "UNKNOWN", "error_code": TRANSPORT_AMBIGUOUS}
        except Exception as exc:  # noqa: BLE001 - the evidence is already signed; report, do not raise
            return {"status": "UNKNOWN", "error_code": f"{TRANSPORT_AMBIGUOUS}: {type(exc).__name__}"}
        return _sanitize_check_run(response, payload["conclusion"])


def _sanitize_check_run(response: Any, conclusion: str) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "PUBLISHED", "conclusion": conclusion}
    if not isinstance(response, dict):
        return {"status": "UNKNOWN", "error_code": TRANSPORT_AMBIGUOUS}
    if type(response.get("id")) is int:
        result["id"] = response["id"]
    html_url = response.get("html_url")
    if isinstance(html_url, str) and html_url.startswith("https://"):
        result["html_url"] = html_url
    return result


def _placeholder_evidence() -> Evidence:
    return Evidence(
        action_id="0" * 64,
        action_type="github.push_branch",
        resource="owner/repository",
        head_sha="0" * 40,
        outcome_status="SUCCEEDED",
        verification={"status": "VERIFIED"},
        bundle_json="{}",
        gate_public_key="0" * 64,
    )


__all__ = [
    "CHECK_RUN_SCOPE",
    "DEFAULT_CHECK_NAME",
    "CheckRunPublisher",
    "Evidence",
    "build_check_run",
    "build_output",
    "conclusion_for",
    "details_url_for",
    "head_sha_for",
]


def evidence_from_documents(
    *,
    action_type: str,
    resource: str,
    outcome: dict[str, Any],
    verification: dict[str, Any] | str,
    bundle_json: str,
    gate_public_key: str,
    executor_public_key: str | None,
) -> Evidence | None:
    """Build the check's evidence from the signed outcome and the verifier result.

    Returns None when the action produced no commit to attach a check to.
    """
    if isinstance(verification, str):
        verification = json.loads(verification)
    head_sha = head_sha_for(action_type, outcome.get("output"))
    if head_sha is None:
        return None
    return Evidence(
        action_id=str(outcome.get("action_id", "")),
        action_type=action_type,
        resource=resource,
        head_sha=head_sha,
        outcome_status=str(outcome.get("status", "")),
        verification=verification,
        bundle_json=bundle_json,
        gate_public_key=gate_public_key,
        executor_public_key=executor_public_key,
    )
