import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result, signoff=None):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        manifest={**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')}
        # 凭证与页面、正式锁库共用同一份候选批次与依据。
        if signoff is not None:
            manifest.update(batch=signoff['batch'],signoff_status=signoff['status'],
                            dual=signoff['dual'],release_allowed=signoff['release_allowed'],
                            approved_reviewers=signoff['approved_reviewers'],
                            lock=signoff.get('lock'),signoff_reasons=signoff['reasons'])
        archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
        if signoff is not None:
            archive.writestr('signoff.json',json.dumps({'batch':signoff['batch'],'status':signoff['status'],
                'dual':signoff['dual'],'release_allowed':signoff['release_allowed'],
                'approved_reviewers':signoff['approved_reviewers'],
                'rejected_reviewers':signoff['rejected_reviewers'],
                'reviewers':signoff['reviewers'],'gate':signoff['gate'],
                'locks':signoff['locks'],'reasons':signoff['reasons'],
                'blocking_reasons':signoff['blocking_reasons']},ensure_ascii=False,indent=2))
    return buffer.getvalue()
