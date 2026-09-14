from __future__ import annotations

import pytest

from cache22.repository_ref import parse_repository_url


def test_parse_repository_url_normalizes_https_and_ssh_for_github() -> None:
    https_repository = parse_repository_url("https://github.com/Ar-Jan/Cache22.git")
    ssh_repository = parse_repository_url("git@github.com:ar-jan/cache22.git")

    assert https_repository == ssh_repository
    assert https_repository.host == "github.com"
    assert https_repository.namespace == ("ar-jan",)
    assert https_repository.name == "cache22"
    assert https_repository.display_path == "github.com/Ar-Jan/Cache22"
    assert ssh_repository.display_path == "github.com/ar-jan/cache22"


def test_parse_repository_url_normalizes_https_and_ssh_for_gitlab() -> None:
    https_repository = parse_repository_url("https://gitlab.com/Group/Subgroup/Cache22.git")
    ssh_repository = parse_repository_url("git@gitlab.com:group/subgroup/cache22.git")

    assert https_repository == ssh_repository
    assert https_repository.host == "gitlab.com"
    assert https_repository.namespace == ("group", "subgroup")
    assert https_repository.name == "cache22"
    assert https_repository.display_path == "gitlab.com/Group/Subgroup/Cache22"
    assert ssh_repository.display_path == "gitlab.com/group/subgroup/cache22"


def test_parse_repository_url_supports_ssh_scheme_for_gitlab() -> None:
    repository = parse_repository_url("ssh://git@gitlab.com/Group/Subgroup/Cache22.git")

    assert repository.host == "gitlab.com"
    assert repository.namespace == ("group", "subgroup")
    assert repository.name == "cache22"


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://Git.Example.ORG/Team/Subgroup/Cache22.git",
        "SSH://git@Git.Example.ORG:2222/Team/Subgroup/Cache22.git",
        "git@Git.Example.ORG:Team/Subgroup/Cache22.git",
        "https://git.example.org/Team/Subgroup/Cache22.GIT/",
        "https://git.example.org/Team/Subgroup/Cache22/",
    ],
)
def test_parse_repository_url_accepts_alternative_git_host(url: str) -> None:
    repository = parse_repository_url(url)

    assert repository.host == "git.example.org"
    assert repository.namespace == ("team", "subgroup")
    assert repository.name == "cache22"
    assert repository.display_path == "git.example.org/Team/Subgroup/Cache22"


def test_parse_repository_url_normalizes_case_for_alternative_git_host() -> None:
    mixed_case = parse_repository_url("git@git.example.org:Team/Subgroup/Cache22.git")
    lowercase = parse_repository_url("git@git.example.org:team/subgroup/cache22.git")

    assert mixed_case == lowercase
    assert hash(mixed_case) == hash(lowercase)
    assert mixed_case.display_path != lowercase.display_path
