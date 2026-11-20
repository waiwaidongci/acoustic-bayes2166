"""备件整件反演：版本冻结、双端排队、并发更正与中断恢复。

规则：
- 版本创建时冻结已完成研究的折外混淆矩阵、判读件数、允许偏差与目标故障；
  worker 只读取该版本冻结资料，不回头读来源或研究结果。
- 每个版本拆成 min / max 两个端，各自一行、各自可重试；两端都得出终局
  （有构成，或明确无解原因）后，且该版本仍是研究的最新版本，才发布。
- 更正判读件数永远另开新版本；旧版本晚到的结果只保留为该版本的历史，
  由最新版本占据研究页面，不会被旧计算覆盖。
- 领取 end 用条件更新，完成与发布都用条件状态迁移并写一次审计，
  因此同一端重跑不会重复发布。
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import HTTPException
from redis import Redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from ..config.database import Session
from ..config.settings import REDIS_URL
from ..models.run import Run
from ..models.spares import SparesEnd, SparesStudy, SparesVersion
from ..utils.serialization import now, row
from . import spares_math
from .audit import log

queue = Redis.from_url(REDIS_URL)
QUEUE_KEY = 'spares:queue'
ENDS = ('min', 'max')
TERMINAL = ('completed', 'infeasible', 'failed')


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _stale_seconds():
    try:
        return max(1, int(os.getenv('SPARES_STALE_SECONDS', '180')))
    except ValueError:
        return 180


def _max_attempts():
    try:
        return max(1, int(os.getenv('SPARES_MAX_ATTEMPTS', '3')))
    except ValueError:
        return 3


def _actor(user):
    return user.email if hasattr(user, 'email') else str(user)


def _checksum(labels, matrix, counts, tolerance, target_label):
    body = {'labels': labels, 'matrix': matrix, 'counts': counts,
            'tolerance': tolerance, 'target_label': target_label}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _matrix_from_run(run):
    if run.state != 'completed' or not run.result:
        raise HTTPException(409, '只有已完成的研究才能冻结折外混淆矩阵')
    result = run.result
    labels = result.get('labels')
    matrix = result.get('confusion_matrix')
    if not isinstance(labels, list) or not isinstance(matrix, list):
        raise HTTPException(422, '该研究结果不包含可用的折外混淆矩阵')
    try:
        spares_math.prepare(labels, matrix, [1] * len(labels), '0')
    except ValueError as e:
        raise HTTPException(422, '折外混淆矩阵不可用于整件反演：' + str(e))
    return list(labels), [list(r) for r in matrix]


def _study_for_run(db, run):
    return db.scalar(select(SparesStudy).where(SparesStudy.run_id == run.id))


def _get_or_create_study(db, user, run):
    study = _study_for_run(db, run)
    if study:
        return study
    study = SparesStudy(run_id=run.id, name=run.name + ' · 备件反演',
                        created_by=_actor(user), created_at=now())
    db.add(study)
    try:
        db.flush()
        log(db, user, 'spares.study.created', study.id, run.name)
        db.commit()
    except IntegrityError:
        db.rollback()
        study = _study_for_run(db, run)
        if not study:
            raise
    return study


def _latest_no(db, study_id):
    return db.scalar(select(func.max(SparesVersion.version_no)).where(SparesVersion.study_id == study_id)) or 0


def _enqueue(end_id):
    """入队前登记时间戳，恢复扫描不会把仍在队列里的端点每轮重复压入；
    超过 _stale_seconds 未领取视为推送丢失，允许再次压入。"""
    if not mark_enqueued(end_id):
        return False
    try:
        queue.rpush(QUEUE_KEY, end_id)
        return True
    except Exception:
        logging.exception('spares queue unavailable; recovery sweep will retry')
        _release_enqueue(end_id)
        return False


def mark_enqueued(end_id):
    with _session() as db:
        end = db.get(SparesEnd, end_id)
        if not end:
            return False
        sent = _timestamp(end.enqueued_at)
        if sent and (datetime.now(timezone.utc) - sent).total_seconds() < _stale_seconds():
            return False
        result = db.execute(update(SparesEnd)
                            .where(SparesEnd.id == end_id)
                            .values(enqueued_at=now()))
        db.commit()
        return result.rowcount == 1


def _release_enqueue(end_id):
    with _session() as db:
        db.execute(update(SparesEnd).where(SparesEnd.id == end_id).values(enqueued_at=''))
        db.commit()


def create_version(db, user, run_id, counts_input, tolerance_input, target_label):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, '未找到分析研究')
    labels, matrix = _matrix_from_run(run)
    if not isinstance(target_label, str) or target_label not in labels:
        raise HTTPException(422, '备件目标故障必须来自该研究的判读类型')
    if not isinstance(counts_input, dict) or set(counts_input) != set(labels):
        raise HTTPException(422, '判读件数必须逐一覆盖该研究全部故障类型：' + '、'.join(labels))
    counts = []
    for label in labels:
        value = counts_input[label]
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(422, '判读件数必须是非负整数：' + label)
        counts.append(value)
    try:
        tolerance = spares_math.parse_tolerance(tolerance_input)
        spares_math.prepare(labels, matrix, counts, tolerance)
    except ValueError as e:
        raise HTTPException(422, str(e))
    tol_text = spares_math.canonical_tolerance(tolerance)
    study = _get_or_create_study(db, user, run)
    version = None
    for _ in range(5):
        version_no = _latest_no(db, study.id) + 1
        version = SparesVersion(
            study_id=study.id, version_no=version_no, target_label=target_label,
            tolerance=tol_text, counts=counts, matrix=matrix, labels=labels,
            checksum=_checksum(labels, matrix, counts, tol_text, target_label),
            state='computing', created_by=_actor(user), created_at=now())
        db.add(version)
        try:
            db.flush()
            break
        except IntegrityError:
            db.rollback()
            version = None
            study = db.get(SparesStudy, study.id)
    if version is None:
        raise HTTPException(409, '判读件数更正过于频繁，请稍后重试')
    ends = []
    for end in ENDS:
        item = SparesEnd(version_id=version.id, end=end, state='queued', attempts=0,
                         claimed_at='', enqueued_at='', finished_at='', created_at=now())
        db.add(item); ends.append(item)
    db.flush()
    log(db, user, 'spares.version.created', version.id,
        json.dumps({'version_no': version.version_no, 'target_label': target_label,
                    'tolerance': tol_text, 'counts': counts}, ensure_ascii=False))
    db.commit()
    for item in ends:
        _enqueue(item.id)
    return row(version)


def publish_if_ready(db, version_id):
    """两端终局且为最新版本才发布；条件迁移保证只写一次审计。"""
    version = db.get(SparesVersion, version_id)
    if not version or version.state != 'computing':
        return version.state if version else ''
    ends = list(db.scalars(select(SparesEnd).where(SparesEnd.version_id == version.id)))
    if len(ends) != 2 or any(e.state not in TERMINAL for e in ends):
        return 'computing'
    latest = _latest_no(db, version.study_id)
    if version.version_no < latest:
        state, detail = 'superseded', '晚到结果保留为v%d历史，研究页面仍采用最新版本' % version.version_no
    elif any(e.state == 'failed' for e in ends):
        state = 'failed'
        detail = '；'.join(e.reason for e in ends if e.state == 'failed')[:2000]
    else:
        state, detail = 'published', 'v%d两端齐备' % version.version_no
    published_at = now() if state == 'published' else ''
    changed = db.execute(update(SparesVersion)
                         .where(SparesVersion.id == version.id, SparesVersion.state == 'computing')
                         .values(state=state, published_at=published_at)).rowcount
    if changed:
        if state == 'published':
            log(db, 'worker', 'spares.version.published', version.id, detail)
        elif state == 'superseded':
            log(db, 'worker', 'spares.version.superseded', version.id, detail)
        else:
            log(db, 'worker', 'spares.version.failed', version.id, detail)
        db.commit()
    return state


def claim(db, end_id):
    result = db.execute(update(SparesEnd)
                        .where(SparesEnd.id == end_id, SparesEnd.state == 'queued')
                        .values(state='running', claimed_at=now(), enqueued_at='',
                                attempts=SparesEnd.attempts + 1))
    db.commit()
    return result.rowcount == 1


def execute_end(end_id):
    """worker 入口：用该版本冻结资料求一端；重跑同一端不会重复发布。"""
    with _session() as db:
        if not claim(db, end_id):
            db.expire_all()
            end = db.get(SparesEnd, end_id)
            return {'skipped': True, 'end_id': end_id, 'state': end.state if end else 'missing'}
        db.expire_all()
        end = db.get(SparesEnd, end_id)
        attempts = end.attempts
        version = db.get(SparesVersion, end.version_id)
        if not version:
            end.state = 'failed'; end.reason = '该端所属版本已不存在'; end.finished_at = now()
            db.commit()
            return {'failed': True, 'end_id': end_id}
        try:
            model = spares_math.prepare(version.labels, version.matrix, version.counts, version.tolerance)
            target_index = version.labels.index(version.target_label)
            result = spares_math.solve_end(model, target_index, end.end)
        except ValueError as e:
            end.state = 'failed'
            end.reason = '该版冻结资料无法用于整件反演：%s' % str(e)[:1800]
            end.finished_at = now(); db.commit()
            publish_if_ready(db, version.id)
            log(db, 'worker', 'spares.end.failed', end.id, end.end); db.commit()
            return {'failed': True, 'end_id': end_id, 'error': str(e)}
        except spares_math.SparesSolveError as e:
            if attempts < _max_attempts():
                end.state = 'queued'; end.claimed_at = ''
                db.commit()
                _enqueue(end.id)
                return {'retry': True, 'end_id': end_id, 'error': str(e)}
            end.state = 'failed'; end.reason = str(e)[:2000]; end.finished_at = now()
            db.commit()
            publish_if_ready(db, version.id)
            log(db, 'worker', 'spares.end.failed', end.id, end.end)
            db.commit()
            return {'failed': True, 'end_id': end_id, 'error': str(e)}
        except Exception as e:
            logging.exception('spares end crashed')
            end.state = 'failed'; end.reason = '计算异常中断：%s' % str(e)[:1800]; end.finished_at = now()
            db.commit()
            publish_if_ready(db, version.id)
            log(db, 'worker', 'spares.end.failed', end.id, end.end); db.commit()
            return {'failed': True, 'end_id': end_id, 'error': str(e)}
        end.result = result
        end.state = 'completed' if result['feasible'] else 'infeasible'
        end.reason = result.get('reason', '')
        end.finished_at = now()
        db.commit()
        publish_if_ready(db, version.id)
        return {'done': True, 'end_id': end_id, 'state': end.state}


def recover(force=False):
    """恢复中断：只对“已推送但超时未完成”或“从未推送”的端补发，不重复压入仍在队列的端；
    所有端都用各自版本的冻结资料，不读来源或研究的最新结果。

    worker 启动时的首轮用 force=True 重推所有未终局端：队列丢失（Redis 重启未落盘等）
    也能续算；重复入队安全，因为领取是条件更新，后到者直接跳过。
    """
    pushed = 0
    with _session() as db:
        stale_cutoff = datetime.now(timezone.utc).timestamp() - _stale_seconds()
        for end in list(db.scalars(select(SparesEnd).where(SparesEnd.state == 'running'))):
            claimed = _timestamp(end.claimed_at)
            if not force and claimed and claimed.timestamp() > stale_cutoff:
                continue
            db.execute(update(SparesEnd).where(SparesEnd.id == end.id)
                       .values(state='queued', claimed_at='', enqueued_at=''))
            db.commit()
            if _enqueue(end.id):
                pushed += 1
        for end in list(db.scalars(select(SparesEnd).where(SparesEnd.state == 'queued'))):
            if force:
                db.execute(update(SparesEnd).where(SparesEnd.id == end.id).values(enqueued_at=''))
                db.commit()
            if _enqueue(end.id):
                pushed += 1
        for version in list(db.scalars(select(SparesVersion).where(SparesVersion.state == 'computing'))):
            publish_if_ready(db, version.id)
    return pushed


def _session():
    return Session()


def _ends_for(db, version):
    return {e.end: e for e in db.scalars(select(SparesEnd).where(SparesEnd.version_id == version.id))}


def serialize_for_run(db, run):
    study = _study_for_run(db, run)
    payload = {'run_id': run.id, 'study': None, 'versions': [], 'current_id': None, 'labels': []}
    if isinstance(run.result, dict) and isinstance(run.result.get('labels'), list):
        payload['labels'] = list(run.result['labels'])
    if not study:
        return payload
    versions = list(db.scalars(select(SparesVersion).where(SparesVersion.study_id == study.id)
                               .order_by(SparesVersion.version_no.desc())))
    latest = versions[0] if versions else None
    payload['study'] = {'id': study.id, 'name': study.name, 'run_id': study.run_id,
                        'created_at': study.created_at, 'created_by': study.created_by}
    payload['current_id'] = latest.id if latest else None
    for v in versions:
        ends = _ends_for(db, v)
        current = latest is not None and v.id == latest.id
        visible = current and v.state == 'published'
        item = {
            'id': v.id, 'version_no': v.version_no, 'current': current, 'state': v.state,
            'labels': v.labels, 'matrix': v.matrix, 'counts': v.counts,
            'tolerance': v.tolerance, 'target_label': v.target_label, 'checksum': v.checksum,
            'created_at': v.created_at, 'created_by': v.created_by, 'published_at': v.published_at,
            'ends': {end: {'state': ends[end].state, 'attempts': ends[end].attempts}
                     for end in ENDS if end in ends},
            'visible': visible, 'results': None, 'failure_reason': '',
        }
        if current and v.state == 'failed':
            item['failure_reason'] = '；'.join(e.reason for end, e in sorted(ends.items()) if e.state == 'failed')[:2000]
        if visible:
            item['results'] = {end: ends[end].result for end in ENDS if end in ends}
        payload['versions'].append(item)
    return payload


def for_run(db, run_id):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, '未找到分析研究')
    return serialize_for_run(db, run)


def seed_demo_for(run_id):
    """首轮种子研究完成后补一版可展示的备件反演；判读件数取混淆矩阵预测列合计。"""
    import os
    if os.getenv('SEED_DEMO', 'true') != 'true':
        return None
    with Session() as db:
        run = db.get(Run, run_id)
        if not run or run.name != '首轮种子研究' or run.state != 'completed' or not run.result:
            return None
        if _study_for_run(db, run):
            return None
        labels = run.result['labels']; matrix = run.result['confusion_matrix']
        counts = {label: sum(matrix[i][j] for i in range(len(labels))) for j, label in enumerate(labels)}
        try:
            return create_version(db, 'seed', run_id, counts, '5', labels[0])
        except HTTPException:
            logging.exception('spares demo seed rejected')
            return None


def retry_current(db, user, run_id):
    """用同一版冻结资料重跑该版未完成的端；已终局或已发布的端保持原样。"""
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, '未找到分析研究')
    study = _study_for_run(db, run)
    if not study:
        raise HTTPException(404, '该研究还没有备件反演版本')
    version = db.scalar(select(SparesVersion).where(SparesVersion.study_id == study.id)
                        .order_by(SparesVersion.version_no.desc()))
    if not version:
        raise HTTPException(404, '该研究还没有备件反演版本')
    if version.state in ('published', 'superseded'):
        raise HTTPException(409, '该版本已经发布，无需重跑；更正判读件数请另开新版')
    ends = list(db.scalars(select(SparesEnd).where(SparesEnd.version_id == version.id)))
    if any(e.state in ('queued', 'running') for e in ends):
        raise HTTPException(409, '该版本仍有端在排队或计算中，请稍候再决定是否重跑')
    requeued, ids = [], {}
    for end in ends:
        if end.state == 'failed':
            changed = db.execute(update(SparesEnd)
                                 .where(SparesEnd.id == end.id, SparesEnd.state == 'failed')
                                 .values(state='queued', claimed_at='', enqueued_at='',
                                         result=None, reason='', attempts=0))
            if changed.rowcount:
                requeued.append(end.end); ids[end.end] = end.id
    if not requeued:
        raise HTTPException(409, '该版本没有需要重跑的端')
    if version.state == 'failed':
        version.state = 'computing'
    db.commit()
    log(db, user, 'spares.version.retried', version.id, '重跑：' + '、'.join(requeued))
    db.commit()
    for end_id in ids.values():
        _enqueue(end_id)
    return {'version_id': version.id, 'requeued': requeued}
