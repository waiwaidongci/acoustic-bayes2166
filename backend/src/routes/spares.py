from fastapi import APIRouter,Depends
from ..config.database import db_session
from ..schemas.spares import SparesInput
from ..services.auth import current_user,writer
from ..controllers.spares import SparesController as C
router=APIRouter()
@router.get('/runs/{id}/spares')
def spares_for_run(id:int,db=Depends(db_session),u=Depends(current_user)):return C.get(db,id)
@router.post('/runs/{id}/spares')
def create_spares(id:int,data:SparesInput,db=Depends(db_session),u=Depends(writer)):return C.create(db,u,id,data.counts,data.tolerance,data.target_label)
@router.post('/runs/{id}/spares/retry')
def retry_spares(id:int,db=Depends(db_session),u=Depends(writer)):return C.retry(db,u,id)
