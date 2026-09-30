"""Read a full BOSS job section in the frozen, already-authenticated browser.

No cookie extraction, new browser/context, HTTP API fallback or challenge retries.
Only the page created by this call is closed; verification pages are left for the user.
"""
from __future__ import annotations
import re
import time
from urllib.parse import urlparse


class RenderedDetailError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


READ_PAGE = r"""() => {
  const text = e => (e?.innerText || '').trim();
  const visible = e => {
    if (!e) return false;
    const r=e.getBoundingClientRect(), s=getComputedStyle(e);
    if (!(r.width>0 && r.height>0) || s.visibility==='hidden') return false;
    for (let node=e; node; node=node.parentElement) {
      const style=getComputedStyle(node);
      if (style.display==='none' || style.visibility==='hidden' || Number(style.opacity)<0.01 || node.getAttribute('aria-hidden')==='true') return false;
    }
    return true;
  };
  const body = text(document.body);
  const url = location.origin+location.pathname;
  if (/访问环境风控|访问环境存在异常|环境存在异常|IP\s*地址存在异常/.test(body)) return {error:'ENVIRONMENT_RISK'};
  if (/security[-_]?check|zhipin-security|captcha/i.test(location.pathname) ||
      visible(document.querySelector('.geetest_panel, .verify-wrap, .captcha-container'))) return {error:'PLATFORM_VERIFICATION_REQUIRED'};
  if (/\/login|passport/.test(location.pathname)) return {error:'AUTH_REQUIRED',login_route:true};
  if ([...document.querySelectorAll('.sign-form,.login-card')].some(visible)) return {error:'AUTH_REQUIRED'};
  const title = text(document.querySelector('.job-primary .name h1,.job-primary h1,h1[title]'));
  const companyEl = document.querySelector('.job-sider a[href^="/gongsi/"],.detail-op a[href*="/gongsi/"]');
  const company = (companyEl?.getAttribute('title') || text(companyEl) || document.title.match(/_([^_]+)招聘-BOSS直聘$/)?.[1] || '').trim();
  const candidates = [...document.querySelectorAll('.job-detail .job-sec-text')];
  const first = candidates.find(e => visible(e) && !e.classList.contains('fold-text'));
  const sections = candidates.filter(e => {
    if (!visible(e) || e.classList.contains('fold-text')) return false;
    const heading = text(e.closest('.job-detail-section,.job-sec')?.querySelector('h3,h4,.job-sec-title'));
    return e===first || /职位描述|岗位职责|任职要求|工作内容|岗位要求/.test(heading);
  });
  const clipped = sections.some(e => {
    const s=getComputedStyle(e);
    return (s.webkitLineClamp && !['none','0'].includes(s.webkitLineClamp)) ||
      (['hidden','clip'].includes(s.overflowY) && e.scrollHeight>e.clientHeight+3);
  });
  const description = [...new Set(sections.map(text).filter(Boolean))].join('\n\n');
  return {url,title,company,description,full_section:!!sections.length && !clipped,
    city:text(document.querySelector('.job-primary .text-city')),
    salary:text(document.querySelector('.job-primary .salary,.job-banner .salary')),
    section_count:sections.length};
}"""


def read_rendered_job(cdp_url: str, job_id: str, timeout_seconds: int = 60) -> dict:
    endpoint = urlparse(cdp_url or '')
    if (endpoint.scheme != 'http' or endpoint.hostname not in ('127.0.0.1','localhost','::1')
            or not endpoint.port or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise RenderedDetailError('CDP_UNAVAILABLE','完整详情读取只接受已冻结的本机浏览器连接')
    if not re.fullmatch(r'[A-Za-z0-9_~-]{3,128}', job_id or ''):
        raise RenderedDetailError('UPSTREAM_CONTRACT_CHANGED','详情缺少稳定的公开岗位标识')
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        raise RenderedDetailError('DEPENDENCY_MISSING','完整详情需要 boss-agent-cli 使用的 patchright 依赖')
    expected_url = f'https://www.zhipin.com/job_detail/{job_id}.html'
    limit = min(max(timeout_seconds,1),60)
    deadline = time.monotonic()+limit
    page = None
    created_page = False
    keep_for_verification = False
    login_observations = 0
    with sync_playwright() as driver:
        browser = driver.chromium.connect_over_cdp(cdp_url, timeout=min(limit*1000,10000))
        contexts = browser.contexts
        if len(contexts) != 1:
            raise RenderedDetailError('BROWSER_SESSION_NOT_FOUND','目标浏览器会话不唯一；不会另建登录环境')
        try:
            # A kept verification page may have finished loading since the last stop.
            # Read it in place; never close a page we did not create in this invocation.
            matches=[candidate for candidate in getattr(contexts[0],'pages',[])
                     if getattr(candidate,'url','').split('?')[0] == expected_url]
            if len(matches)==1:
                page=matches[0]
            else:
                page = contexts[0].new_page()
                created_page = True
                page.goto(expected_url, wait_until='domcontentloaded', timeout=min(limit*1000,25000))
            while time.monotonic()<deadline:
                row = page.evaluate(READ_PAGE)
                if row.get('error'):
                    code = row['error']
                    # Observe the same DOM during hydration; never navigate or retry a challenge.
                    if code == 'AUTH_REQUIRED' and not row.get('login_route'):
                        login_observations += 1
                        if login_observations < 121 and time.monotonic()<deadline-0.5:
                            page.wait_for_timeout(500)
                            continue
                    keep_for_verification = code in ('AUTH_REQUIRED','ENVIRONMENT_RISK','PLATFORM_VERIFICATION_REQUIRED')
                    raise RenderedDetailError(code,'详情页需要本人处理平台验证或登录，已停止且未重试')
                login_observations = 0
                if row.get('title') and row.get('company') and row.get('description'):
                    if row.get('url') != expected_url:
                        raise RenderedDetailError('UPSTREAM_CONTRACT_CHANGED','详情落地页岗位标识与请求不一致')
                    complete = bool(row.get('full_section'))
                    return {'job_id':job_id,'title':row['title'],'company':row['company'],
                        'description':row['description'],'city':row.get('city'),'salary':row.get('salary'),
                        '_detail_source':'boss_rendered_page','_detail_status':'complete' if complete else 'partial',
                        '_rendered_proof':{'source_url':row['url'],'job_id':job_id,
                            'full_section':complete,'section_count':row['section_count']}}
                page.wait_for_timeout(500)
            if login_observations:
                keep_for_verification=True
                raise RenderedDetailError('AUTH_REQUIRED','详情页持续显示登录提示，已保留页面供本人处理且未重试')
            raise RenderedDetailError('UPSTREAM_CONTRACT_CHANGED','详情页未显示可验证的职位描述，未使用卡片替代全文')
        finally:
            if page is not None and created_page and not keep_for_verification:
                page.close()
            # Stopping this driver disconnects it; never call browser.close().
