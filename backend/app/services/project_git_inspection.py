"""Repository tree, file, search, and archive inspection."""

from app.services.project_git_repository import *  # noqa: F401,F403

def _blob_prefix(repo: Path, object_id: str, limit: int) -> bytes:
    """Read only a bounded prefix from an immutable Git object."""

    process = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "blob", object_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        return process.stdout.read(max(0, limit))
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _mime_and_kind(path: str, sample: bytes) -> tuple[str, str, bool]:
    suffix = PurePosixPath(path).suffix.lower()
    sniffed_image = sniff_image_mime_bytes(sample)
    if sniffed_image:
        return sniffed_image, "image", False
    sniffed_media = sniff_media_mime_bytes(sample, path)
    if sniffed_media:
        return sniffed_media, sniffed_media.split("/", 1)[0], False
    mime_type = _TEXT_MIME_BY_SUFFIX.get(suffix)
    if mime_type is None:
        mime_type = mimetypes.guess_type(path, strict=False)[0]

    media_kind = (mime_type or "").split("/", 1)[0]
    if media_kind in {"image", "video", "audio"}:
        return mime_type or "application/octet-stream", media_kind, False

    known_text = bool(
        mime_type
        and (
            mime_type.startswith("text/")
            or mime_type
            in {
                "application/json",
                "application/ld+json",
                "application/sql",
                "application/toml",
                "application/xml",
                "application/yaml",
                "application/javascript",
            }
            or mime_type.endswith(("+json", "+xml"))
        )
    )
    if b"\x00" in sample:
        return mime_type or "application/octet-stream", "binary", False
    try:
        # ``sample`` is a bounded blob prefix and may end in the middle of a
        # valid multi-byte code point. An incremental decoder validates every
        # complete sequence while retaining an incomplete trailing sequence
        # for the unread next chunk.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        decoder.decode(sample, final=False)
    except UnicodeDecodeError:
        return mime_type or "application/octet-stream", "binary", False
    if known_text or mime_type is None:
        return mime_type or "text/plain", "text", True
    return mime_type, "binary", False


def _tree_entry_at(repo: Path, revision: str, path: str) -> tuple[str, int] | None:
    result = _git(repo, "ls-tree", "-l", "-z", revision, "--", path, check=False)
    if result.returncode != 0 or not result.stdout:
        return None
    record = result.stdout.rstrip("\x00")
    metadata, separator, recorded_path = record.partition("\t")
    parts = metadata.split()
    if not separator or recorded_path != path or len(parts) != 4 or parts[1] != "blob":
        return None
    object_id = parts[2]
    try:
        size = int(parts[3])
    except ValueError as exc:
        raise RuntimeError("Git returned an invalid project blob size") from exc
    return object_id, size


def _tree_entry(repo: Path, path: str) -> tuple[str, int]:
    entry = _tree_entry_at(repo, "HEAD", path)
    if entry is None:
        raise HTTPException(status_code=404, detail="Project file is not committed at HEAD")
    return entry


def _file_record_at(repo: Path, revision: str, path: str, *, preview_limit: int = 4000) -> dict:
    entry = _tree_entry_at(repo, revision, path)
    if entry is None:
        raise HTTPException(status_code=404, detail="Project file is not committed at this revision")
    object_id, size = entry
    sample = _blob_prefix(repo, object_id, max(preview_limit + 1, 8192))
    mime_type, kind, is_text = _mime_and_kind(path, sample)
    preview = sample[:preview_limit].decode("utf-8", errors="replace") if is_text else ""
    commit = _git(repo, "log", "-1", "--format=%H", revision, "--", path).stdout.strip()
    head = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").stdout.strip()
    return {
        "id": path,
        "path": path,
        "name": PurePosixPath(path).name,
        "size": size,
        "commit": commit,
        "commit_hash": commit,
        "head": head,
        "object_id": object_id,
        "mime_type": mime_type,
        "kind": kind,
        "is_text": is_text,
        "is_editable": is_text and size <= PROJECT_FILE_EDIT_LIMIT_BYTES,
        "preview": preview,
        "content_preview": preview,
    }


def _file_record(repo: Path, path: str, *, preview_limit: int = 4000) -> dict:
    return _file_record_at(repo, "HEAD", path, preview_limit=preview_limit)


def _batch_blob_prefixes(
    repo: Path,
    entries: list[tuple[str, int]],
    *,
    prefix_limit: int,
) -> dict[str, bytes]:
    """Read bounded previews for many Git blobs through one Git process.

    The file-list endpoint only needs a small preview for editor routing and
    empty-state hints.  Large blobs are deliberately excluded, and the total
    batch size is capped so a repository containing generated assets cannot
    turn a metadata request into an unbounded content download.
    """

    selected: dict[str, int] = {}
    total_size = 0
    for object_id, size in entries:
        if object_id in selected or size > PROJECT_FILE_LIST_PREVIEW_BLOB_MAX_BYTES:
            continue
        if total_size + size > PROJECT_FILE_LIST_PREVIEW_TOTAL_BYTES:
            continue
        selected[object_id] = size
        total_size += size
    if not selected:
        return {}

    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
    }
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repo}",
            "-C",
            str(repo),
            "cat-file",
            "--batch",
        ],
        input="".join(f"{object_id}\n" for object_id in selected).encode(),
        capture_output=True,
        timeout=30,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "git cat-file batch failed")

    payload = result.stdout
    position = 0
    previews: dict[str, bytes] = {}
    for expected_object_id, expected_size in selected.items():
        header_end = payload.find(b"\n", position)
        if header_end < 0:
            raise RuntimeError("Git returned a truncated batch header")
        header = payload[position:header_end].decode("ascii", errors="strict")
        parts = header.split()
        if len(parts) != 3 or parts[1] != "blob":
            raise RuntimeError("Git returned an invalid batch object")
        object_id, _kind, raw_size = parts
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise RuntimeError("Git returned an invalid batch object size") from exc
        if object_id != expected_object_id or size != expected_size:
            raise RuntimeError("Git batch output did not match the requested object")
        content_start = header_end + 1
        content_end = content_start + size
        if content_end >= len(payload) or payload[content_end : content_end + 1] != b"\n":
            raise RuntimeError("Git returned a truncated batch object")
        previews[object_id] = payload[content_start:content_end][:prefix_limit]
        position = content_end + 1
    return previews


def _last_commit_by_path(repo: Path, paths: set[str]) -> dict[str, str]:
    """Resolve last-changing commits for all visible paths in one traversal."""

    if not paths:
        return {}
    history = _git(
        repo,
        "log",
        "--pretty=format:%x1e%H%x1f",
        "--name-only",
        "-z",
        "HEAD",
    ).stdout
    commits: dict[str, str] = {}
    for record in history.split("\x1e")[1:]:
        commit, separator, names = record.partition("\x1f")
        if not separator:
            continue
        for path in names.strip("\x00\n").split("\x00"):
            normalized = path.strip("\n")
            if normalized in paths and normalized not in commits:
                commits[normalized] = commit
        if len(commits) == len(paths):
            break
    return commits


def _list_files(project: Project) -> list[dict]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        tree = _git(repo, "ls-tree", "-r", "-l", "-z", "HEAD").stdout
        entries: list[tuple[str, str, int]] = []
        for record in tree.split("\x00"):
            if not record:
                continue
            metadata, separator, path = record.partition("\t")
            parts = metadata.split()
            if not separator or len(parts) != 4 or parts[1] != "blob":
                continue
            try:
                size = int(parts[3])
            except ValueError as exc:
                raise RuntimeError("Git returned an invalid project blob size") from exc
            entries.append((path, parts[2], size))

        head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        commits = _last_commit_by_path(repo, {path for path, _object_id, _size in entries})
        samples = _batch_blob_prefixes(
            repo,
            [(object_id, size) for _path, object_id, size in entries],
            prefix_limit=8192,
        )
        files: list[dict] = []
        for path, object_id, size in entries:
            sample = samples.get(object_id, b"")
            mime_type, kind, is_text = _mime_and_kind(path, sample)
            preview = sample[:4000].decode("utf-8", errors="replace") if is_text else ""
            commit = commits.get(path, head)
            files.append(
                {
                    "id": path,
                    "path": path,
                    "name": PurePosixPath(path).name,
                    "size": size,
                    "commit": commit,
                    "commit_hash": commit,
                    "head": head,
                    "object_id": object_id,
                    "mime_type": mime_type,
                    "kind": kind,
                    "is_text": is_text,
                    "is_editable": is_text and size <= PROJECT_FILE_EDIT_LIMIT_BYTES,
                    "preview": preview,
                    "content_preview": preview,
                }
            )
        return files


async def list_project_files(project: Project) -> list[dict]:
    return await asyncio.to_thread(_list_files, project)


def _display_project_tool_size(size: int) -> str:
    return f"{size}B" if size < 1024 else f"{size / 1024:.1f}KB"


def _list_project_workspace(project: Project, path: str) -> str:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, target = _safe_project_delivery_root(repo, path)
        if not target.exists():
            return f"Directory not found: {path or '/'}"
        if not target.is_dir():
            return f"Path is not a directory: {path}"
        items: list[str] = []
        folder_count = 0
        file_count = 0
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            if child.name.startswith(".") or child.is_symlink():
                continue
            child_path = f"{normalized}/{child.name}" if normalized else child.name
            if child.is_dir():
                folder_count += 1
                child_count = len(
                    [
                        entry
                        for entry in child.iterdir()
                        if not entry.name.startswith(".") and not entry.is_symlink()
                    ]
                )
                items.append(f"  📁 {child_path}/ ({child_count} items)")
            elif child.is_file():
                file_count += 1
                items.append(f"  📄 {child_path} ({_display_project_tool_size(child.stat().st_size)})")
        label = normalized or "root"
        if not items:
            return f"📂 {label}: Empty directory (0 files, 0 folders)"
        return f"📂 {label}: {folder_count} folder(s), {file_count} file(s)\n" + "\n".join(items)


async def list_project_workspace(project: Project, path: str = "") -> str:
    return await asyncio.to_thread(_list_project_workspace, project, path)


def _read_project_workspace_file(project: Project, path: str, offset: int, limit: int) -> str:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, target = _safe_project_delivery_path(repo, path)
        if not target.exists() or not target.is_file():
            return (
                "File not found.\n"
                f"Requested path: {normalized}\n"
                "The requested path was not modified. Use list_files and copy an exact returned path."
            )
        start = max(0, int(offset))
        bounded_limit = max(0, int(limit))
        selected: list[str] = []
        total_lines = 0
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                total_lines = index + 1
                if start <= index < start + bounded_limit:
                    selected.append(line.rstrip("\r\n"))
        if start >= total_lines and total_lines:
            return f"Offset {offset} exceeds file length ({total_lines} lines total)"
        end = min(total_lines, start + bounded_limit)
        body = "\n".join(
            f"{index + 1:6}\t{line}"
            for index, line in enumerate(selected, start=start)
        )
        if end < total_lines:
            body += f"\n\n... [{total_lines - end} more lines not shown, lines {end + 1}-{total_lines}]"
        header = f"📄 {normalized} (lines {start + 1 if total_lines else 0}-{end} of {total_lines})\n"
        return header + body


async def read_project_workspace_file(
    project: Project,
    path: str,
    *,
    offset: int = 0,
    limit: int = 2000,
) -> str:
    return await asyncio.to_thread(_read_project_workspace_file, project, path, offset, limit)


def _project_workspace_search(
    project: Project,
    pattern: str,
    path: str,
    file_pattern: str,
    ignore_case: bool,
) -> str:
    repo = _repo_for(project)
    with _repo_lock(repo):
        _normalized, target = _safe_project_delivery_root(repo, path)
        if not target.exists() or not target.is_dir():
            return f"Directory not found: {path}"
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return f"Invalid regex pattern: {exc}"

        results: list[str] = []
        total_matches = 0
        files_searched = 0
        binary_suffixes = {
            ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".png",
            ".jpg", ".jpeg", ".gif", ".zip", ".tar", ".gz",
        }
        for candidate in target.rglob("*"):
            if len(results) >= 50:
                break
            try:
                matched_path, candidate = _safe_project_delivery_path(
                    repo,
                    candidate.relative_to(repo).as_posix(),
                )
            except (HTTPException, ValueError):
                continue
            if (
                not candidate.is_file()
                or any(part.startswith(".") for part in PurePosixPath(matched_path).parts)
                or candidate.suffix.lower() in binary_suffixes
            ):
                continue
            relative_match = candidate.relative_to(target).as_posix()
            if not (
                fnmatch.fnmatch(candidate.name, file_pattern)
                or fnmatch.fnmatch(relative_match, file_pattern)
                or fnmatch.fnmatch(matched_path, file_pattern)
            ):
                continue
            files_searched += 1
            try:
                content = candidate.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for line_number, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    results.append(f"{matched_path}:{line_number}: {line.strip()[:100]}")
                    total_matches += 1
                    if len(results) >= 50:
                        break
        if not results:
            return f"No matches found for pattern '{pattern}' in {files_searched} file(s)"
        truncated = total_matches > len(results)
        note = (
            f" (showing first {len(results)} of {total_matches}+ — refine pattern or path for more)"
            if truncated
            else ""
        )
        return (
            f"🔍 Found {total_matches}+ match(es) in {files_searched} file(s) "
            f"for pattern '{pattern}'{note}:\n" + "\n".join(results)
        )


async def search_project_workspace(
    project: Project,
    pattern: str,
    *,
    path: str = ".",
    file_pattern: str = "*",
    ignore_case: bool = False,
) -> str:
    return await asyncio.to_thread(
        _project_workspace_search,
        project,
        pattern,
        path,
        file_pattern,
        ignore_case,
    )


def _find_project_workspace_files(project: Project, pattern: str, path: str) -> str:
    repo = _repo_for(project)
    with _repo_lock(repo):
        _normalized, target = _safe_project_delivery_root(repo, path)
        if not target.exists() or not target.is_dir():
            return f"Directory not found: {path}"
        try:
            found: list[tuple[str, Path]] = []
            for candidate in target.glob(pattern):
                try:
                    matched_path, safe_candidate = _safe_project_delivery_path(
                        repo,
                        candidate.relative_to(repo).as_posix(),
                    )
                except (HTTPException, ValueError):
                    continue
                if any(part.startswith(".") for part in PurePosixPath(matched_path).parts):
                    continue
                found.append((matched_path, safe_candidate))
        except Exception as exc:
            return f"Invalid glob pattern: {exc}"
        found.sort(key=lambda item: item[1].stat().st_mtime if item[1].exists() else 0, reverse=True)
        matches: list[str] = []
        folder_count = 0
        file_count = 0
        for matched_path, candidate in found[:100]:
            if candidate.is_dir():
                folder_count += 1
                matches.append(f"📁 {matched_path}/")
            elif candidate.is_file():
                file_count += 1
                matches.append(f"📄 {matched_path} ({_display_project_tool_size(candidate.stat().st_size)})")
        if not matches:
            return f"No files matching pattern: {pattern}"
        return (
            f"📂 Found {len(found)} item(s) ({folder_count} dirs, {file_count} files) "
            f"matching '{pattern}':\n" + "\n".join(matches)
        )


async def find_project_workspace_files(project: Project, pattern: str, *, path: str = ".") -> str:
    return await asyncio.to_thread(_find_project_workspace_files, project, pattern, path)


def _inspect_file(project: Project, path: str) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, _target = _safe_relative_path(repo, path)
        return _file_record(repo, normalized, preview_limit=0)


async def inspect_project_file(project: Project, path: str) -> dict:
    return await asyncio.to_thread(_inspect_file, project, path)


def _inspect_file_at(project: Project, path: str, revision: str) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, _target = _safe_relative_path(repo, path)
        commit = _resolve_reachable_commit(repo, revision)
        return _file_record_at(repo, commit, normalized, preview_limit=0)


async def inspect_project_file_at(project: Project, path: str, revision: str) -> dict:
    return await asyncio.to_thread(_inspect_file_at, project, path, revision)


def _read_file_content(project: Project, path: str, max_chars: int) -> dict:
    repo = _repo_for(project)
    with _repo_lock(repo):
        normalized, _target = _safe_relative_path(repo, path)
        metadata = _file_record(repo, normalized, preview_limit=0)
        if not metadata["is_text"]:
            return {**metadata, "content": None, "truncated": False, "is_editable": False}
        # A UTF-8 code point uses at most four bytes. The extra byte lets us
        # distinguish an exact boundary from a truncated preview.
        sample = _blob_prefix(repo, metadata["object_id"], max_chars * 4 + 1)
        try:
            decoded = sample.decode("utf-8")
        except UnicodeDecodeError:
            # The sample may end in the middle of one UTF-8 code point.
            decoded = sample.decode("utf-8", errors="ignore")
        content = decoded[:max_chars]
        truncated = metadata["size"] > len(sample) or len(decoded) > max_chars
        return {
            **metadata,
            "content": content,
            "truncated": truncated,
            # Never let a caller overwrite a file from a partial editor buffer.
            "is_editable": bool(metadata["is_editable"] and not truncated),
        }


async def read_project_file_content(project: Project, path: str, max_chars: int = 200_000) -> dict:
    bounded = min(PROJECT_FILE_CONTENT_MAX_CHARS, max(1, int(max_chars)))
    return await asyncio.to_thread(_read_file_content, project, path, bounded)


def iter_project_file_blob(
    project: Project,
    object_id: str,
    *,
    start: int,
    end: int,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    """Yield one immutable HEAD blob range without materializing it in memory."""

    repo = _repo_for(project)
    remaining = max(0, end - start + 1)
    process = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "blob", object_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        skipped = 0
        while skipped < start:
            chunk = process.stdout.read(min(chunk_size, start - skipped))
            if not chunk:
                raise RuntimeError("Git project blob ended before the requested range")
            skipped += len(chunk)
        while remaining:
            chunk = process.stdout.read(min(chunk_size, remaining))
            if not chunk:
                raise RuntimeError("Git project blob ended before the requested range")
            remaining -= len(chunk)
            yield chunk
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _directory_snapshot(project: Project, path: str, revision: str = "HEAD") -> dict[str, Any]:
    repo = _repo_for(project)
    with _repo_lock(repo):
        commit = (
            _git(repo, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
            if revision == "HEAD"
            else _resolve_reachable_commit(repo, revision)
        )
        normalized = ""
        if path:
            normalized, _target = _safe_relative_path(repo, path)
            directory = _git(repo, "ls-tree", "-d", "-z", commit, "--", normalized, check=False)
            if directory.returncode != 0 or not directory.stdout:
                raise HTTPException(status_code=404, detail="Project directory is not committed at this revision")

        listing = _git(repo, "ls-tree", "-r", "-l", "-z", commit, "--", *([normalized] if normalized else []))
        file_count = 0
        total_size = 0
        for raw_record in listing.stdout.split("\x00"):
            if not raw_record:
                continue
            metadata, separator, recorded_path = raw_record.partition("\t")
            fields = metadata.split()
            if not separator or len(fields) != 4 or fields[1] != "blob":
                raise HTTPException(status_code=422, detail="Directory contains an unsupported Git entry")
            # Symlinks can become traversal primitives after ZIP extraction.
            if fields[0] not in {"100644", "100755"}:
                raise HTTPException(status_code=422, detail="Directory archives cannot contain symbolic links")
            safe_path, _target = _safe_relative_path(repo, recorded_path)
            if normalized and not safe_path.startswith(f"{normalized}/"):
                raise HTTPException(status_code=422, detail="Directory archive path escaped its prefix")
            try:
                size = int(fields[3])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Directory contains an unsupported Git entry") from exc
            file_count += 1
            total_size += size
            if file_count > PROJECT_DIRECTORY_ARCHIVE_MAX_FILES:
                raise HTTPException(status_code=413, detail="Directory contains too many files to download")
            if total_size > PROJECT_DIRECTORY_ARCHIVE_MAX_BYTES:
                raise HTTPException(status_code=413, detail="Directory is too large to download")
        if file_count == 0:
            raise HTTPException(status_code=404, detail="Project directory has no committed files")
        archive_stem = PurePosixPath(normalized).name if normalized else f"project-{str(project.id)[:8]}"
        return {
            "path": normalized,
            "head": commit,
            "name": f"{archive_stem}.zip",
            "file_count": file_count,
            "total_size": total_size,
        }


async def inspect_project_directory(
    project: Project,
    path: str = "",
    revision: str = "HEAD",
) -> dict[str, Any]:
    return await asyncio.to_thread(_directory_snapshot, project, path, revision)


def iter_project_directory_archive(
    project: Project,
    revision: str,
    path: str = "",
    *,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    """Stream a validated immutable Git tree as ZIP without staging files."""

    repo = _repo_for(project)
    # Store mode keeps CPU and output expansion predictable after the
    # preflight uncompressed-size limit.
    args = ["git", "-c", f"safe.directory={repo}", "-C", str(repo), "archive", "--format=zip", "-0", revision]
    if path:
        args.extend(["--", path])
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert process.stdout is not None
        while chunk := process.stdout.read(chunk_size):
            yield chunk
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=5)
        if return_code != 0:
            raise RuntimeError("Git directory archive failed")




__all__ = [name for name in globals() if not name.startswith("__")]
