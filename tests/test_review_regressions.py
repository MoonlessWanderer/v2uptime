"""Regression cases confirmed during the full project review."""
import asyncio

import pytest

from app.models import Check, Incident, Server
from app.services.backup import import_data
from app.services.checker import CheckRecord, CycleReport
from app.services.importer import prepare
from app.services.incidents import IncidentService
from app.services.monitor import Monitor
from app.services.parser import parse_link
from app.services.settings import settings
from app.services.stats import StatsService
from app.services.status import LivePoint, LiveRegistry
from app.timeutil import now_ts
from tests.conftest import add_checks, make_server
from tests.test_web import app_factory, client, csrf_from, login


@pytest.mark.parametrize('legacy', [False, True])
def test_backup_replace_keeps_preferred_telegram_server(database, legacy):
    from app.services.backup import export_data
    server = make_server('Preferred')
    server.identity = parse_link(server.raw_link).identity
    server.save()
    settings.set('telegram_preferred_server', server.id)
    payload = export_data()
    if legacy:
        for item in payload['servers']:
            item.pop('id', None)
    import_data(payload, replace=True)
    assert settings.get('telegram_preferred_server') == Server.get().id


def test_backup_merge_maps_preferred_server_to_existing_duplicate(database):
    from app.services.backup import export_data
    server = make_server('Preferred')
    server.identity = parse_link(server.raw_link).identity
    server.save()
    settings.set('telegram_preferred_server', server.id)
    payload = export_data()
    payload['servers'][0]['id'] = 99
    payload['settings']['telegram_preferred_server'] = 99
    import_data(payload)
    assert settings.get('telegram_preferred_server') == server.id


@pytest.mark.parametrize('payload', [
    {'servers': [{'raw_link': 'broken'}]},
    {'servers': [None]},
    {'servers': [], 'settings': []},
    {'servers': [], 'settings': {'check_interval': 'invalid'}},
    {},
])
def test_invalid_replace_preserves_history(database, payload):
    original = make_server('Original')
    add_checks(original, now_ts()-120, 'oo')
    with pytest.raises(ValueError):
        import_data({'format': 'v2uptime-backup', **payload}, replace=True)
    assert Server.get().id == original.id
    assert Check.select().count() == 2


def test_replacement_does_not_inherit_old_live_results(client):
    original = make_server('Original')
    now = now_ts()
    live = client.app.state.services.live
    live.add(original.id, LivePoint(now-60, 'ok', 123))
    live.add(original.id, LivePoint(now, 'ok', 123))
    login(client)
    token = csrf_from(client.get('/admin/backup').text)
    import json
    payload = {'format':'v2uptime-backup', 'servers':[{'raw_link': original.raw_link.replace('Original', 'Replacement')}]}
    response = client.post('/admin/backup/import', data={'csrf_token':token, 'mode':'replace'},
                           files={'file':('backup.json', json.dumps(payload), 'application/json')})
    assert response.status_code == 200
    new = Server.get()
    assert new.id != original.id
    card = client.get('/api/status').json()['servers'][0]
    assert card['ping_ms'] is None and card['status'] == 'unknown'
    assert live.last_check_ts() is None


def test_malformed_ipv6_paste_is_validation_error(database):
    result = asyncio.run(prepare('vless://abc@[broken:443#bad'))
    assert result.share_errors and not result.to_add


def test_disabled_server_ignores_inflight_failure(database):
    settings.set('fail_threshold', 1)
    server = make_server('Disabled', active=False)
    live, incidents, sent = LiveRegistry(), IncidentService(), []
    stats = StatsService(live, incidents.in_incident)
    monitor = Monitor(live, incidents, stats, sent.append)
    now = now_ts()
    asyncio.run(monitor.handle_cycle(CycleReport(started_at=now, checked=1, fail=1,
        records=[CheckRecord(server.id, now, 'fail', None, 'timeout')])))
    assert not Check.select().exists()
    assert not Incident.select().exists()
    assert not sent and not live.points(server.id)


def test_failed_edit_preserves_submitted_values(client):
    server = make_server('Original')
    login(client)
    url = f'/admin/servers/{server.id}/edit'
    token = csrf_from(client.get(url).text)
    response = client.post(url, data={'csrf_token':token, 'name':'My draft', 'country':'1!',
                                    'tags':'draft tag', 'raw_link':'bad link', 'is_active':'1'})
    assert 'My draft' in response.text and 'draft tag' in response.text
    assert 'bad link</textarea>' in response.text
    assert Server.get_by_id(server.id).name == 'Original'
