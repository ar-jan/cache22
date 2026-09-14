from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit


def validate_storage_component(component: str) -> None:
    if (
        not component
        or component in {".", ".."}
        or any(char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in component)
    ):
        raise ValueError(f"Unsafe repository storage component: {component!r}")


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    host: str
    namespace: tuple[str, ...]
    name: str
    display_path: str = field(compare=False)
    clone_url: str = field(default="", compare=False)
    source_path: str = field(default="", compare=False)


def parse_repository_url(url: str, *, case_sensitive: bool = False) -> RepositoryRef:
    raw_url = url.strip()
    if not raw_url:
        raise ValueError("Repository URL must not be empty")

    host, raw_path = _split_clone_url(raw_url)
    validate_storage_component(host)
    repository = _repository_from_path(host, raw_path)
    effective = (
        repository.display_path.split("/", 1)[1]
        if case_sensitive
        else "/".join((*repository.namespace, repository.name))
    )
    suffix = raw_path.strip().rstrip("/")[-4:]
    if suffix.casefold() != ".git":
        suffix = ""
    elif not case_sensitive:
        suffix = ".git"
    path = effective + suffix
    if "://" not in raw_url:
        user_host = raw_url.partition(":")[0]
        user = user_host.rpartition("@")[0]
        clone_url = f"{user}@{host}:{'/' if raw_path.startswith('/') else ''}{path}"
    else:
        parsed = urlsplit(raw_url)
        credentials, separator, authority = parsed.netloc.rpartition("@")
        if not separator:
            authority = parsed.netloc
        if authority.startswith("["):
            port = authority.partition("]")[2]
            url_host = f"[{host}]"
        else:
            port = authority[len(authority.split(":", 1)[0]) :]
            url_host = host
        clone_url = f"{parsed.scheme.lower()}://{credentials + '@' if separator else ''}{url_host}{port}/{path}"
    return RepositoryRef(
        repository.host,
        repository.namespace,
        repository.name,
        repository.display_path,
        clone_url,
        f"{host}/{effective}",
    )


def _split_clone_url(raw_url: str) -> tuple[str, str]:
    if "://" not in raw_url:
        user_host, separator, path = raw_url.partition(":")
        if separator and "@" in user_host:
            _, _, host = user_host.rpartition("@")
            if host:
                return host.lower(), path

    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"https", "ssh"}:
        raise ValueError(f"Only HTTPS and SSH clone URLs are supported: {raw_url}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"Repository URL must not include query or fragment data: {raw_url}")

    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"Repository URL must include a hostname: {raw_url}")

    return host, parsed.path


def _repository_from_path(host: str, raw_path: str) -> RepositoryRef:
    normalized_path = raw_path.strip().strip("/")
    if normalized_path.casefold().endswith(".git"):
        normalized_path = normalized_path[:-4]

    repository_path = f"{host}/{raw_path.strip('/')}"
    parts = _path_parts(
        normalized_path,
        empty_message=(
            "Repository URL must have the form HOST/NAMESPACE[/SUBGROUP/...]/REPO(.git): "
            f"{repository_path}"
        ),
        invalid_segment_message=(
            "Repository URL must have the form HOST/NAMESPACE[/SUBGROUP/...]/REPO(.git): "
            f"{repository_path}"
        ),
    )
    if len(parts) < 2:
        raise ValueError(
            "Repository URL must have the form HOST/NAMESPACE[/SUBGROUP/...]/REPO(.git): "
            f"{repository_path}"
        )

    normalized_parts = _normalize_repository_parts(parts)
    for part in normalized_parts:
        validate_storage_component(part)
    return RepositoryRef(
        host=host,
        namespace=tuple(normalized_parts[:-1]),
        name=normalized_parts[-1],
        display_path="/".join((host, *parts)),
    )


def _path_parts(
    raw_path: str,
    *,
    empty_message: str,
    invalid_segment_message: str,
) -> list[str]:
    parts = [part for part in raw_path.strip().strip("/").split("/") if part]
    if not parts:
        raise ValueError(empty_message)
    if any(part in {".", ".."} for part in parts):
        raise ValueError(invalid_segment_message)
    return parts


def _normalize_repository_parts(parts: list[str]) -> list[str]:
    return [part.casefold() for part in parts]
