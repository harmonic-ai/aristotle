"""Detection of the surrounding CI system and the commit under test."""

import dataclasses
import json
import logging
import os
import re
from enum import Enum
from pathlib import Path

logger = logging.getLogger("aristotle")


class CISystem(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"
    JENKINS = "jenkins"
    BUILDKITE = "buildkite"
    CIRCLECI = "circleci"
    AZURE = "azure"
    LOCAL = "local"

    def __str__(self) -> str:
        return self.value


def _first_env(*names: str) -> str | None:
    """Return the first of ``names`` that is set to a non-empty value."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def detect_system() -> CISystem:
    """The CI system the environment names, from its variables alone."""
    # Checked most-specific first: several systems also set the generic CI variable.
    if os.environ.get("GITHUB_ACTIONS"):
        return CISystem.GITHUB
    if os.environ.get("GITLAB_CI"):
        return CISystem.GITLAB
    if os.environ.get("BUILDKITE"):
        return CISystem.BUILDKITE
    if os.environ.get("CIRCLECI"):
        return CISystem.CIRCLECI
    if os.environ.get("TF_BUILD"):
        return CISystem.AZURE
    # JENKINS_URL and JENKINS_HOME do not reach a docker agent; the per-build variables do.
    if _first_env("JENKINS_URL", "JENKINS_HOME", "JENKINS_NODE_COOKIE", "HUDSON_HOME", "HUDSON_COOKIE"):
        return CISystem.JENKINS
    return CISystem.LOCAL


@dataclasses.dataclass(frozen=True)
class CIContext:
    """Normalized facts about the build we are running inside."""

    system: CISystem
    workspace: Path
    commit_sha: str | None = None
    branch: str | None = None
    base_ref: str | None = None
    """The branch this change is proposed against, when the build is for a pull request."""
    repo_slug: str | None = None
    pr_number: str | None = None
    build_url: str | None = None
    push_before: str | None = None
    """The commit the branch pointed at before the push being built, when the CI system reports one."""

    @property
    def is_pull_request(self) -> bool:
        return self.pr_number is not None or self.base_ref is not None

    def describe(self) -> str:
        parts = [f"CI system: {self.system}"]
        if self.repo_slug:
            parts.append(f"repository: {self.repo_slug}")
        if self.commit_sha:
            parts.append(f"commit: {self.commit_sha[:12]}")
        if self.pr_number:
            parts.append(f"pull request: #{self.pr_number}")
        elif self.branch:
            parts.append(f"branch: {self.branch}")
        return ", ".join(parts)


def _github_base_ref() -> str | None:
    # GITHUB_BASE_REF is set only for pull_request events, and holds a bare branch name.
    base_ref = os.environ.get("GITHUB_BASE_REF")
    return f"origin/{base_ref}" if base_ref else None


_COMMIT_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _real_commit(sha: str | None) -> str | None:
    """``sha`` if it names a commit; both systems send all zeros when the push created the branch."""
    if not sha:
        return None
    sha = sha.strip().lower()
    if not _COMMIT_SHA.fullmatch(sha) or not sha.strip("0"):
        return None
    return sha


def _github_push_before() -> str | None:
    # Only push events carry "before"; the payload is the webhook body, saved to a file.
    if os.environ.get("GITHUB_EVENT_NAME") != "push":
        return None
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return None
    try:
        with open(event_path, encoding="utf-8") as f:
            event = json.load(f)
    except (OSError, ValueError) as e:
        logger.debug(f"Could not read the GitHub event payload: {e}")
        return None
    before = event.get("before") if isinstance(event, dict) else None
    return _real_commit(before) if isinstance(before, str) else None


def _gitlab_base_ref() -> str | None:
    """The merge request target, else the default branch when building another branch; None on the default branch."""
    target = os.environ.get("CI_MERGE_REQUEST_TARGET_BRANCH_NAME")
    if target:
        return f"origin/{target}"
    default = os.environ.get("CI_DEFAULT_BRANCH")
    # CI_COMMIT_REF_NAME names the tag in a tag pipeline, which is not a branch to diff.
    branch = os.environ.get("CI_COMMIT_BRANCH") or (
        None if os.environ.get("CI_COMMIT_TAG") else os.environ.get("CI_COMMIT_REF_NAME")
    )
    if default and branch and branch != default:
        return f"origin/{default}"
    return None


def detect() -> CIContext:
    """Inspect the environment and return the normalized build context."""
    system = detect_system()
    # WORKSPACE is Jenkins's; outside Jenkins it is any tool's, so it names no checkout.
    jenkins_workspace = os.environ.get("WORKSPACE") if system == CISystem.JENKINS else None
    workspace = Path(
        _first_env("GITHUB_WORKSPACE", "CI_PROJECT_DIR", "BUILDKITE_BUILD_CHECKOUT_PATH")
        or jenkins_workspace
        or os.getcwd()
    )

    if system == CISystem.GITHUB:
        repo = os.environ.get("GITHUB_REPOSITORY")
        run_id = os.environ.get("GITHUB_RUN_ID")
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        context = CIContext(
            system=system,
            workspace=workspace,
            commit_sha=os.environ.get("GITHUB_SHA"),
            branch=os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME"),
            base_ref=_github_base_ref(),
            repo_slug=repo,
            pr_number=_github_pr_number(),
            build_url=f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else None,
            push_before=_github_push_before(),
        )
    elif system == CISystem.GITLAB:
        context = CIContext(
            system=system,
            workspace=workspace,
            commit_sha=os.environ.get("CI_COMMIT_SHA"),
            branch=_first_env("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME", "CI_COMMIT_REF_NAME"),
            base_ref=_gitlab_base_ref(),
            repo_slug=os.environ.get("CI_PROJECT_PATH"),
            pr_number=os.environ.get("CI_MERGE_REQUEST_IID"),
            build_url=os.environ.get("CI_JOB_URL"),
            push_before=_real_commit(os.environ.get("CI_COMMIT_BEFORE_SHA")),
        )
    elif system == CISystem.JENKINS:
        context = CIContext(
            system=system,
            workspace=workspace,
            commit_sha=_first_env("GIT_COMMIT", "ghprbActualCommit"),
            branch=_first_env("BRANCH_NAME", "GIT_BRANCH"),
            base_ref=_jenkins_base_ref(),
            repo_slug=os.environ.get("JOB_NAME"),
            pr_number=os.environ.get("CHANGE_ID"),
            build_url=os.environ.get("BUILD_URL"),
        )
    elif system == CISystem.BUILDKITE:
        context = CIContext(
            system=system,
            workspace=workspace,
            commit_sha=os.environ.get("BUILDKITE_COMMIT"),
            branch=os.environ.get("BUILDKITE_BRANCH"),
            base_ref=_buildkite_base_ref(),
            repo_slug=os.environ.get("BUILDKITE_PIPELINE_SLUG"),
            pr_number=_buildkite_pr_number(),
            build_url=os.environ.get("BUILDKITE_BUILD_URL"),
        )
    elif system == CISystem.CIRCLECI:
        context = CIContext(
            system=system,
            workspace=workspace,
            commit_sha=os.environ.get("CIRCLE_SHA1"),
            branch=os.environ.get("CIRCLE_BRANCH"),
            repo_slug=os.environ.get("CIRCLE_PROJECT_REPONAME"),
            build_url=os.environ.get("CIRCLE_BUILD_URL"),
        )
    elif system == CISystem.AZURE:
        base_ref = os.environ.get("SYSTEM_PULLREQUEST_TARGETBRANCH")
        context = CIContext(
            system=system,
            workspace=workspace,
            commit_sha=os.environ.get("BUILD_SOURCEVERSION"),
            branch=os.environ.get("BUILD_SOURCEBRANCHNAME"),
            base_ref=base_ref.replace("refs/heads/", "origin/") if base_ref else None,
            repo_slug=os.environ.get("BUILD_REPOSITORY_NAME"),
            pr_number=os.environ.get("SYSTEM_PULLREQUEST_PULLREQUESTNUMBER"),
        )
    else:
        context = CIContext(system=system, workspace=workspace)

    logger.debug(f"Detected {context.describe()}")
    return context


# Each system spells "fetch the base branch too" differently.
_DEEPEN_CHECKOUT_HINTS = {
    CISystem.GITHUB: "Set fetch-depth: 0 on the actions/checkout step.",
    CISystem.GITLAB: (
        "GitLab fetches only the ref being built, so the job must be able to run "
        "git fetch origin <target branch>; add that to before_script to see why it fails."
    ),
    CISystem.JENKINS: "Turn off the shallow clone option on the checkout step.",
    CISystem.AZURE: "Set fetchDepth: 0 on the checkout step.",
    CISystem.BUILDKITE: "Set BUILDKITE_GIT_CLONE_FLAGS so the clone is not shallow.",
}


def credential_folders() -> list[Path]:
    """The folders, those that exist, where the CI system the environment names keeps a job's credential files beside
    its checkout: GitHub's RUNNER_TEMP, where actions/checkout keeps the job's token, and its mount in a container
    action, Azure's AGENT_TEMPDIRECTORY, GitLab's ``<project>.tmp`` for file variables, and Jenkins' ``<workspace>@tmp``
    for secret files."""
    system = detect_system()
    folders: list[str | None] = []
    if system == CISystem.GITHUB:
        folders = [os.environ.get("RUNNER_TEMP"), "/github/runner_temp"]
    elif system == CISystem.AZURE:
        folders = [os.environ.get("AGENT_TEMPDIRECTORY")]
    elif system == CISystem.GITLAB and (project := os.environ.get("CI_PROJECT_DIR")):
        folders = [f"{os.path.normpath(project)}.tmp"]
    elif system == CISystem.JENKINS and (workspace := os.environ.get("WORKSPACE")):
        folders = [f"{os.path.normpath(workspace)}@tmp"]
    return [Path(folder) for folder in folders if folder and os.path.isdir(folder)]


def deepen_checkout_hint(system: CISystem | None = None) -> str:
    """How to make the base branch reachable, in the words of the CI system in use."""
    system = system if system is not None else detect_system()
    return _DEEPEN_CHECKOUT_HINTS.get(system, "Fetch the base branch from origin before this step.")


def _github_pr_number() -> str | None:
    # GITHUB_REF looks like "refs/pull/123/merge" on pull_request events.
    ref = os.environ.get("GITHUB_REF", "")
    parts = ref.split("/")
    if len(parts) >= 3 and parts[1] == "pull":
        return parts[2]
    return None


def _jenkins_base_ref() -> str | None:
    base_ref = _first_env("CHANGE_TARGET", "ghprbTargetBranch")
    return f"origin/{base_ref}" if base_ref else None


def _buildkite_base_ref() -> str | None:
    base_ref = os.environ.get("BUILDKITE_PULL_REQUEST_BASE_BRANCH")
    return f"origin/{base_ref}" if base_ref else None


def _buildkite_pr_number() -> str | None:
    # Buildkite sets the literal string "false" when the build is not for a pull request.
    pr_number = os.environ.get("BUILDKITE_PULL_REQUEST")
    return pr_number if pr_number and pr_number != "false" else None
