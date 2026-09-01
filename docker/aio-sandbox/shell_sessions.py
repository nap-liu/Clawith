"""Session administration and interactive WebSocket routes for shell API."""

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect

from app.core.service_container import services
from app.logging.websocket import (
    bind_websocket_logid,
    log_websocket_event,
    restore_logid,
)
from app.models.shell import ActiveShellSessionsResult
from app.schemas.response import Response
from app.schemas.shell import ShellSessionStats
from app.services.shell import OpenHandsShellManager


router = APIRouter()
logger = logging.getLogger('app.api.v1.shell')


async def receive_input(websocket: WebSocket, session):
    """接收 WebSocket 输入并发送到终端（使用 terminado）"""
    try:
        while True:
            raw_data = await websocket.receive_text()
            try:
                # 尝试解析 JSON 消息
                message = json.loads(raw_data)
                if message.get('type') == 'input':
                    session.send_input(message.get('data', ''))
                elif message.get('type') == 'resize':
                    cols = message.get('data', {}).get('cols', 80)
                    rows = message.get('data', {}).get('rows', 24)
                    try:
                        session.resize(rows, cols)
                    except Exception as e:
                        logger.warning(f'Failed to resize terminal: {e}')
                elif message.get('type') == 'ping':
                    # 响应心跳ping消息
                    pong_message = {
                        'type': 'pong',
                        'timestamp': message.get('timestamp', int(time.time() * 1000)),
                    }
                    await websocket.send_text(json.dumps(pong_message))
            except json.JSONDecodeError:
                # 如果不是 JSON，直接作为输入发送（向后兼容）
                session.send_input(raw_data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f'Error receiving input: {e}')
        raise


async def send_output(websocket: WebSocket, queue: asyncio.Queue):
    """从队列读取输出并发送到 WebSocket"""
    try:
        while True:
            output = await queue.get()
            # 发送标准格式的消息给 xterm.js
            message = {'type': 'output', 'data': output}
            await websocket.send_text(json.dumps(message))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f'Error sending output: {e}')
        raise


async def send_heartbeat(websocket: WebSocket):
    """定期发送心跳ping消息以保持连接活跃"""
    try:
        while True:
            # 每30秒发送一次心跳
            await asyncio.sleep(30)
            ping_message = {'type': 'ping', 'timestamp': int(time.time() * 1000)}
            await websocket.send_text(json.dumps(ping_message))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f'Error sending heartbeat: {e}')
        raise


@router.get(
    '/terminal-url',
    response_model=Response[str],
    operation_id='get_terminal_url',
    openapi_extra={
        'x-fern-sdk-group-name': 'shell',
        'x-fern-sdk-method-name': 'get_terminal_url',
    },
)
async def get_terminal_url(http_request: Request):
    """
    Create a new shell session and return the terminal URL
    """
    from app.utils import normalize_path_prefix

    terminal_ws_manager = services.require('terminal_ws_manager')

    # Create a new session
    session = await terminal_ws_manager.create_session()

    # Priority: X-Forwarded-Host > Host header > request.url.netloc
    # X-Forwarded-Host preserves the original host when behind reverse proxies
    host = (
        http_request.headers.get('x-forwarded-host')
        or http_request.headers.get('host')
        or http_request.url.netloc
    )

    # Check X-Forwarded-Proto first, fallback to request.url.scheme
    # In reverse proxy scenarios, the internal request may be http
    # but the external client connection is https
    forwarded_proto = http_request.headers.get('x-forwarded-proto', '').lower()
    if forwarded_proto in ('https', 'http'):
        is_https = forwarded_proto == 'https'
    else:
        is_https = http_request.url.scheme == 'https'

    http_scheme = 'https' if is_https else 'http'

    logger.info(
        'Protocol detection - X-Forwarded-Proto: %s, request.url.scheme: %s, final: %s',
        forwarded_proto or 'none',
        http_request.url.scheme,
        http_scheme,
    )

    # Get and normalize path prefix from X-Forwarded-Prefix header
    path_prefix = normalize_path_prefix(
        http_request.headers.get('x-forwarded-prefix')
    )

    # Construct the terminal URL
    terminal_url = (
        f'{http_scheme}://{host}{path_prefix}/terminal?session_id={session.id}'
    )

    return Response(
        success=True, message='Terminal URL created successfully', data=terminal_url
    )


@router.get(
    '/sessions/stats',
    response_model=Response[ShellSessionStats],
    operation_id='get_shell_session_stats',
    openapi_extra={
        'x-fern-sdk-group-name': 'shell',
        'x-fern-sdk-method-name': 'get_session_stats',
    },
)
async def get_session_stats():
    """Return aggregate statistics for shell sessions."""
    terminal_manager: OpenHandsShellManager = services.require('terminal_manager')
    stats = terminal_manager.get_session_stats()

    return Response(
        success=True,
        message='Shell session stats retrieved successfully',
        data=stats.model_dump(),
    )


@router.get(
    '/sessions',
    response_model=Response[ActiveShellSessionsResult],
    operation_id='list_shell_sessions',
    openapi_extra={
        'x-fern-sdk-group-name': 'shell',
        'x-fern-sdk-method-name': 'list_sessions',
    },
)
async def list_sessions():
    """
    List all active shell sessions
    """
    terminal_manager: OpenHandsShellManager = services.require('terminal_manager')
    sessions = terminal_manager.get_active_sessions()

    return Response(
        success=True,
        message=f'Found {len(sessions.sessions)} active sessions',
        data=sessions.model_dump(),
    )


@router.delete(
    '/sessions/{session_id}',
    response_model=Response,
    operation_id='cleanup_shell_session',
    openapi_extra={
        'x-fern-sdk-group-name': 'shell',
        'x-fern-sdk-method-name': 'cleanup_session',
    },
)
async def cleanup_session(session_id: str):
    """
    Manually cleanup a specific shell session
    """
    terminal_manager: OpenHandsShellManager = services.require('terminal_manager')
    success = terminal_manager.cleanup_session(session_id)

    if success:
        return Response(
            success=True,
            message=f'Session {session_id} cleaned up successfully',
            data={'session_id': session_id},
        )
    else:
        return Response(
            success=False,
            message=f'Session {session_id} not found',
            data={'session_id': session_id},
        )


@router.delete(
    '/sessions',
    response_model=Response,
    operation_id='cleanup_all_sessions',
    openapi_extra={
        'x-fern-sdk-group-name': 'shell',
        'x-fern-sdk-method-name': 'cleanup_all_sessions',
    },
)
async def cleanup_all_sessions():
    """
    Cleanup all active shell sessions
    """
    terminal_manager: OpenHandsShellManager = services.require('terminal_manager')
    sessions_before = len(terminal_manager.get_active_sessions().sessions)
    await terminal_manager.cleanup_all_sessions()

    return Response(
        success=True,
        message=f'Cleaned up {sessions_before} sessions',
        data={'cleaned_sessions': sessions_before},
    )


@router.websocket('/ws')
async def websocket_shell_endpoint(
    websocket: WebSocket, session_id: Optional[str] = Query(None)
):
    terminal_ws_manager = services.require('terminal_ws_manager')
    """
    WebSocket endpoint for interactive shell terminal
    """
    ws_logid, previous_logid = bind_websocket_logid(websocket)
    session = None
    is_new_session = False
    subscriber_id = None
    try:
        await websocket.accept()

        # 如果没有 session_id，创建新会话
        if not session_id:
            session = await terminal_ws_manager.create_session()
            session_id = session.id
            is_new_session = True
            # 只有新会话才发送 session_id 消息
            message = {'type': 'session_id', 'data': session_id}
            await websocket.send_text(json.dumps(message))
        else:
            # 连接到现有会话
            session = terminal_ws_manager.get_session(session_id)
            if not session:
                log_websocket_event(
                    logger,
                    'reject',
                    websocket=websocket,
                    route='/v1/shell/ws',
                    session_id=session_id,
                    logid=ws_logid,
                    details={'reason': 'session_not_found'},
                )
                error_message = {'type': 'error', 'data': 'Session not found'}
                await websocket.send_text(json.dumps(error_message))
                await websocket.close()
                return

        if not session.active:
            log_websocket_event(
                logger,
                'reject',
                websocket=websocket,
                route='/v1/shell/ws',
                session_id=session_id,
                logid=ws_logid,
                details={'reason': 'session_inactive'},
            )
            error_message = {'type': 'error', 'data': 'Session is not active'}
            await websocket.send_text(json.dumps(error_message))
            await websocket.close()
            return

        log_websocket_event(
            logger,
            'connect',
            websocket=websocket,
            route='/v1/shell/ws',
            session_id=session_id,
            logid=ws_logid,
            details={'new_session': is_new_session},
        )

        # 订阅字符输出
        subscriber_id = f'ws_{session_id}_{id(websocket)}'
        queue = await terminal_ws_manager.subscribe_char(session_id, subscriber_id)

        if not queue:
            log_websocket_event(
                logger,
                'reject',
                websocket=websocket,
                route='/v1/shell/ws',
                session_id=session_id,
                logid=ws_logid,
                details={'reason': 'duplicate_websocket_connection'},
            )
            error_message = {
                'type': 'error',
                'data': 'Session already has an active WebSocket connection',
            }
            await websocket.send_text(json.dumps(error_message))
            await websocket.close()
            return

        # 如果是现有会话，恢复历史输出
        if not is_new_session:
            async with session._lock:
                # 发送历史输出
                historical_output = session.output_text or (
                    ''.join(session.output_buffer) if session.output_buffer else ''
                )
                if historical_output.strip():
                    restore_message = {
                        'type': 'restore_output',
                        'data': historical_output,
                    }
                    await websocket.send_text(json.dumps(restore_message))

            # 发送会话恢复完成消息
            restored_message = {
                'type': 'terminal_restored',
                'data': f'Session {session_id[:8]} restored',
            }
            await websocket.send_text(json.dumps(restored_message))
        else:
            # 新会话发送就绪消息
            ready_message = {
                'type': 'ready',
                'data': f'Terminal ready - Session: {session_id[:8]}',
            }
            await websocket.send_text(json.dumps(ready_message))

            # 发送初始提示
            session.send_input('\n')
            await asyncio.sleep(0.2)

        # 启动心跳任务（在后台运行，不等待完成）
        heartbeat_task = asyncio.create_task(send_heartbeat(websocket))

        # 创建主要任务：接收输入和发送输出
        receive_task = asyncio.create_task(receive_input(websocket, session))
        send_task = asyncio.create_task(send_output(websocket, queue))

        # 等待主要任务中的任一个完成（不包括心跳任务）
        done, pending = await asyncio.wait(
            [receive_task, send_task], return_when=asyncio.FIRST_COMPLETED
        )

        # 取消未完成的主要任务
        for task in pending:
            task.cancel()

        # 取消心跳任务
        heartbeat_task.cancel()

        # 等待已完成的任务以确保异常被处理
        for task in done:
            try:
                task.result()
            except Exception:
                pass  # 异常会在其他地方处理

    except WebSocketDisconnect:
        log_websocket_event(
            logger,
            'disconnect',
            websocket=websocket,
            route='/v1/shell/ws',
            session_id=session_id,
            logid=ws_logid,
            details={'new_session': is_new_session},
        )
    except Exception as e:
        log_websocket_event(
            logger,
            'error',
            websocket=websocket,
            route='/v1/shell/ws',
            session_id=session_id,
            level=logging.ERROR,
            logid=ws_logid,
            details={
                'new_session': is_new_session,
                'error': {
                    'type': type(e).__name__,
                    'message': str(e),
                },
            },
        )
    finally:
        if subscriber_id:
            await terminal_ws_manager.unsubscribe_char(session_id, subscriber_id)
        # 只有在WebSocket仍然连接时才尝试关闭
        if websocket.client_state.name != 'DISCONNECTED':
            try:
                await websocket.close()
            except RuntimeError:
                # WebSocket已经关闭，忽略错误
                pass
        restore_logid(previous_logid)
