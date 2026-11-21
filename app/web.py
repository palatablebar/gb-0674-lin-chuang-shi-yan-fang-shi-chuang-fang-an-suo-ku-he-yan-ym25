from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app import store, signoff

templates=Jinja2Templates(directory=str(ROOT/'app/templates'))

def context_for(state, db_path):
    """用同一份候选批次与依据计算双签、锁库与冻结视图。"""
    feed=state.get('feed',state.get('source','dirty'))
    return signoff.adjudicate(state,
        signatures=store.signoffs_for_feed(db_path,feed),
        locks=store.locks_for_feed(db_path,feed),
        latest_run_id=state.get('latest_run_id') or store.latest_for_feed(db_path,feed))

def _decorate(state, db_path):
    view=context_for(state,db_path)
    state['signoff']=view
    archive=state['views'].get('archive')
    if archive:
        formal=bool(view['lock'] and view['lock']['current'])
        if formal:
            archive['metrics']['状态']='已冻结'
        elif view['gate']['passed']:
            archive['metrics']['状态']='可冻结（待双签）'
        else:
            archive['metrics']['状态']='待处理'
        archive['lock']=view['lock']
        archive['formal']=formal
        archive['release_allowed']=view['release_allowed']
    locking=state['views'].get('locking')
    if locking:
        locking['signoff_status']=view['status']
        locking['dual']=view['dual']
    return state

def render(request:Request,key):
    from app.main import DB_PATH
    run_id=request.query_params.get('run_id')
    if run_id:
        state=store.get(DB_PATH,run_id)
    else:
        state=store.save(DB_PATH,evaluate({'dataset':request.query_params.get('dataset','dirty')}))
    _decorate(state,DB_PATH)
    return templates.TemplateResponse(request=request,name=f'{key}.html',context={'meta':META,'key':key,'state':state,'view':state['views'][key]})
