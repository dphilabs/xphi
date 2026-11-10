# xphi.state.anchor.mempool
import json
import os
import threading
import time
import uuid
from typing import Any, Dict, Optional

from xphi.node.space.tunnel.factory import TunnelFactory, UniversalFacade, UniversalFacadeSync
from xphi.state.anchor.key import KeyGen, LogicStream, SealedKernel
from xphi.watcher.plane.emitter import get_emitter

log = get_emitter("anchor.mempool", phase="KERNEL")

class MempoolRelay:
    """PROPOSER와 SEALER 간의 분산 Mempool 스트림 중계 및 ACK 동기화 전담 모듈"""
    CONSUMER_GROUP = "anchor_sealer_group"

    def __init__(self, store: Any, ack_timeout: float = 4.0):
        self.store = store
        self.ack_timeout = ack_timeout
        self.consumer_name = f"sealer_{os.getpid()}_{uuid.uuid4().hex[:6]}"

        self.stream_logic = KeyGen.mempool_logic()
        self.stream_objects = KeyGen.mempool_objects()
        self.stream_heads = KeyGen.mempool_heads()

        self.sync_tunnel: UniversalFacadeSync = TunnelFactory.get_sync()
        self._consumer_thread: Optional[threading.Thread] = None
        self._running = False

    # [PROPOSER 측] 요청 발송 및 ACK 대기 (Sync / Async)
    def propose_object_sync(self, obj_type: str, obj_hash: str, data: Dict[str, Any]) -> str:
        req_id = uuid.uuid4().hex
        payload = {
            "req_id": req_id,
            "type": obj_type,
            "hash": obj_hash,
            "data": json.dumps(data),
        }
        try:
            self.sync_tunnel.stream_produce(self.stream_objects, payload)
            ack = self._wait_ack_sync(req_id, timeout=1.5)
            if not ack or ack.get("status") != "OK":
                log.warning(f"[Mempool:PROPOSER] Object relay unacknowledged ({obj_type}:{obj_hash[:8]}): {ack}")
        except Exception as e:
            log.error(f"[Mempool:PROPOSER] Sync object relay failed ({obj_type}:{obj_hash[:8]}): {e}")
        return obj_hash

    def propose_head_sync(self, stream_id: str, commit_hash: str) -> None:
        req_id = uuid.uuid4().hex
        payload = {
            "req_id": req_id,
            "stream_id": stream_id,
            "commit_hash": commit_hash,
        }
        try:
            self.sync_tunnel.stream_produce(self.stream_heads, payload)
            ack = self._wait_ack_sync(req_id, timeout=1.5)
            if not ack or ack.get("status") != "OK":
                log.warning(f"[Mempool:PROPOSER] Head relay unacknowledged ({stream_id} -> {commit_hash[:8]}): {ack}")
        except Exception as e:
            log.error(f"[Mempool:PROPOSER] Sync head relay failed ({stream_id}): {e}")

    async def propose_stream_async(self, stream: LogicStream) -> Optional[SealedKernel]:
        req_id = uuid.uuid4().hex
        stream_data = {
            "req_id": req_id,
            "id": str(stream.id),
            "action": str(stream.action),
            "payload": json.dumps(stream.payload) if isinstance(stream.payload, (dict, list)) else str(stream.payload),
            "metadata": json.dumps(stream.metadata) if isinstance(stream.metadata, dict) else str(stream.metadata),
        }

        try:
            async_tunnel: UniversalFacade = await TunnelFactory.get_default()
            await async_tunnel.stream_produce(self.stream_logic, stream_data)
            ack_data = await self._wait_ack_async(async_tunnel, req_id, timeout=self.ack_timeout)
            if ack_data is None:
                log.warning(
                    f"[Mempool:PROPOSER] ACK timeout ({self.ack_timeout}s) waiting for SEALER on stream '{stream.id}' (req={req_id[:8]})."
                )
                return None
            if ack_data.get("status") == "SEALED" and "kernel" in ack_data:
                return SealedKernel(**ack_data["kernel"])
            log.warning(f"[Mempool:PROPOSER] SEALER rejected stream '{stream.id}': {ack_data}")
        except Exception as e:
            log.error(f"[Mempool:PROPOSER] Async stream relay failed for '{stream.id}': {e}", exc_info=True)

        return None

    # [ACK 송수신 헬퍼: LPUSH + BLPOP]
    def _wait_ack_sync(self, req_id: str, timeout: float) -> Optional[Dict[str, Any]]:
        ack_key = KeyGen.ack_channel(req_id)
        try:
            block_sec = min(float(timeout), 3.5)
            res = self.sync_tunnel.blpop([ack_key], timeout=block_sec)
            if res:
                _, raw_val = res
                self.sync_tunnel.delete(ack_key)
                return json.loads(raw_val)
        except TimeoutError:
            pass
        except Exception as e:
            log.warning(f"[Mempool:ACK] Sync ACK wait error ({req_id[:8]}): {e}")
        return None

    async def _wait_ack_async(
        self, async_tunnel: UniversalFacade, req_id: str, timeout: float
    ) -> Optional[Dict[str, Any]]:
        ack_key = KeyGen.ack_channel(req_id)
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                rem = deadline - time.time()
                if rem <= 0:
                    break
                step_timeout = min(rem, 2.0)
                res = await async_tunnel.blpop([ack_key], timeout=step_timeout)
                if res:
                    _, raw_val = res
                    await async_tunnel.delete(ack_key)
                    return json.loads(raw_val)
        except TimeoutError:
            pass
        except Exception as e:
            log.warning(f"[Mempool:ACK] Async ACK wait error ({req_id[:8]}): {e}")
        return None

    def _send_ack_sync(self, tunnel: UniversalFacadeSync, req_id: str, result: Dict[str, Any], ttl: int = 30) -> None:
        if not req_id:
            return
        ack_key = KeyGen.ack_channel(req_id)
        try:
            tunnel.lpush(ack_key, json.dumps(result))
            tunnel.expire(ack_key, ttl)
        except Exception as e:
            log.error(f"[Mempool:SEALER] Failed to send ACK for {req_id[:8]}: {e}")

    # [SEALER 측] 백그라운드 Mempool Consumer 스레드
    def start_sealer_consumer(self) -> None:
        if self._running and self._consumer_thread and self._consumer_thread.is_alive():
            return
        self._running = True
        self._consumer_thread = threading.Thread(
            target=self._consume_loop_sync,
            name=f"MempoolSealer-{self.consumer_name}",
            daemon=True,
        )
        self._consumer_thread.start()
        log.info(f"[Mempool:SEALER] Background Consumer Thread [{self.consumer_name}] started.")

    def ensure_sealer_consumer(self) -> None:
        if not self._running or self._consumer_thread is None or not self._consumer_thread.is_alive():
            self.start_sealer_consumer()

    def stop_sealer_consumer(self) -> None:
        self._running = False
        if self._consumer_thread and self._consumer_thread.is_alive():
            self._consumer_thread.join(timeout=1.0)

    def _ensure_groups_sync(self, tunnel: UniversalFacadeSync, topics: list[str]) -> None:
        for topic in topics:
            try:
                tunnel.xgroup_create(topic, self.CONSUMER_GROUP, id="0", mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    raise

    def _consume_loop_sync(self) -> None:
        try:
            consumer_tunnel: UniversalFacadeSync = TunnelFactory.get_isolated_sync()
            topics = [self.stream_logic, self.stream_objects, self.stream_heads]
            self._ensure_groups_sync(consumer_tunnel, topics)
            stream_dict = {t: ">" for t in topics}
        except Exception as e:
            log.critical(f"[Mempool:SEALER] Failed to initialize consumer streams: {e}", exc_info=True)
            self._running = False
            return

        while self._running:
            try:
                events = consumer_tunnel.xreadgroup(
                    groupname=self.CONSUMER_GROUP,
                    consumername=self.consumer_name,
                    streams=stream_dict,
                    count=16,
                    block=500,
                )
                if not events:
                    continue

                for stream_name, messages in events:
                    for msg_id, fields in messages:
                        self._handle_message_sync(consumer_tunnel, stream_name, msg_id, fields)
            except TimeoutError:
                continue
            except Exception as e:
                if not self._running:
                    break
                log.error(f"[Mempool:SEALER] Consumer loop error: {e}", exc_info=True)
                time.sleep(0.2)

    def _handle_message_sync(
        self,
        tunnel: UniversalFacadeSync,
        stream_name: str,
        msg_id: str,
        fields: Dict[str, str],
    ) -> None:
        req_id = fields.get("req_id", "")
        try:
            if stream_name == self.stream_objects:
                obj_type = fields["type"]
                data = json.loads(fields["data"])
                obj_hash = self.store._put_object(obj_type, data)
                self._send_ack_sync(tunnel, req_id, {"status": "OK", "hash": obj_hash})

            elif stream_name == self.stream_heads:
                self.store.update_head(fields["stream_id"], fields["commit_hash"])
                self._send_ack_sync(tunnel, req_id, {"status": "OK"})

            elif stream_name == self.stream_logic:
                if getattr(self.store, "broker", None) is None:
                    log.error(f"[Mempool:SEALER] Rejecting stream '{fields.get('id')}': WASM broker not mounted.")
                    self._send_ack_sync(tunnel, req_id, {"status": "NO_WASM"})
                else:
                    raw_payload = fields.get("payload", "{}")
                    raw_meta = fields.get("metadata", "{}")
                    try:
                        parsed_payload = json.loads(raw_payload)
                    except Exception:
                        parsed_payload = raw_payload
                    try:
                        parsed_meta = json.loads(raw_meta)
                    except Exception:
                        parsed_meta = {}

                    ls = LogicStream(
                        id=fields["id"],
                        action=fields.get("action", "default_action"),
                        payload=parsed_payload,
                        metadata=parsed_meta,
                    )
                    sealed = self.store._seal_local_sync(ls)
                    self._send_ack_sync(tunnel, req_id, {"status": "SEALED", "kernel": sealed.model_dump()})
        except Exception as e:
            log.error(f"[Mempool:SEALER] Error processing {stream_name} ({msg_id}): {e}", exc_info=True)
            self._send_ack_sync(tunnel, req_id, {"status": "ERROR", "msg": str(e)})
        finally:
            try:
                tunnel.stream_ack(stream_name, self.CONSUMER_GROUP, msg_id)
            except Exception as ack_err:
                log.warning(f"[Mempool:SEALER] Stream XACK failed for {msg_id}: {ack_err}")