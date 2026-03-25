from pydantic import BaseModel
from typing import Optional

# OCRRequest 已被 Form Data 取代，不再需要用于 Request Body
    
class OCRResponse(BaseModel):
    task_id: str
    status: str
    
class OCRResultResponse(BaseModel):
    task_id: str
    status: str
    filename: Optional[str] = None
    content: Optional[str] = None
    error: Optional[str] = None
    duration: Optional[float] = None
