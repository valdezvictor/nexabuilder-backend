from pydantic import BaseModel

class TradeOut(BaseModel):
    id: int
    code: str
    name: str

    class Config:
        orm_mode = True
