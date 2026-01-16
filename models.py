import enum
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING
from xmlrpc.client import DateTime

if TYPE_CHECKING:
    from runner import TaskRunner


class TaskStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"

class TaskType(enum.Enum):
    HASH = "hash"
    SLEEP = "sleep"

class SubmitPolicy(enum.Enum):
    REJECT = "reject"
    WAIT = "wait"

@dataclass(slots=True)
class TaskRecord:
    id: int
    name : str
    type: TaskType
    created_at: DateTime
    updated_at: DateTime
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None

    def __str__(self):
        return (
            f"TaskRecord("
            f"id={self.id}, "
            f"name={self.name}, "
            f"status={self.status}, "
            f"type={self.type}, "
            f"created_at={self.created_at}, "
            f"updated_at={self.updated_at}, "
            f"result={self.result}, "
            f"error={self.error})"
        )
    
@dataclass(frozen=True)
class TaskHandle:
    id: int
    runner: "TaskRunner"

    def __await__(self):
        return self.runner.wait(self.id).__await__()

    async def cancel(self) -> bool:
        return await self.runner.cancel(self.id)

    async def record(self):
        return await self.runner.get_record(self.id)