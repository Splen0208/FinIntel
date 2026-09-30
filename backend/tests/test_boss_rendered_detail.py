import sys
import types
from contextlib import contextmanager
import pytest
import boss_rendered_detail as reader


def fake_browser(monkeypatch,row,existing=False):
    state={'closed':False,'navigations':[]}
    rows=iter(row) if isinstance(row,list) else None
    class Page:
        def goto(self,url,**kwargs): state['navigations'].append(url)
        def evaluate(self,script): return next(rows) if rows is not None else row
        def wait_for_timeout(self,ms): pass
        def close(self): state['closed']=True
    page=Page()
    if existing: page.url=row['url']
    context=types.SimpleNamespace(new_page=lambda:page,pages=[page] if existing else [])
    browser=types.SimpleNamespace(contexts=[context])
    chromium=types.SimpleNamespace(connect_over_cdp=lambda *a,**k:browser)
    @contextmanager
    def driver(): yield types.SimpleNamespace(chromium=chromium)
    monkeypatch.setitem(sys.modules,'patchright.sync_api',types.SimpleNamespace(sync_playwright=driver))
    return state


def test_full_page_uses_one_navigation_and_closes_only_its_page(monkeypatch):
    state=fake_browser(monkeypatch,{'url':'https://www.zhipin.com/job_detail/TEST-ID.html',
        'title':'offline role','company':'offline company','description':'offline original body',
        'full_section':True,'section_count':1})
    result=reader.read_rendered_job('http://127.0.0.1:9222','TEST-ID')
    assert result['_detail_status']=='complete'
    assert state['closed'] and len(state['navigations'])==1


def test_platform_verification_stops_without_retry_or_closing_user_page(monkeypatch):
    state=fake_browser(monkeypatch,{'error':'PLATFORM_VERIFICATION_REQUIRED'})
    with pytest.raises(reader.RenderedDetailError) as error:
        reader.read_rendered_job('http://127.0.0.1:9222','TEST-ID')
    assert error.value.code=='PLATFORM_VERIFICATION_REQUIRED'
    assert not state['closed'] and len(state['navigations'])==1


def test_detail_page_identity_mismatch_is_rejected(monkeypatch):
    fake_browser(monkeypatch,{'url':'https://www.zhipin.com/job_detail/OTHER-ID.html',
        'title':'offline','company':'offline','description':'offline','full_section':True,'section_count':1})
    with pytest.raises(reader.RenderedDetailError) as error:
        reader.read_rendered_job('http://127.0.0.1:9222','TEST-ID')
    assert error.value.code=='UPSTREAM_CONTRACT_CHANGED'


def test_remote_endpoint_is_rejected_before_browser_connection():
    with pytest.raises(reader.RenderedDetailError) as error:
        reader.read_rendered_job('https://remote.example:9222','TEST-ID')
    assert error.value.code=='CDP_UNAVAILABLE'


def test_transient_login_overlay_during_hydration_does_not_trigger_new_navigation(monkeypatch):
    state=fake_browser(monkeypatch,[{'error':'AUTH_REQUIRED'},
        {'url':'https://www.zhipin.com/job_detail/TEST-ID.html','title':'offline',
         'company':'offline','description':'offline','full_section':True,'section_count':1}])
    result=reader.read_rendered_job('http://127.0.0.1:9222','TEST-ID')
    assert result['_detail_status']=='complete'
    assert state['closed'] and len(state['navigations'])==1


def test_stable_login_overlay_stops_and_keeps_page_for_the_user(monkeypatch):
    state=fake_browser(monkeypatch,{'error':'AUTH_REQUIRED'})
    with pytest.raises(reader.RenderedDetailError) as error:
        reader.read_rendered_job('http://127.0.0.1:9222','TEST-ID')
    assert error.value.code=='AUTH_REQUIRED'
    assert not state['closed'] and len(state['navigations'])==1


def test_public_job_id_accepts_url_unreserved_tilde(monkeypatch):
    state=fake_browser(monkeypatch,{'url':'https://www.zhipin.com/job_detail/TEST-ID~.html',
        'title':'offline','company':'offline','description':'offline','full_section':True,'section_count':1})
    assert reader.read_rendered_job('http://127.0.0.1:9222','TEST-ID~')['_detail_status']=='complete'
    assert state['navigations']==['https://www.zhipin.com/job_detail/TEST-ID~.html']


def test_ready_existing_detail_is_read_without_navigation_or_closing_user_page(monkeypatch):
    state=fake_browser(monkeypatch,{'url':'https://www.zhipin.com/job_detail/TEST-ID.html',
        'title':'offline','company':'offline','description':'offline','full_section':True,'section_count':1},existing=True)
    assert reader.read_rendered_job('http://127.0.0.1:9222','TEST-ID')['_detail_status']=='complete'
    assert not state['closed'] and state['navigations']==[]
