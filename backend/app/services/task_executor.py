"""Background task executor — runs LLM to complete tasks automatically."""

import asyncio
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.task import Task, TaskLog


async def execute_task(task_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """Execute a task using the agent's configured LLM.

    Flow: pending → doing (+ call LLM) → done
    """
    print(f"[TaskExec] Starting task {task_id} for agent {agent_id}")

    # Step 1: Mark as doing
    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            print(f"[TaskExec] Task {task_id} not found")
            return

        task.status = "doing"
        db.add(TaskLog(task_id=task_id, content="🤖 开始执行任务..."))
        await db.commit()
        task_title = task.title
        task_description = task.description or ""
        task_type = task.type  # 'todo' or 'supervision'

    # Step 2: Load agent + model
    async with async_session() as db:
        agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            await _log_error(task_id, "数字员工未找到")
            return

        if not agent.primary_model_id:
            await _log_error(task_id, f"{agent.name} 未配置 LLM 模型，无法执行任务")
            return

        model_result = await db.execute(
            select(LLMModel).where(LLMModel.id == agent.primary_model_id)
        )
        model = model_result.scalar_one_or_none()
        if not model:
            await _log_error(task_id, "配置的模型不存在")
            return

        agent_name = agent.name
        role_description = agent.role_description or ""

    # Step 3: Call LLM
    from datetime import timedelta
    cst = timezone(timedelta(hours=8))
    now_str = datetime.now(cst).strftime("%Y-%m-%d %H:%M:%S (CST)")
    system_prompt = f"你是 {agent_name}，一个企业数字员工。\n当前时间：{now_str}"
    if role_description:
        system_prompt += f"\n你的角色定位：{role_description}"
    system_prompt += "\n用户给你分配了一个任务，请认真完成。给出详细的执行结果。用中文回复。"

    user_prompt = f"请执行以下任务:\n\n任务标题: {task_title}"
    if task_description:
        user_prompt += f"\n任务描述: {task_description}"

    from app.services.llm_utils import get_provider_base_url
    base_url = get_provider_base_url(model.provider, model.base_url)

    if not base_url:
        await _log_error(task_id, f"未配置 {model.provider} 的 API 地址")
        return
    # Normalize: strip /chat/completions if user accidentally included the full endpoint
    if base_url.rstrip('/').endswith('/chat/completions'):
        base_url = base_url.rstrip('/').rsplit('/chat/completions', 1)[0]
    url = f"{base_url.rstrip('/')}/chat/completions"
    api_key = model.api_key_encrypted

    payload = json.dumps({
        "model": model.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 2048,
    })

    try:
        print(f"[TaskExec] Calling LLM for task: {task_title}")
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "--max-time", "60",
            "-X", "POST", url,
            "-H", "Content-Type: application/json",
            "-H", f"Authorization: Bearer {api_key}",
            "-d", payload,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            stderr_msg = stderr.decode().strip()[:200] if stderr else ''
            await _log_error(task_id, f"调用模型失败 (curl exit {proc.returncode}) {stderr_msg}")
            if task_type == 'supervision':
                await _restore_supervision_status(task_id)
            return

        stdout_text = stdout.decode().strip()
        if not stdout_text:
            await _log_error(task_id, "LLM 返回空响应（可能网络超时或服务不可用）")
            # Restore supervision task status
            if task_type == 'supervision':
                await _restore_supervision_status(task_id)
            return

        data = json.loads(stdout_text)
        if not isinstance(data, dict):
            await _log_error(task_id, f"LLM 返回非预期格式: {stdout_text[:300]}")
            if task_type == 'supervision':
                await _restore_supervision_status(task_id)
            return
        if "error" in data:
            await _log_error(task_id, f"LLM 错误: {data['error'].get('message', str(data['error']))[:200]}")
            if task_type == 'supervision':
                await _restore_supervision_status(task_id)
            return

        choices = data.get("choices")
        if not choices or not isinstance(choices, list):
            await _log_error(task_id, f"LLM 响应格式异常: {stdout_text[:300]}")
            if task_type == 'supervision':
                await _restore_supervision_status(task_id)
            return

        reply = choices[0].get("message", {}).get("content", "")
        print(f"[TaskExec] LLM reply: {reply[:80]}")
    except Exception as e:
        error_msg = str(e) or repr(e)
        print(f"[TaskExec] Error: {error_msg}")
        await _log_error(task_id, f"执行出错: {error_msg[:150]}")
        if task_type == 'supervision':
            await _restore_supervision_status(task_id)
        return

    # Step 4: Save result and mark done
    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task:
            if task_type == 'supervision':
                # Supervision tasks stay active; just log the result
                task.status = "pending"
                db.add(TaskLog(task_id=task_id, content=f"✅ 督办执行完成\n\n{reply}"))
            else:
                task.status = "done"
                task.completed_at = datetime.now(timezone.utc)
                db.add(TaskLog(task_id=task_id, content=f"✅ 任务完成\n\n{reply}"))
            await db.commit()
            print(f"[TaskExec] Task {task_id} {'logged' if task_type == 'supervision' else 'completed'}!")


async def _log_error(task_id: uuid.UUID, message: str) -> None:
    """Add an error log to the task."""
    print(f"[TaskExec] Error for {task_id}: {message}")
    async with async_session() as db:
        db.add(TaskLog(task_id=task_id, content=f"❌ {message}"))
        await db.commit()


async def _restore_supervision_status(task_id: uuid.UUID) -> None:
    """Restore supervision task status back to pending after a failed execution."""
    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task and task.status == "doing":
            task.status = "pending"
            await db.commit()
