from contextlib import asynccontextmanager
from importlib import import_module
from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from app.config import ROOT, META, MODULES, DB_PATH
from app.errors import BusinessError
from app.engine import evaluate
from app import store, signoff
from app.archive import serialize

@asynccontextmanager
async def lifespan(app):
    store.save(DB_PATH,evaluate())
    yield
app=FastAPI(title=META['title'],lifespan=lifespan)
app.mount('/static',StaticFiles(directory=str(ROOT/'app/static')),name='static')
@app.exception_handler(BusinessError)
async def business_error(request:Request,error:BusinessError):
    return JSONResponse({'code':error.code,'detail':error.message},status_code=error.status)
@app.get('/')
def home():
    return RedirectResponse('/'+MODULES[0],status_code=303)
@app.get('/healthz')
def health():
    return {'status':'ok','project':META['short']}
@app.post('/api/runs')
def create(payload:dict=Body(default={})):
    return store.save(DB_PATH,evaluate(payload))
@app.post('/api/runs/{run_id}/rerun')
def rerun(run_id:str):
    previous=store.get(DB_PATH,run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    return store.save(DB_PATH,evaluate({'dataset':previous['source']},snapshot=previous['input_snapshot']))
@app.get('/api/runs')
def runs():
    return store.history(DB_PATH)
@app.get('/api/runs/{run_id}')
def run(run_id:str):
    result=store.get(DB_PATH,run_id)
    result['signoff']=_adjudication(result)
    return result


def _adjudication(result):
    feed=result.get('feed',result.get('source','dirty'))
    return signoff.adjudicate(
        result,
        signatures=store.signoffs_for_feed(DB_PATH,feed),
        locks=store.locks_for_feed(DB_PATH,feed),
        latest_run_id=store.latest_for_feed(DB_PATH,feed))


@app.get('/api/runs/{run_id}/signoff')
def signoff_state(run_id:str):
    return _adjudication(store.get(DB_PATH,run_id))


@app.post('/api/runs/{run_id}/signoff')
def signoff_submit(run_id:str,payload:dict=Body(...)):
    result=store.get(DB_PATH,run_id)
    reviewer=signoff.normalize_reviewer(payload.get('reviewer'))
    opinion=signoff.normalize_opinion(payload.get('opinion'))
    note=payload.get('note') if isinstance(payload.get('note'),str) else ''
    basis=payload.get('basis_hash')
    if not reviewer: raise BusinessError('BAD_REVIEWER','复核人不能为空')
    if opinion is None: raise BusinessError('BAD_OPINION','意见须为同意或不同意')
    if not isinstance(basis,str) or not basis: raise BusinessError('BAD_BASIS','缺少所核对的批次依据')
    revision=payload.get('revision')
    if revision is not None and (not isinstance(revision,int) or revision<1): raise BusinessError('BAD_REVISION','版本号须为正整数')
    if result['latest_run_id']!=run_id:
        raise BusinessError('SUPERSEDED','已有更新批次，请在最新批次上重签；当前批次仅作历史留档',409)
    current=signoff.basis_hash(result)
    if basis!=current:
        raise BusinessError('BASIS_CHANGED','批次或依据已更新，请刷新页面重看后再签',409)
    if revision is not None and revision!=result['revision']:
        raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    record=store.record_signoff(DB_PATH,result,current,reviewer,opinion,signoff.snapshot(result),note)
    created_new=bool(record.pop('created_new',False))
    adjudication=_adjudication(store.get(DB_PATH,run_id))
    lock=None
    lock_error=None
    if adjudication['release_allowed']:
        rows=store.approvals_for_basis(DB_PATH,run_id,current)
        try:
            lock=store.create_lock(DB_PATH,result,current,rows,adjudication['approved_reviewers'])
        except BusinessError as error:
            # 并发改判/上游更新使双签在落锁前失效：签署保留，返回最新裁决。
            if error.code not in {'DUAL_SIGN_REQUIRED','SUPERSEDED'}: raise
            lock_error={'code':error.code,'detail':error.message}
    adjudication=_adjudication(store.get(DB_PATH,run_id))
    response={'signature':record,'created_new':created_new,
              'lock_created':bool(lock),'signoff':adjudication,
              'dual':adjudication['dual'],'status':adjudication['status'],
              'release_allowed':adjudication['release_allowed']}
    if lock_error: response['lock_error']=lock_error
    return response


@app.post('/api/runs/{run_id}/review')
def review(run_id:str,payload:dict=Body(...)):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.review(DB_PATH,run_id,revision)
@app.get('/api/export/{run_id}')
def export(run_id:str):
    result=store.get(DB_PATH,run_id)
    adjudication=_adjudication(result)
    return Response(serialize(result,adjudication),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{run_id}.zip"'})
for key in MODULES:
    app.include_router(import_module(f'app.modules.{key}.handler').router)
