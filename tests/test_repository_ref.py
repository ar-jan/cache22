from __future__ import annotations

from cache22.repository_ref import parse_repository_url


def test_parse_repository_url_normalizes_https_and_ssh_for_github() -> None:
    https_repository = parse_repository_url("https://github.com/Ar-Jan/Cache22.git")
    ssh_repository = parse_repository_url("git@github.com:ar-jan/cache22.git")

    assert https_repository == ssh_repository
    assert https_repository.host == "github.com"
    assert https_repository.namespace == ("ar-jan",)
    assert https_repository.name == "cache22"


def test_parse_repository_url_normalizes_https_and_ssh_for_gitlab() -> None:
    https_repository = parse_repository_url("https://gitlab.com/Group/Subgroup/Cache22.git")
    ssh_repository = parse_repository_url("git@gitlab.com:group/subgroup/cache22.git")

    assert https_repository == ssh_repository
    assert https_repository.host == "gitlab.com"
    assert https_repository.namespace == ("group", "subgroup")
    assert https_repository.name == "cache22"


def test_parse_repository_url_supports_ssh_scheme_for_gitlab() -> None:
    repository = parse_repository_url("ssh://git@gitlab.com/Group/Subgroup/Cache22.git")

    assert repository.host == "gitlab.com"
    assert repository.namespace == ("group", "subgroup")
    assert repository.name == "cache22"


def test_parse_repository_url_accepts_alternative_git_host() -> None:
    repository = parse_repository_url("git@git.example.org:Team/Subgroup/Cache22.git")

    assert repository.host == "git.example.org"
    assert repository.namespace == ("Team", "Subgroup")
    assert repository.name == "Cache22"


def test_parse_repository_url_preserves_case_for_alternative_git_host() -> None:
    mixed_case = parse_repository_url("git@git.example.org:Team/Subgroup/Cache22.git")
    lowercase = parse_repository_url("git@git.example.org:team/subgroup/cache22.git")

    assert mixed_case != lowercase
