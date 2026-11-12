# xphi.node.space.tunnel.factory
import os
import logging
import asyncio
from typing import Optional, Any, List, Tuple

import redis
import redis.asyncio as actual_redis
import redis.exceptions
from redis.asyncio.connection import BlockingConnectionPool as AsyncBlockingConnectionPool
from redis.connection import BlockingConnectionPool as SyncBlockingConnectionPool
from xphi.arch.contract.config.tunnel import BackendProtocol, resolve_default_config, parse_connection_urls

log = logging.getLogger("tunnel.factory")

class UniversalPubSub:
    def __init__(self, protocol: BackendProtocol, actual_pubsub=None, mq_client=None):
        self.protocol = protocol
        self.actual_pubsub = actual_pubsub
        self.mq_client = mq_client

    async def subscribe(self, *args, **kwargs):
        if self.protocol == BackendProtocol.REDIS:
            return await self.actual_pubsub.subscribe(*args, **kwargs)

    async def psubscribe(self, *args, **kwargs):
        if self.protocol == BackendProtocol.REDIS:
            return await self.actual_pubsub.psubscribe(*args, **kwargs)

    async def unsubscribe(self, *args, **kwargs):
        if self.protocol == BackendProtocol.REDIS:
            return await self.actual_pubsub.unsubscribe(*args, **kwargs)

    async def punsubscribe(self, *args, **kwargs):
        if self.protocol == BackendProtocol.REDIS:
            return await self.actual_pubsub.punsubscribe(*args, **kwargs)

    async def get_message(self, *args, **kwargs):
        if self.protocol == BackendProtocol.REDIS:
            return await self.actual_pubsub.get_message(*args, **kwargs)

    async def listen(self):
        if self.protocol == BackendProtocol.REDIS:
            async for msg in self.actual_pubsub.listen():
                yield msg

    async def close(self):
        if self.protocol == BackendProtocol.REDIS and hasattr(self.actual_pubsub, "close"):
            await self.actual_pubsub.close()

    def __getattr__(self, name: str):
        if self.protocol == BackendProtocol.REDIS and self.actual_pubsub:
            return getattr(self.actual_pubsub, name)
        raise AttributeError(f"'UniversalPubSub' object has no attribute '{name}'")


class UniversalFacade:
    """Asynchronous facade unifying routing for State, Queue, Stream, and PubSub signals."""

    def __init__(self, state_url: str, mq_url: str, mq_protocol: BackendProtocol, **kwargs):
        self.mq_protocol = mq_protocol
        self.mq_url = mq_url
        self.mq_client = None
        self.wasm_broker = None
        pool_kwargs = {
            "max_connections": 100,
            "timeout": 10.0,
            "socket_timeout": 5.0,
            "socket_connect_timeout": 5.0,
            "retry_on_timeout": True,
        }
        pool_kwargs.update(kwargs)
        if state_url.startswith("unix://"):
            pool_kwargs.pop("socket_connect_timeout", None)

        pool = AsyncBlockingConnectionPool.from_url(state_url, decode_responses=True, **pool_kwargs)
        self.state_store = actual_redis.Redis(connection_pool=pool)

        if self.mq_protocol == BackendProtocol.KAFKA:
            log.debug(f"[Tunnel] Initializing Kafka Producer/Consumer at {self.mq_url}")

    def bind_wasm_broker(self, broker):
        self.wasm_broker = broker
        log.debug("[Tunnel] WasmBroker bound to UniversalFacade for background telemetry auditing.")

    async def _audit_provenance(self, raw_payload: dict, message_id: str):
        try:
            if isinstance(raw_payload, dict) and "_wasm_envelope" in raw_payload:
                data_str = raw_payload.get("data")
                if data_str:
                    res = await self.wasm_broker.invoke("compute_fingerprint", data_str)
                    if res.success:
                        log.debug(f"[Audit] Message {message_id} proven. Fingerprint: {res.output}")
                    else:
                        log.warning(f"[Audit] Failed to prove message {message_id}: {res.error}")
        except Exception as e:
            log.error(f"[Audit] Background provenance task failed for message {message_id}: {e}", exc_info=True)

    async def stream_produce(self, topic: str, payload: dict, maxlen: int = 100000) -> str:
        if self.mq_protocol == BackendProtocol.KAFKA:
            pass
        msg_id = await self.state_store.xadd(topic, payload, maxlen=maxlen)
        if self.wasm_broker:
            asyncio.create_task(self._audit_provenance(payload, msg_id))
        return msg_id

    async def stream_consume(self, topic: str, group: str, consumer: str, count: int = 1, block: int = 0) -> List[Tuple]:
        if self.mq_protocol == BackendProtocol.KAFKA:
            pass
        try:
            await self.state_store.xgroup_create(topic, group, id="0", mkstream=True)
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
        return await self.state_store.xreadgroup(group, consumer, {topic: ">"}, count=count, block=block)

    async def stream_ack(self, topic: str, group: str, message_id: str):
        if self.mq_protocol == BackendProtocol.KAFKA:
            pass
        return await self.state_store.xack(topic, group, message_id)

    async def publish(self, channel: str, message: Any):
        if self.mq_protocol == BackendProtocol.KAFKA:
            pass
        return await self.state_store.publish(channel, message)

    def pubsub(self) -> UniversalPubSub:
        if self.mq_protocol == BackendProtocol.KAFKA:
            return UniversalPubSub(self.mq_protocol, mq_client=self.mq_client)
        return UniversalPubSub(self.mq_protocol, actual_pubsub=self.state_store.pubsub())

    def __getattr__(self, name: str):
        return getattr(self.state_store, name)


class UniversalFacadeSync:
    """Synchronous (Blocking) routing facade."""

    def __init__(self, state_url: str, mq_url: str, mq_protocol: BackendProtocol, **kwargs):
        self.mq_protocol = mq_protocol
        self.mq_url = mq_url
        self.mq_client = None
        pool_kwargs = {
            "max_connections": 50,
            "timeout": 10.0,
            "socket_timeout": 5.0,
            "socket_connect_timeout": 5.0,
            "retry_on_timeout": True,
        }
        pool_kwargs.update(kwargs)
        if state_url.startswith("unix://"):
            pool_kwargs.pop("socket_connect_timeout", None)

        pool = SyncBlockingConnectionPool.from_url(state_url, decode_responses=True, **pool_kwargs)
        self.state_store = redis.Redis(connection_pool=pool)

        if self.mq_protocol == BackendProtocol.KAFKA:
            log.debug(f"[SyncTunnel] Initializing Sync Kafka Producer/Consumer at {self.mq_url}")

    def stream_produce(self, topic: str, payload: dict, maxlen: int = 100000) -> str:
        if self.mq_protocol == BackendProtocol.KAFKA:
            pass
        return self.state_store.xadd(topic, payload, maxlen=maxlen)

    def stream_consume(self, topic: str, group: str, consumer: str, count: int = 1, block: int = 0) -> List[Tuple]:
        if self.mq_protocol == BackendProtocol.KAFKA:
            pass
        try:
            self.state_store.xgroup_create(topic, group, id="0", mkstream=True)
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
        return self.state_store.xreadgroup(group, consumer, {topic: ">"}, count=count, block=block)

    def stream_ack(self, topic: str, group: str, message_id: str):
        if self.mq_protocol == BackendProtocol.KAFKA:
            pass
        return self.state_store.xack(topic, group, message_id)

    async def stream_pending(self, topic: str, group: str) -> dict:
        if self.mq_protocol == BackendProtocol.KAFKA:
            pass
        return await self.state_store.xpending(topic, group)

    async def stream_claim(self, topic: str, group: str, consumer: str, min_idle_time: int, message_ids: List[str]):
        if self.mq_protocol == BackendProtocol.KAFKA:
            pass
        return await self.state_store.xclaim(topic, group, consumer, min_idle_time, message_ids)

    def publish(self, channel: str, message: Any):
        if self.mq_protocol == BackendProtocol.KAFKA:
            pass
        return self.state_store.publish(channel, message)

    def __getattr__(self, name: str):
        return getattr(self.state_store, name)


class TunnelFactory:
    _async_instance: Optional[UniversalFacade] = None
    _sync_instance: Optional[UniversalFacadeSync] = None
    _instance_pid: Optional[int] = None

    @classmethod
    async def get_default(cls, **kwargs) -> UniversalFacade:
        current_pid = os.getpid()
        if cls._async_instance is None or cls._instance_pid != current_pid:
            config = resolve_default_config()
            scheme, state_url, mq_url = parse_connection_urls(config.default_url)

            mq_url = kwargs.pop("mq_url", mq_url)
            state_url = kwargs.pop("state_url", state_url)
            scheme = kwargs.pop("mq_protocol", scheme)

            cls._async_instance = UniversalFacade(state_url, mq_url, scheme, **kwargs)
            cls._instance_pid = current_pid
            log.debug(f"[TunnelFactory] Provisioned Async Tunnel (PID {current_pid}): {mq_url}")
        return cls._async_instance

    @classmethod
    async def get_isolated(cls, **kwargs) -> UniversalFacade:
        config = resolve_default_config()
        scheme, state_url, mq_url = parse_connection_urls(config.default_url)

        mq_url = kwargs.pop("mq_url", mq_url)
        state_url = kwargs.pop("state_url", state_url)
        scheme = kwargs.pop("mq_protocol", scheme)

        return UniversalFacade(state_url, mq_url, scheme, **kwargs)

    @classmethod
    async def get_provenant(cls, wasm_broker, **kwargs) -> UniversalFacade:
        tunnel = await cls.get_default(**kwargs)
        tunnel.bind_wasm_broker(wasm_broker)
        return tunnel

    @classmethod
    def get_sync(cls, **kwargs) -> UniversalFacadeSync:
        current_pid = os.getpid()
        if cls._sync_instance is None or cls._instance_pid != current_pid:
            config = resolve_default_config()
            scheme, state_url, mq_url = parse_connection_urls(config.default_url)

            mq_url = kwargs.pop("mq_url", mq_url)
            state_url = kwargs.pop("state_url", state_url)
            scheme = kwargs.pop("mq_protocol", scheme)

            cls._sync_instance = UniversalFacadeSync(state_url, mq_url, scheme, **kwargs)
            cls._instance_pid = current_pid
            log.debug(f"[TunnelFactory] Provisioned Sync Tunnel (PID {current_pid}): {mq_url}")
        return cls._sync_instance

    @classmethod
    def get_isolated_sync(cls, **kwargs) -> UniversalFacadeSync:
        config = resolve_default_config()
        scheme, state_url, mq_url = parse_connection_urls(config.default_url)

        mq_url = kwargs.pop("mq_url", mq_url)
        state_url = kwargs.pop("state_url", state_url)
        scheme = kwargs.pop("mq_protocol", scheme)

        return UniversalFacadeSync(state_url, mq_url, scheme, **kwargs)

    @classmethod
    async def close_all(cls):
        if cls._async_instance:
            if hasattr(cls._async_instance.state_store, "aclose"):
                await cls._async_instance.state_store.aclose()
            elif hasattr(cls._async_instance.state_store, "close"):
                await cls._async_instance.state_store.close()
            cls._async_instance = None

        if cls._sync_instance:
            cls._sync_instance.state_store.close()
            cls._sync_instance = None

        cls._instance_pid = None

async def from_url(url: str, **kwargs) -> UniversalFacade:
    scheme, state_url, mq_url = parse_connection_urls(url)
    return UniversalFacade(state_url=state_url, mq_url=mq_url, mq_protocol=scheme, **kwargs)

def sync_from_url(url: str, **kwargs) -> UniversalFacadeSync:
    scheme, state_url, mq_url = parse_connection_urls(url)
    return UniversalFacadeSync(state_url=state_url, mq_url=mq_url, mq_protocol=scheme, **kwargs)