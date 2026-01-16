# Async Task Runner (Python / asyncio)

## 1. Project Overview

This repository implements a small but fully‑featured **asyncio-based task runner** in Python.

It is designed as a learning and reference project for:

- Structuring long‑running background workers with `asyncio`.
- Using `asyncio.Queue`, `asyncio.Task`, `asyncio.Future`, and `asyncio.Event` together.
- Safely coordinating shared state with `asyncio.Lock`.
- Handling **timeouts**, **cancellation**, and **result propagation** in a clean, OOP‑friendly way.

You can submit tasks (like sleeping or hashing text), have a pool of worker coroutines execute them concurrently, and then either **await results** or **poll status** until completion. The project also demonstrates common pitfalls (like "coroutine was never awaited") and how to avoid them.


## 2. Architecture

### 2.1 High-Level Diagram

```text
+-------------------+            +--------------------+
|  main.py          |  submit    | TaskRunner         |
|                   +----------->| - queue            |
| - builds runner   |            | - records          |
| - submits tasks   |            | - running tasks    |
| - awaits results  |<-----------+ - futures          |
+-------------------+   wait()   | - workers (tasks)  |
                               ^ +--------------------+
                               |
                               | uses
                               v
                        +----------------+
                        |  BaseTask      |
                        |  SleepTask     |
                        |  HashTask      |
                        +----------------+

                        +----------------+
                        | models.py      |
                        | - TaskRecord   |
                        | - TaskStatus   |
                        | - TaskType     |
                        | - TaskHandle   |
                        | - SubmitPolicy |
                        +----------------+
```

### 2.2 Key Components

#### `TaskRunner` (`runner.py`)

Responsibilities:

- Owns an `asyncio.Queue[(task_id, task)]` that buffers submitted tasks.
- Spawns N worker loops with `asyncio.create_task(self._worker_loop(...))` in `start()`.
- Maintains:
  - `_records: Dict[int, TaskRecord]` guarded by `_records_lock` (an `asyncio.Lock`).
  - `_running: Dict[int, asyncio.Task]` guarded by `_running_lock`.
  - `_futures: Dict[int, asyncio.Future[str]]` guarded by `_futures_lock`.
- Exposes high‑level methods:
  - `submit(task: BaseTask) -> Optional[TaskHandle]`
  - `wait(task_id: int) -> str`
  - `get_record(task_id: int) -> Optional[TaskRecord]`
  - `cancel(task_id: int) -> bool`
  - `stats() -> dict`
  - `start()` and `shutdown()` for lifecycle.

#### `BaseTask` and Task Types (`task.py`)

- `BaseTask` is an abstract base class with:
  - `@property def type(self) -> TaskType`
  - `@property def timeout(self) -> float`
  - `@property def name(self) -> str`
  - `async def run(self, task: TaskRecord)` – the actual work.

Concrete implementations:

- `SleepTask`:
  - Simulates IO‑bound work with `asyncio.sleep`.
  - Demonstrates a short timeout, so some tasks can time out.
- `HashTask`:
  - Performs CPU‑ish work by hashing a string with `hashlib.sha256`.
  - Uses `asyncio.to_thread` to run the **sync** `compute_hash` method in a worker thread without blocking the event loop.

#### Models and Enums (`models.py`)

- `TaskStatus` – lifecycle states: `PENDING`, `RUNNING`, `DONE`, `FAILED`, `CANCELED`.
- `TaskType` – task categories: `HASH`, `SLEEP`.
- `SubmitPolicy` – queue backpressure behavior:
  - `REJECT` – raise `asyncio.QueueFull` immediately.
  - `WAIT` – wait until space is available and then enqueue.
- `TaskRecord` – dataclass that tracks metadata and state of each task:
  - `id`, `name`, `type`, `created_at`, `updated_at`, `status`, `result`, `error`.
- `TaskHandle` – small, awaitable handle for a submitted task:
  - `id: int`, `runner: TaskRunner`.
  - Implements `__await__` by delegating to `runner.wait(self.id)`.
  - Methods: `cancel()`, `record()`.

#### Submit Policy

Queue full behavior is controlled by `SubmitPolicy` and respected in `TaskRunner.submit`:

- `REJECT`: the runner uses `queue.put_nowait` and, on `QueueFull`, removes any provisional `TaskRecord` and `Future` and re‑raises the error.
- `WAIT`: if `put_nowait` fails, it falls back to `await queue.put(...)`, blocking the submitter until queue space is available.


## 3. End-to-End Flow

This describes the full lifecycle: **submit → queue → workers → task execution → result propagation → shutdown**.

1. **Start the runner**
   - `main.py` does:

     ```python
     runner = TaskRunner(num_workers=5, max_queue_size=10, submit_policy=SubmitPolicy.WAIT)
     await runner.start()
     ```

   - `start()` creates N worker tasks using `asyncio.create_task(self._worker_loop(worker_id))`.

2. **Submit a task**
   - You create a `BaseTask` implementation (e.g., `SleepTask`, `HashTask`) and call:

     ```python
     handle = await runner.submit(SleepTask(ms=500))
     ```

   - Internally, `submit`:
     - Allocates a new `task_id` under `_id_lock`.
     - Creates a `TaskRecord` with `status=PENDING` and stores it in `_records` under `_records_lock`.
     - Creates an `asyncio.Future` and stores it in `_futures` under `_futures_lock`.
     - Enqueues `(task_id, task)` onto `self.queue` using `put_nowait` (or waits if the queue is full and `SubmitPolicy.WAIT` is set).
     - Returns a `TaskHandle` containing the `task_id` and reference to the `TaskRunner`.

3. **Worker picks up from the queue**
   - Each worker loop runs roughly:

     ```python
     while not self._shutdown_event.is_set():
         try:
             task_id, task = await asyncio.wait_for(self.queue.get(), timeout=1.0)
         except asyncio.TimeoutError:
             continue
     ```

   - `asyncio.wait_for` around `queue.get()` allows workers to periodically wake up and check for shutdown.
   - When a work item arrives, the worker:
     - Fetches the corresponding `TaskRecord` under `_records_lock`.
     - Marks `status = RUNNING` and updates `updated_at`.

4. **Task execution**
   - The worker creates a dedicated `asyncio.Task` for the task’s `run` method:

     ```python
     running_task = asyncio.create_task(task.run(record))
     async with self._running_lock:
         self._running[task_id] = running_task
     ```

   - Then it enforces the per‑task timeout:

     ```python
     res = await asyncio.wait_for(running_task, timeout=task.timeout)
     ```

   - On **success**:
     - Updates `TaskRecord` with `status = DONE`, `result = str(res)` and refreshed `updated_at`.

   - On **timeout**:
     - Cancels the underlying `running_task` via `running_task.cancel()`.
     - Marks the record as `FAILED` with `error = "timeout"`.

   - On **cancellation** (e.g., via `runner.cancel(task_id)`):
     - A `CancelledError` is raised.
     - Worker marks `status = CANCELED`, sets `error = "cancelled"`, and re‑raises so caller logic can see the cancellation.

   - On **any other exception**:
     - Marks `status = FAILED` and records `error = str(e)`.

5. **Result propagation to callers**

   - In `finally`, the worker always:
     - Calls `self.queue.task_done()`.
     - Removes the `task_id` from `_running` under `_running_lock`.
     - Completes the associated `Future` under `_futures_lock`:

       ```python
       fut = self._futures.get(task_id)
       if fut and not fut.done():
           if record.status == TaskStatus.DONE:
               fut.set_result(record.result)
           elif record.status == TaskStatus.CANCELED:
               fut.cancel()
           else:
               fut.set_exception(RuntimeError(record.error or "failed"))
       ```

   - `TaskRunner.wait(task_id)` simply awaits this `Future` and then removes it from `_futures`.
   - `TaskHandle.__await__` delegates to `runner.wait(self.id)`, so you can `await handle` directly.

6. **Shutdown**

   - `main.py` eventually calls:

     ```python
     await runner.shutdown()
     ```

   - `shutdown`:
     - Sets `_shutdown_event` so worker loops exit their `while` loop once they wake.
     - Cancels all currently running task objects in `_running` under `_running_lock`.
     - Cancels all worker tasks in `_worker_tasks`.
     - Awaits all worker tasks with `asyncio.gather(..., return_exceptions=True)` so shutdown is graceful and does not leak tasks.


## 4. AsyncIO Concepts Used

Each subsection explains an asyncio concept, why it is used here, where it appears, and provides a small snippet.

### 4.1 Event Loop Basics (`async` / `await`)

**What it is**: The event loop runs asynchronous coroutines. Functions defined with `async def` return coroutines that must be awaited.

**Why here**: The entire runner is built around asynchronous execution: tasks, workers, and client code all use `async`/`await`.

**Where**: `main.py`, `TaskRunner` methods (`start`, `submit`, `wait`, `shutdown`, etc.), and all `BaseTask.run` implementations.

**Example** (`main.py`):

```python
async def main():
    runner = TaskRunner(num_workers=5, max_queue_size=10, submit_policy=SubmitPolicy.WAIT)
    await runner.start()
    await create_and_run_future(runner)
    await runner.shutdown()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

### 4.2 `asyncio.create_task`

**What it is**: Schedules a coroutine to run concurrently as an `asyncio.Task`.

**Why here**: Used to start worker loops and to run individual task’s `run` method concurrently.

**Where**: In `TaskRunner.start` and `_worker_loop`.

**Example**:

```python
# In TaskRunner.start
for worker_id in range(self.num_workers):
    worker_task = asyncio.create_task(self._worker_loop(worker_id))
    self._worker_tasks.append(worker_task)

# In TaskRunner._worker_loop
running_task = asyncio.create_task(task.run(record))
```

### 4.3 `asyncio.gather` (with `return_exceptions=True`)

**What it is**: Waits for multiple awaitables to complete; returns results in order. With `return_exceptions=True`, exceptions are collected instead of bubbling immediately.

**Why here**: The demo collects results from multiple `TaskHandle` objects without failing on the first error/timeout.

**Where**: `create_and_run_future` in `main.py` and in `TaskRunner.shutdown` (to wait for worker tasks).

**Example** (`main.py`):

```python
async def create_and_run_future(runner: TaskRunner):
    handles = await submit_tasks(runner)
    results = await asyncio.gather(*handles, return_exceptions=True)
    print(results)
```

### 4.4 `asyncio.Queue` (backpressure, `maxsize`, `put_nowait` vs `put`)

**What it is**: A FIFO queue for coordinating producer/consumer coroutines.

**Why here**: Connects submitters to worker loops and provides **backpressure** via `maxsize`.

**Where**: `self.queue` in `TaskRunner.__init__`, `submit`, and `_worker_loop`.

**Example** (`runner.py`):

```python
self.queue: asyncio.Queue[tuple[int, BaseTask]] = asyncio.Queue(maxsize=max_queue_size)

# Producer side
try:
    self.queue.put_nowait((task_id, task))
except asyncio.QueueFull:
    if self.submit_policy == SubmitPolicy.REJECT:
        ...  # cleanup and re-raise
    elif self.submit_policy == SubmitPolicy.WAIT:
        await self.queue.put((task_id, task))

# Consumer side
task_id, task = await asyncio.wait_for(self.queue.get(), timeout=1.0)
```

- `put_nowait` enforces backpressure immediately.
- `put` waits until the queue has room.

### 4.5 `asyncio.Lock` (protecting shared state)

**What it is**: A mutual exclusion primitive for coroutines.

**Why here**: `TaskRunner` maintains shared dictionaries (`_records`, `_running`, `_futures`) that are touched from multiple coroutines (workers, submitters, waiters). Locks prevent inconsistent state and race conditions.

**Where**: `_records_lock`, `_running_lock`, `_futures_lock`, `_id_lock` in `TaskRunner`.

**Example**:

```python
async with self._records_lock:
    self._records[task_id] = record

async with self._running_lock:
    self._running[task_id] = running_task
```

### 4.6 `asyncio.Event` (shutdown signalling)

**What it is**: A simple flag that coroutines can wait on or check.

**Why here**: To tell workers to stop looping and begin shutdown.

**Where**: `_shutdown_event` in `TaskRunner`.

**Example**:

```python
# Worker loop condition
while not self._shutdown_event.is_set():
    ...

# Shutdown path
async def shutdown(self) -> None:
    self._shutdown_event.set()
    ...  # cancel running tasks and workers
```

### 4.7 `asyncio.Future` (coordination primitive)

**What it is**: A low-level awaitable that will eventually have a result, exception, or be cancelled.

**Why here**: Bridges between worker execution and callers waiting for results. Each submitted task has a corresponding `Future` that is completed by the worker.

**Where**: `_futures` in `TaskRunner`, `TaskRunner.wait`, and worker `finally` block.

**Example**:

```python
# When submitting
fut = asyncio.get_running_loop().create_future()
async with self._futures_lock:
    self._futures[task_id] = fut

# When waiting
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
```

The worker completes the future with `set_result`, `set_exception`, or `cancel` depending on `TaskStatus`.

### 4.8 `asyncio.wait_for` (timeouts)

**What it is**: Wraps an awaitable and enforces a maximum wait time.

**Why here**: 

- To prevent workers from blocking forever on `queue.get` during shutdown.
- To give each task its own **execution timeout**.

**Where**: In `_worker_loop` around `queue.get` and `running_task`.

**Example**:

```python
# Wake workers periodically even with empty queue
try:
    task_id, task = await asyncio.wait_for(self.queue.get(), timeout=1.0)
except asyncio.TimeoutError:
    continue

# Per-task timeout
try:
    res = await asyncio.wait_for(running_task, timeout=task.timeout)
except asyncio.TimeoutError:
    running_task.cancel()
    ...  # mark FAILED with error="timeout"
```

### 4.9 Cancellation Model (`task.cancel()`, `CancelledError`)

**What it is**: Cancelling an `asyncio.Task` requests that it raise `CancelledError` at its next `await` and unwind.

**Why here**: 

- `runner.cancel(task_id)` lets callers cancel in‑flight work.
- `shutdown` cancels all running tasks.

**Where**: `TaskRunner.cancel`, `TaskRunner.shutdown`, and `_worker_loop`’s `except asyncio.CancelledError` block.

**Example**:

```python
# External cancellation
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
    ...

# Worker sees cancellation
except asyncio.CancelledError:
    async with self._records_lock:
        record.status = TaskStatus.CANCELED
        record.updated_at = DateTime()
        record.error = "cancelled"
    raise
```

### 4.10 `asyncio.to_thread` and Executors

**What it is**: `asyncio.to_thread(func, *args)` runs a synchronous function in a default thread pool and returns an awaitable.

**Why here**: `HashTask.compute_hash` is a synchronous CPU‑bound function. Running it directly inside the event loop would block other coroutines. `to_thread` offloads it to a worker thread while presenting an `await`-friendly interface.

**Where**: In `HashTask.run`.

**Example** (`task.py`):

```python
class HashTask(BaseTask):
    ...
    async def run(self, task: TaskRecord):
        return await asyncio.to_thread(self.compute_hash, self.text)

    def compute_hash(self, data: str) -> str:
        hash_object = hashlib.sha256(data.encode())
        return hash_object.hexdigest()
```

**Difference from `ThreadPoolExecutor` / `ProcessPoolExecutor`**:

- `asyncio.to_thread` is a **convenience wrapper** that submits work to the default thread pool used by the event loop. You don’t manage the executor directly.
- With `ThreadPoolExecutor` / `ProcessPoolExecutor`, you explicitly create and manage executors and often submit work using `loop.run_in_executor(executor, func, *args)`.
- For simple offloading of a few blocking calls, `to_thread` is usually simpler and less error‑prone.


## 5. Usage

### 5.1 Prerequisites

- Python **3.10+** (the project uses modern `asyncio` APIs like `asyncio.to_thread`).

Install any dependencies (here it’s only the Python standard library).

### 5.2 Running the Demo

From the project root:

```bash
python3 ./main.py
```

What happens:

- A `TaskRunner` is created with 5 workers and a queue size of 10.
- 10 `SleepTask` and 10 `HashTask` instances are submitted.
- The code awaits all tasks using `asyncio.gather` and prints the resulting list.

Example of printed results (shape, not exact values):

```text
['slept for 500 ms', 'slept for 600 ms', ..., RuntimeError('timeout'), ..., 'c52e20a0ef9...', ...]
```

- Some `SleepTask`s may fail with `RuntimeError('timeout')` because their timeout is intentionally short.
- All `HashTask`s should return SHA‑256 hashes of the corresponding input strings.


## 6. Examples

### 6.1 Submit Tasks and Await Results with `runner.wait`

```python
from runner import TaskRunner
from task import SleepTask
from models import SubmitPolicy

async def run_single():
    runner = TaskRunner(num_workers=1, max_queue_size=10, submit_policy=SubmitPolicy.WAIT)
    await runner.start()

    handle = await runner.submit(SleepTask(ms=1000))

    # Option 1: use TaskHandle directly
    result = await handle

    # Option 2: use runner.wait(task_id)
    # result = await runner.wait(handle.id)

    print("Result:", result)
    await runner.shutdown()
```

### 6.2 Batch Awaiting Using `gather`

```python
from runner import TaskRunner
from task import SleepTask, HashTask
from models import SubmitPolicy
import asyncio

async def batch_example():
    runner = TaskRunner(num_workers=5, max_queue_size=10, submit_policy=SubmitPolicy.WAIT)
    await runner.start()

    handles = []
    for i in range(3):
        handles.append(await runner.submit(SleepTask(ms=500 + i * 100)))
    for i in range(3):
        handles.append(await runner.submit(HashTask(text=f"Task {i}")))

    results = await asyncio.gather(*handles, return_exceptions=True)
    for res in results:
        print(res)

    await runner.shutdown()
```

### 6.3 Cancelling a Task

```python
from runner import TaskRunner
from task import SleepTask
from models import SubmitPolicy
import asyncio

async def cancel_example():
    runner = TaskRunner(num_workers=1, max_queue_size=10, submit_policy=SubmitPolicy.WAIT)
    await runner.start()

    handle = await runner.submit(SleepTask(ms=5000))  # long sleep

    # Cancel after a short delay
    await asyncio.sleep(0.1)
    await handle.cancel()

    try:
        result = await handle
    except asyncio.CancelledError:
        print("Task was cancelled")

    await runner.shutdown()
```

### 6.4 Handling Timeouts

```python
from runner import TaskRunner
from task import SleepTask
from models import SubmitPolicy
import asyncio

async def timeout_example():
    runner = TaskRunner(num_workers=1, max_queue_size=10, submit_policy=SubmitPolicy.WAIT)
    await runner.start()

    # SleepTask.timeout is intentionally short (e.g., 1.0 sec)
    handle = await runner.submit(SleepTask(ms=5000))

    result = await handle  # will likely be RuntimeError('timeout')

    if isinstance(result, Exception):
        print("Task failed:", result)

    await runner.shutdown()
```

In the demo, `asyncio.gather(..., return_exceptions=True)` means timeouts show up as `RuntimeError('timeout')` objects in the result list, not as raised exceptions.


## 7. Stats / Observability

`TaskRunner.stats` provides a simple snapshot of the runner’s current state:

```python
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
```

Interpretation:

- `queue_size`: how many items are waiting in the queue.
- `running`: how many tasks are currently executing.
- `pending`: submitted but not yet running.
- `done`: successfully finished tasks.
- `failed`: tasks that raised exceptions or timed out.
- `cancelled`: tasks cancelled via the API or shutdown.

Example usage:

```python
print(await runner.stats())
# {'queue_size': 0, 'running': 0, 'pending': 0, 'done': 20, 'failed': 0, 'cancelled': 0}
```


## 8. Common Pitfalls and How This Project Avoids Them

### 8.1 "Coroutine Was Never Awaited"

**Bug pattern**:

- Passing an **async function** directly to `asyncio.to_thread` or `run_in_executor`.
- Creating a coroutine and never awaiting it or wrapping it in a task.

Example of what **not** to do:

```python
# WRONG: compute_hash is async here
async def compute_hash(data: str) -> str:
    ...

# This would create a coroutine that is never awaited inside the thread
await asyncio.to_thread(compute_hash, "data")
```

In this project:

- `HashTask.compute_hash` is a **synchronous** function, and `HashTask.run` does:

  ```python
  async def run(self, task: TaskRecord):
      return await asyncio.to_thread(self.compute_hash, self.text)
  ```

  so there is no coroutine leaking into the thread.

- All task `run` methods are awaited exactly once via `asyncio.create_task` + `await asyncio.wait_for(...)`.

### 8.2 Completing or Removing a `Future` Too Early

**Bug pattern**:

- Deleting or completing a `Future` before all interested parties have awaited it.
- Forgetting to complete a `Future`, causing awaiters to hang forever.

In this project:

- The worker is the single authority that completes the `Future`:
  - On DONE: `set_result`.
  - On CANCELLED: `cancel`.
  - On FAILED: `set_exception`.
- `TaskRunner.wait` only removes the `Future` from `_futures` **after** `await fut` finishes (in `finally`).
- Access to `_futures` is always under `_futures_lock` to prevent races.

### 8.3 Cancelling Tasks Without Updating State

**Bug pattern**:

- Calling `task.cancel()` but never catching `CancelledError`, so you don’t update your domain‑level state (`TaskRecord`, logs, metrics).

In this project:

- `TaskRunner.cancel` attempts to cancel both the `Future` and any running task, and updates `TaskRecord` if the task was still pending.
- The worker’s `except asyncio.CancelledError` block:
  - Marks the record as `CANCELED`.
  - Sets a human‑readable `error`.
  - Re‑raises `CancelledError` to preserve proper cancellation semantics.

This ensures that the task’s lifecycle is always reflected correctly in `TaskRecord` and `stats()`.


## 9. Roadmap (Ideas for Extension)

This repository focuses on clarity and core asyncio patterns. Possible future extensions include:

- **Richer `TaskHandle` API**:
  - Convenience methods: `done()`, `result()`, `exception()`, `status()`.
  - Time‑bounded waiting (`await handle.wait(timeout=...)`).

- **Priorities**:
  - Replace `asyncio.Queue` with a priority queue to allow high‑priority tasks to jump ahead.

- **Retries and Backoff**:
  - Configurable retry policies for `FAILED` tasks (with max attempts, backoff strategies, etc.).

- **Persistent Storage**:
  - Persist `TaskRecord`s to disk or a database for long‑running systems.

- **Pluggable Metrics / Logging**:
  - Hooks for logging task lifecycle events and exporting metrics.

These enhancements can be layered on top of the existing architecture without changing its core asyncio patterns.
