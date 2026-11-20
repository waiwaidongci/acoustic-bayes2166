import json,time,logging
from redis import Redis
from sqlalchemy import select
from .config.database import Session
from .config.settings import REDIS_URL,ARTIFACT_DIR
from .models.run import Run
from .models import source,record,user,note,audit,spares
from .services.algorithm import calculate
from .services.audit import log
from .services.export import csv_response
from .services import spares as spares_service
queue=Redis.from_url(REDIS_URL)
ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
def execute(id):
    with Session() as db:
        r=db.get(Run,id)
        if not r or r.state!='queued':return
        r.state='running';db.commit()
        try:
            result=calculate(r.snapshot['records'],r.parameters)
            json.dumps(result,allow_nan=False)
            r.result=result;r.state='completed'
            response=csv_response(result,'run.csv')
            (ARTIFACT_DIR/f'run-{r.id}.csv').write_bytes(response.body)
            log(db,'worker','run.completed',r.id)
        except Exception as e:
            logging.exception('analysis failed');r.state='failed';r.error=str(e)[:2000]
            log(db,'worker','run.failed',r.id,r.error)
        db.commit()
        if r.state=='completed':
            try:spares_service.seed_demo_for(r.id)
            except Exception:logging.exception('spares demo seed failed')
def main():
    last_sweep=0.0;first=True
    while True:
        (ARTIFACT_DIR/'heartbeat').write_text(str(time.time()))
        if first or time.monotonic()-last_sweep>=15:
            try:spares_service.recover(force=first)
            except Exception:logging.exception('spares recovery sweep failed')
            first=False;last_sweep=time.monotonic()
        value=queue.blpop(['analysis:queue',spares_service.QUEUE_KEY],timeout=2)
        if value:
            if value[0].endswith(b'spares:queue'):
                try:spares_service.execute_end(int(value[1]))
                except Exception:logging.exception('spares end failed')
            else:execute(int(value[1]))
if __name__=='__main__':main()
