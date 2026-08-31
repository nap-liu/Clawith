from project_actions_support import *  # noqa: F401,F403

async def test_file_commits_restore_branch_and_milestone_preserve_history(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Git evidence")
    project_id = project["id"]

    initial_git = await env.client.get(f"/api/projects/{project_id}/git")
    assert initial_git.status_code == 200, initial_git.text
    initial_head = initial_git.json()["head"]
    assert {"README.md", "PROJECT.json"} <= set(initial_git.json()["files"])

    write_response = await env.client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "deliverables/report.md", "content": "# Evidence\n\nversion one\n"},
    )
    assert write_response.status_code == 200, write_response.text
    write_commit = write_response.json()["commit"]
    assert write_commit != initial_head

    diff_response = await env.client.get(
        f"/api/projects/{project_id}/git/diff",
        params={"commit": write_commit, "path": "deliverables/report.md"},
    )
    assert diff_response.status_code == 200, diff_response.text
    diff_payload = diff_response.json()
    assert diff_payload["parent_commit"] == initial_head
    assert diff_payload["files"][0]["path"] == "deliverables/report.md"
    assert diff_payload["files"][0]["original_content"] == ""
    assert diff_payload["files"][0]["modified_content"].startswith("# Evidence")

    unsafe_diff = await env.client.get(
        f"/api/projects/{project_id}/git/diff",
        params={"commit": write_commit, "path": "../outside.md"},
    )
    assert unsafe_diff.status_code == 422
    env.authenticate_as(env.viewer_id)
    hidden_diff = await env.client.get(
        f"/api/projects/{project_id}/git/diff",
        params={"commit": write_commit},
    )
    assert hidden_diff.status_code == 404
    env.authenticate_as(env.owner_id)

    for unsafe_path in ("../outside.md", ".git/config", ".GIT/config", "deliverables//hidden.md"):
        rejected = await env.client.put(
            f"/api/projects/{project_id}/files",
            json={"path": unsafe_path, "content": "must not be written"},
        )
        assert rejected.status_code == 422, rejected.text

    unchanged = await env.client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "deliverables/report.md", "content": "# Evidence\n\nversion one\n"},
    )
    assert unchanged.status_code == 409

    files_response = await env.client.get(f"/api/projects/{project_id}/files")
    assert files_response.status_code == 200, files_response.text
    files_payload = files_response.json()
    files = files_payload["files"] if isinstance(files_payload, dict) else files_payload
    assert any(
        (item == "deliverables/report.md") or (isinstance(item, dict) and item.get("path") == "deliverables/report.md")
        for item in files
    )

    milestone = await env.client.post(
        f"/api/projects/{project_id}/git/commit",
        json={"message": "Acceptance milestone", "milestone": True},
    )
    assert milestone.status_code == 200, milestone.text
    milestone_commit = milestone.json()["commit"]

    restore = await env.client.post(
        f"/api/projects/{project_id}/git/restore",
        json={"commit": initial_head, "message": "Restore initial project tree"},
    )
    assert restore.status_code == 200, restore.text
    restore_commit = restore.json()["commit"]
    assert restore_commit not in {initial_head, write_commit, milestone_commit}

    branch = await env.client.post(
        f"/api/projects/{project_id}/git/branches",
        json={"name": "review/version-one", "from_commit": write_commit},
    )
    assert branch.status_code == 200, branch.text
    assert branch.json()["from_commit"] == write_commit

    final_git = (await env.client.get(f"/api/projects/{project_id}/git?limit=20")).json()
    commit_ids = {item["commit"] for item in final_git["commits"]}
    assert {initial_head, write_commit, milestone_commit, restore_commit} <= commit_ids
    assert "review/version-one" in final_git["branches"]

    # Prove the old commits still resolve in the real repository: restore is a
    # new commit, never reset/force-push history rewriting.
    repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
    for commit in (initial_head, write_commit, milestone_commit, restore_commit):
        resolved = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
            check=False,
            capture_output=True,
        )
        assert resolved.returncode == 0

    events = (await env.client.get(f"/api/projects/{project_id}/events?limit=200")).json()
    event_types = {event["event_type"] for event in events}
    assert {
        "project.file.committed",
        "git.milestone.created",
        "git.restore_commit.created",
        "git.branch.created",
    } <= event_types

    db_events = (
        (await env.db.execute(select(ProjectEvent).where(ProjectEvent.project_id == uuid.UUID(project_id))))
        .scalars()
        .all()
    )
    assert len(db_events) == len(events)
    assert any(event.event_metadata.get("commit") == restore_commit for event in db_events)

async def test_project_head_file_preview_media_range_and_acl(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="HEAD file previews")
    project_id = project["id"]
    repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
    (repo / "assets").mkdir()
    binary = b"\x00\x01\x02\x03\x04\x05\xff\x10"
    (repo / "assets" / "sample.mp4").write_bytes(binary)
    (repo / "large.txt").write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "--", "assets/sample.mp4", "large.txt"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "Add preview fixtures"],
        check=True,
        capture_output=True,
    )

    env.authenticate_as(env.viewer_id)
    hidden = await env.client.get(
        f"/api/projects/{project_id}/files/content",
        params={"path": "assets/sample.mp4"},
    )
    assert hidden.status_code == 404

    env.authenticate_as(env.owner_id)
    for unsafe_path in ("../README.md", ".git/config", "nested/.GIT/config", "assets//sample.mp4"):
        rejected = await env.client.get(
            f"/api/projects/{project_id}/files/content",
            params={"path": unsafe_path},
        )
        assert rejected.status_code == 422, rejected.text

    listing = (await env.client.get(f"/api/projects/{project_id}/files")).json()
    media_item = next(item for item in listing if item["path"] == "assets/sample.mp4")
    assert media_item["kind"] == "video"
    assert media_item["mime_type"] == "video/mp4"
    assert media_item["preview"] == ""
    assert media_item["is_editable"] is False
    large_item = next(item for item in listing if item["path"] == "large.txt")
    assert large_item["kind"] == "text"
    assert large_item["is_editable"] is False

    media = await env.client.get(
        f"/api/projects/{project_id}/files/content",
        params={"path": "assets/sample.mp4"},
    )
    assert media.status_code == 200, media.text
    media_payload = media.json()
    assert media_payload["content"] is None
    assert media_payload["raw_url"].startswith(f"/api/projects/{project_id}/files/raw?")

    partial = await env.client.get(media_payload["raw_url"], headers={"Range": "bytes=2-5"})
    assert partial.status_code == 206, partial.text
    assert partial.content == binary[2:6]
    assert partial.headers["content-type"] == "video/mp4"
    assert partial.headers["content-range"] == f"bytes 2-5/{len(binary)}"
    assert partial.headers["accept-ranges"] == "bytes"
    assert partial.headers["content-disposition"].startswith("inline;")

    downloaded = await env.client.get(media_payload["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.content == binary
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    raw_head = await env.client.head(media_payload["raw_url"])
    assert raw_head.status_code == 200
    assert raw_head.content == b""
    assert raw_head.headers["content-length"] == str(len(binary))
    invalid_range = await env.client.get(media_payload["raw_url"], headers={"Range": "bytes=99-100"})
    assert invalid_range.status_code == 416
    assert invalid_range.headers["content-range"] == f"bytes */{len(binary)}"

    large = await env.client.get(
        f"/api/projects/{project_id}/files/content",
        params={"path": "large.txt", "max_chars": 64},
    )
    assert large.status_code == 200
    assert large.json()["content"] == "x" * 64
    assert large.json()["truncated"] is True
    assert large.json()["is_editable"] is False

    shared = await env.client.patch(
        f"/api/projects/{project_id}",
        json={
            "visibility": "shared",
            "shared_with_user_ids": [str(env.viewer_id)],
        },
    )
    assert shared.status_code == 200
    env.authenticate_as(env.viewer_id)
    viewer_content = await env.client.get(
        f"/api/projects/{project_id}/files/content",
        params={"path": "assets/sample.mp4"},
    )
    assert viewer_content.status_code == 200

    env.authenticate_as(env.owner_id)
    revoked = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "private", "shared_with_user_ids": []},
    )
    assert revoked.status_code == 200
    # Raw tickets are re-authorized on every request, not bearer URLs that
    # outlive revoked project access.
    old_viewer_raw = await env.client.get(viewer_content.json()["raw_url"])
    assert old_viewer_raw.status_code == 404

async def test_project_directory_archive_and_html_preview_use_one_immutable_head(project_api: ProjectApiEnv):
    env = project_api
    project = await _create_project(env, name="Immutable preview")
    project_id = project["id"]
    repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
    (repo / "site" / "assets").mkdir(parents=True)
    (repo / "site" / "index.html").write_text(
        '<!doctype html><link rel="stylesheet" href="./style.css"><script src="./app.js"></script>'
        '<img src="./assets/logo.svg">',
        encoding="utf-8",
    )
    (repo / "site" / "style.css").write_text("body { color: rgb(1, 2, 3); }\n", encoding="utf-8")
    (repo / "site" / "app.js").write_text("document.body.dataset.loaded = 'yes';\n", encoding="utf-8")
    (repo / "site" / "assets" / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="4" cx="4" cy="4"/></svg>',
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "--", "site"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "Add preview site"], check=True, capture_output=True)
    snapshot_head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "site" / "untracked-secret.txt").write_text("must-not-download", encoding="utf-8")

    archive_ticket = await env.client.get(
        f"/api/projects/{project_id}/files/archive",
        params={"path": "site"},
    )
    assert archive_ticket.status_code == 200, archive_ticket.text
    archive_payload = archive_ticket.json()
    assert archive_payload["head"] == snapshot_head
    assert archive_payload["file_count"] == 4

    html_content = await env.client.get(
        f"/api/projects/{project_id}/files/content",
        params={"path": "site/index.html"},
    )
    assert html_content.status_code == 200, html_content.text
    preview_url = html_content.json()["html_preview_url"]
    assert preview_url.startswith(f"/api/projects/{project_id}/files/preview/")

    # Change HEAD after both tickets were issued. Their resources must remain
    # an internally consistent snapshot rather than mixing new HEAD content.
    (repo / "site" / "style.css").write_text("body { color: red; }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "--", "site/style.css"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "Change preview style"], check=True, capture_output=True)

    archive = await env.client.get(archive_payload["download_url"])
    assert archive.status_code == 200, archive.text
    assert archive.headers["content-type"].startswith("application/zip")
    assert archive.headers["x-project-git-head"] == snapshot_head
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        assert set(bundle.namelist()) == {
            "site/",
            "site/app.js",
            "site/assets/",
            "site/assets/logo.svg",
            "site/index.html",
            "site/style.css",
        }
        assert bundle.read("site/style.css") == b"body { color: rgb(1, 2, 3); }\n"
        assert "site/untracked-secret.txt" not in bundle.namelist()

    preview = await env.client.get(preview_url)
    assert preview.status_code == 200, preview.text
    assert preview.headers["x-project-git-head"] == snapshot_head
    preview_csp = preview.headers["content-security-policy"]
    assert "sandbox allow-scripts" in preview_csp
    assert "connect-src 'none'" in preview_csp
    assert "allow-same-origin" not in preview_csp
    assert preview_url.split("/files/preview/", 1)[1].split("/", 1)[0] not in preview_csp
    assert len(preview_csp) < 2_048
    preview_base = preview_url.rsplit("/", 1)[0]
    css = await env.client.get(f"{preview_base}/style.css")
    script = await env.client.get(f"{preview_base}/app.js")
    image = await env.client.get(f"{preview_base}/assets/logo.svg")
    assert css.text == "body { color: rgb(1, 2, 3); }\n"
    assert css.headers["x-project-git-head"] == snapshot_head
    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
    assert image.status_code == 200 and image.headers["content-type"].startswith("image/svg+xml")

    tampered_archive = await env.client.get(archive_payload["download_url"].replace("path=site", "path=site%2Fassets"))
    assert tampered_archive.status_code == 401

    shared = await env.client.patch(
        f"/api/projects/{project_id}",
        json={
            "visibility": "shared",
            "shared_with_user_ids": [str(env.viewer_id)],
        },
    )
    assert shared.status_code == 200
    env.authenticate_as(env.viewer_id)
    viewer_archive = (
        await env.client.get(
            f"/api/projects/{project_id}/files/archive",
            params={"path": "site"},
        )
    ).json()["download_url"]
    viewer_preview = (
        await env.client.get(
            f"/api/projects/{project_id}/files/content",
            params={"path": "site/index.html"},
        )
    ).json()["html_preview_url"]
    env.authenticate_as(env.owner_id)
    revoked = await env.client.patch(
        f"/api/projects/{project_id}",
        json={"visibility": "private", "shared_with_user_ids": []},
    )
    assert revoked.status_code == 200
    assert (await env.client.get(viewer_archive)).status_code == 404
    assert (await env.client.get(viewer_preview)).status_code == 404

async def test_owner_manages_provider_neutral_remotes_and_atomically_clones(
    project_api: ProjectApiEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.services import project_git_service

    async def allow_local_git_fixture(raw_url: str) -> str:
        return project_git_service._safe_remote_url(raw_url)

    monkeypatch.setattr(project_git_service, "validate_project_remote_url", allow_local_git_fixture)
    env = project_api
    project = await _create_project(env, name="Clone target")
    project_id = project["id"]
    initial = (await env.client.get(f"/api/projects/{project_id}/git")).json()
    initial_repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
    initial_files = set(
        subprocess.run(
            ["git", "-C", str(initial_repo), "ls-tree", "-r", "--name-only", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )

    empty = await env.client.get(f"/api/projects/{project_id}/git/remotes")
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    configured = await env.client.put(
        f"/api/projects/{project_id}/git/remotes/upstream",
        json={"url": "https://example.com/vendor/project.git"},
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["created"] is True
    assert (await env.client.get(f"/api/projects/{project_id}/git")).json()["head"] == initial["head"]
    assert (await env.client.get(f"/api/projects/{project_id}/git/remotes")).json()["items"] == [
        {"name": "upstream", "url": "https://example.com/vendor/project.git"}
    ]
    ssh_remote = await env.client.put(
        f"/api/projects/{project_id}/git/remotes/ssh-upstream",
        json={"url": "ssh://git@example.com/vendor/project.git"},
    )
    assert ssh_remote.status_code == 200, ssh_remote.text
    assert ssh_remote.json()["url"] == "ssh://git@example.com/vendor/project.git"
    scp_remote = await env.client.put(
        f"/api/projects/{project_id}/git/remotes/scp-upstream",
        json={"url": "git@example.com:vendor/project.git"},
    )
    assert scp_remote.status_code == 200, scp_remote.text

    for name, url in [
        ("origin", "file:///tmp/repository.git"),
        ("origin", "/tmp/repository.git"),
        ("origin", "https://token@example.com/vendor/project.git"),
        ("origin", "ssh://git:secret@example.com/vendor/project.git"),
        ("origin", "ssh://git@example.com/vendor/$(touch-pwned).git"),
        ("origin", "ssh://git@example.com/vendor/repo.git;touch-pwned"),
        ("origin", "ssh://git@example.com/vendor/%24%28touch-pwned%29.git"),
        ("origin", "ssh://git@example.com/vendor/../private.git"),
        ("origin", "git@example.com:vendor/project.git;touch-pwned"),
        ("origin", "git@example.com:vendor/$(touch-pwned).git"),
        ("origin", "git@example.com:vendor/../private.git"),
        ("--upload-pack", "https://example.com/vendor/project.git"),
    ]:
        rejected = await env.client.put(
            f"/api/projects/{project_id}/git/remotes/{name}",
            json={"url": url},
        )
        assert rejected.status_code == 422, rejected.text

    env.authenticate_as(env.viewer_id)
    hidden = await env.client.get(f"/api/projects/{project_id}/git/remotes")
    assert hidden.status_code == 404
    env.authenticate_as(env.owner_id)
    deleted = await env.client.delete(f"/api/projects/{project_id}/git/remotes/upstream")
    assert deleted.status_code == 200, deleted.text
    deleted_ssh = await env.client.delete(f"/api/projects/{project_id}/git/remotes/ssh-upstream")
    assert deleted_ssh.status_code == 200, deleted_ssh.text
    deleted_scp = await env.client.delete(f"/api/projects/{project_id}/git/remotes/scp-upstream")
    assert deleted_scp.status_code == 200, deleted_scp.text

    source = tmp_path / "source"
    bare = tmp_path / "served" / "source.git"
    source.mkdir()
    subprocess.run(["git", "-C", str(source), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Source Author"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "source@example.test"], check=True)
    (source / "SOURCE.md").write_text("# Imported source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "--", "SOURCE.md"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "Imported baseline"], check=True)
    bare.parent.mkdir()
    subprocess.run(["git", "clone", "--bare", "--", str(source), str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "--git-dir", str(bare), "update-server-info"], check=True)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(bare.parent),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _attempt in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            raise AssertionError("local Git HTTP fixture did not start")
        clone_url = f"http://127.0.0.1:{port}/source.git"

        # A prepared journal survives the exact process-exit window after the
        # filesystem swap and restores the initialization repository on access.
        from app.models.project import ProjectRepositoryOperation

        model = await env.db.get(Project, uuid.UUID(project_id))
        assert model is not None
        staged_only = await project_git_service.begin_project_repository_clone(model, clone_url, "main")
        env.db.add(
            ProjectRepositoryOperation(
                id=staged_only.id,
                tenant_id=model.tenant_id,
                project_id=model.id,
                operation_type="clone",
                state="prepared",
                old_head=staged_only.old_head,
                new_head=staged_only.new_head,
                backup_name=staged_only.backup.name,
                staging_name=staged_only.staging_root.name,
            )
        )
        await env.db.commit()
        assert staged_only.staging_root.exists()
        assert staged_only.lock_handle is not None
        staged_only.lock_handle.close()  # Simulate exit before the filesystem swap.
        staged_only.lock_handle = None
        assert (
            await project_git_service.reconcile_project_repository_operations(
                model.id,
                db=env.db,
            )
            == 1
        )
        await env.db.commit()
        assert not staged_only.staging_root.exists()
        assert not (staged_only.repo / "SOURCE.md").exists()

        abandoned = await project_git_service.begin_project_repository_clone(model, clone_url, "main")
        env.db.add(
            ProjectRepositoryOperation(
                id=abandoned.id,
                tenant_id=model.tenant_id,
                project_id=model.id,
                operation_type="clone",
                state="prepared",
                old_head=abandoned.old_head,
                new_head=abandoned.new_head,
                backup_name=abandoned.backup.name,
                staging_name=abandoned.staging_root.name,
            )
        )
        await env.db.commit()
        await project_git_service.apply_project_repository_clone(abandoned)
        assert (abandoned.repo / "SOURCE.md").exists()
        assert abandoned.lock_handle is not None
        abandoned.lock_handle.close()  # Simulate OS releasing flock on process death.
        abandoned.lock_handle = None
        assert (
            await project_git_service.reconcile_project_repository_operations(
                model.id,
                db=env.db,
            )
            == 1
        )
        await env.db.commit()
        assert not (abandoned.repo / "SOURCE.md").exists()
        assert list(abandoned.repo.parent.glob(".repo-backup-*")) == []
        assert (await env.db.execute(select(ProjectRepositoryOperation))).scalars().all() == []

        # The repository swap stays compensatable until settings and the
        # audit row are durable. A database failure restores the generated
        # initialization baseline and leaves the endpoint safely retryable.
        original_commit = AsyncSession.commit
        clone_commit_count = {"value": 0}

        async def fail_clone_commit_once(self: AsyncSession):
            if self is env.db:
                clone_commit_count["value"] += 1
            if self is env.db and clone_commit_count["value"] == 2:
                raise RuntimeError("forced clone metadata commit failure")
            return await original_commit(self)

        monkeypatch.setattr(AsyncSession, "commit", fail_clone_commit_once)
        with pytest.raises(RuntimeError, match="forced clone metadata commit failure"):
            await env.client.post(
                f"/api/projects/{project_id}/git/clone",
                json={"url": clone_url, "branch": "main"},
            )
        monkeypatch.setattr(AsyncSession, "commit", original_commit)

        restored_repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
        restored_files = subprocess.run(
            ["git", "-C", str(restored_repo), "ls-tree", "-r", "--name-only", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        assert set(restored_files) == initial_files
        assert not (restored_repo / "SOURCE.md").exists()
        assert (
            subprocess.run(
                ["git", "-C", str(restored_repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == initial["head"]
        )
        assert list(restored_repo.parent.glob(".repo-backup-*")) == []
        failed_settings = (await env.client.get(f"/api/projects/{project_id}/settings")).json()
        assert failed_settings["git"].get("source") != "cloned"
        failed_events = (await env.client.get(f"/api/projects/{project_id}/events?limit=200")).json()
        assert "git.repository.cloned" not in {event["event_type"] for event in failed_events}

        # A transport error after the database accepted COMMIT must not roll
        # the repository back to a state that contradicts durable metadata.
        tenant = await env.db.get(Tenant, env.tenant_id)
        assert tenant is not None
        ambiguous_leader = await _agent(env.db, tenant, env.owner, "Clone Leader", "Drive clone recovery")
        await env.db.commit()
        ambiguous_project = await _create_project(
            env,
            name="Ambiguous clone commit",
            leader_id=ambiguous_leader.id,
        )
        ambiguous_project_id = ambiguous_project["id"]
        ambiguous_commit_count = {"value": 0}

        async def commit_then_report_disconnect(self: AsyncSession):
            if self is env.db:
                ambiguous_commit_count["value"] += 1
            if self is env.db and ambiguous_commit_count["value"] == 2:
                await original_commit(self)
                raise RuntimeError("simulated disconnect after metadata commit")
            return await original_commit(self)

        monkeypatch.setattr(AsyncSession, "commit", commit_then_report_disconnect)
        with pytest.raises(RuntimeError, match="simulated disconnect after metadata commit"):
            await env.client.post(
                f"/api/projects/{ambiguous_project_id}/git/clone",
                json={"url": clone_url, "branch": "main"},
            )
        monkeypatch.setattr(AsyncSession, "commit", original_commit)
        ambiguous_repo = project_repo_path(env.tenant_id, uuid.UUID(ambiguous_project_id))
        assert (ambiguous_repo / "SOURCE.md").exists()
        ambiguous_settings = (await env.client.get(f"/api/projects/{ambiguous_project_id}/settings")).json()
        assert ambiguous_settings["git"]["source"] == "cloned"
        ambiguous_events = (await env.client.get(f"/api/projects/{ambiguous_project_id}/events?limit=200")).json()
        assert "git.repository.cloned" in {event["event_type"] for event in ambiguous_events}
        ambiguous_journal = (
            await env.db.execute(
                select(ProjectRepositoryOperation).where(
                    ProjectRepositoryOperation.project_id == uuid.UUID(ambiguous_project_id)
                )
            )
        ).scalar_one()
        assert ambiguous_journal.state == "committed"
        recovered_ambiguous_git = await env.client.get(f"/api/projects/{ambiguous_project_id}/git")
        assert recovered_ambiguous_git.status_code == 200
        assert (
            await env.db.execute(
                select(ProjectRepositoryOperation).where(
                    ProjectRepositoryOperation.project_id == uuid.UUID(ambiguous_project_id)
                )
            )
        ).scalars().all() == []

        original_finalize = projects_api.finalize_project_repository_clone

        async def simulate_exit_after_metadata_commit(_operation):
            assert _operation.lock_handle is not None
            _operation.lock_handle.close()  # Process exit releases the kernel flock.
            _operation.lock_handle = None
            raise RuntimeError("simulated exit before clone backup cleanup")

        monkeypatch.setattr(projects_api, "finalize_project_repository_clone", simulate_exit_after_metadata_commit)
        cloned = await env.client.post(
            f"/api/projects/{project_id}/git/clone",
            json={"url": clone_url, "branch": "main"},
        )
        assert cloned.status_code == 200, cloned.text
        assert cloned.json()["operation"] == "clone"
        assert cloned.json()["default_branch"] == "main"
        imported_repo = project_repo_path(env.tenant_id, uuid.UUID(project_id))
        assert (imported_repo / "SOURCE.md").read_text(encoding="utf-8") == "# Imported source\n"
        committed_journal = (await env.db.execute(select(ProjectRepositoryOperation))).scalar_one()
        assert committed_journal.state == "committed"
        assert list(imported_repo.parent.glob(".repo-backup-*"))
        monkeypatch.setattr(projects_api, "finalize_project_repository_clone", original_finalize)
        recovered_git = await env.client.get(f"/api/projects/{project_id}/git")
        assert recovered_git.status_code == 200, recovered_git.text
        assert recovered_git.json()["head"] == cloned.json()["head"]
        assert (await env.db.execute(select(ProjectRepositoryOperation))).scalars().all() == []
        assert list(imported_repo.parent.glob(".repo-backup-*")) == []
        settings = (await env.client.get(f"/api/projects/{project_id}/settings")).json()
        assert settings["git"]["mode"] == "managed"
        assert settings["git"]["repository_mode"] == "managed"
        assert settings["git"]["source"] == "cloned"
        assert settings["git"]["remotes"] == [
            {
                "name": "origin",
                "url_sha256": hashlib.sha256(clone_url.encode("utf-8")).hexdigest(),
            }
        ]
        assert settings["git"]["remote_count"] == 1
        second_clone = await env.client.post(
            f"/api/projects/{project_id}/git/clone",
            json={"url": clone_url, "branch": "main"},
        )
        assert second_clone.status_code == 409

        running_model = await env.db.get(Project, uuid.UUID(project_id))
        assert running_model is not None
        running_model.status = "running"
        await env.db.commit()
        running_clone = await env.client.post(
            f"/api/projects/{project_id}/git/clone",
            json={"url": clone_url, "branch": "main"},
        )
        assert running_clone.status_code == 409
        assert "planning" in running_clone.json()["detail"]
    finally:
        server.terminate()
        server.wait(timeout=5)

    events = (await env.client.get(f"/api/projects/{project_id}/events?limit=200")).json()
    assert {
        "git.remote.configured",
        "git.remote.deleted",
        "git.repository.cloned",
    } <= {event["event_type"] for event in events}

    configured_event = next(
        event
        for event in events
        if event["event_type"] == "git.remote.configured" and event["event_metadata"]["remote_name"] == "upstream"
    )
    assert configured_event["event_metadata"]["remote_name"] == "upstream"
    assert (
        configured_event["event_metadata"]["remote_url_sha256"]
        == hashlib.sha256(b"https://example.com/vendor/project.git").hexdigest()
    )
    cloned_event = next(event for event in events if event["event_type"] == "git.repository.cloned")
    assert cloned_event["event_metadata"]["remote_name"] == "origin"
    assert cloned_event["event_metadata"]["remote_url_sha256"] == hashlib.sha256(clone_url.encode("utf-8")).hexdigest()

    # Audit events are visible to explicitly shared viewers, so no Git event
    # may disclose a provider URL or the remote response object.
    shared = await env.client.patch(
        f"/api/projects/{project_id}",
        json={
            "visibility": "shared",
            "shared_with_user_ids": [str(env.viewer_id)],
        },
    )
    assert shared.status_code == 200, shared.text
    env.authenticate_as(env.viewer_id)
    viewer_events_response = await env.client.get(f"/api/projects/{project_id}/events?limit=200")
    assert viewer_events_response.status_code == 200, viewer_events_response.text
    viewer_git_events = [event for event in viewer_events_response.json() if event["event_type"].startswith("git.")]
    serialized_events = json.dumps(viewer_git_events, sort_keys=True)
    for private_url in (
        "https://example.com/vendor/project.git",
        "ssh://git@example.com/vendor/project.git",
        "git@example.com:vendor/project.git",
        clone_url,
    ):
        assert private_url not in serialized_events
    assert viewer_git_events
    assert all("remote" not in event["event_metadata"] for event in viewer_git_events)
    assert all("url" not in event["event_metadata"] for event in viewer_git_events)
