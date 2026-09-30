"""Dedicated browser identity, login command, and pending intent regressions (offline)."""
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
import test_pipeline as base
import managed_browser as mb
import channel_state
import runs
import store
import json
from upstream_config import build_login_argv
from upstream_envelope import UpstreamError


@pytest.fixture
def client():
    with TestClient(base.app_module.app, raise_server_exceptions=False) as test_client:
        yield test_client


def _cdp_client(recorded, *, listing, put_status=200,
                payload=None):
    """假 CDP 客户端：记录每个浏览器级请求，供断言「只打专用端点、零业务请求」。"""
    body = payload if payload is not None else {'url': mb.OFFICIAL_URL}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            recorded.append(('GET', url))
            if url.endswith('/json/list'):
                return SimpleNamespace(status_code=200, json=lambda: listing,
                                       raise_for_status=lambda: None)
            # 仅用于 PUT /json/new 返回 405 时的旧版回退
            return SimpleNamespace(status_code=200, json=lambda: body,
                                   raise_for_status=lambda: None)

        def put(self, url, params=None):
            recorded.append(('PUT', url))
            return SimpleNamespace(status_code=put_status, json=lambda: body,
                                   raise_for_status=lambda: None)

    return Client


def _assert_zero_business_requests(recorded):
    """只允许浏览器级 ``/json/*`` 命令，且全部命中专用端点；不得有 search/detail/login。"""
    for _, url in recorded:
        assert url.startswith('http://127.0.0.1:9231/json/'), url
        assert not any(bad in url for bad in ('/api/auth/login', '/search', '/detail')), url


def test_login_uses_explicit_cdp_without_clearing_session(tmp_path, monkeypatch):
    base_argv = build_login_argv('C:/boss.exe', tmp_path, timeout_seconds=180)
    # 注入现在必须匹配专用 profile 的活端点；这里把活端点打桩成同一地址。
    monkeypatch.setattr(mb, 'endpoint_for_profile', lambda profile: 'http://127.0.0.1:9231')
    argv = mb.login_argv(base_argv, 'http://127.0.0.1:9231')
    assert argv[argv.index('--cdp-url')+1] == 'http://127.0.0.1:9231'
    assert argv.index('--cdp-url') < argv.index('login') < argv.index('--cdp')
    assert '--force' not in argv and '--cdp' not in base_argv


@pytest.mark.parametrize('host,path,expected', [
    ('127.0.0.1', '/devtools/browser/owned', True),
    ('127.0.0.1', '/devtools/browser/unrelated', False),
    ('example.com', '/devtools/browser/owned', False),
])
def test_endpoint_checks_profile_identity(tmp_path, monkeypatch, host, path, expected):
    (tmp_path/'DevToolsActivePort').write_text('9231\n/devtools/browser/owned\n')
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url):
            assert url == 'http://127.0.0.1:9231/json/version'
            return SimpleNamespace(raise_for_status=lambda: None,
                json=lambda: {'webSocketDebuggerUrl': f'ws://{host}:9231{path}'})
    monkeypatch.setattr(mb.httpx, 'Client', Client)
    assert bool(mb.endpoint_for_profile(tmp_path)) is expected


def test_existing_owned_browser_is_reused(monkeypatch):
    monkeypatch.setattr(mb, 'endpoint_for_profile', lambda p: 'http://127.0.0.1:9231')
    selected = []
    monkeypatch.setattr(mb, 'select_channel', selected.append)
    monkeypatch.setattr(mb.subprocess, 'Popen', lambda *a, **k: pytest.fail('must reuse browser'))
    assert mb.ensure() == 'http://127.0.0.1:9231'
    assert selected == ['http://127.0.0.1:9231']


def test_pending_intent_adopts_login_channel_only_before_collection(monkeypatch):
    rid = runs.create_run('pending managed', {'source':'demo', 'queries':['pending managed']})['id']
    config = runs.get_run(rid)['config_snapshot']
    config.update(source='zhipin', channel={'fingerprint':'old'})
    store.execute("UPDATE research_run SET stage='pending_login', config_snapshot=? WHERE id=?",
                  (store.jdumps(config), rid))
    monkeypatch.setattr(channel_state, 'freeze_channel', lambda: {'fingerprint':'new'})
    base.PIPELINE._bind_unstarted_channel(rid)
    assert runs.get_run(rid)['config_snapshot']['channel']['fingerprint'] == 'new'
    counters = runs.get_run(rid)['counters']
    counters['search_calls'] = 1
    store.execute('UPDATE research_run SET counters=? WHERE id=?', (store.jdumps(counters), rid))
    monkeypatch.setattr(channel_state, 'freeze_channel', lambda: {'fingerprint':'third'})
    base.PIPELINE._bind_unstarted_channel(rid)
    assert runs.get_run(rid)['config_snapshot']['channel']['fingerprint'] == 'new'


def test_empty_exhausted_search_can_start_fresh():
    query = 'managed exhausted regression'
    rid = runs.create_run(query, {'source':'demo','queries':[query]})['id']
    run = runs.get_run(rid)
    run['counters']['search_calls'] = run['limits']['max_search_calls']
    store.execute("UPDATE research_run SET origin='simple', stage='paused', query_text=?, counters=? WHERE id=?",
                  (query, store.jdumps(run['counters']), rid))
    assert base.PIPELINE._find_same_query_run(query) is None


@pytest.mark.parametrize('browser_source', ['stored-cookie', 'existing-browser'])
def test_explicit_resume_rechecks_managed_connection(monkeypatch, browser_source):
    rid = runs.create_run('resume managed', {'source':'demo','queries':['resume managed']})['id']
    store.execute("UPDATE research_run SET stage='paused' WHERE id=?", (rid,))
    checked = []
    monkeypatch.setattr(base.PIPELINE, '_rearm_channel_for_resume', lambda r: True)
    monkeypatch.setattr(channel_state, 'current_policy', lambda: SimpleNamespace(source=browser_source))
    monkeypatch.setattr(channel_state, 'precheck_current', lambda **k: checked.append(k['reason']))
    monkeypatch.setattr(base.PIPELINE, '_login_state', lambda: {'state':'connected'})
    monkeypatch.setattr(base.PIPELINE, '_login_collectable', lambda: bool(checked))
    monkeypatch.setattr(base.PIPELINE, '_spawn_worker', lambda *a, **k: None)
    assert base.PIPELINE.resume_task(rid)['task']['stage'] == 'creating'
    assert checked == ['explicit_resume']


def test_environment_risk_is_not_misreported_as_missing_login_or_captcha():
    from upstream_envelope import map_upstream_error
    import user_view
    envelope = SimpleNamespace(error={'code':'ENVIRONMENT_RISK', 'message':'访问环境风控'},
        operator_actions=[], layer='business', raw_snippet='', argv_redacted=[],
        data_dir='', elapsed_ms=1, command='search', returncode=1)
    error = map_upstream_error(envelope)
    assert error.code == 'ENVIRONMENT_RISK' and error.retryable is False
    assert runs._state_for_error(error.code) == 'risk_stopped'
    view = user_view.user_view('paused', 'risk_stopped', error.code)
    assert '登录已成功' in view['user_message'] and '访问环境' in view['user_message']
    assert not any(a['id'] == 'resume' for a in view['actions'])


def test_launch_nonzero_port_and_record_browser_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, 'PROFILE', tmp_path)
    monkeypatch.setattr(mb, 'chrome_path', lambda: tmp_path/'chrome.exe')
    monkeypatch.setattr(mb, '_choose_port', lambda: 9231)
    monkeypatch.setattr(mb, '_version', lambda port: {'webSocketDebuggerUrl':'ws://127.0.0.1:9231/devtools/browser/new'})
    monkeypatch.setattr(mb, '_listener_owned', lambda port,pid: port==9231 and pid==456)
    spawned=[]
    def spawn(argv, **kwargs):
        spawned.append(argv)
        return SimpleNamespace(pid=456, poll=lambda: None)
    monkeypatch.setattr(mb.subprocess, 'Popen', spawn)
    selected=[]
    monkeypatch.setattr(mb, 'select_channel', selected.append)
    assert mb.ensure()=='http://127.0.0.1:9231'
    assert '--remote-debugging-port=9231' in spawned[0]
    assert '--remote-debugging-port=0' not in spawned[0]
    assert '--headless' not in spawned[0] and '--enable-automation' not in spawned[0]
    assert json.loads((tmp_path/mb.RECEIPT_NAME).read_text())['browser_path']=='/devtools/browser/new'
    assert selected==['http://127.0.0.1:9231']


def test_port_race_rejects_unrelated_listener(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, 'PROFILE', tmp_path)
    monkeypatch.setattr(mb, 'chrome_path', lambda: tmp_path/'chrome.exe')
    monkeypatch.setattr(mb, '_choose_port', lambda: 9231)
    monkeypatch.setattr(mb, '_version', lambda port: {'webSocketDebuggerUrl':'ws://127.0.0.1:9231/devtools/browser/other'})
    monkeypatch.setattr(mb, '_listener_owned', lambda *args: False)
    monkeypatch.setattr(mb.subprocess, 'Popen', lambda *a,**k: SimpleNamespace(pid=456,poll=lambda:None))
    monkeypatch.setattr(mb, 'select_channel', lambda *a: pytest.fail('must not adopt foreign browser'))
    with pytest.raises(OSError, match='归属不匹配'): mb.ensure()
    assert not (tmp_path/mb.RECEIPT_NAME).exists()


def test_stale_owned_receipt_cannot_fall_back_to_another_browser(tmp_path, monkeypatch):
    (tmp_path/mb.RECEIPT_NAME).write_text(json.dumps({'port':9231,'browser_path':'/devtools/browser/old'}))
    (tmp_path/'DevToolsActivePort').write_text('9232\n/devtools/browser/other\n')
    monkeypatch.setattr(mb, '_version', lambda port: {'webSocketDebuggerUrl':f'ws://127.0.0.1:{port}/devtools/browser/other'})
    assert mb.endpoint_for_profile(tmp_path) is None


def test_repair_api_rejects_cross_origin_without_touching_browser(monkeypatch):
    from fastapi.testclient import TestClient
    import app
    monkeypatch.setattr(mb,'restart_owned',lambda:pytest.fail('cross origin must not restart'))
    response=TestClient(app.app,base_url='http://127.0.0.1:8766').post('/api/auth/browser/repair',
        headers={'origin':'https://unrelated.example','sec-fetch-site':'cross-site'})
    assert response.json()['error']['code']=='TOOL_NOT_ALLOWED'


def test_repair_api_does_not_login_or_search(monkeypatch):
    from fastapi.testclient import TestClient
    import app
    calls=[]
    monkeypatch.setattr(app.login_bridge.controller,'active',lambda:None)
    monkeypatch.setattr(app.pipeline.pipeline,'_threads',{})
    monkeypatch.setattr(mb,'restart_owned',lambda:calls.append('restart'))
    monkeypatch.setattr(channel_state,'precheck_current',lambda **k:calls.append('precheck'))
    monkeypatch.setattr(app.login_bridge.controller,'start',lambda *a:pytest.fail('must not login'))
    response=TestClient(app.app,base_url='http://127.0.0.1:8766').post('/api/auth/browser/repair',json={},
        headers={'origin':'http://127.0.0.1:8766'})
    assert response.json()['ok'] and calls==['restart','precheck']


# ---------------------------------------------------------------------------
# 二轮修正 #2：环境风控的人工确认入口**只能**开在专用浏览器里
# （被标记的就是专用 profile 那个会话；在日常浏览器里确认毫无作用）
# ---------------------------------------------------------------------------

def test_open_official_page_focuses_existing_tab_on_dedicated_endpoint(monkeypatch):
    """已有一个官方页签 → 只聚焦，**只**发一次 ``/json/list``，零业务请求。"""
    monkeypatch.setattr(mb, 'endpoint_for_profile', lambda p: 'http://127.0.0.1:9231')
    recorded = []
    listing = [{'type': 'page', 'url': 'https://www.zhipin.com/web/user/'}]
    monkeypatch.setattr(mb.httpx, 'Client', _cdp_client(recorded, listing=listing))

    result = mb.open_official_page()
    assert result == {'opened': False, 'already_open': True,
                      'url': 'https://www.zhipin.com/web/user/'}
    assert recorded == [('GET', 'http://127.0.0.1:9231/json/list')]
    _assert_zero_business_requests(recorded)


def test_open_official_page_opens_new_tab_via_put_on_dedicated_endpoint(monkeypatch):
    """没有官方页签 → 用 PUT ``/json/new`` 新建一个（Chrome 111+），命中专用端点。"""
    monkeypatch.setattr(mb, 'endpoint_for_profile', lambda p: 'http://127.0.0.1:9231')
    recorded = []
    monkeypatch.setattr(mb.httpx, 'Client', _cdp_client(recorded, listing=[]))

    result = mb.open_official_page()
    assert result['opened'] is True and result['already_open'] is False
    assert ('GET', 'http://127.0.0.1:9231/json/list') in recorded
    assert ('PUT', 'http://127.0.0.1:9231/json/new') in recorded
    _assert_zero_business_requests(recorded)


def test_open_official_page_falls_back_to_get_when_put_is_405(monkeypatch):
    """Chrome <111 的 ``PUT /json/new`` 返回 405 → 回退 GET ``/json/new``（仍是浏览器级命令）。"""
    monkeypatch.setattr(mb, 'endpoint_for_profile', lambda p: 'http://127.0.0.1:9231')
    recorded = []
    monkeypatch.setattr(mb.httpx, 'Client',
                        _cdp_client(recorded, listing=[], put_status=405))

    result = mb.open_official_page()
    assert result['opened'] is True
    assert recorded == [('GET', 'http://127.0.0.1:9231/json/list'),
                        ('PUT', 'http://127.0.0.1:9231/json/new'),
                        ('GET', 'http://127.0.0.1:9231/json/new')]
    _assert_zero_business_requests(recorded)


def test_open_official_page_requires_live_dedicated_browser(monkeypatch):
    """专用浏览器未运行 → 明确报错，绝不静默失败，也绝不发任何请求。"""
    monkeypatch.setattr(mb, 'endpoint_for_profile', lambda p: None)
    monkeypatch.setattr(mb.httpx, 'Client', lambda **k: pytest.fail('未运行的浏览器不该发请求'))

    with pytest.raises(UpstreamError) as excinfo:
        mb.open_official_page()
    assert excinfo.value.code == 'OWN_BROWSER_NOT_READY'
    assert excinfo.value.retryable is True


def test_open_official_page_rejects_non_official_host(monkeypatch):
    """只允许 BOSS 官方 host；别的 URL 一律拒绝（白名单，且不触网）。"""
    monkeypatch.setattr(mb, 'endpoint_for_profile', lambda p: 'http://127.0.0.1:9231')
    monkeypatch.setattr(mb.httpx, 'Client', lambda **k: pytest.fail('非法 host 不该触网'))

    with pytest.raises(UpstreamError) as excinfo:
        mb.open_official_page('https://evil.example.com/')
    assert excinfo.value.code == 'TOOL_NOT_ALLOWED'


def test_open_official_endpoint_uses_dedicated_browser_with_zero_business_requests(client, monkeypatch):
    """HTTP 层：``POST /api/channel/open-official`` 只经专用浏览器 CDP，零业务请求。"""
    monkeypatch.setattr(mb, 'endpoint_for_profile', lambda p: 'http://127.0.0.1:9231')
    recorded = []
    monkeypatch.setattr(mb.httpx, 'Client', _cdp_client(recorded, listing=[]))

    body = client.post('/api/channel/open-official').json()
    assert body['ok'], body
    assert body['data']['url'].startswith('https://www.zhipin.com')
    _assert_zero_business_requests(recorded)


def test_open_official_endpoint_surfaces_not_running_honestly(client, monkeypatch):
    """HTTP 层：专用浏览器未运行 → 如实返回错误码，不谎报成功。"""
    monkeypatch.setattr(mb, 'endpoint_for_profile', lambda p: None)

    body = client.post('/api/channel/open-official').json()
    assert body['ok'] is False, body
    assert body['error']['code'] == 'OWN_BROWSER_NOT_READY'
