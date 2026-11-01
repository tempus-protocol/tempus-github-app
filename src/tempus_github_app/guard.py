"""Tempus Guard for GitHub: one permit in, one verified check run out.

The flow closes the whole chain the protocol defines, in order:

1. the executor consumes the permit and performs exactly its GitHub action, signing
   the outcome (``GitHubAppExecutorAdapter.execute``);
2. the gate consumes the outcome and signs the execution receipt
   (``TempusDDB.commit_outcome_signed``), so the action's trace is complete;
3. the gate exports the evidence bundle for the action (``export_bundle``);
4. the bundle is verified against the pinned gate key, exactly as a third party would;
5. the "Verified by Tempus" check run is published on the commit the action produced,
   carrying the bundle and the link to the public verifier.

Steps 1 to 3 produce signed evidence and fail closed. Steps 4 and 5 only report; a
check that could not be published never alters what the gate recorded.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tempus_ddb import verify_bundle as _verify_bundle

from .check_run import CheckRunPublisher, evidence_from_documents
from .strict_json import loads as loads_strict
from .transport import GitHubExecutorError

Verifier = Callable[[str, str, list[str] | None], str]


class GuardError(GitHubExecutorError):
    """A step after execution failed; ``partial`` holds what was recorded before it."""

    def __init__(self, message: str, partial: dict[str, Any]):
        super().__init__(message)
        self.partial = partial


def read_public_key(keyfile: str | Path) -> str:
    """The public key of a Tempus key file; the private part is never read into a string."""
    try:
        data = loads_strict(Path(keyfile).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GitHubExecutorError("cannot read the key file") from exc
    key = data.get("public_key") if isinstance(data, dict) else None
    if not isinstance(key, str) or len(key) != 64:
        raise GitHubExecutorError("key file has no public_key")
    return key.lower()


class GuardFlow:
    """Execute a permit and publish its evidence as a check run."""

    def __init__(
        self,
        executor: Any,
        gate: Any,
        *,
        gate_public_key: str,
        executor_public_key: str | None = None,
        publisher: CheckRunPublisher | None = None,
        verifier: Verifier = _verify_bundle,
        max_stream_events: int | None = None,
        create_checkpoint: bool = False,
    ):
        if not isinstance(gate_public_key, str) or len(gate_public_key) != 64:
            raise GitHubExecutorError("gate_public_key must be the gate's 64-hex public key")
        self._executor = executor
        self._gate = gate
        self._gate_public_key = gate_public_key.lower()
        self._executor_public_key = executor_public_key.lower() if executor_public_key else None
        self._publisher = publisher
        self._verifier = verifier
        self._max_stream_events = max_stream_events
        self._create_checkpoint = create_checkpoint

    def run(
        self,
        permit_json: str,
        *,
        bundle_path: str | Path | None = None,
        bundle_out: str | Path | None = None,
    ) -> dict[str, Any]:
        """Run the five steps. Raises ``UnknownExecutionError`` from the executor unchanged."""
        permit = loads_strict(permit_json)
        if not isinstance(permit, dict):
            raise GitHubExecutorError("permit must be a JSON object")
        authorization = permit.get("authorization")
        intent = permit.get("intent")
        if not isinstance(authorization, dict) or not isinstance(intent, dict):
            raise GitHubExecutorError("permit must carry authorization and intent objects")
        authorization_id = authorization.get("authorization_id")
        action_id = authorization.get("action_id")
        tenant_id = authorization.get("tenant_id")
        action_type = intent.get("action_type")
        resource = intent.get("resource")
        for name, value in (
            ("authorization_id", authorization_id),
            ("action_id", action_id),
            ("tenant_id", tenant_id),
            ("action_type", action_type),
            ("resource", resource),
        ):
            if not isinstance(value, str) or not value:
                raise GitHubExecutorError(f"permit {name} must be a non-empty string")

        # 1. Execute: the only external effect. Ambiguity propagates as UnknownExecutionError.
        outcome_json = self._executor.execute(permit_json, bundle_path=bundle_path)
        outcome = json.loads(outcome_json)
        result: dict[str, Any] = {
            "action_id": action_id,
            "authorization_id": authorization_id,
            "action_type": action_type,
            "resource": resource,
            "outcome": outcome,
        }

        # 2. Close the trace in the gate.
        try:
            receipt = json.loads(self._gate.commit_outcome_signed(authorization_id, outcome_json))
        except Exception as exc:
            raise GuardError(f"gate refused the outcome: {exc}", result) from exc
        result["receipt"] = receipt

        # 3. Export the evidence.
        try:
            if self._create_checkpoint:
                self._gate.create_checkpoint(tenant_id)
            bundle_json = self._gate.export_bundle(action_id, self._max_stream_events)
        except Exception as exc:
            raise GuardError(f"bundle export failed: {exc}", result) from exc
        if bundle_out is not None:
            try:
                Path(bundle_out).write_text(bundle_json, encoding="utf-8")
                result["bundle_path"] = str(bundle_out)
            except OSError as exc:
                raise GuardError(f"cannot write the bundle: {exc}", result) from exc

        # 4. Verify as a third party would: pinned gate key, optional pinned executor key.
        executors = [self._executor_public_key] if self._executor_public_key else None
        verification = json.loads(self._verifier(bundle_json, self._gate_public_key, executors))
        result["verification"] = {
            key: verification.get(key)
            for key in ("status", "kind", "phase", "decision", "execution_status", "errors", "trust")
        }
        result["bundle_digest"] = verification.get("bundle_digest")
        result["verify_url"] = verification.get("verify_url")

        # 5. Publish the check run, when there is a commit to attach it to.
        if self._publisher is None:
            result["check_run"] = {"status": "SKIPPED", "reason": "check run publishing is off"}
            return result
        evidence = evidence_from_documents(
            action_type=action_type,
            resource=resource,
            outcome=outcome,
            verification=verification,
            bundle_json=bundle_json,
            gate_public_key=self._gate_public_key,
            executor_public_key=self._executor_public_key,
        )
        if evidence is None:
            result["check_run"] = {
                "status": "SKIPPED",
                "reason": f"{action_type} produced no commit to attach a check run to",
            }
            return result
        result["check_run"] = self._publisher.publish(evidence)
        return result


__all__ = ["GuardError", "GuardFlow", "read_public_key"]
