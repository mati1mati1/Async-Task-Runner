

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

        self._running: Dict[int, asyncio.Task] = {}
        self._running_lock = asyncio.Lock()

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
        
    async def cancel(self, task_id: int) -> bool:
        async with self._running_lock:
            running_task = self._running.get(task_id)
            if running_task:
                running_task.cancel()
                return True
            
        async with self._records_lock:
            record = self._records.get(task_id)
            if not record:
                return False

            if record.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELED}:
                return False

            if record.status == TaskStatus.PENDING:
                record.status = TaskStatus.CANCELED
                record.updated_at = DateTime()
                record.error = "cancelled"
                return True

        return False

    async def shutdown(self) -> None:
        self._shutdown_event.set()

        async with self._running_lock:
            for running_task in self._running.values():
                running_task.cancel()

        for worker_task in self._worker_tasks:
            worker_task.cancel()

        await asyncio.gather(*self._worker_tasks, return_exceptions=True)

    async def _worker_loop(self, worker_id: int) -> None:
        while not self._shutdown_event.is_set():
            try:
                task_id, task = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            async with self._records_lock:
                record = self._records.get(task_id)
                if not record:
                    self.queue.task_done()
                    continue

                record.status = TaskStatus.RUNNING
                record.updated_at = DateTime()

            running_task = asyncio.create_task(task.run(record))
            async with self._running_lock:
                self._running[task_id] = running_task

            try:
                res = await asyncio.wait_for(running_task, timeout=task.timeout)
                async with self._records_lock:
                    record.status = TaskStatus.DONE
                    record.updated_at = DateTime()
                    record.result = str(res)
                    
            except asyncio.TimeoutError:
                running_task.cancel()
                async with self._records_lock:
                    record.status = TaskStatus.FAILED
                    record.error = "timeout"
                
            except asyncio.CancelledError:
                async with self._records_lock:
                    record.status = TaskStatus.CANCELED
                    record.updated_at = DateTime()
                    record.error = "cancelled"
                raise
            except Exception as e:
                async with self._records_lock:
                    record.status = TaskStatus.FAILED
                    record.error = str(e)
                    record.updated_at = DateTime()
            finally:
                self.queue.task_done()
                async with self._running_lock:
                    self._running.pop(task_id, None)