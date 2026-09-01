"""Run resumable, public-API acceptance projects across business domains.

The matrix deliberately varies inputs, professional roles, hand-off topology,
review gates, and deliverables.  It verifies both traceability and business
semantics so a collection of generic summaries cannot pass as genuine
cross-functional work.  The runner never writes project state directly to the
database.  Authentication is passed through ``CLAWITH_API_TOKEN`` and progress
is persisted so an interrupted run can safely resume without duplicating
projects or messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any, ClassVar

import httpx

from scripts.acceptance import cross_domain_verification as _cross_domain_verification
from scripts.acceptance.cross_domain_matrix import (
    ROLES, SCENARIOS, DeliverableContract, Scenario, describe_matrix, scenario_deliverables, validate_matrix,
)

API_BASE = os.getenv("CLAWITH_API_BASE", "http://127.0.0.1:8000/api").rstrip("/")
TOKEN = os.getenv("CLAWITH_API_TOKEN", "").strip()
STATE_PATH = Path(
    os.getenv(
        "CLAWITH_ACCEPTANCE_STATE",
        "/data/logs/acceptance/eight-domain-20260821.json",
    )
)
RUN_PREFIX = os.getenv("CLAWITH_ACCEPTANCE_PREFIX", "真实业务验收·20260821")
POLL_SECONDS = int(os.getenv("CLAWITH_ACCEPTANCE_POLL_SECONDS", "20"))

class AcceptanceRunner(_cross_domain_verification.CrossDomainVerificationMixin):
    INDEPENDENT_REVIEW_ROLES: ClassVar[frozenset[str]] = frozenset(
        {
            "risk",
            "stat_reviewer",
            "hr_compliance",
            "support_qa",
            "security",
            "legal",
            "independent",
            "code_reviewer",
            "qa_engineer",
            "accessibility_reviewer",
            "security_reviewer",
        }
    )
    CRITIQUE_TERMS: ClassVar[tuple[str, ...]] = (
        "风险",
        "问题",
        "质疑",
        "限制",
        "不通过",
        "改进",
        "risk",
        "issue",
        "challenge",
        "limitation",
        "reject",
    )
    MECHANICAL_STATUS_TERMS: ClassVar[tuple[str, ...]] = (
        "收到",
        "状态同步",
        "操作汇总",
        "已处理完毕",
        "等待上游",
        "等待依赖",
        "进度更新",
        "acknowledged",
        "status update",
        "waiting for upstream",
    )
    TRADEOFF_TERMS: ClassVar[tuple[str, ...]] = (
        "权衡",
        "取舍",
        "异议",
        "反对",
        "替代",
        "假设",
        "限制",
        "风险",
        "trade-off",
        "tradeoff",
        "alternative",
        "assumption",
        "limitation",
        "risk",
    )
    PROFESSIONAL_REQUEST_TERMS: ClassVar[tuple[str, ...]] = (
        "请分析",
        "请评审",
        "请核验",
        "请决定",
        "请设计",
        "请计算",
        "请比较",
        "请提出",
        "预期产出",
        "专业问题",
        "analyze",
        "review",
        "verify",
        "decide",
        "design",
        "calculate",
        "compare",
        "recommend",
        "expected output",
        "professional question",
    )

    def __init__(self, *, timeout: float = 60.0) -> None:
        if not TOKEN:
            raise SystemExit("CLAWITH_API_TOKEN is required")
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.state: dict[str, Any] = self._load_state()
        self.client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=timeout,
        )

    def _load_state(self) -> dict[str, Any]:
        if STATE_PATH.exists():
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            state_prefix = state.get("prefix")
            if state_prefix != RUN_PREFIX:
                raise RuntimeError(f"Acceptance state belongs to {state_prefix!r}, not {RUN_PREFIX!r}: {STATE_PATH}")
            state.setdefault("agents", {})
            state.setdefault("projects", {})
            return state
        return {"prefix": RUN_PREFIX, "agents": {}, "projects": {}}

    def save(self) -> None:
        temporary_path = STATE_PATH.with_suffix(f"{STATE_PATH.suffix}.tmp")
        temporary_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_path, STATE_PATH)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:1200]}")
        return response.json() if response.content else None

    async def ensure_agents(self, scenarios: tuple[Scenario, ...] = SCENARIOS) -> None:
        available = await self.request("GET", "/agents/")
        by_name = {agent["name"]: agent for agent in available}
        models = await self.request("GET", "/enterprise/llm-models")
        model_rows = models if isinstance(models, list) else models.get("items", [])
        model = next(
            (row for row in model_rows if row.get("model") == "qwen3.6-plus" and row.get("enabled", True)),
            next((row for row in model_rows if row.get("enabled", True)), None),
        )
        if model is None:
            raise RuntimeError("No enabled tenant model is available")
        model_id = model["id"]
        required = {key for scenario in scenarios for key in scenario.roles}
        for key in sorted(required):
            role = ROLES[key]
            name = f"{RUN_PREFIX}·{role.name}"
            agent = by_name.get(name)
            if agent is not None and agent.get("role_description") != role.description:
                raise RuntimeError(f"Agent {name!r} has a different role definition; use a new acceptance prefix")
            if agent is None:
                agent = await self.request(
                    "POST",
                    "/agents/",
                    json={
                        "name": name,
                        "role_description": role.description,
                        "bio": "真实跨领域项目验收岗位；只提交可核验产物，不以口头总结代替交付。",
                        "personality": "专业、克制、证据优先；主动识别依赖并通过点对点协作推进。",
                        "boundaries": "不得伪造数据、审批、外部联系或完成状态；风险与人工门禁必须明确保留。",
                        "primary_model_id": model_id,
                        "permission_scope_type": "company",
                        "permission_access_level": "use",
                    },
                )
                by_name[name] = agent
            if agent.get("status") not in {"idle", "running"}:
                agent = await self.request("POST", f"/agents/{agent['id']}/start")
            self.state["agents"][key] = agent["id"]
            self.save()
            print(f"AGENT {key}: {agent['id']} {agent.get('status')}", flush=True)

    async def prepare_project(self, scenario: Scenario) -> None:
        entry = self.state["projects"].setdefault(scenario.key, {})
        project_id = entry.get("project_id")
        project_name = f"{RUN_PREFIX}·{scenario.name}"
        if project_id:
            project = await self.request("GET", f"/projects/{project_id}")
        else:
            candidates = await self.request(
                "GET",
                "/projects",
                params={"scope": "mine", "q": project_name},
            )
            project = next((row for row in candidates if row.get("name") == project_name), None)
            if project is None:
                members = [
                    {"agent_id": self.state["agents"][key], "is_leader": index == 0}
                    for index, key in enumerate(scenario.roles)
                ]
                project = await self.request(
                    "POST",
                    "/projects",
                    json={
                        "name": project_name,
                        "description": scenario.description,
                        "goal": scenario.goal,
                        "success_criteria": list(scenario.criteria),
                        "visibility": "private",
                        "members": members,
                        "settings": {
                            "git": {"repository_mode": "managed", "branch_policy": "work_item"},
                            "runtime": {"model": "qwen3.6-plus", "max_parallel_runs": 3},
                            "policies": {
                                "max_group_mentions_per_message": 3,
                                "max_a2a_wakes": 24,
                                "loop_protection": True,
                            },
                        },
                    },
                )
            project_id = project["id"]
            entry.update({"project_id": project_id, "status": project.get("status")})
            self.save()

        project_id = project["id"]
        project_agents = await self.request("GET", f"/projects/{project_id}/agents")
        agents_by_source = {
            str(agent.get("source_agent_id")): str(agent["id"])
            for agent in project_agents
            if agent.get("source_agent_id") and agent.get("id")
        }
        entry["role_agents"] = {
            key: agents_by_source[str(self.state["agents"][key])]
            for key in scenario.roles
            if str(self.state["agents"][key]) in agents_by_source
        }
        if len(entry["role_agents"]) != len(scenario.roles):
            raise RuntimeError(f"Project {scenario.key} is missing one or more project-owned role Agents")
        if not entry.get("input_commit"):
            files = await self.request("GET", f"/projects/{project_id}/files")
            if scenario.input_path in self._collect_paths(files):
                existing = await self.request(
                    "GET",
                    f"/projects/{project_id}/files/content",
                    params={"path": scenario.input_path, "max_chars": 1},
                )
                entry["input_commit"] = existing.get("head")
            else:
                seed = await self.request(
                    "PUT",
                    f"/projects/{project_id}/files",
                    json={"path": scenario.input_path, "content": scenario.input_content},
                )
                entry["input_commit"] = seed.get("commit")
            self.save()
        if not entry.get("group_session_id"):
            group = await self.request("GET", f"/projects/{project_id}/group-session")
            entry["group_session_id"] = group["id"]
        entry["status"] = project.get("status")
        entry["completed"] = project.get("status") == "completed"
        self.save()
        print(f"PROJECT {scenario.key}: {project_id}", flush=True)

    @staticmethod
    def _collect_paths(value: Any) -> set[str]:
        if isinstance(value, list):
            return set().union(*(AcceptanceRunner._collect_paths(item) for item in value))
        if not isinstance(value, dict):
            return set()
        own_path = str(value.get("path") or "").strip()
        nested = set().union(
            *(AcceptanceRunner._collect_paths(value.get(key)) for key in ("files", "items", "children", "tree"))
        )
        return ({own_path} if own_path else set()) | nested

    @staticmethod
    def _run_recovery_key(run: dict[str, Any]) -> tuple[str, ...]:
        """Return the durable business identity used to prove a failed Run recovered."""

        work_item_id = str(run.get("work_item_id") or "").strip()
        if work_item_id:
            return ("work_item", work_item_id)

        trigger_type = str(run.get("trigger_type") or "").strip()
        agent_id = str(run.get("agent_id") or "").strip()
        run_input = run.get("input") if isinstance(run.get("input"), dict) else {}
        run_output = run.get("output") if isinstance(run.get("output"), dict) else {}
        if trigger_type == "a2a":
            session_id = str(
                run_input.get("session_id") or run_output.get("a2a_session_id") or run_output.get("session_id") or ""
            ).strip()
            return (trigger_type, agent_id, session_id)
        if trigger_type == "leader_reply_batch":
            group_session_id = str(
                run_input.get("group_session_id") or run_output.get("group_session_id") or ""
            ).strip()
            return (trigger_type, agent_id, group_session_id)
        return (trigger_type, agent_id)

    @classmethod
    def _unrecovered_failed_runs(cls, runs: list[dict[str, Any]]) -> list[str]:
        """Keep failure history while requiring a later success for the same work."""

        successful_runs = [run for run in runs if run.get("status") == "succeeded"]
        unrecovered: list[str] = []
        for failed_run in runs:
            if failed_run.get("status") not in {"failed", "cancelled"}:
                continue
            failed_id = str(failed_run["id"])
            failed_key = cls._run_recovery_key(failed_run)
            recovered = any(
                str(candidate.get("created_at") or "") > str(failed_run.get("created_at") or "")
                and (
                    cls._run_recovery_key(candidate) == failed_key
                    or str((candidate.get("input") or {}).get("retry_of_run_id") or "") == failed_id
                )
                for candidate in successful_runs
            )
            if not recovered:
                unrecovered.append(failed_id)
        return unrecovered

    def planning_prompt(self, scenario: Scenario) -> str:
        role_charters = "\n".join(f"- {ROLES[key].name}：{ROLES[key].description}" for key in scenario.roles)
        contracts = scenario_deliverables(scenario)
        outputs = "\n".join(
            (
                f"- `{item.path}` — 主责：{ROLES[item.owner_role].name}；"
                f"专业目的：{item.purpose}；"
                + (f"独立复核：{ROLES[item.review_role].name}" if item.review_role else "由主责岗位自检")
            )
            for item in contracts
        )
        criteria = "\n".join(f"- {item}" for item in scenario.criteria)
        return f"""我们要执行真实的“{scenario.name}”项目。输入已保存于 `{scenario.input_path}`，内容如下；请你作为项目负责人基于这些输入与我完成方案讨论，本轮不调用工具。

{scenario.input_content}

协作模式：{scenario.collaboration}

岗位职责（不得互相冒充或把所有工作交给负责人）：
{role_charters}

交付责任：
{outputs}

验收条件：
{criteria}

请先回复一份具体执行方案，包括工作项、依赖、各岗位负责人、交付文件、审查门禁和里程碑。每个岗位必须基于输入证据作出本专业判断，说明质疑或风险、决策依据以及可执行下一步；独立复核岗位不能照抄交付方结论。方案确认启动后，你必须使用项目管理工具创建工作项，由全部项目 Agent 按各自岗位真实协作；负责人自己的契约、编排与集成收口属于负责人 Run，不另建工作项，只为需要委派给其他岗位的专业交付创建工作项。每个工作项都必须指定项目成员，并通过点对点 A2A 产生独立 Run；只允许点对点 A2A 交接或评审，禁止广播唤醒。只有上游输入已经具备且存在明确专业问题或交付任务时才能唤醒下游岗位；不得用 A2A 发送进度通知、要求确认收到或通知对方等待，普通进度只更新工作项和项目记录。所有项目交付文件只能通过项目文件工具保存到项目工作区，不能写入数字员工私有工作区。业务判断和真实产物优先，Run、会话、Git 与验收证据应由实际工作自然形成，禁止为了留痕而让 Agent 复述工具操作。最后创建一个同时关联全部已完成工作项和对应成功 Run 的交付里程碑，再将项目状态设置为 completed。不要用总结代替文件，不要等待额外人工输入。"""

    async def send_planning_message(self, scenario: Scenario) -> None:
        entry = self.state["projects"][scenario.key]
        if entry.get("planning_message_id") or entry.get("completed"):
            return
        payload = await self.request(
            "POST",
            f"/projects/{entry['project_id']}/group-sessions/{entry['group_session_id']}/messages",
            json={
                "content": self.planning_prompt(scenario),
                "mentions": [],
                "attachments": [],
                "client_message_id": f"{RUN_PREFIX}:{scenario.key}:planning",
            },
        )
        entry["planning_message_id"] = payload.get("message", {}).get("id")
        entry["status"] = "discussing"
        self.save()
        print(f"DISCUSS {scenario.key}: {entry['planning_message_id']}", flush=True)

    async def wait_for_plan(self, scenario: Scenario, timeout_seconds: int = 1800) -> None:
        entry = self.state["projects"][scenario.key]
        if entry.get("plan_ready") or entry.get("completed"):
            return
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            timeline = await self.request(
                "GET",
                f"/projects/{entry['project_id']}/group-sessions/{entry['group_session_id']}/messages?limit=100",
            )
            items = timeline.get("items", [])
            replies = [
                item
                for item in items
                if item.get("role") == "assistant"
                and item.get("sender_agent_id") == entry["role_agents"][scenario.roles[0]]
                and str(item.get("content") or "").strip()
                and "[LLM Error]" not in str(item.get("content") or "")
                and "Request timed out" not in str(item.get("content") or "")
            ]
            if replies:
                entry["plan_ready"] = True
                entry["plan_reply_id"] = replies[-1].get("id")
                self.save()
                print(f"PLAN READY {scenario.key}: {entry['plan_reply_id']}", flush=True)
                return
            await asyncio.sleep(POLL_SECONDS)
        raise TimeoutError(f"Planning reply timed out: {scenario.key}")

    async def kickoff(self, scenario: Scenario) -> None:
        entry = self.state["projects"][scenario.key]
        if entry.get("kickoff_run_id") or entry.get("completed"):
            return
        project = await self.request("GET", f"/projects/{entry['project_id']}")
        if project.get("status") != "planning":
            runs = await self.request("GET", f"/projects/{entry['project_id']}/runs")
            kickoff_run = next(
                (run for run in runs if run.get("trigger_type") == "leader_kickoff"),
                None,
            )
            if kickoff_run is None:
                raise RuntimeError(f"Project {scenario.key} is {project.get('status')} without a kickoff run")
            entry["kickoff_run_id"] = kickoff_run["id"]
            entry["status"] = project.get("status")
            self.save()
            return
        result = await self.request(
            "POST",
            f"/projects/{entry['project_id']}/kickoff/confirm",
            json={"confirmation": "方案确认。请负责人按讨论方案自驱推进，逐项提交证据并完成项目。"},
        )
        entry["kickoff_run_id"] = result["run_id"]
        entry["status"] = "running"
        self.save()
        print(f"KICKOFF {scenario.key}: {result['run_id']}", flush=True)

    async def monitor(self, scenarios: tuple[Scenario, ...], timeout_seconds: int) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        pending = {scenario.key: scenario for scenario in scenarios}
        while pending and asyncio.get_running_loop().time() < deadline:
            for key, scenario in list(pending.items()):
                entry = self.state["projects"][key]
                project = await self.request("GET", f"/projects/{entry['project_id']}")
                runs = await self.request("GET", f"/projects/{entry['project_id']}/runs")
                items = await self.request("GET", f"/projects/{entry['project_id']}/work-items")
                active = [run for run in runs if run.get("status") in {"queued", "running", "waiting"}]
                failed = [run for run in runs if run.get("status") in {"failed", "cancelled"}]
                done_items = [item for item in items if item.get("status") == "done"]
                print(
                    f"STATUS {key}: project={project.get('status')} items={len(done_items)}/{len(items)} active={len(active)} failed={len(failed)}",
                    flush=True,
                )
                entry.update(
                    {
                        "status": project.get("status"),
                        "work_items": len(items),
                        "done_work_items": len(done_items),
                        "runs": len(runs),
                        "active_runs": len(active),
                        "failed_runs": len(failed),
                    }
                )
                if project.get("status") == "completed" and not active:
                    entry["completed"] = True
                    pending.pop(key)
                self.save()
            if pending:
                await asyncio.sleep(POLL_SECONDS)
        if pending:
            raise TimeoutError(f"Projects did not complete: {', '.join(sorted(pending))}")

    def require_prepared_projects(self, scenarios: tuple[Scenario, ...]) -> None:
        """Fail clearly when a recovery command has no durable project state."""

        missing = [
            scenario.key for scenario in scenarios if not self.state["projects"].get(scenario.key, {}).get("project_id")
        ]
        if missing:
            raise RuntimeError(
                f"Acceptance projects are not prepared for: {', '.join(missing)}. Run the normal launch workflow first."
            )

    @staticmethod
    def _retry_objective(run: dict[str, Any]) -> str:
        """Reuse business intent without replaying persisted dispatch internals."""

        run_input = run.get("input") if isinstance(run.get("input"), dict) else {}
        dispatch = run_input.get("dispatch") if isinstance(run_input.get("dispatch"), dict) else {}
        for candidate in (
            run_input.get("task"),
            run_input.get("objective"),
            run_input.get("message"),
            dispatch.get("task"),
            run.get("title"),
        ):
            value = str(candidate or "").strip()
            if value:
                return value
        return "Retry the failed project execution and preserve traceable evidence of the recovery."

    async def retry_failed_run(self, scenarios: tuple[Scenario, ...], run_id: str) -> dict[str, Any]:
        """Create one idempotent, public-API retry for an exact failed Run."""

        self.require_prepared_projects(scenarios)
        matches: list[tuple[Scenario, dict[str, Any], list[dict[str, Any]]]] = []
        for scenario in scenarios:
            entry = self.state["projects"][scenario.key]
            runs = await self.request("GET", f"/projects/{entry['project_id']}/runs")
            failed_run = next((run for run in runs if str(run.get("id")) == run_id), None)
            if failed_run is not None:
                matches.append((scenario, failed_run, runs))

        if not matches:
            raise RuntimeError(f"Run {run_id!r} was not found in the selected acceptance projects")
        if len(matches) > 1:
            raise RuntimeError(f"Run {run_id!r} unexpectedly belongs to multiple selected projects")

        scenario, failed_run, runs = matches[0]
        entry = self.state["projects"][scenario.key]
        existing_retry = next(
            (run for run in runs if str((run.get("input") or {}).get("retry_of_run_id") or "") == run_id),
            None,
        )
        if existing_retry is not None:
            entry.setdefault("retries", {})[run_id] = existing_retry["id"]
            self.save()
            print(f"RETRY EXISTS {scenario.key}: {run_id} -> {existing_retry['id']}", flush=True)
            return existing_retry

        if failed_run.get("status") not in {"failed", "cancelled"}:
            raise RuntimeError(
                f"Run {run_id!r} is {failed_run.get('status')!r}; only failed or cancelled Runs can be retried"
            )
        project = await self.request("GET", f"/projects/{entry['project_id']}")
        if project.get("status") != "running":
            raise RuntimeError(
                f"Project {scenario.key!r} is {project.get('status')!r}; public execution retries require a running project"
            )
        if not failed_run.get("agent_id"):
            raise RuntimeError(f"Run {run_id!r} has no responsible Agent and cannot be safely retried")

        retry_input = {
            "retry_of_run_id": run_id,
            "title": str(failed_run.get("title") or "").strip() or f"Retry {run_id[:8]}",
            "objective": self._retry_objective(failed_run),
        }
        payload = {
            "work_item_id": failed_run.get("work_item_id"),
            "agent_id": failed_run["agent_id"],
            "trigger_type": "retry",
            "input": retry_input,
        }
        retry = await self.request(
            "POST",
            f"/projects/{entry['project_id']}/runs",
            json=payload,
        )
        entry.setdefault("retries", {})[run_id] = retry["id"]
        self.save()
        print(f"RETRY CREATED {scenario.key}: {run_id} -> {retry['id']}", flush=True)
        return retry
    async def verify(self, scenario: Scenario) -> dict[str, Any]:
        entry = self.state["projects"][scenario.key]
        project_id = entry["project_id"]
        project, runs, items, events, git, files, milestones = await asyncio.gather(
            self.request("GET", f"/projects/{project_id}"),
            self.request("GET", f"/projects/{project_id}/runs"),
            self.request("GET", f"/projects/{project_id}/work-items"),
            self.request("GET", f"/projects/{project_id}/events?limit=500"),
            self.request("GET", f"/projects/{project_id}/git?limit=200"),
            self.request("GET", f"/projects/{project_id}/files"),
            self.request("GET", f"/projects/{project_id}/milestones"),
        )
        file_names = self._collect_paths(files)
        missing_outputs = [path for path in scenario.outputs if path not in file_names]
        active = [run["id"] for run in runs if run.get("status") in {"queued", "running", "waiting"}]
        unlinked_business_runs = [
            run["id"]
            for run in runs
            if run.get("trigger_type") in {"manual", "leader", "a2a", "retry"} and not run.get("work_item_id")
        ]
        work_item_run_ids = {
            str(run.get("work_item_id")) for run in runs if run.get("work_item_id") and run.get("status") == "succeeded"
        }
        items_without_run = [str(item["id"]) for item in items if str(item["id"]) not in work_item_run_ids]
        items_without_assignee = [str(item["id"]) for item in items if not item.get("assignee_agent_id")]
        items_without_criteria = [str(item["id"]) for item in items if not item.get("acceptance_criteria")]
        work_linked_runs = [run for run in runs if run.get("work_item_id")]
        runs_without_session = [
            str(run["id"]) for run in work_linked_runs if run.get("status") == "succeeded" and not run.get("session_id")
        ]
        runs_without_title = [str(run["id"]) for run in work_linked_runs if not str(run.get("title") or "").strip()]
        unrecovered_failed_runs = self._unrecovered_failed_runs(runs)
        participating_agents = {
            str(run["agent_id"]) for run in work_linked_runs if run.get("agent_id") and run.get("status") == "succeeded"
        }
        expected_role_agents = {
            key: str(entry.get("role_agents", {}).get(key) or "")
            for key in scenario.roles
        }
        missing_participating_roles = [
            ROLES[key].name
            for key, agent_id in expected_role_agents.items()
            if not agent_id or agent_id not in participating_agents
        ]
        event_types = {str(event.get("event_type") or "") for event in events}
        missing_a2a_events = sorted({"a2a.queued", "a2a.delivered", "a2a.completed"} - event_types)
        milestone_with_links = any(
            milestone.get("commit") and milestone.get("related_run_ids") and milestone.get("related_work_item_ids")
            for milestone in milestones
        )
        result = {
            "project_id": project_id,
            "status": project.get("status"),
            "work_items": len(items),
            "done_work_items": sum(item.get("status") == "done" for item in items),
            "runs": len(runs),
            "active_run_ids": active,
            "unlinked_business_run_ids": unlinked_business_runs,
            "work_item_ids_without_succeeded_run": items_without_run,
            "work_item_ids_without_assignee": items_without_assignee,
            "work_item_ids_without_acceptance_criteria": items_without_criteria,
            "run_ids_without_session": runs_without_session,
            "run_ids_without_title": runs_without_title,
            "unrecovered_failed_run_ids": unrecovered_failed_runs,
            "participating_agent_ids": sorted(participating_agents),
            "missing_participating_roles": missing_participating_roles,
            "missing_a2a_events": missing_a2a_events,
            "events": len(events),
            "milestones": len(milestones),
            "milestone_with_run_and_work_item_links": milestone_with_links,
            "git_head": git.get("head"),
            "missing_outputs": missing_outputs,
        }
        failures = []
        if project.get("status") != "completed":
            failures.append(f"project status is {project.get('status')!r}")
        if not items:
            failures.append("no work items were created")
        if len(items) != sum(item.get("status") == "done" for item in items):
            failures.append("not all work items are done")
        for label, values in (
            ("active runs", active),
            ("unlinked business runs", unlinked_business_runs),
            ("work items without a succeeded run", items_without_run),
            ("work items without an assignee", items_without_assignee),
            ("work items without acceptance criteria", items_without_criteria),
            ("succeeded runs without an exact session", runs_without_session),
            ("runs without a meaningful title", runs_without_title),
            ("failed runs without a later successful recovery", unrecovered_failed_runs),
            ("missing A2A event stages", missing_a2a_events),
            ("missing output files", missing_outputs),
            ("project roles without completed work", missing_participating_roles),
        ):
            if values:
                failures.append(f"{label}: {', '.join(map(str, values))}")
        if len(participating_agents) < 5:
            failures.append(f"only {len(participating_agents)} agents completed work-item runs")
        if not git.get("head"):
            failures.append("Git HEAD is missing")
        if not milestones:
            failures.append("no delivery milestone was created")
        elif not milestone_with_links:
            failures.append("no milestone links both business runs and work items")
        result["passed"] = not failures
        result["failures"] = failures
        entry["verification"] = result
        self.save()
        if failures:
            raise AssertionError(f"{scenario.key} acceptance failed: {'; '.join(failures)}")
        return result

    async def close(self) -> None:
        await self.client.aclose()


_cross_domain_verification.AcceptanceRunner = AcceptanceRunner


async def verify_scenarios(runner: AcceptanceRunner, scenarios: tuple[Scenario, ...]) -> dict[str, Any]:
    """Verify every selected scenario and emit one complete machine-readable report."""

    runner.require_prepared_projects(scenarios)
    report: dict[str, Any] = {}
    verification_failures: list[str] = []
    for scenario in scenarios:
        try:
            report[scenario.key] = await runner.verify(scenario)
        except AssertionError as exc:
            report[scenario.key] = runner.state["projects"][scenario.key]["verification"]
            verification_failures.append(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if verification_failures:
        raise AssertionError("\n".join(verification_failures))
    return report


async def run(args: argparse.Namespace) -> None:
    validate_matrix()
    runner = AcceptanceRunner()
    selected = tuple(scenario for scenario in SCENARIOS if not args.scenario or scenario.key in set(args.scenario))
    try:
        if args.verify_only:
            await verify_scenarios(runner, selected)
            return

        if args.retry_run:
            for run_id in args.retry_run:
                await runner.retry_failed_run(selected, run_id)
            if not args.continue_monitor:
                return

        if args.continue_monitor:
            runner.require_prepared_projects(selected)
            await runner.monitor(selected, args.timeout)
            await verify_scenarios(runner, selected)
            return

        await runner.ensure_agents(selected)
        for scenario in selected:
            await runner.prepare_project(scenario)
            await runner.send_planning_message(scenario)
        for start in range(0, len(selected), args.batch_size):
            batch = selected[start : start + args.batch_size]
            await asyncio.gather(*(runner.wait_for_plan(scenario) for scenario in batch))
            await asyncio.gather(*(runner.kickoff(scenario) for scenario in batch))
        if not args.launch_only:
            await runner.monitor(selected, args.timeout)
            await verify_scenarios(runner, selected)
    finally:
        await runner.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", action="append", choices=[item.key for item in SCENARIOS])
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=4 * 60 * 60)
    parser.add_argument("--launch-only", action="store_true")
    parser.add_argument(
        "--describe-matrix",
        action="store_true",
        help="Print the role, input, collaboration, and deliverable contracts without API access.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify existing projects without provisioning, sending messages, or starting Runs.",
    )
    parser.add_argument(
        "--retry-run",
        action="append",
        metavar="RUN_ID",
        help="Create an idempotent public-API retry for an exact failed Run. May be repeated.",
    )
    parser.add_argument(
        "--continue-monitor",
        action="store_true",
        help="Resume monitoring existing projects, then run strict verification.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.timeout < 1:
        parser.error("--timeout must be at least 1")
    if args.verify_only and (args.launch_only or args.retry_run or args.continue_monitor):
        parser.error("--verify-only cannot be combined with --launch-only, --retry-run, or --continue-monitor")
    if args.launch_only and (args.retry_run or args.continue_monitor):
        parser.error("--launch-only cannot be combined with --retry-run or --continue-monitor")
    if args.describe_matrix:
        execution_flags = (
            args.launch_only,
            args.verify_only,
            bool(args.retry_run),
            args.continue_monitor,
        )
        if any(execution_flags):
            parser.error("--describe-matrix cannot be combined with execution options")
        selected = tuple(scenario for scenario in SCENARIOS if not args.scenario or scenario.key in set(args.scenario))
        print(json.dumps(describe_matrix(selected), ensure_ascii=False, indent=2))
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
