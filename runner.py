

import asyncio
from typing import Dict, List, Optional, Tuple
from xmlrpc.client import DateTime

from models import SubmitPolicy, TaskHandle, TaskRecord, TaskStatus
from task import BaseTask


class TaskRunner:
    def __init__(self, num_workers: int = 4, max_queue_size: int = 100, submit_policy: SubmitPolicy = SubmitPolicy.REJECT) -> None:
        self.num_workers = num_workers
        self.submit_policy = submit_policy
        self.queue: asyncio.Queue[Tuple[int, BaseTask]] = asyncio.Queue(maxsize=max_queue_size)

        self._records: Dict[int, TaskRecord] = {}
        self._records_lock = asyncio.Lock()

        self._next_id = 0
        self._id_lock = asyncio.Lock()

        self._running: Dict[int, asyncio.Task] = {}
        self._running_lock = asyncio.Lock()

        self._worker_tasks: List[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()

        self._futures: dict[int, asyncio.Future[str]] = {}
        self._futures_lock = asyncio.Lock()

    
    async def start(self) -> None:
        for worker_id in range(self.num_workers):
            worker_task = asyncio.create_task(self._worker_loop(worker_id))
            self._worker_tasks.append(worker_task)


    async def submit(self, task: BaseTask) -> Optional[TaskHandle]:
        recored_id = await self._allocate_id()
        record = TaskRecord(id=recored_id, name=task.name, type=task.type, created_at=DateTime(), updated_at=DateTime())
        
        async with self._records_lock:
            self._records[recored_id] = record
        
        fut = asyncio.get_running_loop().create_future()
        async with self._futures_lock:
            self._futures[recored_id] = fut


        try:
            self.queue.put_nowait((recored_id, task))
        except asyncio.QueueFull:
            if self.submit_policy == SubmitPolicy.REJECT:
                async with self._records_lock:
                    self._records.pop(recored_id, None)
                async with self._futures_lock:
                    self._futures.pop(recored_id, None)
                raise
            elif self.submit_policy == SubmitPolicy.WAIT:
                await self.queue.put((recored_id, task))

        return TaskHandle(id=recored_id, runner=self)
    
    
    async def _allocate_id(self) -> int:
        async with self._id_lock:
            recored_id = self._next_id
            self._next_id += 1
        return recored_id
    
    async def get_record(self, task_id: int) -> Optional[TaskRecord]:
        async with self._records_lock:
            return self._records.get(task_id)
        
    async def cancel(self, task_id: int) -> bool:
        async with self._futures_lock:
            fut = self._futures.get(task_id)
            if fut and not fut.done():
                fut.cancel()

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

                async with self._futures_lock:
                    fut = self._futures.get(task_id)
                    
                    if fut and not fut.done():
                        if record.status == TaskStatus.DONE:
                            fut.set_result(record.result)
                        elif record.status == TaskStatus.CANCELED:
                            fut.cancel()
                        else:
                            fut.set_exception(RuntimeError(record.error or "failed"))



    async def wait(self, task_id: int) -> str:
        async with self._futures_lock:
            fut = self._futures.get(task_id)
        if not fut:
            raise KeyError(f"task {task_id} not found")

        try:
            return await fut
        finally:
            async with self._futures_lock:
                self._futures.pop(task_id, None)

    async def stats(self) -> dict:
        async with self._records_lock:
            statuses = [r.status for r in self._records.values()]

        async with self._running_lock:
            running = len(self._running)

        return {
            "queue_size": self.queue.qsize(),
            "running": running,
            "pending": statuses.count(TaskStatus.PENDING),
            "done": statuses.count(TaskStatus.DONE),
            "failed": statuses.count(TaskStatus.FAILED),
            "cancelled": statuses.count(TaskStatus.CANCELED),
        }
