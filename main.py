

import asyncio
from models import SubmitPolicy, TaskStatus
from runner import TaskRunner
from task import HashTask, SleepTask


async def main():
    runner = TaskRunner(num_workers=5, max_queue_size=10, submit_policy=SubmitPolicy.WAIT)
    await runner.start()
    for i in range(5):
        print(await runner.stats())
        await create_and_run(runner)
    await runner.shutdown()

async def create_and_run(runner: TaskRunner):
    
    ids = []
    for i in range(10):
        try:
            ids.append(await runner.submit(SleepTask(ms=500 + i * 100)))
        except asyncio.QueueFull:
            print(f"Failed to submit SleepTask {i}: queue is full")
    for i in range(10):
        try:
            ids.append(await runner.submit(HashTask(text=f"Task number {i}")))
        except asyncio.QueueFull:
            print(f"Failed to submit HashTask {i}: queue is full")

    while True:
        done = 0
        for task_id in ids:
            record = await runner.get_record(task_id)
            if not record:
                continue
            if record and record.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELED}:
                done += 1
            print(f"Task {task_id} - Status: {record.status}, Result: {record.result}, Error: {record.error}")
        
        print(f"Completed {done}/{len(ids)} tasks.")
        if done == len(ids):
            break
        await asyncio.sleep(0.2)

        for task_id in ids:
            record = await runner.get_record(task_id)
            if record:
                print(record)

    print(await runner.stats())

if __name__ == "__main__":
    asyncio.run(main())