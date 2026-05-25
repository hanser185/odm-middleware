from typing import Optional
from pydantic import BaseModel


class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str


class TaskStatus(BaseModel):
    task_id: str
    status: str
    progress: float
    images_count: int
    processing_time: Optional[int] = None
    error: Optional[str] = None
