import logging
import asyncio
import os
import json
from datetime import timezone, datetime

log = logging.getLogger(__name__)


class BaseCheckPointer:
    def __init__(self, name="", id=None, loop=None):
        self.loop = loop if loop else asyncio.get_event_loop()
        self._id = id if id else os.getpid()
        self._name = name
        self._items = {}

    def get_id(self):
        return self._id

    def get_ref(self):
        return "{}/{}".format(self._name, self._id)

    def get_all_checkpoints(self):
        return self._items.copy()

    def get_checkpoint(self, shard_id):
        return self._items.get(shard_id)

    async def close(self):
        log.info("{} stopping..".format(self.get_ref()))
        await asyncio.gather(
            *[self.deallocate(shard_id) for shard_id in self._items.keys()]
        )

    def is_allocated(self, shard_id):
        return shard_id in self._items


class BaseHeartbeatCheckPointer(BaseCheckPointer):
    def __init__(
        self, name, id=None, session_timeout=60, heartbeat_frequency=15, loop=None
    ):
        super().__init__(name=name, id=id, loop=loop)

        self.session_timeout = session_timeout
        self.heartbeat_frequency = heartbeat_frequency

        self.heartbeat_task = asyncio.Task(self.heartbeat(), loop=self.loop)

    async def close(self):
        log.debug("Cancelling heartbeat task..")
        self.heartbeat_task.cancel()

        await super().close()

    async def heartbeat(self):
        while True:
            await asyncio.sleep(self.heartbeat_frequency, loop=self.loop)

            # todo: don't heartbeat if checkpoint already updated it recently
            for shard_id, sequence in self._items.items():
                key = self.get_key(shard_id)
                val = {"ref": self.get_ref(), "ts": self.get_ts(), "sequence": sequence}
                log.info("Heartbeating {}@{}".format(shard_id, sequence))
                await self.do_heartbeat(key, val)


class MemoryCheckPointer(BaseCheckPointer):
    async def deallocate(self, shard_id):
        log.info(
            "{} deallocated on {}@{}".format(
                self.get_ref(), shard_id, self._items[shard_id]
            )
        )
        self._items[shard_id]["active"] = False

    def is_allocated(self, shard_id):
        return shard_id in self._items and self._items[shard_id]["active"]

    async def allocate(self, shard_id):
        if shard_id not in self._items:
            self._items[shard_id] = {"sequence": None}

        self._items[shard_id]["active"] = True

        return True, self._items[shard_id]["sequence"]

    async def checkpoint(self, shard_id, sequence):
        log.debug(
            "{} checkpointed on {} @ {}".format(self.get_ref(), shard_id, sequence)
        )
        self._items[shard_id]["sequence"] = sequence


class RedisCheckPointer(BaseHeartbeatCheckPointer):
    def __init__(
        self, name, id=None, session_timeout=60, heartbeat_frequency=15, loop=None
    ):
        super().__init__(
            name=name,
            id=id,
            session_timeout=session_timeout,
            heartbeat_frequency=heartbeat_frequency,
            loop=loop,
        )

        # todo StrictRedisCluster
        from aredis import StrictRedis

        self.client = StrictRedis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"), loop=self.loop
        )

    async def do_heartbeat(self, key, value):
        await self.client.set(key, json.dumps(value))

    def get_key(self, shard_id):
        return "pyredis-{}-{}".format(self._name, shard_id)

    def get_ts(self):
        return round(int(datetime.now(tz=timezone.utc).timestamp()))

    async def checkpoint(self, shard_id, sequence):

        key = self.get_key(shard_id)

        val = {"ref": self.get_ref(), "ts": self.get_ts(), "sequence": sequence}

        previous_val = await self.client.getset(key, json.dumps(val))
        previous_val = json.loads(previous_val) if previous_val else None

        if not previous_val:
            raise NotImplementedError(
                "{} checkpointed on {} but key did not exist?".format(
                    self.get_ref(), shard_id
                )
            )

        if previous_val["ref"] != self.get_ref():
            raise NotImplementedError(
                "{} checkpointed on {} but ref is different".format(
                    self.get_ref(), shard_id, val["ref"]
                )
            )

        log.debug("{} checkpointed on {}@{}".format(self.get_ref(), shard_id, sequence))
        self._items[shard_id] = sequence

    async def deallocate(self, shard_id):

        key = self.get_key(shard_id)

        val = {"ref": None, "ts": None, "sequence": self._items[shard_id]}

        await self.client.set(key, json.dumps(val))

        log.info(
            "{} deallocated on {}@{}".format(
                self.get_ref(), shard_id, self._items[shard_id]
            )
        )

        self._items.pop(shard_id)

    async def allocate(self, shard_id):

        key = self.get_key(shard_id)

        ts = self.get_ts()

        # try to set lock
        success = await self.client.set(
            key,
            json.dumps({"ref": self.get_ref(), "ts": ts, "sequence": None}),
            nx=True,
        )

        val = await self.client.get(key)
        val = json.loads(val) if val else None

        original_ts = val["ts"]

        if success:
            log.info(
                "{} allocated {} (new checkpoint)".format(self.get_ref(), shard_id)
            )
            self._items[shard_id] = None
            return True, None

        if val["ts"]:

            log.info(
                "{} could not allocate {}, still in use by {}".format(
                    self.get_ref(), shard_id, val["ref"]
                )
            )

            age = ts - original_ts

            # still alive?
            if age < self.session_timeout:
                return False, None

            log.info(
                "Attempting to take lock as {} is {} seconds over due..".format(
                    val["ref"], age - self.session_timeout
                )
            )

        val["ref"] = self.get_ref()
        val["ts"] = ts

        previous_val = await self.client.getset(key, json.dumps(val))
        previous_val = json.loads(previous_val) if previous_val else None

        if previous_val["ts"] != original_ts:
            log.info("{} beat me to the lock..".format(previous_val["ref"]))
            return False, None

        log.info(
            "{} allocating {}@{}".format(self.get_ref(), shard_id, val["sequence"])
        )

        self._items[shard_id] = val["sequence"]

        return True, val["sequence"]