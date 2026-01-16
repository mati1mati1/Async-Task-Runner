

import asyncio
from models import TaskStatus
from runner import TaskRunner
from task import HashTask, SleepTask


async def main():
    runner = TaskRunner(num_workers=5)
    await runner.start()

    ids = []
    for i in range(10):
        ids.append(await runner.submit(SleepTask(ms=500 + i * 100)))
    for i in range(10):
        ids.append(await runner.submit(HashTask(text=f"Task number {i}")))

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

    await runner.shutdown()

if __name__ == "__main__":
    asyncio.run(main())