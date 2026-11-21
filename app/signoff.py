"""双签裁决。

一份签署只对它当时看到的「候选批次 + 依据」有效。双签成立要求两位不同
复核人都在同一个 run_id / 批次摘要 / 依据摘要上给出同意；旧批次的签署不会
顺延到新批次，也不会被“最早确认”规则带过来。
"""
import hashlib
import json

OPINION_LABELS = {'approve': '同意', 'reject': '不同意'}
_OPINIONS = {
    'approve': 'approve', '同意': 'approve', '通过': 'approve', 'yes': 'approve',
    'reject': 'reject', '不同意': 'reject', '拒绝': 'reject', 'no': 'reject',
}
_DIFF_FIELDS = [
    ('batch_sha256', '批次摘要'),
    ('rule_version', '规则版本'),
    ('seal', '候选依据'),
    ('evidence_hash', '证据摘要'),
    ('results', '候选结果数'),
    ('blocked', '阻断项'),
    ('deviations', '访视偏离'),
    ('retest_issues', '复检问题'),
    ('quarantine', '采信隔离'),
    ('completed', '已完成访视'),
    ('expected', '应访视'),
    ('gate_status', '锁库闸门'),
]


def normalize_reviewer(value):
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())


def normalize_opinion(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return _OPINIONS.get(text) or _OPINIONS.get(text.lower())


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def evidence_hash(result):
    """候选证据本身的摘要：任何结果值、窗口、偏离或隔离变化都会改变依据。"""
    payload = {
        'selected_results': result.get('selected_results') or [],
        'windows': result.get('windows') or [],
        'deviations': result.get('deviations') or [],
        'retest_issues': result.get('retest_issues') or [],
        'quarantine': result.get('quarantine') or [],
        'counts': result.get('counts') or {},
    }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def batch_basis(result):
    """候选批次与依据的稳定描述；锁库、签署、页面和冻结凭证都用这一份。"""
    manifest = result.get('manifest') or {}
    balance = result.get('balance') or {}
    archive = result.get('archive') or {}
    return {
        'run_id': manifest.get('run_id', ''),
        'batch_sha256': manifest.get('sha256', ''),
        'rule_version': manifest.get('rule_version', ''),
        'generated_at': manifest.get('generated_at', ''),
        'seal': archive.get('seal', ''),
        'evidence_hash': evidence_hash(result),
        'candidate': {
            'results': len(result.get('selected_results') or []),
            'windows': len(result.get('windows') or []),
            'deviations': len(result.get('deviations') or []),
            'retest_issues': len(result.get('retest_issues') or []),
            'quarantine': len(result.get('quarantine') or []),
        },
        'gate': {
            'status': balance.get('status', '待处理'),
            'blocked': balance.get('blocked', 0),
            'deviations': balance.get('deviations', 0),
            'retest_issues': balance.get('retest_issues', 0),
            'completed': balance.get('completed', 0),
            'expected': balance.get('expected', 0),
            'row_balanced': balance.get('row_balanced', False),
        },
    }


def basis_hash(result):
    basis = batch_basis(result)
    # 生成时刻只用于展示，不参与依据；同输入同规则重跑不得让已有双签失效。
    basis.pop('generated_at', None)
    return hashlib.sha256(
        json.dumps(basis, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def snapshot(result):
    """签署时点看到的关键字段，用于页面展示与当前批次的差异。"""
    basis = batch_basis(result)
    return {
        'run_id': basis['run_id'],
        'batch_sha256': basis['batch_sha256'],
        'rule_version': basis['rule_version'],
        'seal': basis['seal'],
        'evidence_hash': basis['evidence_hash'],
        'results': basis['candidate']['results'],
        'blocked': basis['gate']['blocked'],
        'deviations': basis['gate']['deviations'],
        'retest_issues': basis['gate']['retest_issues'],
        'quarantine': basis['candidate']['quarantine'],
        'completed': basis['gate']['completed'],
        'expected': basis['gate']['expected'],
        'gate_status': basis['gate']['status'],
    }


def _current_snapshot(result):
    return snapshot(result)


def _short(value, size=12):
    text = str(value or '')
    return text[:size]


_SHORT_FIELDS = {'batch_sha256', 'seal', 'evidence_hash'}


def _diff(signed, current):
    changes = []
    for key, label in _DIFF_FIELDS:
        old, new = signed.get(key), current.get(key)
        if old != new:
            if key in _SHORT_FIELDS:
                old, new = _short(old), _short(new)
            changes.append({'field': key, 'label': label, 'signed': old, 'current': new})
    return changes


def _parse_snapshot(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}


def _latest_by_reviewer(signatures):
    latest = {}
    for signature in signatures:
        reviewer = signature.get('reviewer', '')
        if not reviewer:
            continue
        stamp = (signature.get('updated_at') or signature.get('created_at') or '', signature.get('id') or 0)
        if reviewer not in latest:
            latest[reviewer] = signature
            continue
        previous = latest[reviewer]
        previous_stamp = (previous.get('updated_at') or previous.get('created_at') or '', previous.get('id') or 0)
        if stamp > previous_stamp:
            latest[reviewer] = signature
    return list(latest.values())


def _gate_reasons(result):
    balance = result.get('balance') or {}
    reasons = []
    if balance.get('status') != '可锁库':
        reasons.append('锁库闸门未通过：' + str(balance.get('status', '待处理')))
    if balance.get('blocked'):
        reasons.append(
            '仍有阻断项 %s 项（访视偏离 %s、复检问题 %s、采信隔离 %s）'
            % (balance.get('blocked'),
               balance.get('deviations', 0),
               balance.get('retest_issues', 0),
               len(result.get('quarantine') or []))
        )
    if balance.get('completed') != balance.get('expected'):
        reasons.append('已完成访视 %s / 应访视 %s' % (balance.get('completed'), balance.get('expected')))
    if balance.get('row_balanced') is not True:
        reasons.append('输入行不守恒，不能锁库')
    return reasons


def _reviewer_view(signature, result, current_hash, is_current):
    signed = _parse_snapshot(signature.get('snapshot'))
    current = _current_snapshot(result)
    same_batch = signature.get('batch_sha256') == current['batch_sha256']
    same_basis = signature.get('basis_hash') == current_hash
    opinion = signature.get('opinion')
    if not same_batch:
        state = '已失效'
    elif not same_basis:
        state = '需重看'
    elif opinion == 'reject':
        state = '已拒绝'
    elif not is_current:
        state = '历史签署'
    else:
        state = '有效'
    return {
        'reviewer': signature.get('reviewer', ''),
        'opinion': opinion,
        'opinion_label': OPINION_LABELS.get(opinion, opinion or ''),
        'note': signature.get('note', ''),
        'signed_at': signature.get('updated_at') or signature.get('created_at', ''),
        'run_id': signature.get('run_id', ''),
        'batch_sha256': signature.get('batch_sha256', ''),
        'batch_short': _short(signature.get('batch_sha256')),
        'basis_hash': signature.get('basis_hash', ''),
        'basis_short': _short(signature.get('basis_hash')),
        'rule_version': signed.get('rule_version', ''),
        'same_batch': same_batch,
        'same_basis': same_basis,
        'valid': same_batch and same_basis,
        'state': state,
        'diff': [] if same_basis else _diff(signed, current),
        'snapshot': signed,
    }


def _lock_view(lock, is_current, current_hash, latest_run_id):
    if not lock:
        return None
    signatures = lock.get('signatures')
    if isinstance(signatures, str):
        try:
            signatures = json.loads(signatures)
        except ValueError:
            signatures = []
    reviewers = lock.get('reviewers')
    if isinstance(reviewers, str):
        try:
            reviewers = json.loads(reviewers)
        except ValueError:
            reviewers = []
    matches = lock.get('basis_hash') == current_hash
    current = bool(is_current and lock.get('status') == '正式' and matches)
    return {
        'id': lock.get('id'),
        'run_id': lock.get('run_id', ''),
        'batch_sha256': lock.get('batch_sha256', ''),
        'batch_short': _short(lock.get('batch_sha256')),
        'basis_hash': lock.get('basis_hash', ''),
        'basis_short': _short(lock.get('basis_hash')),
        'seal': lock.get('seal', ''),
        'seal_short': _short(lock.get('seal')),
        'signatures': signatures or [],
        'reviewers': reviewers or [],
        'created_at': lock.get('created_at', ''),
        'status': lock.get('status', ''),
        'invalidated_at': lock.get('invalidated_at'),
        'invalidated_reason': lock.get('invalidated_reason'),
        'matches_basis': matches,
        'current': current,
        'historical': not current,
        'latest_run_id': latest_run_id,
    }


def adjudicate(result, signatures=None, locks=None, latest_run_id=None):
    """返回双签/锁库裁决。signatures 与 locks 为数据库行（dict）列表。"""
    signatures = signatures or []
    locks = locks or []
    run_id = (result.get('manifest') or {}).get('run_id', '')
    latest_run_id = latest_run_id or run_id
    is_current = run_id == latest_run_id
    basis = batch_basis(result)
    current_hash = basis_hash(result)

    # 当前批次页展示整条上游版本线的签署（便于看到旧批次上的确认已失效）；
    # 历史批次页只展示属于该批次的签署，避免把新批次的签署错配到旧依据。
    visible = signatures if is_current else [
        s for s in signatures if s.get('run_id') == run_id
    ]
    review_rows = [
        _reviewer_view(s, result, current_hash, is_current)
        for s in sorted(
            _latest_by_reviewer(visible),
            key=lambda x: (x.get('updated_at') or x.get('created_at') or '', x.get('id') or 0),
            reverse=True,
        )
    ]

    current_signoffs = [
        s for s in signatures
        if s.get('run_id') == run_id and s.get('basis_hash') == current_hash
    ]
    approvers = sorted({
        s.get('reviewer', '') for s in current_signoffs
        if s.get('opinion') == 'approve' and s.get('reviewer')
    })
    rejecters = sorted({
        s.get('reviewer', '') for s in current_signoffs
        if s.get('opinion') == 'reject' and s.get('reviewer')
    })
    dual = len(approvers) >= 2 and not rejecters

    gate_reasons = _gate_reasons(result)
    gate_ok = not gate_reasons
    reasons = []

    if not is_current:
        reasons.append('本批次不是最新候选批次（最新 %s），旧批次签署与锁库仅作历史留档' % latest_run_id)
    if rejecters:
        reasons.append('%s 不同意，双签不成立' % '、'.join(rejecters))
    if not dual and not rejecters:
        if approvers:
            reasons.append('还差 %s 位复核人在同一批次同一依据上确认' % (2 - len(approvers)))
        stale = [
            row for row in review_rows
            if not row['same_basis'] and row['reviewer'] not in approvers
        ]
        for row in stale:
            reasons.append(
                '%s 核对的是批次 %s（依据 %s），与当前批次 %s（依据 %s）不同，需重看'
                % (row['reviewer'], row['run_id'], row['basis_short'], run_id, _short(current_hash))
            )

    locks_for_run = [l for l in locks if l.get('run_id') == run_id]
    visible_locks = locks if is_current else locks_for_run
    # 当前批次只认绑定本批次当前依据的正式记录；其他记录一律作为历史结论展示。
    active = next((l for l in locks_for_run
                   if l.get('status') == '正式' and l.get('basis_hash') == current_hash), None)
    if active is None and locks_for_run:
        active = sorted(locks_for_run, key=lambda x: x.get('id') or 0, reverse=True)[0]
    lock_view = _lock_view(active, is_current, current_hash, latest_run_id)
    if lock_view:
        lock_view['current'] = bool(active is not None
                                    and active.get('status') == '正式'
                                    and active.get('basis_hash') == current_hash
                                    and is_current)
        lock_view['historical'] = not lock_view['current']
        if not lock_view['matches_basis']:
            reasons.append('本批次的正式结论绑定的是另一份依据，不能沿用到当前候选批量')

    if not is_current:
        status = '历史批次，双签仅留档'
    elif rejecters:
        status = '已拒绝'
    elif dual and gate_ok:
        status = '双签成立'
    elif dual:
        status = '双签成立，锁库阻断'
    elif approvers:
        mismatch = any(
            row['reviewer'] not in approvers and not row['same_basis']
            for row in review_rows
        )
        status = '批次不一致，需重看' if mismatch else '等待第二位复核人'
    elif current_signoffs:
        status = '待双签'
    elif signatures:
        status = '上游已更新，需重新双签'
    else:
        status = '尚无签署'

    blocking = list(reasons)
    for gate_reason in gate_reasons:
        if gate_reason not in blocking:
            blocking.append(gate_reason)

    release_allowed = bool(is_current and dual and not rejecters and gate_ok)

    lock_records = []
    for lock in sorted(visible_locks, key=lambda x: x.get('id') or 0, reverse=True):
        view = _lock_view(lock, lock.get('run_id') == latest_run_id,
                          lock.get('basis_hash') == current_hash if lock.get('run_id') == run_id else False,
                          latest_run_id)
        if view:
            view['current'] = bool(lock.get('run_id') == latest_run_id
                                   and lock.get('status') == '正式'
                                   and lock.get('basis_hash') == current_hash)
            view['historical'] = not view['current']
            lock_records.append(view)

    batch = {
        'run_id': run_id,
        'sha256': basis['batch_sha256'],
        'sha256_short': _short(basis['batch_sha256']),
        'basis_hash': current_hash,
        'basis_short': _short(current_hash),
        'evidence_hash': basis['evidence_hash'],
        'evidence_short': _short(basis['evidence_hash']),
        'rule_version': basis['rule_version'],
        'seal': basis['seal'],
        'seal_short': _short(basis['seal']),
        'generated_at': basis['generated_at'],
        'results': basis['candidate']['results'],
        'gate_status': basis['gate']['status'],
        'blocked': basis['gate']['blocked'],
        'latest_run_id': latest_run_id,
    }

    return {
        'batch': batch,
        'is_current': is_current,
        'latest_run_id': latest_run_id,
        'reviewers': review_rows,
        'current_signoff_reviewers': sorted({s.get('reviewer', '') for s in current_signoffs}),
        'approved_reviewers': approvers,
        'rejected_reviewers': rejecters,
        'dual': dual,
        'status': status,
        'reasons': reasons,
        'gate': {
            'passed': gate_ok,
            'reasons': gate_reasons,
            'status': basis['gate']['status'],
            'blocked': basis['gate']['blocked'],
            'completed': basis['gate']['completed'],
            'expected': basis['gate']['expected'],
            'row_balanced': basis['gate']['row_balanced'],
        },
        'release_allowed': release_allowed,
        'blocking_reasons': blocking,
        'lock': lock_view,
        'locks': lock_records,
    }
