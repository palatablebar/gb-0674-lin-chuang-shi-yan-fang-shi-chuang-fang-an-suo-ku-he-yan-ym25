from fastapi.testclient import TestClient
from app import main, store, signoff
from app.errors import BusinessError
import pytest
from app.config import ROOT
from copy import deepcopy
import zipfile, io, json

CLEAN = (ROOT/'sample_data/clean.csv').read_text()


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'signoff.sqlite'))
    return TestClient(main.app)


def batch(c, text=None, dataset=None):
    payload = {'csv_text':text} if text is not None else ({} if dataset is None else {'dataset':dataset})
    data = c.post('/api/runs', json=payload).json()
    return data['manifest']['run_id']


def state(c, run_id):
    response = c.get(f'/api/runs/{run_id}/signoff')
    assert response.status_code == 200
    return response.json()


def sign(c, run_id, basis, reviewer, opinion='approve', note=''):
    return c.post(f'/api/runs/{run_id}/signoff',
                  json={'reviewer':reviewer,'opinion':opinion,'note':note,'basis_hash':basis})


def test_two_reviewers_same_batch_form_dual_sign(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = batch(c, dataset='clean')
    view = state(c, rid)
    assert view['status'] == '尚无签署'
    basis = view['batch']['basis_hash']

    first = sign(c, rid, basis, '甲')
    assert first.status_code == 200
    assert first.json()['dual'] is False and first.json()['release_allowed'] is False
    assert first.json()['status'] == '等待第二位复核人'

    second = sign(c, rid, basis, '乙')
    assert second.status_code == 200
    body = second.json()
    assert body['dual'] is True and body['release_allowed'] is True
    assert body['lock_created'] is True
    assert body['signoff']['approved_reviewers'] == ['乙','甲']

    final = state(c, rid)
    assert final['dual'] is True
    assert final['lock']['current'] is True
    assert sorted(final['lock']['reviewers']) == ['乙','甲']
    # 正式锁库、页面和冻结凭证绑定同一份候选批次与依据
    assert final['lock']['basis_hash'] == final['batch']['basis_hash']
    assert final['lock']['batch_sha256'] == final['batch']['sha256']


def test_same_reviewer_cannot_double_count(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = batch(c, dataset='clean')
    basis = state(c, rid)['batch']['basis_hash']
    assert sign(c, rid, basis, '甲').json()['dual'] is False
    retry = sign(c, rid, basis, '甲')
    assert retry.status_code == 200
    assert retry.json()['created_new'] is False
    assert retry.json()['dual'] is False
    assert retry.json()['release_allowed'] is False


def test_same_opinion_retry_does_not_add_signature(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = batch(c, dataset='clean')
    basis = state(c, rid)['batch']['basis_hash']
    sign(c, rid, basis, '甲')
    sign(c, rid, basis, '甲')
    sign(c, rid, basis, '甲', note='复核无误')
    view = state(c, rid)
    assert len([r for r in view['reviewers'] if r['reviewer'] == '甲']) == 1
    assert view['approved_reviewers'] == ['甲']


def test_different_reviewers_on_different_batches_never_dual(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    old = batch(c, text=CLEAN)
    old_basis = state(c, old)['batch']['basis_hash']
    assert sign(c, old, old_basis, '甲').status_code == 200

    # 中心补来新结果，形成新批次；甲看到的是旧批，乙刷新到新批。
    new_text = CLEAN.replace('00001-R1,00001,V1,2026-10-09T08:00:00+08:00',
                             '00001-R1,00001,V1,2026-10-10T08:00:00+08:00')
    new = batch(c, text=new_text)
    assert new != old

    new_view = state(c, new)
    assert new_view['is_current'] is True
    assert new_view['dual'] is False
    assert new_view['release_allowed'] is False
    assert new_view['status'] == '上游已更新，需重新双签'
    # 甲在旧批的确认标为已失效且需重看，并列出与当前批次的差异
    jia = next(r for r in new_view['reviewers'] if r['reviewer'] == '甲')
    assert jia['state'] == '已失效'
    assert jia['valid'] is False
    assert jia['same_basis'] is False
    assert jia['diff']

    # 乙不能用旧依据签新批次
    stale = sign(c, new, old_basis, '乙')
    assert stale.status_code == 409
    assert stale.json()['code'] == 'BASIS_CHANGED'
    assert state(c, new)['dual'] is False

    # 旧批不能再签
    late = sign(c, old, state(c, old)['batch']['basis_hash'], '乙')
    assert late.status_code == 409
    assert late.json()['code'] == 'SUPERSEDED'


def test_upstream_update_invalidates_old_lock_and_requires_new_dual(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    old = batch(c, text=CLEAN)
    old_basis = state(c, old)['batch']['basis_hash']
    sign(c, old, old_basis, '甲')
    response = sign(c, old, old_basis, '乙')
    assert response.json()['release_allowed'] is True
    old_lock = state(c, old)['lock']
    assert old_lock['current'] is True

    new = batch(c, text=CLEAN.replace('1.2,g/L', '1.3,g/L', 1))
    new_view = state(c, new)
    assert new_view['dual'] is False
    assert new_view['release_allowed'] is False
    # 旧正式结论保留可查，但已标为失效
    assert new_view['locks']
    assert new_view['locks'][0]['status'] == '已失效'
    assert new_view['locks'][0]['run_id'] == old
    old_view = state(c, old)
    assert old_view['is_current'] is False
    assert old_view['release_allowed'] is False
    assert old_view['locks'][0]['status'] == '已失效'
    assert old_view['locks'][0]['invalidated_reason']

    new_basis = new_view['batch']['basis_hash']
    sign(c, new, new_basis, '甲')
    assert state(c, new)['dual'] is False
    response = sign(c, new, new_basis, '乙')
    assert response.json()['release_allowed'] is True
    assert response.json()['lock_created'] is True
    assert state(c, new)['lock']['current'] is True


def test_reject_blocks_release_and_records_reason(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = batch(c, dataset='clean')
    basis = state(c, rid)['batch']['basis_hash']
    sign(c, rid, basis, '甲')
    sign(c, rid, basis, '乙')
    assert state(c, rid)['release_allowed'] is True

    response = sign(c, rid, basis, '乙', 'reject', note='差异未解释')
    assert response.status_code == 200
    view = response.json()['signoff']
    assert view['dual'] is False
    assert view['release_allowed'] is False
    assert view['status'] == '已拒绝'
    assert view['lock']['status'] == '已失效'
    assert any('乙' in reason for reason in view['blocking_reasons'])
    assert any('不同意' in (view['locks'][0]['invalidated_reason'] or '') for _ in [0])


def test_dual_sign_alone_does_not_release_blocked_batch(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = batch(c, dataset='dirty')
    basis = state(c, rid)['batch']['basis_hash']
    sign(c, rid, basis, '甲')
    response = sign(c, rid, basis, '乙')
    view = response.json()['signoff']
    assert view['dual'] is True
    assert view['gate']['passed'] is False
    assert view['release_allowed'] is False
    assert view['status'] == '双签成立，锁库阻断'
    assert view['lock'] is None
    assert view['blocking_reasons']


def test_stale_revision_is_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = batch(c, dataset='clean')
    basis = state(c, rid)['batch']['basis_hash']
    assert c.post(f'/api/runs/{rid}/review', json={'revision':1}).status_code == 200
    response = c.post(f'/api/runs/{rid}/signoff',
                      json={'reviewer':'甲','opinion':'approve','basis_hash':basis,'revision':1})
    assert response.status_code == 409
    assert response.json()['code'] == 'STALE_VERSION'


def test_invalid_signoff_payload(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = batch(c, dataset='clean')
    basis = state(c, rid)['batch']['basis_hash']
    assert c.post(f'/api/runs/{rid}/signoff', json={'opinion':'approve','basis_hash':basis}).status_code == 422
    assert c.post(f'/api/runs/{rid}/signoff', json={'reviewer':'甲','opinion':'maybe','basis_hash':basis}).status_code == 422
    assert c.post(f'/api/runs/{rid}/signoff', json={'reviewer':'甲','opinion':'approve'}).status_code == 422
    assert c.post('/api/runs/none/signoff', json={'reviewer':'甲','opinion':'approve','basis_hash':basis}).status_code == 404


def test_page_shows_each_batch_and_diff(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    old = batch(c, text=CLEAN)
    old_basis = state(c, old)['batch']['basis_hash']
    sign(c, old, old_basis, '甲')
    new = batch(c, text=CLEAN.replace('1.2,g/L', '1.4,g/L', 1))
    html = c.get('/locking?run_id=' + new).text
    assert '双签裁决' in html
    assert '甲' in html
    assert '需重看' in html or '已失效' in html
    assert '与当前批次差异' in html
    assert '不可放行' in html
    assert '候选批次' in html
    assert new in html


def test_export_contains_same_basis_as_page(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = batch(c, dataset='clean')
    basis = state(c, rid)['batch']['basis_hash']
    sign(c, rid, basis, '甲')
    sign(c, rid, basis, '乙')
    view = state(c, rid)
    archive = zipfile.ZipFile(io.BytesIO(c.get('/api/export/' + rid).content))
    manifest = json.loads(archive.read('manifest.json'))
    assert manifest['batch']['basis_hash'] == view['batch']['basis_hash']
    assert manifest['batch']['sha256'] == view['batch']['sha256']
    assert manifest['dual'] is True
    assert manifest['release_allowed'] is True
    assert manifest['lock']['basis_hash'] == basis
    assert sorted(manifest['lock']['reviewers']) == ['乙','甲']
    signoff_bundle = json.loads(archive.read('signoff.json'))
    assert signoff_bundle['batch']['basis_hash'] == basis
    assert signoff_bundle['locks'][0]['status'] == '正式'


def test_history_batch_shows_its_own_signatures_only(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    old = batch(c, text=CLEAN)
    old_basis = state(c, old)['batch']['basis_hash']
    sign(c, old, old_basis, '甲')
    new = batch(c, text=CLEAN.replace('1.2,g/L', '1.5,g/L', 1))
    new_basis = state(c, new)['batch']['basis_hash']
    sign(c, new, new_basis, '乙')

    old_view = state(c, old)
    assert [r['reviewer'] for r in old_view['reviewers']] == ['甲']
    assert old_view['is_current'] is False
    assert old_view['release_allowed'] is False
    html = c.get('/locking?run_id=' + old).text
    assert '正在查看历史批次' in html
    assert new in html


def test_rerun_same_input_keeps_single_batch(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = batch(c, text=CLEAN)
    basis = state(c, rid)['batch']['basis_hash']
    sign(c, rid, basis, '甲')
    repeat = c.post(f'/api/runs/{rid}/rerun', json={})
    assert repeat.status_code == 200
    assert repeat.json()['manifest']['run_id'] == rid
    view = state(c, rid)
    assert view['batch']['basis_hash'] == basis
    assert view['approved_reviewers'] == ['甲']
    assert sign(c, rid, basis, '乙').json()['release_allowed'] is True


def test_basis_hash_changes_with_evidence_not_just_counts():
    from app.engine import evaluate
    a = evaluate({'dataset':'clean'})
    b = deepcopy(a)
    b['selected_results'][0]['value_mg_dl'] = '999'
    assert signoff.basis_hash(a) != signoff.basis_hash(b)
    c = deepcopy(a)
    c['balance']['status'] = '待处理'
    assert signoff.basis_hash(a) != signoff.basis_hash(c)


def test_reject_retry_is_idempotent_and_keeps_reason(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = batch(c, dataset='clean')
    basis = state(c, rid)['batch']['basis_hash']
    sign(c, rid, basis, '甲')
    sign(c, rid, basis, '乙')
    first = sign(c, rid, basis, '乙', 'reject', note='差异未解释').json()
    assert first['created_new'] is False  # 改判沿用原签署
    second = sign(c, rid, basis, '乙', 'reject', note='差异未解释').json()
    assert second['created_new'] is False
    view = state(c, rid)
    assert len([r for r in view['reviewers'] if r['reviewer'] == '乙']) == 1
    assert view['rejected_reviewers'] == ['乙']
    assert view['status'] == '已拒绝'
    assert view['release_allowed'] is False


def test_other_reviewers_reject_prevents_lock(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = batch(c, dataset='clean')
    basis = state(c, rid)['batch']['basis_hash']
    sign(c, rid, basis, '丙', 'reject')
    sign(c, rid, basis, '甲')
    response = sign(c, rid, basis, '乙')
    view = response.json()['signoff']
    assert view['dual'] is False
    assert view['release_allowed'] is False
    assert view['lock'] is None
    assert view['status'] == '已拒绝'


def test_create_lock_requires_stored_dual(tmp_path, monkeypatch):
    from app.engine import evaluate
    c = client(tmp_path, monkeypatch)
    rid = batch(c, dataset='clean')
    result = store.get(main.DB_PATH, rid)
    basis = signoff.basis_hash(result)
    sign(c, rid, basis, '甲')
    with pytest.raises(BusinessError) as error:
        store.create_lock(main.DB_PATH, result, basis, [], ['甲'])
    assert error.value.code == 'DUAL_SIGN_REQUIRED'
    assert store.locks_for_feed(main.DB_PATH, 'clean') == []


def test_history_lists_source_and_newer_batch_first(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    old = batch(c, text=CLEAN)
    new = batch(c, text=CLEAN.replace('1.2,g/L', '1.8,g/L', 1))
    rows = c.get('/api/runs').json()
    assert rows[0]['id'] == new
    assert all(row['feed'] == 'custom' for row in rows)


def test_jia_old_batch_and_yi_refreshed_never_report_dual(tmp_path, monkeypatch):
    """题目原场景：甲确认后中心补来结果，乙刷新到新批次再签。

    页面不得显示双签完成；甲的旧确认无人继承，必须两位都在新批次重看。
    """
    c = client(tmp_path, monkeypatch)
    old = batch(c, text=CLEAN)
    old_basis = state(c, old)['batch']['basis_hash']
    assert sign(c, old, old_basis, '甲').status_code == 200

    # 只改首个首次测量值，不改变访视完成情况，确保新批仍可通过闸门。
    new = batch(c, text=CLEAN.replace('1.2,g/L', '1.4,g/L', 1))
    new_view = state(c, new)
    new_basis = new_view['batch']['basis_hash']
    # 乙刷新页面拿到新依据后签署
    response = sign(c, new, new_basis, '乙')
    assert response.status_code == 200
    view = response.json()
    assert view['dual'] is False
    assert view['release_allowed'] is False
    assert view['status'] == '批次不一致，需重看'
    assert view['signoff']['approved_reviewers'] == ['乙']
    # 甲的旧批确认仍可见但不计入
    jia = next(r for r in view['signoff']['reviewers'] if r['reviewer'] == '甲')
    assert jia['valid'] is False and jia['state'] == '已失效'
    assert jia['run_id'] == old

    # 页面明确说明缺甲在新批次的确认
    html = c.get('/locking?run_id=' + new).text
    assert '双签成立' not in html.split('双签裁决')[1].split('阻断原因')[0]
    assert '批次不一致' in html or '需重看' in html

    # 甲在新批次重看后签署，双签才成立
    again = sign(c, new, new_basis, '甲')
    assert again.json()['dual'] is True
    assert again.json()['release_allowed'] is True
