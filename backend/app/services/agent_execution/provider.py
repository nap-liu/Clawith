"""Keep provider concurrency in the supervising process across child turns."""

import uuid
from contextvars import ContextVar

provider_bridge: ContextVar[tuple | None] = ContextVar("execution_provider_bridge", default=None)


class ProviderLeases:
    def __init__(self):
        self.leases = {}

    async def acquire(self, model):
        from app.services.llm.provider_retry import _provider_slot

        slot = _provider_slot(model)
        await slot.__aenter__()
        key = uuid.uuid4().hex
        self.leases[key] = slot
        return key

    async def release(self, key):
        slot = self.leases.pop(key, None)
        if slot is not None:
            await slot.__aexit__(None, None, None)

    async def close(self):
        for key in list(self.leases):
            await self.release(key)


class RemoteProviderSlot:
    """Reusable like asyncio.Semaphore across retries of one provider call."""

    def __init__(self, model, bridge):
        self.model = model
        self.acquire, self.release = bridge
        self.key = None

    async def __aenter__(self):
        self.key = await self.acquire(self.model)
        return self

    async def __aexit__(self, *exc):
        key, self.key = self.key, None
        if key is not None:
            await self.release(key)
