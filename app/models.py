from typing import Optional
from pydantic import BaseModel, Field


class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str


class TaskStatus(BaseModel):
    task_id: str
    status: str
    progress: float = Field(..., ge=0, le=100)
    images_count: int = Field(..., ge=0)
    processing_time: Optional[int] = None
    error: Optional[str] = None


class TaskSummary(BaseModel):
    task_id: str
    name: str = ""
    status: str
    progress: float = 0.0
    images_count: int = 0
    created_at: str = ""
    error: Optional[str] = None
