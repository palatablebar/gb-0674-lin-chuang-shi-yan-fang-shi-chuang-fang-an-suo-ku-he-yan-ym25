import sqlite3, json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from app.errors import BusinessError

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')",
    "CREATE TABLE IF NOT EXISTS signoffs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, basis_hash TEXT NOT NULL, batch_sha256 TEXT NOT NULL, reviewer TEXT NOT NULL, opinion TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(run_id,basis_hash,reviewer))",
    "CREATE TABLE IF NOT EXISTS locks (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, basis_hash TEXT NOT NULL, batch_sha256 TEXT NOT NULL, seal TEXT NOT NULL, signatures TEXT NOT NULL, reviewers TEXT NOT NULL, status TEXT NOT NULL DEFAULT '正式', created_at TEXT NOT NULL, invalidated_at TEXT, invalidated_reason TEXT)",
    "CREATE UNIQUE INDEX IF NOT EXISTS locks_active_unique ON locks(run_id,basis_hash) WHERE status='正式'",
]


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=15000')
    for statement in SCHEMA: con.execute(statement)
    columns={row['name'] for row in con.execute('PRAGMA table_info(runs)')}
    if 'source' not in columns:
        con.execute("ALTER TABLE runs ADD COLUMN source TEXT NOT NULL DEFAULT 'dirty'")
        for row in con.execute('SELECT id,result FROM runs').fetchall():
            try: source=json.loads(row['result']).get('source','dirty')
            except ValueError: source='dirty'
            con.execute('UPDATE runs SET source=? WHERE id=?',(source,row['id']))
    if 'created_at' not in columns:
        con.execute("ALTER TABLE runs ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
    if 'feed' not in columns:
        con.execute("ALTER TABLE runs ADD COLUMN feed TEXT NOT NULL DEFAULT 'dirty'")
        for row in con.execute('SELECT id,source,result FROM runs').fetchall():
            try: feed=json.loads(row['result']).get('feed',row['source'])
            except ValueError: feed=row['source']
            con.execute('UPDATE runs SET feed=? WHERE id=?',(feed,row['id']))
    con.execute("UPDATE runs SET created_at=? WHERE created_at=''",(now(),))
    con.commit()
    return con


@contextmanager
def session(path):
    con=connect(path)
    try:
        with con:
            yield con
    finally:
        con.close()


@contextmanager
def write_session(path):
    """写事务。BEGIN IMMEDIATE 先取得写锁，使“读最新批次→写入”对并发重试原子化。"""
    con=connect(path)
    con.isolation_level=None
    try:
        con.execute('BEGIN IMMEDIATE')
        try:
            yield con
            con.execute('COMMIT')
        except BaseException:
            con.execute('ROLLBACK')
            raise
    finally:
        con.close()


def _latest_for_feed(con, feed):
    row=con.execute('SELECT id FROM runs WHERE feed=? ORDER BY rowid DESC LIMIT 1',(feed,)).fetchone()
    return row['id'] if row else None


def save(path, result):
    run_id=result['manifest']['run_id']
    source=result.get('source','dirty')
    feed=result.get('feed',source)
    with write_session(path) as con:
        cursor=con.execute('INSERT OR IGNORE INTO runs(id,digest,result,source,feed,created_at) VALUES(?,?,?,?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False),source,feed,now()))
        if cursor.rowcount==1:
            # 上游补来新结果：同源旧批次的正式双签与锁库立即失效，但保留可查。
            con.execute(
                "UPDATE locks SET status='已失效',invalidated_at=?,invalidated_reason=? WHERE status='正式' AND run_id IN (SELECT id FROM runs WHERE feed=? AND id<>?)",
                (now(),f'上游更新，出现新批次 {run_id}，旧双签失效需重算',feed,run_id))
    return get(path,run_id)


def get(path, run_id):
    with session(path) as con:
        row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
        latest=_latest_for_feed(con,row['feed'])
    result=json.loads(row['result'])
    result.update(revision=row['revision'],status=row['status'],source=row['source'],
                  feed=row['feed'],created_at=row['created_at'],latest_run_id=latest)
    return result


def history(path):
    with session(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status,source,feed,created_at FROM runs ORDER BY rowid DESC LIMIT 100')]


def review(path,run_id,revision):
    with session(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)


def signoffs_for_feed(path, feed):
    with session(path) as con:
        return [dict(row) for row in con.execute(
            'SELECT s.* FROM signoffs s JOIN runs r ON r.id=s.run_id WHERE r.feed=? ORDER BY s.id', (feed,))]


def locks_for_feed(path, feed):
    with session(path) as con:
        return [dict(row) for row in con.execute(
            'SELECT l.* FROM locks l JOIN runs r ON r.id=l.run_id WHERE r.feed=? ORDER BY l.id DESC', (feed,))]


def latest_for_feed(path, feed):
    with session(path) as con:
        return _latest_for_feed(con, feed)


def _active_lock(con, run_id, basis_hash):
    return con.execute("SELECT * FROM locks WHERE run_id=? AND basis_hash=? AND status='正式'",(run_id,basis_hash)).fetchone()


def _require_latest(con, result, code, message):
    if _latest_for_feed(con,result.get('feed',result.get('source','dirty')))!=result['manifest']['run_id']:
        raise BusinessError(code,message,409)


def record_signoff(path, result, basis_hash, reviewer, opinion, snapshot, note=''):
    """记录一次签署。

    同一复核人在同一批次同一依据上重复提交同一意见只返回原签署，不新增行；
    改判则更新原签署。调用方负责先校验 basis_hash 与当前批次一致。
    """
    run_id=result['manifest']['run_id']
    batch_sha256=result['manifest']['sha256']
    stamp=now()
    payload=json.dumps(snapshot,ensure_ascii=False,sort_keys=True)
    with write_session(path) as con:
        _require_latest(con,result,'SUPERSEDED','已有更新批次，请在最新批次上重签，旧批次不能再签')
        existing=con.execute('SELECT * FROM signoffs WHERE run_id=? AND basis_hash=? AND reviewer=?',(run_id,basis_hash,reviewer)).fetchone()
        created=False
        if existing is None:
            try:
                cursor=con.execute('INSERT INTO signoffs(run_id,basis_hash,batch_sha256,reviewer,opinion,note,snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                            (run_id,basis_hash,batch_sha256,reviewer,opinion,note,payload,stamp,stamp))
            except sqlite3.IntegrityError:
                # 同一复核人并发重试：唯一约束只允许一条，按已有签署处理。
                cursor=None
            if cursor is not None and cursor.rowcount==1:
                created=True
                signature_id=cursor.lastrowid
            else:
                existing=con.execute('SELECT * FROM signoffs WHERE run_id=? AND basis_hash=? AND reviewer=?',(run_id,basis_hash,reviewer)).fetchone()
                signature_id=existing['id']
        else:
            signature_id=existing['id']
        if existing is not None and not created and (existing['opinion']!=opinion or (note and note!=existing['note'])):
            con.execute('UPDATE signoffs SET opinion=?,note=?,batch_sha256=?,snapshot=?,updated_at=? WHERE id=?',
                        (opinion,note or existing['note'],batch_sha256,payload,stamp,signature_id))
        signature=dict(con.execute('SELECT * FROM signoffs WHERE id=?',(signature_id,)).fetchone())
        signature['created_new']=created
        # 签署变化后，双签（两位不同复核人同意且无不同意）不再成立时，
        # 正式结论失效但保留可查。
        active=_active_lock(con,run_id,basis_hash)
        if active:
            rows=con.execute("SELECT reviewer,opinion FROM signoffs WHERE run_id=? AND basis_hash=?",(run_id,basis_hash)).fetchall()
            approvers={row['reviewer'] for row in rows if row['opinion']=='approve'}
            has_reject=any(row['opinion']=='reject' for row in rows)
            if has_reject or len(approvers)<2:
                con.execute("UPDATE locks SET status='已失效',invalidated_at=?,invalidated_reason=? WHERE id=?",
                            (stamp,'复核人撤回、改判或提出不同意，双签不再成立',active['id']))
        return signature


def active_lock(path, run_id, basis_hash):
    with session(path) as con:
        row=_active_lock(con,run_id,basis_hash)
        return dict(row) if row else None


def approvals_for_basis(path, run_id, basis_hash):
    """当前批次当前依据上的同意签署，用于把正式锁库绑定到具体签署。"""
    with session(path) as con:
        rows=con.execute("SELECT id,reviewer,opinion,note,updated_at FROM signoffs WHERE run_id=? AND basis_hash=? AND opinion='approve' ORDER BY reviewer",(run_id,basis_hash)).fetchall()
        return [{'id':r['id'],'reviewer':r['reviewer'],'opinion':r['opinion'],'note':r['note'],'signed_at':r['updated_at']} for r in rows]


def invalidate_lock(path, run_id, basis_hash, reason):
    """把某批次某依据上的正式结论标记为失效，历史仍可查。"""
    with write_session(path) as con:
        row=_active_lock(con, run_id, basis_hash)
        if row:
            con.execute("UPDATE locks SET status='已失效',invalidated_at=?,invalidated_reason=? WHERE id=?",
                        (now(),reason,row['id']))
            return True
        return False


def create_lock(path, result, basis_hash, signatures, reviewers):
    """双签成立且闸门通过时落一条正式锁库记录，绑定当时的批次与依据。

    写入前在同一个写事务内复核签署，避免读签署与落锁之间被改判。
    """
    run_id=result['manifest']['run_id']
    with write_session(path) as con:
        _require_latest(con,result,'SUPERSEDED','已有更新批次，旧批次不能形成正式锁库')
        rows=con.execute("SELECT reviewer,opinion FROM signoffs WHERE run_id=? AND basis_hash=?",(run_id,basis_hash)).fetchall()
        approvers={row['reviewer'] for row in rows if row['opinion']=='approve'}
        if len(approvers)<2 or any(row['opinion']=='reject' for row in rows):
            raise BusinessError('DUAL_SIGN_REQUIRED','两位复核人尚未在同一批次同一依据上确认，不能正式锁库',409)
        existing=_active_lock(con,run_id,basis_hash)
        if existing: return dict(existing)
        con.execute('INSERT OR IGNORE INTO locks(run_id,basis_hash,batch_sha256,seal,signatures,reviewers,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    (run_id,basis_hash,result['manifest']['sha256'],(result.get('archive') or {}).get('seal',''),
                     json.dumps(signatures,ensure_ascii=False,sort_keys=True),
                     json.dumps(sorted(reviewers),ensure_ascii=False),'正式',now()))
        row=_active_lock(con,run_id,basis_hash)
        return dict(row) if row else None
