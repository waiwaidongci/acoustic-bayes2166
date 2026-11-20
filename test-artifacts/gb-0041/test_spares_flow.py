import os
import sys
import threading
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ['DATABASE_URL'] = 'sqlite:///' + str(ROOT / 'test-artifacts' / 'gb-0041' / 'flow.db')
os.environ['SEED_DEMO'] = 'false'
os.environ['JWT_SECRET'] = 'gb-0041-test'
os.environ['ARTIFACT_DIR'] = str(ROOT / 'test-artifacts' / 'gb-0041' / 'artifacts')

from src.config.database import Base, Session, engine  # noqa: E402
from src.models.audit import Audit  # noqa: E402
from src.models.run import Run  # noqa: E402
from src.models.spares import SparesEnd, SparesVersion  # noqa: E402
from src.models.user import User  # noqa: E402
from src.services import runs as runs_service  # noqa: E402
from src.services import spares as spares_service  # noqa: E402
from src.utils.security import hash_password  # noqa: E402
from src.utils.serialization import now  # noqa: E402

fake = fakeredis.FakeRedis(decode_responses=False)
runs_service.queue = fake
spares_service.queue = fake


@pytest.fixture()
def client(tmp_path):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    fake.flushall()
    with Session() as db:
        for role in ('admin', 'analyst', 'viewer'):
            db.add(User(email=role + '@x', password_hash=hash_password('pw'), role=role))
        db.add(Run(source_id=1, name='研究A', state='completed', parameters={}, created_at=now(),
                   snapshot={'source': {'id': 1, 'name': 's', 'filename': 'f'}, 'records': []},
                   result={'labels': ['正常', '轴承磨损', '转子偏心'],
                           'confusion_matrix': [[88, 8, 4], [10, 82, 8], [5, 7, 88]], 'rows': []}))
        db.add(Run(source_id=1, name='进行中', state='running', parameters={}, created_at=now(),
                   snapshot={'source': {}, 'records': []}))
        db.commit()
    return TestClient(app_factory())


def app_factory():
    from src.main import app
    return app


def token(client, role):
    return {'Authorization': 'Bearer ' + client.post('/auth/login', json={
        'email': role + '@x', 'password': 'pw'}).json()['token']}


COUNTS = {'正常': 100, '轴承磨损': 60, '转子偏心': 40}


def drain():
    while fake.llen('spares:queue'):
        spares_service.execute_end(int(fake.lindex('spares:queue', 0)))
        fake.lpop('spares:queue')


def version_of(client, headers, version_no=None):
    body = client.get('/runs/1/spares', headers=headers).json()
    return body if version_no is None else next(v for v in body['versions'] if v['version_no'] == version_no)


def test_requires_completed_run_and_writer(client):
    assert client.get('/runs/1/spares').status_code == 401
    viewer = token(client, 'viewer')
    assert client.post('/runs/1/spares', headers=viewer, json={
        'target_label': '正常', 'tolerance': '5', 'counts': COUNTS}).status_code == 403
    analyst = token(client, 'analyst')
    assert client.post('/runs/2/spares', headers=analyst, json={
        'target_label': '正常', 'tolerance': '5', 'counts': COUNTS}).status_code == 409


def test_both_ends_required_before_visible(client):
    headers = token(client, 'analyst')
    assert client.post('/runs/1/spares', headers=headers, json={
        'target_label': '轴承磨损', 'tolerance': '5', 'counts': COUNTS}).status_code == 200
    first = version_of(client, headers)['versions'][0]
    assert first['state'] == 'computing' and first['visible'] is False and first['results'] is None
    queued=[int(x) for x in fake.lrange('spares:queue',0,-1)]
    spares_service.execute_end(queued[0])
    body = version_of(client, headers)
    assert body['versions'][0]['state'] == 'computing'
    assert body['versions'][0]['visible'] is False
    drain()
    body = version_of(client, headers)
    v = body['versions'][0]
    assert v['state'] == 'published' and v['visible'] is True
    assert set(v['results']) == {'min', 'max'}
    for end in ('min', 'max'):
        assert v['results'][end]['predicted_totals'] == [100, 60, 40]
        assert v['results'][end]['true_totals'][1] == v['results'][end]['target_count']


def test_correction_opens_new_version_and_old_cannot_overwrite(client):
    headers = token(client, 'analyst')
    client.post('/runs/1/spares', headers=headers, json={
        'target_label': '正常', 'tolerance': '5', 'counts': COUNTS})
    drain()
    first = version_of(client, headers)['versions'][0]
    assert first['state'] == 'published'
    v2 = client.post('/runs/1/spares', headers=headers, json={
        'target_label': '正常', 'tolerance': '5',
        'counts': {'正常': 120, '轴承磨损': 60, '转子偏心': 40}}).json()
    assert v2['version_no'] == 2
    body = version_of(client, headers)
    assert body['versions'][0]['version_no'] == 2
    assert body['versions'][0]['visible'] is False
    assert body['versions'][1]['version_no'] == 1 and body['versions'][1]['visible'] is False
    # v1 already published; v2 still computing -> nothing current to show except v2 progress
    assert body['versions'][0]['results'] is None
    drain()
    body = version_of(client, headers)
    assert body['versions'][0]['version_no'] == 2 and body['versions'][0]['visible'] is True
    assert sum(1 for v in body['versions'] if v['results']) == 1


def test_late_old_version_result_marked_superseded(client):
    headers = token(client, 'analyst')
    client.post('/runs/1/spares', headers=headers, json={
        'target_label': '正常', 'tolerance': '5', 'counts': COUNTS})
    v2 = client.post('/runs/1/spares', headers=headers, json={
        'target_label': '正常', 'tolerance': '5',
        'counts': {'正常': 130, '轴承磨损': 60, '转子偏心': 40}}).json()
    # process only v2's ends, leaving v1's ends in queue
    queued = [int(x) for x in fake.lrange('spares:queue', 0, -1)]
    with Session() as db:
        v2_ids = {e.id for e in db.scalars(select(SparesEnd).where(SparesEnd.version_id == v2['id']))}
    for end_id in queued:
        spares_service.execute_end(end_id)
    body = version_of(client, headers)
    assert body['versions'][0]['id'] == v2['id'] and body['versions'][0]['visible'] is True
    assert body['versions'][1]['state'] == 'superseded' and body['versions'][1]['visible'] is False
    assert body['versions'][1]['results'] is None


def test_rerun_same_end_does_not_duplicate_publication(client):
    headers = token(client, 'analyst')
    client.post('/runs/1/spares', headers=headers, json={
        'target_label': '正常', 'tolerance': '5', 'counts': COUNTS})
    queued = [int(x) for x in fake.lrange('spares:queue', 0, -1)]
    for end_id in queued:
        spares_service.execute_end(end_id)
    with Session() as db:
        published = db.scalar(select(func.count()).select_from(Audit).where(Audit.action == 'spares.version.published'))
    assert published == 1
    for end_id in queued:
        assert spares_service.execute_end(end_id).get('skipped')
    drain()
    with Session() as db:
        again = db.scalar(select(func.count()).select_from(Audit).where(Audit.action == 'spares.version.published'))
    assert again == 1


def test_recovery_reuses_frozen_version_data(client):
    headers = token(client, 'analyst')
    client.post('/runs/1/spares', headers=headers, json={
        'target_label': '正常', 'tolerance': '5', 'counts': COUNTS})
    fake.flushall()
    with Session() as db:
        ends = list(db.scalars(select(SparesEnd)))
        ends[0].state = 'running'; ends[0].claimed_at = '2020-01-01T00:00:00+00:00'
        ends[1].state = 'queued'
        db.commit()
        frozen = db.get(SparesVersion, ends[0].version_id)
        counts, matrix, labels, tol = frozen.counts, frozen.matrix, frozen.labels, frozen.tolerance
    assert spares_service.recover(force=True) >= 1
    drain()
    body = version_of(client, headers)
    v = body['versions'][0]
    assert v['state'] == 'published'
    assert v['counts'] == counts and v['matrix'] == matrix and v['labels'] == labels and v['tolerance'] == tol


def test_concurrent_corrections_get_distinct_versions(client):
    results = []

    def worker(i):
        with Session() as db:
            try:
                v = spares_service.create_version(db, 'u@x', 1,
                                                  {'正常': 60 + i, '轴承磨损': 40, '转子偏心': 30}, '3', '正常')
                results.append(v['version_no'])
            except Exception as e:  # pragma: no cover
                results.append(('error', str(e)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 6 and all(isinstance(x, int) for x in results)
    assert sorted(results) == list(range(1, 7))


def test_validation_and_retry_endpoint(client):
    headers = token(client, 'analyst')
    bad = [
        {'target_label': '不存在', 'tolerance': '5', 'counts': COUNTS},
        {'target_label': '正常', 'tolerance': '5', 'counts': {'正常': 1}},
        {'target_label': '正常', 'tolerance': '5', 'counts': {'正常': 1, '轴承磨损': -1, '转子偏心': 1}},
        {'target_label': '正常', 'tolerance': '-2', 'counts': COUNTS},
        {'target_label': '正常', 'tolerance': '5', 'counts': {'正常': 0, '轴承磨损': 0, '转子偏心': 0}},
    ]
    for payload in bad:
        assert client.post('/runs/1/spares', headers=headers, json=payload).status_code == 422
    client.post('/runs/1/spares', headers=headers, json={
        'target_label': '正常', 'tolerance': '5', 'counts': COUNTS})
    drain()
    assert client.post('/runs/1/spares/retry', headers=headers).status_code == 409


def test_impossible_inputs_show_reason_for_both_ends(client):
    headers = token(client, 'analyst')
    client.post('/runs/1/spares', headers=headers, json={
        'target_label': '正常', 'tolerance': '5',
        'counts': {'正常': 100, '轴承磨损': 0, '转子偏心': 40}})
    drain()
    v = version_of(client, headers)['versions'][0]
    assert v['state'] == 'published' and v['visible'] is True
    for end in ('min', 'max'):
        assert v['results'][end]['feasible'] is False
        assert v['results'][end]['reason']
