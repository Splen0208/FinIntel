"""finintel ai_provider：真实模型调用适配器（《简历解析实施指令》§4.1）。

- 读取 app_setting 的 llm_provider / llm_api_base / llm_api_key / llm_model / llm_timeout_seconds。
- 首版只接一个 OpenAI 兼容 chat/completions 端点（覆盖绝大多数托管 provider）。
- 未配置凭据/模型 → AI_NOT_CONFIGURED；超时 → AI_TIMEOUT；非法 JSON → AI_OUTPUT_INVALID。
- 绝不回退 mock：正常 API 失败必须返回错误，只有显式 demo 入口才允许规则匹配器。
- 密钥不进入响应、日志、报告；不打印值。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from typing import Any, Optional

import store
from store import query_one

DEFAULT_TIMEOUT_SECONDS = 120


class AIError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False,
                 operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.operator_action = operator_action


def _setting(key: str) -> Optional[str]:
    row = query_one("SELECT value FROM app_setting WHERE key=?", (key,))
    if not row:
        return None
    v = row["value"]
    return v if v else None


def ai_config() -> dict:
    """返回模型配置状态（不含密钥值）。"""
    api_key = _setting("llm_api_key")
    model = _setting("llm_model")
    base = _setting("llm_api_base")
    provider = _setting("llm_provider") or "openai_compat"
    configured = bool(api_key and model and base)
    return {
        "provider": provider,
        "model": model,
        "base_url": base,
        "configured": configured,
        "has_api_key": bool(api_key),
        "demo_mode": (_setting("demo_mode") or "0") == "1",
    }


def _require_config() -> tuple[str, str, str]:
    cfg = ai_config()
    if not cfg["configured"]:
        raise AIError(
            "AI_NOT_CONFIGURED",
            "AI 服务未配置（缺少 llm_api_key / llm_model / llm_api_base 之一）",
            False,
            "在设置页配置模型后重试；文件解析结果已保留，不会回退为模拟结果",
        )
    return cfg["base_url"], cfg["model"], _setting("llm_api_key")


def _timeout_seconds() -> int:
    try:
        return int(_setting("llm_timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def chat_json(system_prompt: str, user_content: str, *,
              temperature: float = 0.0, max_tokens: Optional[int] = None,
              json_schema_note: str = "只返回一个合法 JSON 对象，不要输出任何解释、markdown 代码块或额外文字") -> Any:
    """调用模型并要求返回 JSON；返回解析后的对象（dict/list）。

    失败按结构化错误抛出 AIError（AI_NOT_CONFIGURED/AI_TIMEOUT/AI_OUTPUT_INVALID/UPSTREAM_*）。
    """
    base_url, model, api_key = _require_config()
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content + "\n\n" + json_schema_note},
        ],
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    # DeepSeek's documented JSON mode; leave other compatible providers unchanged.
    if urlsplit(base_url).hostname == 'api.deepseek.com':
        payload['response_format'] = {'type': 'json_object'}
        payload.setdefault('max_tokens', 8192)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    started = _now_ms()
    try:
        with urllib.request.urlopen(req, timeout=_timeout_seconds()) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = _safe_read(e)
        if e.code in (401, 403):
            raise AIError("AUTH_REQUIRED", f"模型鉴权失败（HTTP {e.code}）", False,
                          "检查 API Key 是否有效")
        if e.code == 429:
            raise AIError("RATE_LIMITED", "模型服务限流（HTTP 429）", True, "稍后重试或降低调用频率")
        raise AIError("UPSTREAM_TIMEOUT" if e.code == 504 else "UPSTREAM_CONTRACT_CHANGED",
                      f"模型服务返回 HTTP {e.code}：{detail[:200]}", e.code >= 500,
                      "检查模型服务状态或 base_url")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AIError("AI_TIMEOUT", f"模型调用超时或网络错误：{type(e).__name__}", True,
                      "检查网络与 base_url，稍后重试")
    elapsed = _now_ms() - started
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise AIError("AI_OUTPUT_INVALID", "模型返回非 JSON，无法解析", True, "重试或检查模型")
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    if choice.get('finish_reason') == 'length':
        raise AIError('AI_OUTPUT_INVALID', '模型输出达到长度上限，JSON 可能不完整', True,
                      '减少本次材料或提高模型输出上限后重试')
    try:
        # 容忍模型把 JSON 包在 markdown 代码块里
        content = _strip_code_fence(content)
        parsed = json.loads(content)
    except json.JSONDecodeError:
        raise AIError("AI_OUTPUT_INVALID", "模型返回内容不是合法 JSON", True, "重试或调整模型")
    if isinstance(parsed, dict):
        parsed.setdefault("_meta", {}).update({"provider": "openai_compat", "model": model,
                                               "elapsed_ms": elapsed})
    return parsed


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _safe_read(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)


def demo_enabled() -> bool:
    """显式演示模式开关（正常入口默认关闭）。"""
    return (_setting("demo_mode") or "0") == "1"
