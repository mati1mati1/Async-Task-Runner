from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass

from models import TaskRecord, TaskType
import hashlib


class BaseTask(ABC):
    @property
    @abstractmethod
    def type(self) -> TaskType:
        pass

    @property
    def timeout(self) -> float:
        return 10.0  

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    async def run(self, task: TaskRecord):
        """Execute the task with given arguments."""
        pass

@dataclass(frozen=True, slots=True)
class SleepTask(BaseTask):
    ms: int = 1000  
    @property
    def type(self) -> TaskType:
        return TaskType.SLEEP
    
    @property
    def name(self) -> str:
        return f"sleep for {self.ms} ms"
    
    @property
    def timeout(self) -> float:
        return 1.0  
    
    async def run(self, task: TaskRecord):
        await self.sleep(self.ms / 1000)
        return f"slept for {self.ms} ms"
    
    async def sleep(self, seconds: int):
        import asyncio
        await asyncio.sleep(seconds)

@dataclass(frozen=True, slots=True)
class HashTask(BaseTask):
    text: str = "default text"

    @property
    def type(self) -> TaskType:
        return TaskType.HASH

    @property
    def name(self) -> str:
        return f"hash text: {self.text}"
    
    @property
    def timeout(self) -> float:
        return 15.0  

    async def run(self, task: TaskRecord):
        return await asyncio.to_thread(self.compute_hash, self.text)

 
    def compute_hash(self, data: str) -> str:
        hash_object = hashlib.sha256(data.encode())
        return hash_object.hexdigest()


