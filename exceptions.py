
class TaskError(Exception):
    """Base class for task-related exceptions."""
    pass

class TaskFailed(TaskError):
    def __init__(self, task_id: str, reason: str):
        super().__init__(f"Task {task_id} failed: {reason}")
        self.task_id = task_id
        self.reason = reason

class TaskTimeoutError(TaskFailed):
    def __init__(self, task_id: str):
        super().__init__(task_id, "timeout")

class TaskNotFoundError(TaskError):
    def __init__(self, task_id: str):
        super().__init__(f"Task {task_id} not found.")
        self.task_id = task_id

class TaskCanceledError(TaskError):
    def __init__(self, task_id: str):
        super().__init__(f"Task {task_id} was canceled.")
        self.task_id = task_id