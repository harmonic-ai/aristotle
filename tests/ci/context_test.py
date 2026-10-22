"""Tests for aristotlelib.ci.context."""

import os
from pathlib import Path

import pytest

from aristotlelib.ci import context

# Cleared before each test so the suite gives the same answer when run inside CI.
CI_ENV_VARS = [
    "GITHUB_ACTIONS",
    "GITHUB_WORKSPACE",
    "GITHUB_SHA",
    "GITHUB_REF",
    "GITHUB_REF_NAME",
    "GITHUB_HEAD_REF",
    "GITHUB_BASE_REF",
    "GITHUB_REPOSITORY",
    "GITHUB_RUN_ID",
    "GITHUB_SERVER_URL",
    "GITHUB_EVENT_NAME",
    "GITHUB_EVENT_PATH",
    "GITLAB_CI",
    "CI_PROJECT_DIR",
    "CI_COMMIT_SHA",
    "CI_COMMIT_REF_NAME",
    "CI_COMMIT_BRANCH",
    "CI_COMMIT_TAG",
    "CI_COMMIT_BEFORE_SHA",
    "CI_MERGE_REQUEST_IID",
    "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
    "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME",
    "CI_DEFAULT_BRANCH",
    "CI_PROJECT_PATH",
    "CI_JOB_URL",
    "JENKINS_URL",
    "JENKINS_HOME",
    "JENKINS_NODE_COOKIE",
    "HUDSON_HOME",
    "HUDSON_COOKIE",
    "WORKSPACE",
    "GIT_COMMIT",
    "GIT_BRANCH",
    "BRANCH_NAME",
    "CHANGE_ID",
    "CHANGE_TARGET",
    "JOB_NAME",
    "BUILD_URL",
    "BUILDKITE",
    "BUILDKITE_COMMIT",
    "BUILDKITE_BRANCH",
    "BUILDKITE_PULL_REQUEST",
    "BUILDKITE_PULL_REQUEST_BASE_BRANCH",
    "BUILDKITE_BUILD_CHECKOUT_PATH",
    "CIRCLECI",
    "CIRCLE_SHA1",
    "CIRCLE_BRANCH",
    "TF_BUILD",
    "BUILD_SOURCEVERSION",
    "SYSTEM_PULLREQUEST_TARGETBRANCH",
    "RUNNER_TEMP",
    "AGENT_TEMPDIRECTORY",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in CI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_no_ci_is_detected_as_local():
    assert context.detect().system == context.CISystem.LOCAL


def test_github_pull_request(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_WORKSPACE", "/home/runner/work/chip/chip")
    monkeypatch.setenv("GITHUB_SHA", "abc123def456")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/42/merge")
    monkeypatch.setenv("GITHUB_HEAD_REF", "feature/fifo")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/chip")
    monkeypatch.setenv("GITHUB_RUN_ID", "999")

    build = context.detect()

    assert build.system == context.CISystem.GITHUB
    # Compared as a Path: on Windows the same workspace renders with backslashes.
    assert build.workspace == Path("/home/runner/work/chip/chip")
    assert build.pr_number == "42"
    assert build.branch == "feature/fifo"
    assert build.base_ref == "origin/main"
    assert build.is_pull_request
    assert build.build_url == "https://github.com/acme/chip/actions/runs/999"


def test_github_push_has_no_pull_request(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")

    build = context.detect()

    assert build.pr_number is None
    assert build.base_ref is None
    assert build.branch == "main"


BEFORE = "0123456789abcdef0123456789abcdef01234567"


def _github_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, payload: str) -> None:
    event = tmp_path / "event.json"
    event.write_text(payload)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))


def test_a_github_push_reports_the_commit_before_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _github_event(monkeypatch, tmp_path, "push", f'{{"before": "{BEFORE}", "after": "ffff"}}')

    assert context.detect().push_before == BEFORE


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("push", '{"before": "0000000000000000000000000000000000000000"}'),
        ("push", '{"before": "--upload-pack=evil"}'),
        ("push", "not json"),
        ("push", "[]"),
        ("pull_request", f'{{"before": "{BEFORE}"}}'),
    ],
    ids=["new-branch", "not-a-sha", "unreadable", "not-an-object", "not-a-push"],
)
def test_a_github_event_without_a_usable_before_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, payload: str
):
    _github_event(monkeypatch, tmp_path, name, payload)

    assert context.detect().push_before is None


@pytest.mark.parametrize(("value", "expected"), [(BEFORE, BEFORE), ("0" * 40, None), ("", None)])
def test_gitlab_reports_the_commit_before_the_push(monkeypatch: pytest.MonkeyPatch, value: str, expected: str | None):
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_COMMIT_BEFORE_SHA", value)

    assert context.detect().push_before == expected


def test_gitlab_merge_request(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_PROJECT_DIR", "/builds/acme/chip")
    monkeypatch.setenv("CI_COMMIT_SHA", "deadbeef")
    monkeypatch.setenv("CI_MERGE_REQUEST_IID", "7")
    monkeypatch.setenv("CI_MERGE_REQUEST_TARGET_BRANCH_NAME", "develop")
    monkeypatch.setenv("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME", "feature/alu")
    monkeypatch.setenv("CI_PROJECT_PATH", "acme/chip")

    build = context.detect()

    assert build.system == context.CISystem.GITLAB
    assert build.pr_number == "7"
    assert build.base_ref == "origin/develop"
    assert build.branch == "feature/alu"


def test_gitlab_diffs_a_feature_branch_against_the_default_branch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_DEFAULT_BRANCH", "main")
    monkeypatch.setenv("CI_COMMIT_BRANCH", "feature/alu")
    monkeypatch.setenv("CI_COMMIT_REF_NAME", "feature/alu")

    assert context.detect().base_ref == "origin/main"


@pytest.mark.parametrize("branch_variable", ["CI_COMMIT_BRANCH", "CI_COMMIT_REF_NAME"])
def test_gitlab_on_the_default_branch_has_no_base(monkeypatch: pytest.MonkeyPatch, branch_variable: str):
    # Diffing main against origin/main finds nothing; no base makes the scope fall back to HEAD~1.
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_DEFAULT_BRANCH", "main")
    monkeypatch.setenv(branch_variable, "main")

    assert context.detect().base_ref is None


def test_gitlab_tag_pipeline_has_no_base(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_DEFAULT_BRANCH", "main")
    monkeypatch.setenv("CI_COMMIT_TAG", "v1.0.0")
    monkeypatch.setenv("CI_COMMIT_REF_NAME", "v1.0.0")

    assert context.detect().base_ref is None


def test_jenkins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.internal/")
    monkeypatch.setenv("WORKSPACE", "/var/jenkins/workspace/chip")
    monkeypatch.setenv("GIT_COMMIT", "cafe1234")
    monkeypatch.setenv("CHANGE_ID", "15")
    monkeypatch.setenv("CHANGE_TARGET", "trunk")

    build = context.detect()

    assert build.system == context.CISystem.JENKINS
    assert build.workspace == Path("/var/jenkins/workspace/chip")
    assert build.commit_sha == "cafe1234"
    assert build.base_ref == "origin/trunk"


def test_buildkite_reports_no_pull_request_as_the_string_false(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BUILDKITE", "true")
    monkeypatch.setenv("BUILDKITE_PULL_REQUEST", "false")

    build = context.detect()

    assert build.system == context.CISystem.BUILDKITE
    assert build.pr_number is None


def test_github_wins_over_a_generic_ci_flag(monkeypatch: pytest.MonkeyPatch):
    # GitHub-hosted runners also set JENKINS_URL in some self-hosted images.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("JENKINS_URL", "https://jenkins.internal/")

    assert context.detect().system == context.CISystem.GITHUB


def test_describe_mentions_the_repository_and_commit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/chip")
    monkeypatch.setenv("GITHUB_SHA", "abcdef0123456789")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/3/merge")

    description = context.detect().describe()

    assert "acme/chip" in description
    assert "abcdef012345" in description
    assert "#3" in description


def test_a_jenkins_docker_agent_is_still_jenkins(clean_env, monkeypatch, tmp_path):
    """Neither JENKINS_URL nor JENKINS_HOME reaches a docker agent."""
    monkeypatch.setenv("JENKINS_NODE_COOKIE", "991f2b7a")
    monkeypatch.setenv("HUDSON_HOME", "/var/jenkins_home")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("JOB_NAME", "aristotle-verify")

    build = context.detect()

    assert build.system == context.CISystem.JENKINS
    assert build.repo_slug == "aristotle-verify"


def test_a_workspace_variable_outside_jenkins_names_no_checkout(clean_env, monkeypatch, tmp_path):
    """WORKSPACE is Jenkins's; outside Jenkins any tool may set it."""
    monkeypatch.setenv("WORKSPACE", "/somewhere/else")
    monkeypatch.chdir(tmp_path)

    build = context.detect()

    assert build.system == context.CISystem.LOCAL
    assert build.workspace == Path.cwd()


def test_the_shallow_checkout_hint_names_the_right_setting(clean_env, monkeypatch):
    """The hint names the setting of the CI system in use."""
    monkeypatch.setenv("GITLAB_CI", "true")
    assert "git fetch origin" in context.deepen_checkout_hint()

    monkeypatch.delenv("GITLAB_CI")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert "fetch-depth" in context.deepen_checkout_hint()


def test_an_unrecognized_system_still_gets_usable_advice(clean_env):
    assert "base branch" in context.deepen_checkout_hint(context.CISystem.LOCAL)


@pytest.mark.parametrize(
    ("detected", "variable", "suffix"),
    [
        ({"GITHUB_ACTIONS": "true"}, "RUNNER_TEMP", ""),
        ({"TF_BUILD": "True"}, "AGENT_TEMPDIRECTORY", ""),
        ({"GITLAB_CI": "true"}, "CI_PROJECT_DIR", ".tmp"),
        ({"JENKINS_URL": "https://jenkins.example.com/"}, "WORKSPACE", "@tmp"),
    ],
    ids=["GitHub", "Azure", "GitLab", "Jenkins"],
)
def test_the_folder_of_a_jobs_credential_files_is_the_ci_systems(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, detected: dict[str, str], variable: str, suffix: str
):
    (tmp_path / f"job{suffix}").mkdir()
    for name, value in detected.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(variable, str(tmp_path / "job"))

    assert context.credential_folders() == [tmp_path / f"job{suffix}"]


def test_no_folder_of_credential_files_outside_its_ci_system_or_when_it_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    assert context.credential_folders() == []

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "missing"))
    assert context.credential_folders() == []


@pytest.mark.parametrize(
    ("detected", "variable", "suffix"), [("GITLAB_CI", "CI_PROJECT_DIR", ".tmp"), ("JENKINS_URL", "WORKSPACE", "@tmp")]
)
def test_the_folder_beside_the_checkout_ignores_a_trailing_separator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, detected: str, variable: str, suffix: str
):
    (tmp_path / f"job{suffix}").mkdir()
    monkeypatch.setenv(detected, "true")
    monkeypatch.setenv(variable, f"{tmp_path / 'job'}{os.sep}")

    assert context.credential_folders() == [tmp_path / f"job{suffix}"]
