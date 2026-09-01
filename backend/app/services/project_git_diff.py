"""Project repository state and commit diff operations."""

from app.services.project_git_mutations import *  # noqa: F401,F403

def _resolve_reachable_commit(repo: Path, raw_commit: str) -> str:
    value = raw_commit.strip()
    if not _COMMIT_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail="Invalid commit identifier")
    resolved = _git(repo, "rev-parse", "--verify", f"{value}^{{commit}}", check=False)
    if resolved.returncode != 0:
        raise HTTPException(status_code=422, detail="Commit does not exist in this project repository")
    commit = resolved.stdout.strip()
    # Git can retain unreachable objects after a branch is deleted. Knowing an
    # object id must not be enough to read repository content through this API.
    if _git(repo, "merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode != 0:
        raise HTTPException(status_code=422, detail="Commit is not reachable from the project history")
    return commit


def _utf8_patch_prefix(data: bytes, *, truncated: bool) -> str:
    text = data.decode("utf-8", errors="ignore")
    if truncated and "\n" in text:
        # Keep the response on a complete diff line. The response is explicitly
        # marked truncated and is a viewer payload, never an applicable patch.
        text = text[: text.rfind("\n") + 1]
    return text


def _text_blob_at(repo: Path, revision: str | None, path: str) -> dict[str, Any]:
    if revision is None:
        return {"content": "", "truncated": False, "binary": False, "size": 0}
    entry = _tree_entry_at(repo, revision, path)
    if entry is None:
        return {"content": "", "truncated": False, "binary": False, "size": 0}
    object_id, size = entry
    sample = _blob_prefix(repo, object_id, PROJECT_GIT_DIFF_CONTENT_MAX_BYTES + 1)
    _mime_type, _kind, is_text = _mime_and_kind(path, sample[:8192])
    if not is_text:
        return {"content": None, "truncated": False, "binary": True, "size": size}
    prefix = sample[:PROJECT_GIT_DIFF_CONTENT_MAX_BYTES]
    content = prefix.decode("utf-8", errors="ignore")
    return {
        "content": content,
        "truncated": size > PROJECT_GIT_DIFF_CONTENT_MAX_BYTES,
        "binary": False,
        "size": size,
    }


def _parse_numstat(payload: bytes) -> tuple[list[tuple[str, int | None, int | None]], bool]:
    records = payload.split(b"\x00")
    complete = payload.endswith(b"\x00")
    if complete:
        records.pop()
    elif records:
        records.pop()  # Never expose a partial path/stat record.
    parsed: list[tuple[str, int | None, int | None]] = []
    for raw_record in records:
        fields = raw_record.split(b"\t", 2)
        if len(fields) != 3:
            continue
        additions_raw, deletions_raw, raw_path = fields
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            continue
        try:
            additions = None if additions_raw == b"-" else int(additions_raw)
            deletions = None if deletions_raw == b"-" else int(deletions_raw)
        except ValueError:
            continue
        parsed.append((path, additions, deletions))
        if len(parsed) >= PROJECT_GIT_DIFF_MAX_FILES:
            return parsed, True
    return parsed, not complete


def _project_commit_diff(
    project: Project,
    commit: str,
    parent: str | None,
    path: str | None,
    max_patch_bytes: int,
) -> dict[str, Any]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        target_commit = _resolve_reachable_commit(repo, commit)
        revision_line = _git(repo, "rev-list", "--parents", "-n", "1", target_commit).stdout.strip().split()
        available_parents = revision_line[1:]
        if parent:
            parent_commit = _resolve_reachable_commit(repo, parent)
            if parent_commit not in available_parents:
                raise HTTPException(status_code=422, detail="Parent must be a direct parent of the target commit")
        else:
            parent_commit = available_parents[0] if available_parents else None

        normalized_path = _safe_relative_path(repo, path)[0] if path else None
        common_options = [
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
        ]
        if parent_commit is None:
            base_args = ["diff-tree", "--root", "--no-commit-id", "-r"]
            revisions = [target_commit]
        else:
            base_args = ["diff"]
            revisions = [parent_commit, target_commit]

        path_args = ["--", normalized_path] if normalized_path else ["--"]
        patch_data, patch_truncated = _git_stdout_prefix(
            repo,
            *base_args,
            "-p",
            "--unified=3",
            *common_options,
            *revisions,
            *path_args,
            limit=max_patch_bytes,
        )
        stats_data, stats_truncated = _git_stdout_prefix(
            repo,
            *base_args,
            "--numstat",
            "-z",
            *common_options,
            *revisions,
            *path_args,
            limit=PROJECT_GIT_DIFF_FILE_LIST_MAX_BYTES,
        )
        stats, files_truncated = _parse_numstat(stats_data)
        files: list[dict[str, Any]] = []
        for changed_path, additions, deletions in stats:
            try:
                safe_path = _safe_relative_path(repo, changed_path)[0]
            except HTTPException:
                files_truncated = True
                continue
            original_entry = _tree_entry_at(repo, parent_commit, safe_path) if parent_commit else None
            modified_entry = _tree_entry_at(repo, target_commit, safe_path)
            if normalized_path:
                original = _text_blob_at(repo, parent_commit, safe_path)
                modified = _text_blob_at(repo, target_commit, safe_path)
            else:
                # Commit-wide requests remain metadata/patch bounded. Full
                # before/after blobs are returned only for an explicitly
                # validated path, which is what the Monaco viewer requests.
                is_binary = additions is None or deletions is None
                original = {
                    "content": None,
                    "truncated": False,
                    "binary": is_binary,
                    "size": original_entry[1] if original_entry else 0,
                }
                modified = {
                    "content": None,
                    "truncated": False,
                    "binary": is_binary,
                    "size": modified_entry[1] if modified_entry else 0,
                }
            if parent_commit is None or original_entry is None:
                status = "added"
            elif modified_entry is None:
                status = "deleted"
            else:
                status = "modified"
            files.append(
                {
                    "path": safe_path,
                    "status": status,
                    "additions": additions,
                    "deletions": deletions,
                    "binary": bool(original["binary"] or modified["binary"]),
                    "original_content": original["content"],
                    "modified_content": modified["content"],
                    "content_included": normalized_path is not None,
                    "content_truncated": bool(original["truncated"] or modified["truncated"]),
                    "original_size": original["size"],
                    "modified_size": modified["size"],
                }
            )

        safe_patch_limit = max(1, min(int(max_patch_bytes), PROJECT_GIT_DIFF_PATCH_HARD_LIMIT_BYTES))
        return {
            "commit": target_commit,
            "target_commit": target_commit,
            "parent": parent_commit,
            "parent_commit": parent_commit,
            "available_parent_commits": available_parents,
            "is_root": parent_commit is None,
            "path": normalized_path,
            "patch": _utf8_patch_prefix(patch_data, truncated=patch_truncated),
            "patch_truncated": patch_truncated,
            "patch_bytes": min(len(patch_data), safe_patch_limit),
            "max_patch_bytes": safe_patch_limit,
            "files": files,
            "files_truncated": bool(files_truncated or stats_truncated),
        }


async def project_commit_diff(
    project: Project,
    commit: str,
    parent: str | None = None,
    path: str | None = None,
    max_patch_bytes: int = PROJECT_GIT_DIFF_PATCH_MAX_BYTES,
) -> dict[str, Any]:
    return await asyncio.to_thread(_project_commit_diff, project, commit, parent, path, max_patch_bytes)


def _repository_state(project: Project, limit: int) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        log = _git(
            repo,
            "log",
            f"-{limit}",
            "--date=iso-strict",
            "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s",
        ).stdout
        commits = []
        for line in log.splitlines():
            full, short, author, created_at, subject = line.split("\x1f", 4)
            if author.strip().casefold() in _LEGACY_PROJECT_AUTHOR_NAMES:
                author = "项目成员"
            commits.append(
                {
                    "commit": full,
                    "short_commit": short,
                    "author": author,
                    "created_at": created_at,
                    "message": subject,
                }
            )
        files = [line for line in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines() if line]
        branches = [
            line.lstrip("* ") for line in _git(repo, "branch", "--format=%(refname:short)").stdout.splitlines() if line
        ]
        return {
            "mode": "managed",
            "head": _git(repo, "rev-parse", "HEAD").stdout.strip(),
            "commits": commits,
            "files": files,
            "branches": branches,
        }


async def repository_state(project: Project, limit: int = 50) -> dict:
    return await asyncio.to_thread(_repository_state, project, limit)


__all__ = [name for name in globals() if not name.startswith("__")]
