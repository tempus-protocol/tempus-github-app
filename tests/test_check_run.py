"""The "Verified by Tempus" check run payload and its publisher."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import pytest
from tempus_ddb.executor_runtime import AmbiguousTransportError

from tempus_github_app.check_run import (
    CHECK_RUN_SCOPE,
    DEFAULT_CHECK_NAME,
    MAX_OUTPUT_TEXT,
    CheckRunPublisher,
    Evidence,
    build_check_run,
    conclusion_for,
    details_url_for,
    evidence_from_documents,
    head_sha_for,
)
from tempus_github_app.transport import GitHubAPIError, GitHubExecutorError

ACTION = "f" * 64
GATE = "a" * 64
EXECUTOR = "b" * 64
HEAD = "c" * 40
DIGEST = "d" * 64
VERIFY_URL = f"https://julio-patron.github.io/tempus-ddb/verify.html#bundle={DIGEST}"
BUNDLE = json.dumps({"schema_version": "tempus.evidence-bundle.v1", "action_id": ACTION, "verify_url": VERIFY_URL})


def verification(status="VERIFIED", **extra) -> dict[str, Any]:
    base = {
        "status": status,
        "kind": "bundle",
        "action_id": ACTION,
        "phase": "COMPLETED",
        "errors": [] if status == "VERIFIED" else ["AUTHORIZATION_ID_MISMATCH"],
        "trust": {
            "mode": "PINNED_GATE_KEY",
            "independent": True,
            "gate_public_key": GATE,
            "executor_keys_pinned": 1,
            "not_checked": ["SINGLE_USE_CONSUMPTION (only the ledger shows it)", "COMPLETENESS"],
        },
        "bundle_digest": DIGEST,
        "verify_url": VERIFY_URL,
    }
    base.update(extra)
    return base


def evidence(**overrides) -> Evidence:
    fields = {
        "action_id": ACTION,
        "action_type": "github.push_branch",
        "resource": "acme/widget",
        "head_sha": HEAD,
        "outcome_status": "SUCCEEDED",
        "verification": verification(),
        "bundle_json": BUNDLE,
        "gate_public_key": GATE,
        "executor_public_key": EXECUTOR,
    }
    fields.update(overrides)
    return Evidence(**fields)


class RecordingTransport:
    def __init__(self, response: Any = None, failure: Exception | None = None):
        self.response = response if response is not None else {"id": 7, "html_url": "https://github.com/acme/widget/runs/7"}
        self.failure = failure
        self.calls: list[tuple[str, str, dict[str, str], dict[str, Any]]] = []

    def request(self, method, url, headers, payload, *, context=None):
        self.calls.append((method, url, headers, payload))
        if self.failure is not None:
            raise self.failure
        return self.response


def credentials(token: str = "installation-checks-token") -> Mock:
    creds = Mock()
    creds.token_for.return_value = token
    return creds


# --- payload ---------------------------------------------------------------------------------


def test_payload_names_the_commit_the_action_and_the_verifier():
    payload = build_check_run(evidence())
    assert payload["name"] == DEFAULT_CHECK_NAME
    assert payload["head_sha"] == HEAD
    assert payload["external_id"] == ACTION
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "success"
    assert payload["details_url"] == VERIFY_URL + "&gate=" + GATE
    output = payload["output"]
    assert output["title"] == "VERIFIED · SUCCEEDED · github.push_branch"
    assert ACTION[:16] in output["summary"]
    assert ACTION in output["text"]
    assert GATE in output["text"]
    assert EXECUTOR in output["text"]
    assert DIGEST in output["text"]
    assert VERIFY_URL in output["text"]
    assert "SINGLE_USE_CONSUMPTION" in output["text"]
    assert "COMPLETENESS" in output["text"]
    assert "```json\n" + BUNDLE + "\n```" in output["text"]
    assert len(output["text"]) <= MAX_OUTPUT_TEXT


@pytest.mark.parametrize("verified,outcome,expected", [
    ("VERIFIED", "SUCCEEDED", "success"),
    ("VERIFIED", "FAILED", "failure"),
    ("INVALID", "SUCCEEDED", "failure"),
    ("INVALID", "FAILED", "failure"),
])
def test_conclusion_is_green_only_for_verified_success(verified, outcome, expected):
    assert conclusion_for(verified, outcome) == expected
    payload = build_check_run(evidence(verification=verification(verified), outcome_status=outcome))
    assert payload["conclusion"] == expected
    assert payload["output"]["title"].startswith(f"{verified} · {outcome}")


def test_invalid_evidence_lists_the_verifier_errors():
    text = build_check_run(evidence(verification=verification("INVALID")))["output"]["text"]
    assert "AUTHORIZATION_ID_MISMATCH" in text
    assert "does NOT verify" in build_check_run(evidence(verification=verification("INVALID")))["output"]["summary"]


def test_an_oversized_bundle_is_not_embedded_but_named_by_digest():
    big = json.dumps({"schema_version": "tempus.evidence-bundle.v1", "pad": "x" * MAX_OUTPUT_TEXT})
    text = build_check_run(evidence(bundle_json=big))["output"]["text"]
    assert "x" * 1000 not in text
    assert "exceeds GitHub's check output limit" in text
    assert DIGEST in text
    assert len(text) <= MAX_OUTPUT_TEXT


def test_details_url_keeps_a_fragment_that_already_names_the_gate():
    url = VERIFY_URL + "&gate=" + GATE
    assert details_url_for(evidence(verification=verification(verify_url=url))) == url
    assert details_url_for(evidence(verification=verification(verify_url=None))) is None
    assert "details_url" not in build_check_run(evidence(verification=verification(verify_url=None)))


def test_not_independent_verification_is_said_in_the_summary():
    trust = {"mode": "DATABASE_SELF_ATTESTED", "independent": False, "not_checked": []}
    summary = build_check_run(evidence(verification=verification(trust=trust)))["output"]["summary"]
    assert "not independent" in summary


@pytest.mark.parametrize("action_type,output,expected", [
    ("github.push_branch", {"sha": HEAD}, HEAD),
    ("github.merge_pull_request", {"sha": HEAD.upper()}, HEAD),
    ("github.create_pull_request", {"head_sha": HEAD}, HEAD),
    ("github.create_pull_request", {"number": 2}, None),
    ("github.create_issue", {"number": 1}, None),
    ("github.push_branch", {"sha": "not-a-sha"}, None),
    ("github.push_branch", None, None),
])
def test_head_sha_comes_only_from_actions_that_produce_a_commit(action_type, output, expected):
    assert head_sha_for(action_type, output) == expected


def test_evidence_from_documents_returns_none_without_a_commit():
    outcome = {"action_id": ACTION, "status": "SUCCEEDED", "output": {"number": 1}}
    assert evidence_from_documents(
        action_type="github.create_issue", resource="acme/widget", outcome=outcome,
        verification=verification(), bundle_json=BUNDLE, gate_public_key=GATE, executor_public_key=None,
    ) is None
    built = evidence_from_documents(
        action_type="github.push_branch", resource="acme/widget",
        outcome={"action_id": ACTION, "status": "SUCCEEDED", "output": {"sha": HEAD}},
        verification=json.dumps(verification()), bundle_json=BUNDLE, gate_public_key=GATE,
        executor_public_key=EXECUTOR,
    )
    assert built is not None and built.head_sha == HEAD and built.verification["status"] == "VERIFIED"


@pytest.mark.parametrize("field,value", [
    ("action_id", "short"),
    ("resource", "acme/widget/extra"),
    ("head_sha", HEAD.upper()),
    ("outcome_status", "UNKNOWN"),
    ("verification", "VERIFIED"),
    ("gate_public_key", "A" * 64),
    ("executor_public_key", "zz"),
])
def test_evidence_rejects_malformed_fields(field, value):
    with pytest.raises(GitHubExecutorError):
        evidence(**{field: value})


@pytest.mark.parametrize("name", ["", "   ", "x" * 256])
def test_check_name_must_be_short_and_non_empty(name):
    with pytest.raises(GitHubExecutorError):
        build_check_run(evidence(), name)
    with pytest.raises(GitHubExecutorError):
        CheckRunPublisher(credentials(), transport=RecordingTransport(), name=name)


# --- publisher -------------------------------------------------------------------------------


def test_publisher_posts_one_check_run_with_a_checks_token():
    transport = RecordingTransport()
    creds = credentials()
    publisher = CheckRunPublisher(creds, transport=transport, name="Verified by Tempus (staging)")

    result = publisher.publish(evidence())

    assert result == {"status": "PUBLISHED", "conclusion": "success", "id": 7,
                      "html_url": "https://github.com/acme/widget/runs/7"}
    creds.token_for.assert_called_once_with("acme/widget", CHECK_RUN_SCOPE)
    assert len(transport.calls) == 1
    method, url, headers, payload = transport.calls[0]
    assert (method, url) == ("POST", "https://api.github.com/repos/acme/widget/check-runs")
    assert headers["Authorization"] == "Bearer installation-checks-token"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert payload["name"] == "Verified by Tempus (staging)"
    assert payload["head_sha"] == HEAD and payload["external_id"] == ACTION


def test_publisher_reports_a_credential_failure_without_calling_github():
    transport = RecordingTransport()
    creds = credentials()
    creds.token_for.side_effect = RuntimeError("private-secret-value")
    result = CheckRunPublisher(creds, transport=transport).publish(evidence())
    assert result == {"status": "FAILED", "error_code": "TEMPUS_GITHUB_BINDING_REJECTED"}
    assert transport.calls == []


@pytest.mark.parametrize("failure,expected", [
    (GitHubAPIError(422, "Validation Failed"), {"status": "FAILED", "error_code": "GITHUB_HTTP_422"}),
    (GitHubAPIError(403, "Forbidden"), {"status": "FAILED", "error_code": "GITHUB_HTTP_403"}),
    (GitHubAPIError(502, "Bad Gateway"), {"status": "UNKNOWN", "error_code": "GITHUB_SERVER_RESPONSE_502"}),
    (AmbiguousTransportError("GITHUB_TRANSPORT_AMBIGUOUS"), {"status": "UNKNOWN", "error_code": "GITHUB_TRANSPORT_AMBIGUOUS"}),
    (ConnectionResetError("reset by peer with token ghs_secret"),
     {"status": "UNKNOWN", "error_code": "GITHUB_TRANSPORT_AMBIGUOUS: ConnectionResetError"}),
])
def test_publisher_reports_github_errors_instead_of_raising(failure, expected):
    result = CheckRunPublisher(credentials(), transport=RecordingTransport(failure=failure)).publish(evidence())
    assert result == expected
    assert "ghs_secret" not in json.dumps(result)


def test_publisher_sanitizes_the_response():
    transport = RecordingTransport(response={"id": "7", "html_url": "http://evil.example/run", "token": "leak"})
    result = CheckRunPublisher(credentials(), transport=transport).publish(evidence())
    assert result == {"status": "PUBLISHED", "conclusion": "success"}
    assert CheckRunPublisher(credentials(), transport=RecordingTransport(response=[1, 2])).publish(evidence()) == {
        "status": "UNKNOWN", "error_code": "GITHUB_TRANSPORT_AMBIGUOUS",
    }


def test_publisher_requires_an_https_api_url():
    with pytest.raises(GitHubExecutorError):
        CheckRunPublisher(credentials(), api_url="http://api.github.com", transport=RecordingTransport())
