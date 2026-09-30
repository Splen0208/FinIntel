"""One local, dedicated Chrome profile for BOSS login and collection."""
from pathlib import Path
import os
import json
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse
import httpx

from store import PROJECT_DIR, get_conn, query_one
from upstream_config import sanitized_child_env

PROFILE = PROJECT_DIR / '.local' / 'boss-browser'
LOGIN_URL = 'https://www.zhipin.com/web/user/'
_lock = threading.RLock()
RECEIPT_NAME = 'finintel-browser.json'


def _version(port: int) -> dict:
    if not 0 < port < 65536:
        raise ValueError('invalid local port')
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=1) as client:
        response = client.get(f'http://127.0.0.1:{port}/json/version')
        response.raise_for_status()
        data = response.json()
    ws = urlparse(data.get('webSocketDebuggerUrl', ''))
    if (ws.scheme != 'ws' or ws.hostname not in ('127.0.0.1', 'localhost', '::1')
            or ws.port != port or not ws.path.startswith('/devtools/browser/')
            or ws.username or ws.password or ws.query or ws.fragment):
        raise ValueError('browser endpoint does not match local listener')
    return data


def endpoint_for_profile(profile: Path) -> str | None:
    """Verify the browser UUID as well as the port; do not attach to arbitrary Chrome."""
    try:
        receipt = profile / RECEIPT_NAME
        if receipt.exists():
            owned = json.loads(receipt.read_text(encoding='utf-8'))
            port, browser_path = int(owned['port']), owned['browser_path']
        else:
            # Compatibility with the previous port=0 launcher; never rewrite its receipt.
            lines = (profile / 'DevToolsActivePort').read_text(encoding='utf-8').splitlines()
            port, browser_path = int(lines[0]), lines[1]
        if not 0 < port < 65536 or not browser_path.startswith('/devtools/browser/'):
            return None
        endpoint = f'http://127.0.0.1:{port}'
        ws = urlparse(_version(port)['webSocketDebuggerUrl'])
        if ws.hostname not in ('127.0.0.1', 'localhost', '::1') or ws.port != port or ws.path != browser_path:
            return None
        return endpoint
    except (OSError, ValueError, IndexError, KeyError, TypeError, AttributeError, httpx.HTTPError):
        return None


def _choose_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def _listener_owned(port: int, pid: int) -> bool:
    """Do not trust a free-port race: only the Chrome process we launched may own it."""
    if os.name != 'nt':
        return False
    result = subprocess.run(['netstat', '-ano', '-p', 'tcp'], capture_output=True,
                            text=True, timeout=3,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        return False
    listeners = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 5 and fields[0] == 'TCP' and fields[1].endswith(f':{port}'):
            if fields[2] in ('0.0.0.0:0', '[::]:0'):
                listeners.append(fields)
    return bool(listeners) and all(f[1] == f'127.0.0.1:{port}' and f[-1] == str(pid)
                                   for f in listeners)


def restart_owned() -> str:
    """Explicit repair only: gracefully reopen this profile, preserving its site data."""
    from websockets.sync.client import connect
    with _lock:
        endpoint = endpoint_for_profile(PROFILE)
        if endpoint:
            ws = _version(urlparse(endpoint).port)['webSocketDebuggerUrl']
            if endpoint_for_profile(PROFILE) != endpoint:
                raise OSError('专用浏览器连接已变化，请刷新后重试')
            # The UUID checked above scopes Browser.close to our dedicated browser.
            with connect(ws, proxy=None, open_timeout=3, close_timeout=1, max_size=65536) as client:
                client.send(json.dumps({'id': 1, 'method': 'Browser.close'}))
            deadline = time.monotonic() + 8
            while endpoint_for_profile(PROFILE):
                if time.monotonic() >= deadline:
                    raise OSError('专用浏览器尚未关闭，请手动关闭该窗口后重试')
                time.sleep(.2)
        return ensure()


def chrome_path() -> Path:
    candidates = [Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
                  Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe',
                  Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local'))) / 'Google/Chrome/Application/chrome.exe']
    for path in candidates:
        if path.is_file():
            return path
    raise OSError('未找到 Chrome，请安装 Chrome 后再次点击登录 BOSS')


def select_channel(endpoint: str) -> None:
    import channel_state
    settings = {'boss_browser_source': 'existing-browser', 'boss_cdp_url': endpoint}
    changed = False
    conn = get_conn()
    with conn:
        for key, value in settings.items():
            row = query_one('SELECT value FROM app_setting WHERE key=?', (key,))
            changed |= not row or row['value'] != value
            conn.execute('INSERT INTO app_setting(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,value))
    if changed:
        channel_state.bump_config_version()


def ensure() -> str:
    """Open a visible login window; leave it alive after the CLI login process exits."""
    with _lock:
        PROFILE.mkdir(parents=True, exist_ok=True)
        endpoint = endpoint_for_profile(PROFILE)
        if endpoint:
            select_channel(endpoint)
            return endpoint
        port = _choose_port()
        process = subprocess.Popen([str(chrome_path()), '--remote-debugging-address=127.0.0.1',
                          f'--remote-debugging-port={port}', f'--user-data-dir={PROFILE}',
                          '--no-first-run', '--no-default-browser-check', LOGIN_URL],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=sanitized_child_env(),
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise OSError('专用浏览器仍在退出或已被其他窗口占用，请关闭专用窗口后重试')
            try:
                version = _version(port)
                if not _listener_owned(port, process.pid):
                    raise OSError('浏览器端口归属不匹配，未连接任何浏览器')
                owned = {'port': port, 'browser_path': urlparse(version['webSocketDebuggerUrl']).path,
                         'pid': process.pid, 'launch_version': 2}
                pending = PROFILE / (RECEIPT_NAME + '.tmp')
                pending.write_text(json.dumps(owned), encoding='utf-8')
                pending.replace(PROFILE / RECEIPT_NAME)
                endpoint = endpoint_for_profile(PROFILE)
                if endpoint:
                    select_channel(endpoint)
                    return endpoint
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.25)
        raise OSError('专用 BOSS 浏览器未能启动，请关闭该专用窗口后重试登录')


def login_argv(base_argv: list[str], endpoint: str) -> list[str]:
    """把登录接到本项目专用浏览器：**委托**给唯一、已校验的显式入口。

    不再在此处事后拼接 ``--cdp-url`` / ``--cdp``（那会绕开登录守卫）。端点校验
    （回环 + 专用 profile 的 ``DevToolsActivePort``/浏览器 UUID 一致）与注入后复核
    统一由 ``upstream_config.build_login_argv_with_cdp`` 负责。
    """
    from upstream_config import build_login_argv_with_cdp
    return build_login_argv_with_cdp(base_argv, endpoint)


# 环境风控的人工确认入口：**只能**打开 BOSS 官方页面（白名单 host）。
OFFICIAL_URL = 'https://www.zhipin.com/'
_OFFICIAL_HOSTS = frozenset({'www.zhipin.com', 'zhipin.com'})


def _is_official_page(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return (parsed.scheme in ('http', 'https')
            and (parsed.hostname or '').lower() in _OFFICIAL_HOSTS)


def open_official_page(url: str = OFFICIAL_URL, *, timeout: float = 3.0) -> dict:
    """在**本项目专用浏览器**里打开（或聚焦）一个 BOSS 官方页面页签。

    为什么必须是专用浏览器：被风控标记的是**专用 profile 那个会话**；只有在该浏览器里
    打开官方页面，zhipin 页面 JS 才会重新写出 ``__zp_stoken__``，而浏览器通道只认
    浏览器里**活的**该 cookie（见 docs/env-risk-compliance-design.md §A3-A3.1）。
    在日常浏览器里确认，对被标记的会话毫无作用。

    只发 CDP 的**浏览器级**命令（``/json/list`` / ``/json/new``），**绝不**发任何
    search / detail / login 业务请求。专用浏览器未运行（拿不到活端点）时抛
    ``UpstreamError`` 明确告知用户，而不是静默失败。
    """
    from upstream_envelope import FailureLayer, UpstreamError

    if not _is_official_page(url):
        raise UpstreamError(
            'TOOL_NOT_ALLOWED', '只能打开 BOSS 官方页面', False,
            '该动作仅用于在专用浏览器里打开 BOSS 官方页面',
            layer=FailureLayer.CONTRACT,
        )
    endpoint = endpoint_for_profile(PROFILE)
    if not endpoint:
        raise UpstreamError(
            'OWN_BROWSER_NOT_READY',
            '本项目的专用浏览器当前没有运行，无法在其中打开官方页面',
            True,
            '请先在登录页点「登录 BOSS」打开专用浏览器，再重试「在专用浏览器中打开 BOSS 官方页面」',
            layer=FailureLayer.TRANSPORT,
        )
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
        try:
            listing = client.get(endpoint + '/json/list').json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError(
                'CDP_UNAVAILABLE',
                '专用浏览器的调试会话不可用，未能打开官方页面',
                True,
                '请关闭专用浏览器窗口后重新在登录页打开它，再重试',
                layer=FailureLayer.TRANSPORT,
                evidence={'endpoint_host': urlparse(endpoint).hostname or '', 'error': type(exc).__name__},
            ) from exc
        for target in listing if isinstance(listing, list) else []:
            if target.get('type') == 'page' and _is_official_page(str(target.get('url') or '')):
                return {'opened': False, 'already_open': True, 'url': str(target.get('url'))}
        # 没有已打开的官方页签 → 新建一个（Chrome 111+ 要求 PUT；旧版用 GET）
        response = client.put(endpoint + '/json/new', params={'url': url})
        if response.status_code == 405:
            response = client.get(endpoint + '/json/new', params={'url': url})
        response.raise_for_status()
        created = response.json()
    return {'opened': True, 'already_open': False,
            'url': str(created.get('url') or url)}
