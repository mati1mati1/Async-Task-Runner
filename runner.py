

from ast import Dict, List, Tuple
import asyncio
from typing import Optional
from xmlrpc.client import DateTime

from models import TaskRecord, TaskStatus
from task import BaseTask


class TaskRunner:
    def __init__(self, num_workers: int = 4):
        self.num_workers = num_workers
        self.queue: asyncio.Queue[Tuple[int, BaseTask]] = asyncio.Queue()

        self._records: Dict[int, TaskRecord] = {}
        self._records_lock = asyncio.Lock()

        self._next_id = 0
        self._id_lock = asyncio.Lock()

        self._worker_tasks: List[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()
    
    async def start(self) -> None:
        for worker_id in range(self.num_workers):
            worker_task = asyncio.create_task(self._worker_loop(worker_id))
            self._worker_tasks.append(worker_task)


    async def submit(self, task: BaseTask) -> int:
        async with self._id_lock:
            recored_id = self._next_id
            self._next_id += 1
        record = TaskRecord(id=recored_id, name=task.name, type=task.type, created_at=DateTime(), updated_at=DateTime())
        async with self._records_lock:
            self._records[recored_id] = record

        await self.queue.put((recored_id, task))
        return recored_id
    
    async def get_record(self, task_id: int) -> Optional[TaskRecord]:
        async with self._records_lock:
            return self._records.get(task_id)

    async def shutdown(self) -> None:
        # TODO: signal shutdown + cancel workers + await them
        self._shutdown_event.set()
        for worker_task in self._worker_tasks:
            worker_task.cancel()
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)

    async def _worker_loop(self, worker_id: int) -> None:
        # TODO: read from queue, mark RUNNING, await task.run(), update DONE/FAILED
        while not self._shutdown_event.is_set():
            try:
                task_id, task = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            async with self._records_lock:
                record = self._records.get(task_id)
                if record:
                    record.status = TaskStatus.RUNNING
                    record.updated_at = DateTime()

            try:
                await task.run(record)
                async with self._records_lock:
                    record = self._records.get(task_id)
                    if record:
                        record.status = TaskStatus.DONE
                        record.updated_at = DateTime()

            except Exception as e:
                async with self._records_lock:
                    record = self._records.get(task_id)
                    if record:
                        record.status = TaskStatus.FAILED
                        record.error = str(e)
                        record.updated_at = DateTime()
            finally:
                self.queue.task_done()