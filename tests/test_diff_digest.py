"""`diff_sha256`: the local client's digest of the change, re-derived by the executor."""

from __future__ import annotations

import hashlib
import subprocess

import pytest

from tempus_github_app.git_push import DIFF_DIGEST_ARGS, parse_push_request
from tempus_github_app.transport import GitHubExecutorError
from tests.test_push_branch import GIT_ENV, Repos, make_adapter, push_input, push_intent


def diff_digest(repos: Repos, base: str, tip: str) -> str:
    """What `tempus guard intent` computes on the agent's machine: raw bytes, no stripping."""
    completed = subprocess.run(
        ["git", *DIFF_DIGEST_ARGS, base, tip],
        cwd=repos.work, env=GIT_ENV, capture_output=True, check=True,
    )
    return hashlib.sha256(completed.stdout).hexdigest()


def test_digest_command_is_plumbing_and_nul_separated():
    assert DIFF_DIGEST_ARGS[0] == "diff-tree"
    assert "-z" in DIFF_DIGEST_ARGS and "--raw" in DIFF_DIGEST_ARGS and "--no-renames" in DIFF_DIGEST_ARGS


def test_matching_diff_digest_is_accepted_and_recorded(tmp_path):
    repos = Repos(tmp_path)
    repos.write("src/app.py", "print('fix')\n")
    tip = repos.commit("fix")
    bundle, digest = repos.bundle(repos.base)
    expected = diff_digest(repos, repos.base, tip)
    adapter, _ = make_adapter(repos)

    result = adapter.execute_action(
        push_intent(push_input(repos, digest, ["src/app.py"], diff_sha256=expected)),
        artifacts={"bundle": bundle},
    )

    assert result.status == "SUCCEEDED", result.payload
    assert result.payload["diff_sha256"] == expected
    assert result.payload["sha"] == tip
    assert repos.remote_ref("agent/fix-123") == tip


def test_a_digest_of_a_different_change_is_rejected_before_the_push(tmp_path):
    repos = Repos(tmp_path)
    repos.write("src/app.py", "print('fix')\n")
    repos.commit("fix")
    bundle, digest = repos.bundle(repos.base)
    adapter, credentials = make_adapter(repos)

    result = adapter.execute_action(
        push_intent(push_input(repos, digest, ["src/app.py"], diff_sha256="0" * 64)),
        artifacts={"bundle": bundle},
    )

    assert result.status == "FAILED"
    assert result.payload == {"error_code": "TEMPUS_GITHUB_BINDING_REJECTED"}
    assert repos.remote_ref("agent/fix-123") is None
    credentials.token_for.assert_called_once()


def test_the_digest_changes_with_the_content_not_only_the_paths(tmp_path):
    repos = Repos(tmp_path)
    repos.write("src/app.py", "print('one')\n")
    first = repos.commit("one")
    repos.write("src/app.py", "print('two')\n")
    second = repos.commit("two")
    assert diff_digest(repos, repos.base, first) != diff_digest(repos, repos.base, second)
    # Same tree reached by a different history has the same diff digest: it is about the change.
    assert diff_digest(repos, repos.base, second) == diff_digest(repos, repos.base, second)


def test_intents_without_the_digest_keep_working(tmp_path):
    repos = Repos(tmp_path)
    repos.write("a.txt", "1\n")
    repos.commit("a")
    bundle, digest = repos.bundle(repos.base)
    adapter, _ = make_adapter(repos)
    result = adapter.execute_action(
        push_intent(push_input(repos, digest, ["a.txt"])), artifacts={"bundle": bundle}
    )
    assert result.status == "SUCCEEDED"
    assert "diff_sha256" not in result.payload


@pytest.mark.parametrize("value", ["", "abc", "A" * 64, 1, ["x"]])
def test_malformed_digests_are_refused_at_parse_time(value):
    inputs = {
        "branch": "agent/x", "base_sha": "a" * 40, "tip_sha": "b" * 40, "new_branch": True,
        "bundle_sha256": "c" * 64, "paths": ["a"], "diff_sha256": value,
    }
    with pytest.raises(GitHubExecutorError):
        parse_push_request(inputs, "agent/")
    inputs["diff_sha256"] = "d" * 64
    assert parse_push_request(inputs, "agent/").diff_sha256 == "d" * 64
    del inputs["diff_sha256"]
    assert parse_push_request(inputs, "agent/").diff_sha256 is None
