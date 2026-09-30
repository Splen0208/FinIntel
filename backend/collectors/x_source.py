# -*- coding: utf-8 -*-
"""X（Twitter）采集源：twitter-cli 优先（任务书 §5）。

- 认证只经环境变量注入（TWITTER_AUTH_TOKEN / TWITTER_CT0），绝不进 argv/日志/快照。
- search 用 --json 输出；时间过滤用 --since/--until；语言过滤用 --lang。
- 缺失互动指标为 None（不填 0）；转发保留关联；英文不自动等于海外作者。

契约加固（指令 §5-C 的 C4）
--------------------------
本机实测 twitter-cli **0.8.5**（``twitter_cli/serialization.py`` 的 ``tweet_to_dict``）：

- JSON 信封：``{"ok": true, "schema_version": "1", "data": [ ...tweets ]}``；
  失败时 ``{"ok": false, "schema_version": "1", "error": {"code", "message"}}``；
  裸列表同样是合法形状（``tweets_to_json`` 直接序列化数组）。
- 单条推文字段是 **camelCase**：``id / text / author{id,name,screenName} /
  metrics{likes,retweets,replies,quotes,views,bookmarks} / createdAt /
  createdAtISO / media / urls / isRetweet / retweetedBy / lang / score /
  articleTitle / articleText / quotedTweet{id,text,author}``。
  旧实现按 ``created_at`` / ``screen_name`` / ``is_retweet`` / ``tweets`` 取值，
  与真实输出**不一致**，会对着合法响应当成契约错误——本文件已按真实形状重写。

- ``tweet`` 命令的语义是「目标推文 **+ 它的回复**」（``fetch_tweet_detail`` 返回
  focal tweet 与 replies 的混合列表），因此**必须按目标 id 定位**，
  绝不能取 ``payload[0]``；找不到目标 → ``DETAIL_REDIRECTED``。
- 长帖：``articleText`` 比 ``text`` 完整时用它；``truncated`` 或正文以省略号结尾 →
  ``detail_status='partial'``，不许把截断正文当完整证据。
- 引用/转推：``quotedTweet.id`` 记为 ``quoted_tweet_id``，``retweetedBy`` 记转发人
  （同样走匿名键）。``is_retweet`` 只作标记：换 author_key 或打标记**不足以证明**
  独立来源已去重，去重口径是 ``(platform, source_id)``。
- 缺失指标一律 ``None``，绝不填 0。
- ``author_key`` 用稳定匿名键 ``anon_<hex16>``（``contracts.stable_anon_key``），
  不再直存明文 screen_name。
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from collectors.contracts import (
    CapabilitySet, CollectorError, ContentRecord, SearchBatch,
    RESULT_SEMANTICS_EMPTY_OK, RESULT_SEMANTICS_EMPTY_UNVERIFIED,
    RESULT_SEMANTICS_HIDDEN, RESULT_SEMANTICS_OK,
    error_envelope_code, int_or_none, looks_like_manifest,
    map_upstream_error_code, normalize_published_at, stable_anon_key,
)
from collectors.runtime import X_READ_COMMANDS, parse_json_or_none, run_command, redact

# 结果列表所在的键（twitter-cli 的真实信封用 data；也兼容自定义包装）
_LIST_KEYS = ("data", "tweets", "results", "items")
_TWEET_ID_RE = re.compile(r"(\d{6,})")


def _normalize_tweet_id(value: Any) -> str:
    """从数字 ID 或完整 URL 中提取推文 ID 的数字部分。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    match = _TWEET_ID_RE.search(text)
    return match.group(1) if match else text


class XSource:
    """X 采集源，封装 twitter-cli 的只读调用。"""

    platform = "x"

    def __init__(self, exe: str):
        self.exe = exe

    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            platform=self.platform,
            search=True,
            detail=True,          # tweet 命令读单条 + 回复
            comments=False,        # X 本轮不做评论线程采集
            replies=False,
            time_filter=True,      # --since/--until
            sort=True,             # --type top/latest
            pagination=False,      # 单次 --max 有限
        )

    # ------------------------------------------------------------------
    # 认证环境变量：只在受控子进程注入
    # ------------------------------------------------------------------
    def _auth_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        token = os.environ.get("TWITTER_AUTH_TOKEN")
        ct0 = os.environ.get("TWITTER_CT0")
        if token:
            env["TWITTER_AUTH_TOKEN"] = token
        if ct0:
            env["TWITTER_CT0"] = ct0
        return env

    def auth_configured(self) -> bool:
        return bool(os.environ.get("TWITTER_AUTH_TOKEN") and os.environ.get("TWITTER_CT0"))

    # ------------------------------------------------------------------
    # 公共：执行 + 业务响应校验
    # ------------------------------------------------------------------
    async def _call(self, subcommand: str, positional: tuple[str, ...],
                    options: dict[str, Any], timeout_seconds: int) -> Any:
        result = await run_command(
            self.exe, subcommand, positional=positional, options=options,
            allowed_commands=X_READ_COMMANDS, timeout_seconds=timeout_seconds,
            env=self._request_env(),
        )
        return self._decode(result, subcommand)

    def _request_env(self) -> dict[str, str]:
        """使用用户已有的网络出口；curl_cffi 不会自动读取 Windows 系统代理。"""
        from urllib.request import getproxies
        env = self._auth_env()
        proxy = os.environ.get("TWITTER_PROXY") or getproxies().get("https")
        if proxy:
            env["TWITTER_PROXY"] = proxy
        return env

    def _decode(self, result: dict[str, Any], subcommand: str) -> Any:
        if result["returncode"] != 0 or result["timed_out"]:
            raise self._error_from(result)
        payload = parse_json_or_none(result["stdout"])
        if payload is None:
            raise CollectorError("CONTRACT_CHANGED",
                                 f"twitter {subcommand} 未返回有效 JSON"
                                 "（returncode=0 也不代表业务成功）",
                                 evidence={"stdout_head": result["stdout"][:200]})
        if looks_like_manifest(payload):
            raise CollectorError(
                "DEPENDENCY_MISSING",
                "twitter-cli 返回的是命令清单而不是业务结果（可执行程序未被正确解析）",
                operator_action="在设置页固定 twitter-cli 的可执行路径后重试",
                evidence={"stdout_head": result["stdout"][:200]})
        upstream_code = error_envelope_code(payload)
        if upstream_code:
            code, message = map_upstream_error_code(upstream_code, "X")
            raise CollectorError(code, message, upstream_code=upstream_code,
                                 evidence={"payload_keys": sorted(payload)[:10]})
        return payload

    def _extract_rows(self, payload: Any, subcommand: str) -> tuple[list[Any], str, str]:
        """取出推文列表；返回 ``(rows, container, semantics)``。

        空列表同样要区分语义：上游明示总数 0 → ``empty_ok``，否则 ``empty_unverified``。
        """
        if isinstance(payload, list):
            rows: list[Any] = payload
            container = "bare_list"
        elif isinstance(payload, dict):
            if "id" in payload and "text" in payload:
                rows, container = [payload], "single_dict"
            else:
                found: Optional[list[Any]] = None
                container = ""
                for key in _LIST_KEYS:
                    if key not in payload:
                        continue
                    value = payload[key]
                    if value is None:
                        raise CollectorError(
                            "CONTRACT_CHANGED",
                            f"twitter {subcommand} 的 {key} 为 null，无法判定是否有内容",
                            evidence={"payload_keys": sorted(payload)[:10]})
                    if isinstance(value, dict) and isinstance(value.get("tweets"), list):
                        found, container = value["tweets"], f"{key}.tweets"
                        break
                    if not isinstance(value, list):
                        raise CollectorError(
                            "CONTRACT_CHANGED",
                            f"twitter {subcommand} 的 {key} 不是列表",
                            evidence={"payload_type": type(value).__name__})
                    found, container = value, key
                    break
                if found is None:
                    raise CollectorError(
                        "CONTRACT_CHANGED",
                        f"twitter {subcommand} 缺少结果列表",
                        evidence={"payload_keys": sorted(payload)[:10]})
                rows = found
        else:
            raise CollectorError("CONTRACT_CHANGED",
                                 f"twitter {subcommand} 返回结构变化",
                                 evidence={"payload_type": type(payload).__name__})

        if not rows:
            if isinstance(payload, dict):
                for key in ("total", "count", "total_count"):
                    value = payload.get(key)
                    if isinstance(value, bool):
                        continue
                    if isinstance(value, (int, float)):
                        semantics = (RESULT_SEMANTICS_EMPTY_OK if int(value) == 0
                                     else RESULT_SEMANTICS_EMPTY_UNVERIFIED)
                        break
                else:
                    semantics = RESULT_SEMANTICS_EMPTY_UNVERIFIED
            else:
                semantics = RESULT_SEMANTICS_EMPTY_UNVERIFIED
        else:
            semantics = RESULT_SEMANTICS_OK
        return (rows, container, semantics)

    # ------------------------------------------------------------------
    async def search(self, query: str, *, max_count: int = 10,
                     since: Optional[str] = None, until: Optional[str] = None,
                     lang: Optional[str] = None,
                     timeout_seconds: int = 60) -> SearchBatch:
        if not self.auth_configured():
            raise CollectorError(
                "AUTH_REQUIRED",
                "X 未配置登录凭据（TWITTER_AUTH_TOKEN / TWITTER_CT0）",
                operator_action="在设置页配置 X 凭据后重试",
            )
        options: dict[str, Any] = {"--json": True, "--max": max_count}
        if since:
            options["--since"] = since
        if until:
            options["--until"] = until
        if lang:
            options["--lang"] = lang
        payload = await self._call("search", (query,), options, timeout_seconds)
        rows, _container, semantics = self._extract_rows(payload, "search")
        warnings: list[str] = []
        records: list[ContentRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                warnings.append("搜索结果含非对象行，已跳过")
                continue
            mapped = self._map_tweet(row)
            if mapped is None:
                warnings.append("搜索结果有行缺少推文 ID，已跳过")
                continue
            records.append(mapped)
        if not rows and semantics == RESULT_SEMANTICS_EMPTY_UNVERIFIED:
            warnings.append(
                "搜索返回 0 条，但无法区分「确实没有结果」与「被平台限制或未返回」")
        return SearchBatch(records=records, completeness="full", warnings=warnings,
                           coverage={"retrieval_mode": "keyword_search",
                                     "time_filter_applied": bool(since or until),
                                     "result_semantics": semantics})

    def _map_tweet(self, t: dict[str, Any]) -> Optional[ContentRecord]:
        """把一条真实 twitter-cli 推文映射为 ContentRecord；缺 ID 返回 None。"""
        tid_raw = t.get("id") or t.get("id_str")
        tid = _normalize_tweet_id(tid_raw)
        if not tid:
            return None

        author = t.get("author") if isinstance(t.get("author"), dict) else {}
        raw_author = (author.get("screenName") or author.get("screen_name")
                      or author.get("id") or author.get("name"))
        author_key = stable_anon_key(self.platform, raw_author)

        metrics_raw = t.get("metrics") if isinstance(t.get("metrics"), dict) else None
        metrics_present = metrics_raw is not None
        metrics_src: dict[str, Any] = metrics_raw or {}

        # 长帖：articleText 比 text 完整时用它（twitter-cli 对长帖会另给 articleText）
        text = str(t.get("text") or "")
        article_text = t.get("articleText") or t.get("article_text")
        if article_text and len(str(article_text)) > len(text):
            text = str(article_text)
        truncated = bool(t.get("truncated")) or text.rstrip().endswith(("…", "..."))

        detail_status = "complete" if text else "missing"
        if text and truncated:
            detail_status = "partial"

        is_retweet = bool(t.get("isRetweet") or t.get("is_retweet"))
        retweeted_by = t.get("retweetedBy") or t.get("retweeted_by")
        quoted = t.get("quotedTweet") or t.get("quoted_tweet")
        quoted_id = None
        if isinstance(quoted, dict):
            quoted_id = _normalize_tweet_id(quoted.get("id")) or None

        published_at, published_raw, published_quality = normalize_published_at(
            t.get("createdAtISO") or t.get("created_at_iso") or t.get("createdAt")
            or t.get("created_at"))

        lang = t.get("lang")
        lang = str(lang) if lang else None

        return ContentRecord(
            platform=self.platform,
            source_id=tid,
            canonical_url=f"https://x.com/i/status/{tid}",
            title=t.get("articleTitle") or None,
            evidence_text=text,
            language=lang,
            published_at=published_at,
            fetched_at="",  # 由 social_pipeline 回填 utcnow
            detail_status=detail_status,
            author_key=author_key,
            metrics={
                # 缺字段一律 None，绝不填 0；None = "未知"，0 = "确实是 0"
                "likes": int_or_none(metrics_src.get("likes")),
                "retweets": int_or_none(metrics_src.get("retweets")),
                "replies": int_or_none(metrics_src.get("replies")),
                "quotes": int_or_none(metrics_src.get("quotes")),
                "views": int_or_none(metrics_src.get("views")),
                "bookmarks": int_or_none(metrics_src.get("bookmarks")),
                "metrics_present": metrics_present,
                "truncated": truncated,
                "is_retweet": is_retweet,
                # 转发人同样匿名化：不号称匿名却存明文 screen_name
                "retweeted_by": stable_anon_key(self.platform, retweeted_by),
                "quoted_tweet_id": quoted_id,
            },
            published_at_raw=published_raw,
            published_at_quality=published_quality,
            # search/detail 的 URL 由推文 ID 构造，且 detail 会核对 ID → 视为已验证
            source_url_verified=False,
        )

    # ------------------------------------------------------------------
    async def detail(self, tweet_id: str, timeout_seconds: int = 60) -> ContentRecord:
        """读单条推文（tweet 命令）。

        C4：``tweet`` 返回的是「目标推文 + 回复」的混合列表，**必须按 ID 定位目标**，
        不能取第一条；找不到目标 → ``DETAIL_REDIRECTED``。
        """
        if not self.auth_configured():
            raise CollectorError("AUTH_REQUIRED", "X 未配置登录凭据",
                                 operator_action="在设置页配置 X 凭据后重试")
        target = _normalize_tweet_id(tweet_id)
        if not target:
            raise CollectorError("LOCATOR_MISSING", "缺少推文 ID，无法读取正文",
                                 operator_action="从搜索结果重新定位该推文后重试")
        payload = await self._call("tweet", (tweet_id,),
                                   {"--json": True, "--full-text": True},
                                   timeout_seconds)
        rows, _container, semantics = self._extract_rows(payload, "tweet")
        if not rows:
            raise CollectorError(
                "DETAIL_REDIRECTED" if semantics == RESULT_SEMANTICS_EMPTY_OK
                else "CONTRACT_CHANGED",
                "twitter tweet 没有返回任何推文，无法定位目标帖子"
                if semantics == RESULT_SEMANTICS_EMPTY_OK
                else "twitter tweet 返回空列表，无法确认目标帖子（不是「正文为空」）",
                evidence={"requested_id": target,
                          "result_semantics": semantics})

        hit: Optional[dict[str, Any]] = None
        returned_ids: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = _normalize_tweet_id(row.get("id") or row.get("id_str"))
            if rid:
                returned_ids.append(rid)
            if rid == target:
                hit = row
                break
        if hit is None:
            raise CollectorError(
                "DETAIL_REDIRECTED",
                "twitter tweet 返回的结果里没有目标帖子（不得取第一条当目标）",
                evidence={"requested_id": target, "returned_ids": returned_ids[:5],
                          "result_semantics": RESULT_SEMANTICS_HIDDEN})

        record = self._map_tweet(hit)
        if record is None or _normalize_tweet_id(record.source_id) != target:
            raise CollectorError(
                "DETAIL_REDIRECTED",
                "twitter tweet 定位到的推文 ID 与请求目标不一致",
                evidence={"requested_id": target,
                          "resolved_id": None if record is None else record.source_id})
        record.source_url_verified = True
        return record

    # ------------------------------------------------------------------
    def _error_from(self, result: dict[str, Any]) -> CollectorError:
        """把子进程失败映射为稳定错误码。"""
        stderr = result.get("stderr", "")
        stdout = result.get("stdout", "")
        combined = (stdout + "\n" + stderr)
        payload = parse_json_or_none(stdout)
        if isinstance(payload, dict) and payload.get("ok") is False:
            error = payload.get("error") or {}
            code, _ = map_upstream_error_code(error.get("code"), "X")
            message = redact(str(error.get("message") or "X 上游调用失败"))[:600]
            if error.get("code") == "not_found" and "HTTP 404" in message:
                code = "CONTRACT_CHANGED"
            if code:
                return CollectorError(code, message, retryable=code in ("TIMEOUT", "CONNECTION_UNAVAILABLE"),
                                      operator_action="查看来源诊断后处理登录、网络或接口问题",
                                      evidence={"upstream_code": str(error.get("code") or "")})
        if result.get("timed_out"):
            return CollectorError("TIMEOUT", "X 采集超时", retryable=True,
                                  operator_action="稍后重试或减小采集数量")
        if "not_authenticated" in combined or "No Twitter cookies" in combined or \
                "auth" in combined.lower() and "fail" in combined.lower():
            return CollectorError("AUTH_REQUIRED", "X 登录态失效",
                                  operator_action="重新配置 X 凭据后重试")
        if "rate" in combined.lower() or "429" in combined:
            return CollectorError("RATE_LIMITED", "X 接口限流", retryable=True,
                                  operator_action="稍后重试")
        if result.get("returncode") == -1:
            return CollectorError("DEPENDENCY_MISSING", "twitter-cli 不可用",
                                  operator_action="检查 twitter-cli 安装")
        return CollectorError("CONNECTION_UNAVAILABLE", "X 采集失败，请查看连接诊断",
                              retryable=True, operator_action="检查网络后重试",
                              evidence={"stderr_head": redact(stderr)[:600],
                                        "stdout_head": redact(stdout)[:600]})


class XBrowserSource(XSource):
    """复用用户已登录 Chrome 的 OpenCLI 关键词搜索通道。"""

    def auth_configured(self) -> bool:
        # 浏览器工具自行检查真实登录态，不传入另一个通道的 Cookie。
        return True

    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(platform="x", search=True, detail=False,
                             comments=False, replies=False, time_filter=True,
                             sort=True, pagination=False)

    async def _call(self, subcommand, positional, options, timeout_seconds):
        if subcommand != "search":
            raise CollectorError("UNSUPPORTED_CAPABILITY", "浏览器通道当前只支持关键词搜索")
        query = positional[0]
        for flag, operator in (("--since", "since"), ("--until", "until"), ("--lang", "lang")):
            if options.get(flag):
                query += f" {operator}:{options[flag]}"
        result = await run_command(self.exe, "search", positional=(query,),
            options={"--format":"json", "--limit":options.get("--max", 10), "--product":"live"},
            allowed_commands=frozenset({"search"}), platform="twitter",
            timeout_seconds=timeout_seconds)
        if result.get("returncode") != 0 or result.get("timed_out"):
            from collectors.xhs_source import XhsSource
            exc = XhsSource(self.exe)._error_from(result)
            raise CollectorError(exc.code, exc.message.replace("小红书", "X"),
                retryable=exc.retryable, operator_action=exc.operator_action.replace("小红书", "X"))
        payload = self._decode(result, subcommand)
        rows, _, _ = self._extract_rows(payload, subcommand)
        mapped = []
        for row in rows:
            if not isinstance(row, dict):
                raise CollectorError("CONTRACT_CHANGED", "X 浏览器搜索返回了无效内容行")
            item = dict(row)
            if isinstance(item.get("author"), str):
                item["author"] = {"screenName":item["author"]}
            item["metrics"] = {key:row[key] for key in
                ("likes", "retweets", "replies", "quotes", "views", "bookmarks") if key in row}
            mapped.append(item)
        return mapped


__all__ = ["XSource", "XBrowserSource"]
