"""Cross-domain acceptance for reusable project Agent templates.

These cases protect the product contract that a template keeps each role's
professional operating model instead of flattening every Agent into a generic
project worker.  They also exercise the same runtime context assembly used by
real project turns after the template has created fresh Agent identities.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.services import agent_context, agent_memory
from app.services.agent_context import build_agent_context
from app.services.agent_runtime_workspace import AgentRuntimeWorkspace, bind_agent_runtime_workspace
from app.services.project_agent_template_assets import (
    ProjectAgentTemplateSource,
    export_project_agent_template_assets,
    instantiate_project_agent_template_assets,
)
from app.services.project_agent_workspace import create_project_agent_workspace, project_agent_workspace
from app.services.storage import LocalStorageBackend, normalize_storage_key


@dataclass(frozen=True, slots=True)
class DomainRole:
    domain: str
    name: str
    role_description: str
    soul_marker: str
    memory_marker: str
    deliverable_path: str
    deliverable_marker: str


DOMAIN_ROLES = (
    DomainRole(
        domain="marketing",
        name="B2B 营销策略负责人",
        role_description="负责 ICP、渠道组合、声明证据与活动转化闭环。",
        soul_marker="每个活动主张必须绑定证据编号和唯一 UTM",
        memory_marker="预算分配先满足获客成本边界，再按渠道增量贡献调整",
        deliverable_path="campaign/launch-playbook.md",
        deliverable_marker="ICP 分群、渠道日历、UTM 和声明审查是一组联合交付物",
    ),
    DomainRole(
        domain="hr",
        name="结构化招聘负责人",
        role_description="负责岗位胜任力、结构化面试、公平性和人工录用门禁。",
        soul_marker="只依据岗位相关行为证据评分，不使用受保护个人属性",
        memory_marker="候选人必须完成独立评分后才能进入人工录用决策",
        deliverable_path="hiring/interview-kit.md",
        deliverable_marker="题库、评分锚点、隐私边界和复核记录缺一不可",
    ),
    DomainRole(
        domain="support",
        name="客户服务治理负责人",
        role_description="负责工单分诊、SLA、重大升级、知识闭环和服务质量。",
        soul_marker="先按影响范围和紧急度分级，再决定 SLA 与升级路径",
        memory_marker="P0 事件恢复后仍需补齐时间线、根因和知识库回写",
        deliverable_path="support/incident-playbook.md",
        deliverable_marker="分诊矩阵、升级树、恢复证据和 VOC 闭环必须可追溯",
    ),
    DomainRole(
        domain="operations",
        name="供应链营运负责人",
        role_description="负责需求预测、库存健康、供应约束和门店执行收口。",
        soul_marker="补货建议必须同时核对提前期、MOQ、安全库存和缺货风险",
        memory_marker="高风险缺口先确认供应窗口，再发布门店调拨与替代方案",
        deliverable_path="operations/replenishment-plan.md",
        deliverable_marker="预测、库存对账、供应确认和门店执行形成同一闭环",
    ),
)


async def _utc_timezone(_agent_id: uuid.UUID) -> str:
    return "UTC"


async def _no_extension_prompts(_agent_id: uuid.UUID) -> list[str]:
    return []


@pytest.mark.asyncio
async def test_cross_domain_templates_preserve_role_identity_and_runtime_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    storage = LocalStorageBackend(str(storage_root))
    monkeypatch.setattr(agent_context, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_memory, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_context, "_collect_extension_prompts", _no_extension_prompts)
    monkeypatch.setattr("app.services.timezone_utils.get_agent_timezone", _utc_timezone)

    runtime_prompts: dict[str, str] = {}
    for role in DOMAIN_ROLES:
        source_project_id = uuid.uuid4()
        source_agent_id = uuid.uuid4()
        target_project_id = uuid.uuid4()
        source_project = storage_root / role.domain / "source"
        target_project = storage_root / role.domain / "target"
        source_project.mkdir(parents=True)
        target_project.mkdir(parents=True)

        source = await create_project_agent_workspace(source_project, source_agent_id)
        source.workspace.soul.write_text(
            f"# {role.name}\n\n{role.soul_marker}\n负责人：owner-{role.domain}@example.com\n",
            encoding="utf-8",
        )
        source.workspace.memory.write_text(
            f"# 核心业务记忆\n\n{role.memory_marker}\n原项目：{source_project_id}\n",
            encoding="utf-8",
        )
        deliverable = source.workspace.workspace / role.deliverable_path
        deliverable.parent.mkdir(parents=True)
        deliverable.write_text(
            f"{role.deliverable_marker}\nBearer {role.domain}-private-token-2026\n",
            encoding="utf-8",
        )
        (source.workspace.workspace / ".env.production").write_text(
            f"API_KEY={role.domain}-secret",
            encoding="utf-8",
        )

        definition = export_project_agent_template_assets(
            source_project,
            [
                ProjectAgentTemplateSource(
                    agent_id=source_agent_id,
                    name=role.name,
                    role_description=role.role_description,
                    is_leader=True,
                )
            ],
            redact_values=(source_project_id,),
        )
        serialized = json.dumps(definition, ensure_ascii=False)
        assert role.soul_marker in serialized
        assert role.memory_marker in serialized
        assert role.deliverable_marker in serialized
        assert role.deliverable_path in serialized
        assert ".env.production" not in serialized
        assert f"{role.domain}-secret" not in serialized
        assert f"{role.domain}-private-token-2026" not in serialized
        assert "[redacted-email]" in serialized
        assert "[redacted-id]" in serialized
        assert "Bearer [redacted]" in serialized

        target_agent_id = uuid.uuid4()
        instances = await instantiate_project_agent_template_assets(
            target_project,
            definition,
            agent_ids=[target_agent_id],
        )
        instance = instances[0]
        assert instance.agent_id == target_agent_id
        assert instance.agent_id != source_agent_id
        assert instance.agent_dir == f".agents/{target_agent_id}"
        assert instance.is_leader is True

        target = project_agent_workspace(target_project, target_agent_id)
        assert role.soul_marker in target.soul.read_text(encoding="utf-8")
        assert role.memory_marker in target.memory.read_text(encoding="utf-8")
        assert role.deliverable_marker in (target.workspace / role.deliverable_path).read_text(encoding="utf-8")
        assert not (target.workspace / ".env.production").exists()

        runtime = AgentRuntimeWorkspace(
            agent_id=target_agent_id,
            local_root=target.root,
            storage_prefix=normalize_storage_key(target.root.relative_to(storage_root).as_posix()),
            project_id=target_project_id,
            project_repo_root=target_project,
        )
        with bind_agent_runtime_workspace(runtime):
            static_prompt, dynamic_prompt = await build_agent_context(
                target_agent_id,
                instance.name,
                instance.role_description,
            )
        combined_prompt = f"{static_prompt}\n{dynamic_prompt}"
        runtime_prompts[role.domain] = combined_prompt
        assert role.role_description in combined_prompt
        assert role.soul_marker in combined_prompt
        assert role.memory_marker in combined_prompt
        assert "## Project Memory" in combined_prompt

    for role in DOMAIN_ROLES:
        other_markers = {other.soul_marker for other in DOMAIN_ROLES if other.domain != role.domain} | {
            other.memory_marker for other in DOMAIN_ROLES if other.domain != role.domain
        }
        assert not any(marker in runtime_prompts[role.domain] for marker in other_markers)
