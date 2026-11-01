"""Tempus Guard end to end: permit in, signed evidence out, check run on the commit."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from tempus_ddb import TempusDDB, verify_bundle
from tempus_ddb.executor_runtime import UnknownExecutionError

from tempus_github_app.check_run import CheckRunPublisher
from tempus_github_app.credentials import GitHubAppCredentials
from tempus_github_app.executor import GitHubAppExecutorAdapter
from tempus_github_app.git_push import BundlePublisher, GitRunner
from tempus_github_app.guard import GuardError, GuardFlow, read_public_key
from tempus_github_app.transport import GitHubExecutorError
from tests.conftest import MockAppTransport, setup_gate_and_agents
from tests.test_push_branch import Repos, push_input, push_intent

PR_HEAD = "9" * 40


class GuardTransport(MockAppTransport):
    """GitHub stand-in: tokens, a PR whose head is known, and the Checks API."""

    def __init__(self):
        super().__init__()
        self.check_runs: list[dict[str, Any]] = []
        self.check_response: Any = {"id": 7, "html_url": "https://github.com/acme/widget/runs/7"}
        # Raised for the GitHub write only (tokens still mint): an ambiguous execution.
        self.write_failure: Exception | None = None

    def request(self, method, url, headers, payload, *, context=None):
        if self.write_failure is not None and "/access_tokens" not in url:
            self.calls.append((method, url, headers, payload))
            raise self.write_failure
        if url.endswith("/check-runs"):
            self.calls.append((method, url, headers, payload))
            self.check_runs.append(payload)
            if isinstance(self.check_response, Exception):
                raise self.check_response
            return self.check_response
        response = super().request(method, url, headers, payload, context=context)
        if "/pulls" in url:
            response = {**response, "head": {"sha": PR_HEAD, "ref": "patch-1"}}
        return response


def make_flow(tmp_path: Path, app_key, *, publish: bool = True, repos: Repos | None = None, **flow_kwargs):
    env = setup_gate_and_agents(tmp_path, tenant_id="tenant-guard")
    transport = GuardTransport()
    credentials = GitHubAppCredentials(
        "Iv1.guard", str(app_key[0]), 42, "acme/widget", transport=transport,
    )
    executor_kwargs: dict[str, Any] = {}
    if repos is not None:
        executor_kwargs = {
            "publisher": BundlePublisher(GitRunner(allowed_protocols=("file",)), work_dir=tmp_path),
            "remote_url_for": lambda _: repos.remote.as_uri(),
        }
    executor = GitHubAppExecutorAdapter(
        executor_db=env["exec_db"], executor_keyfile=env["exec_keyfile"],
        trusted_gate_id=env["gate_id"], trusted_tenant_id=env["tenant_id"],
        credentials=credentials, transport=transport, gate_db=env["gate_db"], **executor_kwargs,
    )
    publisher = CheckRunPublisher(credentials, transport=transport) if publish else None
    flow = GuardFlow(
        executor, env["gate"],
        gate_public_key=env["gate_id"], executor_public_key=env["exec_id"], publisher=publisher,
        **flow_kwargs,
    )
    return env, transport, flow


def intent_for(env, action_type: str, inputs: dict[str, Any], key: str) -> dict[str, Any]:
    return {
        "schema_version": "tempus.action-intent.v1",
        "tenant_id": env["tenant_id"],
        "agent_id": env["agent_id"],
        "idempotency_key": key,
        "action_type": action_type,
        "resource": "acme/widget",
        "requested_at": time.time_ns() // 1000,
        "input": inputs,
    }


def permit_for(env, intent: dict[str, Any]) -> str:
    return env["gate"].request_action(json.dumps(intent), env["agent_keyfile"], 60)


def embedded_bundle(check_payload: dict[str, Any]) -> str:
    match = re.search(r"```json\n(.*?)\n```", check_payload["output"]["text"], re.DOTALL)
    assert match, "the check run must embed the bundle"
    return match.group(1)


def test_pull_request_gets_a_verified_check_on_its_head_commit(tmp_path, app_key):
    env, transport, flow = make_flow(tmp_path, app_key)
    intent = intent_for(env, "github.create_pull_request",
                        {"title": "fix: leak", "head": "patch-1", "base": "main"}, "guard-pr-1")
    permit = permit_for(env, intent)
    action_id = json.loads(permit)["authorization"]["action_id"]

    result = flow.run(permit, bundle_out=tmp_path / "evidence.json")

    # 1-2. The outcome is signed by the executor and the receipt by the gate.
    assert result["action_id"] == action_id
    assert result["outcome"]["status"] == "SUCCEEDED"
    assert result["outcome"]["output"]["head_sha"] == PR_HEAD
    assert result["receipt"]["receipt"]["status"] == "SUCCEEDED"
    assert result["receipt"]["receipt"]["verify_url"].endswith("#action=" + action_id)
    # 3-4. The bundle is exported and verifies with the pinned keys, as a stranger would check it.
    bundle = (tmp_path / "evidence.json").read_text(encoding="utf-8")
    assert result["verification"]["status"] == "VERIFIED"
    assert result["verification"]["trust"]["independent"] is True
    assert result["verification"]["trust"]["executor_keys_pinned"] == 1
    assert result["verify_url"].endswith("#bundle=" + result["bundle_digest"])
    assert json.loads(bundle)["verify_url"] == result["verify_url"]
    # 5. One check run, on the PR head, green, linking to the verifier, carrying the bundle.
    assert result["check_run"] == {"status": "PUBLISHED", "conclusion": "success", "id": 7,
                                   "html_url": "https://github.com/acme/widget/runs/7"}
    assert len(transport.check_runs) == 1
    check = transport.check_runs[0]
    assert check["name"] == "Verified by Tempus"
    assert check["head_sha"] == PR_HEAD
    assert check["external_id"] == action_id
    assert check["conclusion"] == "success"
    assert check["details_url"] == result["verify_url"] + "&gate=" + env["gate_id"]
    assert env["gate_id"] in check["output"]["text"]
    # The bundle inside the check is the bundle, and it verifies on its own.
    shown = embedded_bundle(check)
    assert json.loads(shown) == json.loads(bundle)
    independent = json.loads(verify_bundle(shown, env["gate_id"], [env["exec_id"]]))
    assert independent["status"] == "VERIFIED"
    assert independent["bundle_digest"] == result["bundle_digest"]
    # The checks token was minted with the checks scope only.
    token_calls = [call for call in transport.calls if call[1].endswith("/access_tokens")]
    assert {"checks": "write"} in [call[3]["permissions"] for call in token_calls]
    # No private key material anywhere in what left the process.
    assert "private_key" not in json.dumps(result) + json.dumps(transport.check_runs)


def test_a_push_check_binds_the_diff_digest_and_the_pushed_tip(tmp_path, app_key):
    repos = Repos(tmp_path / "git")
    repos.write("src/app.py", "print('fix')\n")
    tip = repos.commit("fix")
    bundle, digest = repos.bundle(repos.base)
    from tests.test_diff_digest import diff_digest

    env, transport, flow = make_flow(tmp_path, app_key, repos=repos)
    inputs = push_input(repos, digest, ["src/app.py"], diff_sha256=diff_digest(repos, repos.base, tip))
    permit = permit_for(env, {**push_intent(inputs), **intent_for(env, "github.push_branch", inputs, "guard-push")})

    result = flow.run(permit, bundle_path=bundle)

    assert result["outcome"]["status"] == "SUCCEEDED"
    assert result["outcome"]["output"]["sha"] == tip
    assert result["outcome"]["output"]["diff_sha256"] == inputs["diff_sha256"]
    assert result["verification"]["status"] == "VERIFIED"
    assert result["check_run"]["status"] == "PUBLISHED"
    assert transport.check_runs[0]["head_sha"] == tip
    assert repos.remote_ref("agent/fix-123") == tip


def test_an_action_without_a_commit_skips_the_check_but_keeps_the_evidence(tmp_path, app_key):
    env, transport, flow = make_flow(tmp_path, app_key)
    permit = permit_for(env, intent_for(env, "github.create_issue", {"title": "report"}, "guard-issue"))
    result = flow.run(permit)
    assert result["outcome"]["status"] == "SUCCEEDED"
    assert result["receipt"]["receipt"]["status"] == "SUCCEEDED"
    assert result["verification"]["status"] == "VERIFIED"
    assert result["check_run"]["status"] == "SKIPPED"
    assert "no commit" in result["check_run"]["reason"]
    assert transport.check_runs == []


def test_publishing_can_be_turned_off(tmp_path, app_key):
    env, transport, flow = make_flow(tmp_path, app_key, publish=False)
    permit = permit_for(env, intent_for(env, "github.create_pull_request",
                                        {"title": "t", "head": "h", "base": "main"}, "guard-off"))
    result = flow.run(permit)
    assert result["verification"]["status"] == "VERIFIED"
    assert result["check_run"] == {"status": "SKIPPED", "reason": "check run publishing is off"}
    assert transport.check_runs == []


def test_a_failed_check_publish_does_not_touch_the_evidence(tmp_path, app_key):
    env, transport, flow = make_flow(tmp_path, app_key)
    transport.check_response = ConnectionResetError("github hung up")
    permit = permit_for(env, intent_for(env, "github.create_pull_request",
                                        {"title": "t", "head": "h", "base": "main"}, "guard-check-fails"))
    result = flow.run(permit)
    action_id = result["action_id"]
    assert result["check_run"]["status"] == "UNKNOWN"
    # The gate has the complete trace regardless.
    trace = json.loads(env["gate"].verify_trace(action_id, env["gate_id"]))
    assert trace["status"] == "VERIFIED" and trace["phase"] == "COMPLETED"


def test_a_failed_action_is_still_evidence_and_the_check_is_red(tmp_path, app_key):
    env, transport, flow = make_flow(tmp_path, app_key)
    transport.failure = None
    # A merge GitHub refuses: the executor records FAILED, the gate signs that, the check is red.
    from tempus_github_app.transport import GitHubAPIError

    original = transport.request

    def refuse_merge(method, url, headers, payload, *, context=None):
        if url.endswith("/merge"):
            raise GitHubAPIError(405, "not mergeable")
        return original(method, url, headers, payload, context=context)

    transport.request = refuse_merge
    permit = permit_for(env, intent_for(env, "github.merge_pull_request",
                                        {"pull_number": 3, "sha": "e" * 40, "merge_method": "squash"},
                                        "guard-merge-refused"))
    result = flow.run(permit)
    assert result["outcome"]["status"] == "FAILED"
    assert result["outcome"]["output"]["error_code"] == "GITHUB_HTTP_405"
    assert result["receipt"]["receipt"]["status"] == "FAILED"
    assert result["verification"]["status"] == "VERIFIED"
    # A refused merge produces no commit, so there is nothing to attach the check to.
    assert result["check_run"]["status"] == "SKIPPED"


def test_a_gate_that_refuses_the_outcome_raises_with_the_signed_partial(tmp_path, app_key):
    env, transport, flow = make_flow(tmp_path, app_key)

    class RefusingGate:
        def commit_outcome_signed(self, authorization_id, outcome):
            raise RuntimeError("TEMPUS_PERMIT_ALREADY_CONSUMED")

    flow._gate = RefusingGate()
    permit = permit_for(env, intent_for(env, "github.create_issue", {"title": "x"}, "guard-refused"))
    with pytest.raises(GuardError) as info:
        flow.run(permit)
    assert "TEMPUS_PERMIT_ALREADY_CONSUMED" in str(info.value)
    assert info.value.partial["outcome"]["status"] == "SUCCEEDED"
    assert transport.check_runs == []


def test_ambiguous_execution_propagates_and_publishes_nothing(tmp_path, app_key):
    env, transport, flow = make_flow(tmp_path, app_key)
    transport.write_failure = ConnectionResetError("mid-flight")
    permit = permit_for(env, intent_for(env, "github.create_issue", {"title": "x"}, "guard-unknown"))
    with pytest.raises(UnknownExecutionError):
        flow.run(permit)
    assert transport.check_runs == []


def test_the_permit_must_be_a_well_formed_object(tmp_path, app_key):
    _, _, flow = make_flow(tmp_path, app_key)
    with pytest.raises(GitHubExecutorError):
        flow.run("[]")
    with pytest.raises(GitHubExecutorError):
        flow.run(json.dumps({"authorization": {"authorization_id": "x"}, "intent": {}}))
    with pytest.raises(ValueError):
        flow.run('{"authorization": 1, "authorization": 2}')


def test_flow_requires_the_gate_public_key():
    with pytest.raises(GitHubExecutorError):
        GuardFlow(object(), object(), gate_public_key="short")


def test_read_public_key_reads_only_the_public_part(tmp_path):
    keyfile = tmp_path / "k.json"
    keyfile.write_text(json.dumps({"public_key": "A" * 64, "private_key": "secret"}), encoding="utf-8")
    assert read_public_key(keyfile) == "a" * 64
    keyfile.write_text(json.dumps({"private_key": "secret"}), encoding="utf-8")
    with pytest.raises(GitHubExecutorError):
        read_public_key(keyfile)
    with pytest.raises(GitHubExecutorError):
        read_public_key(tmp_path / "missing.json")


def test_cli_runs_the_guard_flow_with_gate_keyfile(tmp_path, app_key, monkeypatch, capsys):
    from tempus_github_app.cli import main

    env = setup_gate_and_agents(tmp_path, tenant_id="tenant-guard-cli")
    gate: TempusDDB = env["gate"]
    transport = GuardTransport()
    for cls in (GitHubAppCredentials, GitHubAppExecutorAdapter, CheckRunPublisher):
        original = cls.__init__

        def patched(self, *args, _original=original, **kwargs):
            kwargs["transport"] = transport
            _original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", patched)

    permit = gate.request_action(json.dumps(intent_for(
        env, "github.create_pull_request", {"title": "cli", "head": "p", "base": "main"}, "guard-cli",
    )), env["agent_keyfile"], 60)
    permit_file = tmp_path / "permit.json"
    permit_file.write_text(permit, encoding="utf-8")

    main([
        "--permit", str(permit_file),
        "--executor-db", env["exec_db"], "--executor-keyfile", env["exec_keyfile"],
        "--gate-id", env["gate_id"], "--tenant-id", env["tenant_id"],
        "--gate-db", env["gate_db"], "--gate-keyfile", env["gate_keyfile"],
        "--app-client-id", "Iv1.cli", "--app-private-key", str(app_key[0]),
        "--installation-id", "42", "--repository", "acme/widget",
        "--check-name", "Verified by Tempus (test)",
        "--bundle-out", str(tmp_path / "out.json"),
    ])
    result = json.loads(capsys.readouterr().out)
    assert result["check_run"]["status"] == "PUBLISHED"
    assert result["verification"]["status"] == "VERIFIED"
    assert transport.check_runs[0]["name"] == "Verified by Tempus (test)"
    assert transport.check_runs[0]["head_sha"] == PR_HEAD
    assert (tmp_path / "out.json").exists()


def test_cli_gate_keyfile_requires_gate_db(tmp_path, app_key, capsys):
    from tempus_github_app.cli import main

    with pytest.raises(SystemExit) as info:
        main([
            "--permit", "p.json", "--executor-db", "e.db", "--executor-keyfile", "e.json",
            "--gate-id", "g", "--tenant-id", "t", "--app-client-id", "Iv1.x",
            "--app-private-key", str(app_key[0]), "--installation-id", "42",
            "--repository", "acme/widget", "--gate-keyfile", "gate.json",
        ])
    assert info.value.code == 1
    assert "--gate-db" in capsys.readouterr().err
