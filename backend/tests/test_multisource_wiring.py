"""离线回归：三个通道独立认证、X 浏览器字段和接口错误分类。"""
import asyncio
import json
from types import SimpleNamespace
import social_testkit as kit
import social_source_status as status
import social_pipeline
import managed_browser
import store
from collectors.x_source import XSource, XBrowserSource


def test_boss_ready_cannot_authenticate_xhs():
    with kit.isolated_db():
        assert status._xhs_channel_states()[:2] == ('unknown', 'unknown')
        status.record_collection('xiaohongshu')
        assert status._xhs_channel_states()[:2] == ('ready', 'authenticated')
        status.record_collection('x', error_code='AUTH_REQUIRED')
        assert status._xhs_channel_states()[:2] == ('ready', 'authenticated')


def test_managed_browser_selects_actual_browser_channel():
    with kit.isolated_db():
        managed_browser.select_channel('http://127.0.0.1:9222')
        assert store.query_one("SELECT value FROM app_setting WHERE key='boss_browser_source'")['value'] == 'existing-browser'


def test_owned_browser_connection_check_does_not_wait_for_unrelated_online_doctor(monkeypatch):
    import channel_state
    monkeypatch.setattr(managed_browser, 'endpoint_for_profile', lambda profile: 'http://127.0.0.1:9222')
    probe = channel_state.ChannelProbe()
    monkeypatch.setattr(probe, 'run_doctor', lambda *args: (_ for _ in ()).throw(AssertionError('doctor must not run')))
    result = probe.precheck(channel_state.ChannelPolicy.for_source('existing-browser'), 'boss.exe', '.', 'http://127.0.0.1:9222')
    assert result['ok'] and result['cdp_ok']


def test_installed_node_entry_is_used_without_shell_shim(monkeypatch):
    from collectors import provider
    monkeypatch.setattr(provider, 'resolve_provider', lambda kind: SimpleNamespace(entry_path='/installed/main.js', exe_path='node'))
    assert social_pipeline._resolve_exe('xhs') == '/installed/main.js'


def test_x_api_404_is_compatibility_failure():
    error = XSource('unused')._error_from({'returncode':1, 'stdout':json.dumps({
        'ok':False, 'error':{'code':'not_found', 'message':'Twitter API error (HTTP 404)'}}), 'stderr':''})
    assert error.code == 'CONTRACT_CHANGED'


def test_readonly_source_probe_preserves_actual_x_collection(monkeypatch):
    with kit.isolated_db():
        status.record_collection('x')
        monkeypatch.setattr(status, 'x_credentials_state', lambda: ('authenticated', []))
        monkeypatch.setattr(status, '_handle_for', lambda platform: (
            SimpleNamespace(version='offline',exe_path='offline',node_path=None),None,None))
        checked = status.check_platform('x')
        assert checked.connection_state == 'ready'
        assert checked.auth_state == 'authenticated'


def test_x_browser_search_retains_time_filter_metrics_and_anonymous_author(monkeypatch):
    from collectors import x_source
    captured = {}
    async def fake_command(*args, **kwargs):
        captured.update(kwargs)
        return {'returncode':0, 'timed_out':False, 'stderr':'', 'stdout':json.dumps([
            {'id':'offline1234567','author':'offline_author','text':'offline consumer lending',
             'created_at':'Tue Sep 29 10:35:05 +0000 2026','likes':0,'views':'162'}])}
    monkeypatch.setattr(x_source, 'run_command', fake_command)
    batch = asyncio.run(XBrowserSource('unused').search('consumer lending', since='2026-09-01', max_count=3))
    assert captured['platform'] == 'twitter'
    assert captured['positional'] == ('consumer lending since:2026-09-01',)
    assert captured['options']['--product'] == 'live'
    assert 'env' not in captured
    assert batch.records[0].author_key.startswith('anon_')
    assert batch.records[0].metrics['likes'] == 0
    assert batch.records[0].metrics['replies'] is None
    assert batch.records[0].metrics['views'] == 162
