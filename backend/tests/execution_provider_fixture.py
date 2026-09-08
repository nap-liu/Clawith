"""Local HTTP provider used by real-process conversation integration tests."""

import asyncio
import json
from contextlib import asynccontextmanager


@asynccontextmanager
async def provider(responses):
    requests = []
    errors = []

    async def handle(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            request = json.loads(await reader.readexactly(length))
            requests.append(request)
            response = dict(responses[min(len(requests) - 1, len(responses) - 1)])
            gate = response.pop("_wait_for", None)
            if gate is not None:
                await gate.wait()
            if request.get("stream"):
                delta = dict(response)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{"index": index, **call} for index, call in enumerate(delta["tool_calls"])]
                frame = {"id": "test-response", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                finish = {"choices": [{"index": 0, "delta": {},
                                       "finish_reason": "tool_calls" if "tool_calls" in response else "stop"}],
                          "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
                body = ("data: " + json.dumps(frame) + "\n\ndata: " + json.dumps(finish)
                        + "\n\ndata: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                body = json.dumps({"choices": [{"message": {"role": "assistant", **response},
                                                 "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 100, "completion_tokens": 10}}).encode()
                content_type = "application/json"
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:
            errors.append(exc)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/v1", requests
        assert not errors
    finally:
        server.close()
        await server.wait_closed()
