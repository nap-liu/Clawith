"""File management API routes for agent workspaces."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File as FastFile, HTTPException, Request, UploadFile as UploadFileType, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.user import User
from app.services.agent_runtime_workspace import (
    bind_agent_runtime_workspace,
    current_agent_runtime_workspace,
    project_agent_runtime_workspace,
    standard_agent_runtime_workspace,
)
from app.services.storage import ensure_local_path, get_storage_backend, guess_content_type, normalize_storage_key
from app.services.storage_runtime.base import StorageEntry
from app.api import files_enterprise_ops, files_playback_ops, files_route_ops, files_workspace_support

settings = get_settings()
router = APIRouter(prefix="/agents/{agent_id}/files", tags=["files"])
upload_router = APIRouter(prefix="/agents/{agent_id}/files", tags=["files"])
enterprise_kb_router = APIRouter(prefix="/enterprise/knowledge-base", tags=["enterprise"])
MODULE = sys.modules[__name__]

CREATOR_ONLY_FILES = {"secrets.md"}
DEFAULT_UPLOAD_DIR = "workspace/uploads"


class FileInfo(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int = 0
    modified_at: str = ""
    version_token: str | None = None
    url: str | None = None


class FileContent(BaseModel):
    path: str
    content: str
    version_token: str | None = None


class FileWrite(BaseModel):
    content: str
    autosave: bool = False
    session_id: str | None = None
    expected_version_token: str | None = None


class FileLockBody(BaseModel):
    path: str
    session_id: str | None = None


class RestoreRevisionBody(BaseModel):
    revision_id: uuid.UUID
    expected_version_token: str | None = None


class PlaybackTicketBody(BaseModel):
    path: str
    message_id: uuid.UUID


class ImportSkillBody(BaseModel):
    skill_id: str


class ClawhubImportBody(BaseModel):
    slug: str


class UrlImportBody(BaseModel):
    url: str


TEXT_PREVIEW_EXTENSIONS = {
    ".bat", ".bash", ".c", ".cfg", ".clj", ".cpp", ".cs", ".css", ".dart", ".env", ".go",
    ".h", ".hpp", ".ini", ".java", ".js", ".jsx", ".kt", ".kts", ".less", ".lua", ".m",
    ".mm", ".php", ".pl", ".pm", ".properties", ".py", ".r", ".rb", ".rs", ".sass", ".scala",
    ".scss", ".sh", ".sql", ".swift", ".toml", ".ts", ".tsx", ".vue", ".xml", ".yaml", ".yml", ".zsh",
}
TEXT_PREVIEW_FILENAMES = {
    ".dockerignore", ".env", ".env.example", ".gitignore", ".npmrc", ".prettierrc", "dockerfile", "makefile",
}


def _agent_base_dir(agent_id: uuid.UUID) -> Path:
    return current_agent_runtime_workspace(agent_id).local_root


def _agent_storage_key(agent_id: uuid.UUID, rel_path: str = "") -> str:
    return current_agent_runtime_workspace(agent_id).storage_key(rel_path)


async def _bind_file_workspace(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent, _access = await check_agent_access(db, current_user, agent_id)
    workspace = _runtime_workspace(agent)
    with bind_agent_runtime_workspace(workspace):
        yield agent


async def _resolve_workspace_agent(
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID,
    candidate: object,
) -> Agent:
    return await files_workspace_support.resolve_workspace_agent_impl(
        MODULE,
        db,
        current_user,
        agent_id,
        candidate,
    )


def _runtime_workspace(agent: Agent):
    return files_workspace_support.runtime_workspace_impl(MODULE, agent)


async def _record_project_skill_change(
    db: AsyncSession,
    agent: Agent,
    current_user: User,
    path: str,
) -> None:
    await files_workspace_support.record_project_skill_change_impl(
        MODULE,
        db,
        agent,
        current_user,
        path,
    )


async def _delete_bound_project_skill(
    db: AsyncSession,
    agent: Agent,
    current_user: User,
    path: str,
):
    return await files_workspace_support.delete_bound_project_skill_impl(
        MODULE,
        db,
        agent,
        current_user,
        path,
    )


def _safe_path(agent_id: uuid.UUID, rel_path: str) -> Path:
    return files_workspace_support.safe_path_impl(MODULE, agent_id, rel_path)


def _visible_path(agent_id: uuid.UUID, rel_path: str, tenant_id: uuid.UUID | None):
    return files_workspace_support.visible_path_impl(MODULE, agent_id, rel_path, tenant_id)


def _is_enterprise_visible_path(rel_path: str) -> bool:
    return files_workspace_support.is_enterprise_visible_path_impl(rel_path)


def _visible_storage_key(
    agent_id: uuid.UUID,
    rel_path: str,
    tenant_id: uuid.UUID | None,
    *,
    workspace=None,
):
    return files_workspace_support.visible_storage_key_impl(
        MODULE,
        agent_id,
        rel_path,
        tenant_id,
        workspace=workspace,
    )


async def _require_agent_file_delete_access(
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID,
) -> None:
    await files_workspace_support.require_agent_file_delete_access_impl(
        MODULE,
        db,
        current_user,
        agent_id,
    )


def _entry_version_token(entry: StorageEntry) -> str | None:
    return files_workspace_support.entry_version_token_impl(entry)


def _file_kind(path: str) -> str:
    return files_workspace_support.file_kind_impl(MODULE, path)


def _find_companion_text_preview(target: Path) -> Path | None:
    return files_workspace_support.find_companion_text_preview_impl(target)


def _extract_document_text(target: Path, kind: str) -> str:
    return files_workspace_support.extract_document_text_impl(target, kind)


def _detect_csv_delimiter(text: str) -> str:
    return files_workspace_support.detect_csv_delimiter_impl(text)


def _parse_csv_rows(text: str) -> list[list[str]]:
    return files_workspace_support.parse_csv_rows_impl(MODULE, text)


def _playback_headers() -> dict[str, str]:
    return files_playback_ops.playback_headers_impl()


def _storage_entry_version_token(entry: StorageEntry) -> str:
    return files_playback_ops.storage_entry_version_token_impl(entry)


def _message_references_media_path(message: ChatMessage, path: str) -> bool:
    return files_playback_ops.message_references_media_path_impl(message, path)


async def _authorize_message_media_path(
    db: AsyncSession,
    user: User,
    agent_id: uuid.UUID,
    message_id: str,
    path: str,
) -> None:
    await files_playback_ops.authorize_message_media_path_impl(
        MODULE,
        db,
        user,
        agent_id,
        message_id,
        path,
    )


def _parse_media_range(value: str | None, size: int) -> tuple[int, int, bool]:
    return files_playback_ops.parse_media_range_impl(value, size)


async def _authorize_playback_ticket(
    *,
    agent_id: uuid.UUID,
    ticket_id: str,
    signature: str,
    cookie_token: str | None,
    db: AsyncSession,
):
    return await files_playback_ops.authorize_playback_ticket_impl(
        MODULE,
        agent_id=agent_id,
        ticket_id=ticket_id,
        signature=signature,
        cookie_token=cookie_token,
        db=db,
    )


def _enterprise_kb_dir(tenant_id: str) -> Path:
    return files_enterprise_ops.enterprise_kb_dir_impl(MODULE, tenant_id)


def _enterprise_info_dir(tenant_id: str) -> Path:
    return files_enterprise_ops.enterprise_info_dir_impl(MODULE, tenant_id)


def _enterprise_storage_key(tenant_id: str, rel_path: str = "") -> str:
    return files_enterprise_ops.enterprise_storage_key_impl(MODULE, tenant_id, rel_path)


@router.get("/", response_model=list[FileInfo])
async def list_files(
    agent_id: uuid.UUID,
    path: str = "",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """List files and directories in an agent's file system."""
    return await files_route_ops.list_files_impl(
        MODULE,
        agent_id=agent_id,
        path=path,
        current_user=current_user,
        db=db,
        workspace_agent=workspace_agent,
    )


@router.get("/content", response_model=FileContent)
async def read_file(
    agent_id: uuid.UUID,
    path: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """Read the content of a file."""
    return await files_route_ops.read_file_impl(
        MODULE,
        agent_id=agent_id,
        path=path,
        current_user=current_user,
        db=db,
        workspace_agent=workspace_agent,
    )


@router.get("/preview")
async def preview_file(
    agent_id: uuid.UUID,
    path: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """Return a browser-friendly preview payload for Workspace files."""
    return await files_route_ops.preview_file_impl(
        MODULE,
        agent_id=agent_id,
        path=path,
        current_user=current_user,
        db=db,
    )


@router.post("/playback-ticket")
async def create_media_playback_ticket(
    agent_id: uuid.UUID,
    body: PlaybackTicketBody,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a fresh, cookie-bound URL for one audio/video playback."""
    return await files_playback_ops.create_media_playback_ticket_impl(
        MODULE,
        agent_id=agent_id,
        body=body,
        request=request,
        current_user=current_user,
        db=db,
    )


@router.get("/playback/{ticket_id}/status")
async def get_media_playback_status(
    agent_id: uuid.UUID,
    ticket_id: str,
    signature: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await files_playback_ops.get_media_playback_status_impl(
        MODULE,
        agent_id=agent_id,
        ticket_id=ticket_id,
        signature=signature,
        request=request,
        db=db,
    )


@router.api_route("/playback/{ticket_id}", methods=["GET", "HEAD"])
async def stream_media_playback(
    agent_id: uuid.UUID,
    ticket_id: str,
    signature: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await files_playback_ops.stream_media_playback_impl(
        MODULE,
        agent_id=agent_id,
        ticket_id=ticket_id,
        signature=signature,
        request=request,
        db=db,
    )


@router.get("/download")
async def download_file(
    agent_id: uuid.UUID,
    path: str,
    token: str = "",
    inline: bool = False,
    credentials: HTTPAuthorizationCredentials | None = Depends(HTTPBearer(auto_error=False)),
    db: AsyncSession = Depends(get_db),
):
    """Download / serve a file from the agent workspace (browser-friendly).

    Auth via Bearer header OR `token` query parameter (for <img> tags).
    """
    return await files_route_ops.download_file_impl(
        MODULE,
        agent_id=agent_id,
        path=path,
        token=token,
        inline=inline,
        credentials=credentials,
        db=db,
    )


@router.put("/content")
async def write_file(
    agent_id: uuid.UUID,
    path: str,
    data: FileWrite,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """Write content to a file (create or overwrite)."""
    return await files_route_ops.write_file_impl(
        MODULE,
        agent_id=agent_id,
        path=path,
        data=data,
        current_user=current_user,
        db=db,
        workspace_agent=workspace_agent,
    )


@router.post("/locks")
async def lock_file(
    agent_id: uuid.UUID,
    data: FileLockBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """Acquire or refresh a short-lived human editing lock for a file."""
    return await files_route_ops.lock_file_impl(
        MODULE,
        agent_id=agent_id,
        data=data,
        current_user=current_user,
        db=db,
    )


@router.delete("/locks")
async def unlock_file(
    agent_id: uuid.UUID,
    path: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """Release the current user's edit lock for a file."""
    return await files_route_ops.unlock_file_impl(
        MODULE,
        agent_id=agent_id,
        path=path,
        current_user=current_user,
        db=db,
    )


@router.get("/revisions")
async def get_file_revisions(
    agent_id: uuid.UUID,
    path: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """List version history for the currently opened Workspace file."""
    return await files_route_ops.get_file_revisions_impl(
        MODULE,
        agent_id=agent_id,
        path=path,
        current_user=current_user,
        db=db,
    )


@router.post("/restore")
async def restore_file_revision(
    agent_id: uuid.UUID,
    data: RestoreRevisionBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """Restore a file to a previous revision's after-content."""
    return await files_route_ops.restore_file_revision_impl(
        MODULE,
        agent_id=agent_id,
        data=data,
        current_user=current_user,
        db=db,
        workspace_agent=_workspace_agent,
    )


@router.delete("/content")
async def delete_file(
    agent_id: uuid.UUID,
    path: str,
    expected_version_token: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """Delete a file."""
    return await files_route_ops.delete_file_impl(
        MODULE,
        agent_id=agent_id,
        path=path,
        expected_version_token=expected_version_token,
        current_user=current_user,
        db=db,
        workspace_agent=workspace_agent,
    )


@router.post("/import-skill")
async def import_skill_to_agent(
    agent_id: uuid.UUID,
    body: ImportSkillBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """Import a global skill into this agent's skills/ workspace folder.

    Copies all files from the global skill registry into
    <agent_workspace>/skills/<folder_name>/.
    """
    return await files_route_ops.import_skill_to_agent_impl(
        MODULE,
        agent_id=agent_id,
        body=body,
        current_user=current_user,
        db=db,
        workspace_agent=workspace_agent,
    )


@upload_router.post("/upload")
async def upload_file_to_workspace(
    agent_id: uuid.UUID,
    file: UploadFileType = FastFile(...),
    path: str = "workspace/knowledge_base",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """Upload a binary file to agent workspace."""
    return await files_route_ops.upload_file_to_workspace_impl(
        MODULE,
        agent_id=agent_id,
        file=file,
        path=path,
        current_user=current_user,
        db=db,
    )


@enterprise_kb_router.get("/files")
async def list_enterprise_kb_files(
    path: str = "",
    current_user: User = Depends(get_current_user),
):
    """List files in enterprise knowledge base (tenant-scoped)."""
    return await files_enterprise_ops.list_enterprise_kb_files_impl(
        MODULE,
        path=path,
        current_user=current_user,
    )


@enterprise_kb_router.post("/upload")
async def upload_enterprise_kb_file(
    file: UploadFileType = FastFile(...),
    sub_path: str = "",
    current_user: User = Depends(get_current_user),
):
    """Upload a file to enterprise knowledge base (tenant-scoped)."""
    return await files_enterprise_ops.upload_enterprise_kb_file_impl(
        MODULE,
        file=file,
        sub_path=sub_path,
        current_user=current_user,
    )


@enterprise_kb_router.get("/content")
async def read_enterprise_file(
    path: str,
    current_user: User = Depends(get_current_user),
):
    """Read content of an enterprise knowledge base file (tenant-scoped)."""
    return await files_enterprise_ops.read_enterprise_file_impl(
        MODULE,
        path=path,
        current_user=current_user,
    )


@enterprise_kb_router.put("/content")
async def write_enterprise_file(
    path: str,
    data: FileWrite,
    current_user: User = Depends(get_current_user),
):
    """Write content to an enterprise file (tenant-scoped)."""
    return await files_enterprise_ops.write_enterprise_file_impl(
        MODULE,
        path=path,
        data=data,
        current_user=current_user,
    )


@enterprise_kb_router.delete("/content")
async def delete_enterprise_file(
    path: str,
    current_user: User = Depends(get_current_user),
):
    """Delete an enterprise knowledge base file (tenant-scoped)."""
    return await files_enterprise_ops.delete_enterprise_file_impl(
        MODULE,
        path=path,
        current_user=current_user,
    )


@router.post("/import-from-clawhub")
async def agent_import_from_clawhub(
    agent_id: uuid.UUID,
    body: ClawhubImportBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """Import a skill from ClawHub directly into this agent's skills/ workspace."""
    return await files_enterprise_ops.agent_import_from_clawhub_impl(
        MODULE,
        agent_id=agent_id,
        body=body,
        current_user=current_user,
        db=db,
        workspace_agent=workspace_agent,
    )


@router.post("/import-from-url")
async def agent_import_from_url(
    agent_id: uuid.UUID,
    body: UrlImportBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    workspace_agent: Agent = Depends(_bind_file_workspace),
):
    """Import a skill from a GitHub URL directly into this agent's skills/ workspace."""
    return await files_enterprise_ops.agent_import_from_url_impl(
        MODULE,
        agent_id=agent_id,
        body=body,
        current_user=current_user,
        db=db,
        workspace_agent=workspace_agent,
    )
