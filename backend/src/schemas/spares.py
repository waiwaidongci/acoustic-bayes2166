from pydantic import BaseModel,Field

class SparesInput(BaseModel):
    target_label:str=Field(min_length=1,max_length=120)
    tolerance:str|float|int
    counts:dict
