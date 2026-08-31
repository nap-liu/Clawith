"""Managed gateway connection loop for the DingTalk Stream adapter."""

import asyncio
import json
import uuid
from urllib.parse import quote_plus

import httpx
from loguru import logger


class DingTalkStreamConnectionMixin:
    async def _open_connection(
        self,
        client,
        http_client: httpx.AsyncClient,
    ) -> dict:
        """Open the DingTalk gateway with a bounded, cancellable HTTP call."""
        topics = []
        if getattr(client, "_is_event_required", False):
            topics.append({"type": "EVENT", "topic": "*"})
        topics.extend(
            {"type": "CALLBACK", "topic": topic}
            for topic in client.callback_handler_map
        )
        response = await http_client.post(
            client.OPEN_CONNECTION_API,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "DingTalkStream/managed-platform",
            },
            json={
                "clientId": client.credential.client_id,
                "clientSecret": client.credential.client_secret,
                "subscriptions": topics,
                "ua": "dingtalk-sdk-python/platform-managed",
                "localIp": client.get_host_ip(),
            },
        )
        response.raise_for_status()
        return response.json()

    async def _open_connection_or_stop(
        self,
        client,
        http_client: httpx.AsyncClient,
        async_stop_event: asyncio.Event,
    ) -> dict | None:
        open_task = asyncio.create_task(self._open_connection(client, http_client))
        stop_task = asyncio.create_task(async_stop_event.wait())
        done, pending = await asyncio.wait(
            {open_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and stop_task.result():
            open_task.cancel()
            await asyncio.gather(open_task, return_exceptions=True)
            return None
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        return open_task.result()

    async def _run_managed_client(
        self,
        *,
        agent_id: uuid.UUID,
        generation: int,
        client,
        async_stop_event: asyncio.Event,
        retry_delays: list[int],
    ) -> None:
        """Run the SDK protocol with an explicit, stoppable reconnect loop."""
        import websockets

        client.pre_start()
        retry_count = 0
        failure_notified = False
        async with httpx.AsyncClient(timeout=15) as gateway_http:
            while not async_stop_event.is_set():
                websocket = None
                keepalive_task = None
                try:
                    connection = await self._open_connection_or_stop(
                        client,
                        gateway_http,
                        async_stop_event,
                    )
                    if connection is None and async_stop_event.is_set():
                        break
                    if not connection:
                        raise RuntimeError("DingTalk open connection returned no endpoint")
                    uri = f'{connection["endpoint"]}?ticket={quote_plus(connection["ticket"])}'
                    logger.info(
                        f"[DingTalk Stream] Connecting for agent {agent_id} "
                        f"(generation={generation}, retry={retry_count})"
                    )
                    async with websockets.connect(uri) as websocket:
                        client.websocket = websocket
                        retry_count = 0
                        failure_notified = False
                        await self._publish_connection_state(agent_id, generation, ready=True)
                        keepalive_task = asyncio.create_task(client.keepalive(websocket))
                        while not async_stop_event.is_set():
                            receive_task = asyncio.create_task(websocket.recv())
                            stop_task = asyncio.create_task(async_stop_event.wait())
                            done, pending = await asyncio.wait(
                                {receive_task, stop_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for task in pending:
                                task.cancel()
                            if pending:
                                await asyncio.gather(*pending, return_exceptions=True)
                            if stop_task in done and stop_task.result():
                                if not receive_task.done():
                                    receive_task.cancel()
                                await websocket.close()
                                break
                            raw_message = receive_task.result()
                            json_message = json.loads(raw_message)
                            asyncio.create_task(client.background_task(json_message))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not async_stop_event.is_set():
                        retry_count += 1
                        logger.warning(
                            f"[DingTalk Stream] Connection error for {agent_id} "
                            f"(generation={generation}, retry={retry_count}): {exc}"
                        )
                        if retry_count >= 6 and not failure_notified:
                            failure_notified = True
                            main_loop = self._main_loop
                            if main_loop and main_loop.is_running():
                                asyncio.run_coroutine_threadsafe(
                                    self._notify_connection_failed(agent_id, str(exc)),
                                    main_loop,
                                )
                finally:
                    if keepalive_task is not None:
                        keepalive_task.cancel()
                        await asyncio.gather(keepalive_task, return_exceptions=True)
                    client.websocket = None
                    await self._publish_connection_state(agent_id, generation, ready=False)

                if async_stop_event.is_set():
                    break
                delay = retry_delays[min(max(retry_count - 1, 0), len(retry_delays) - 1)]
                try:
                    await asyncio.wait_for(async_stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass

