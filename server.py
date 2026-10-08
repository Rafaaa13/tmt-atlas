#!/usr/bin/env python3
"""TMT Atlas local-first research workbench.

Zero-dependency local server for the portfolio MVP. The demo intentionally works
without an LLM, database server, Docker, or network access.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import os
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import finance_engine
from connectors import Collector
from workspace_db import MAX_SOURCE_BYTES, WorkspaceDB

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
FRONTEND_DIR = ROOT / "frontend"
SEED_PATH = DATA_DIR / "demo_seed.json"
SOURCES_PATH = DATA_DIR / "source_registry.json"
SETTINGS_PATH = DATA_DIR / "local_settings.json"
LOCAL_SOURCES_PATH = DATA_DIR / "local_sources.json"
VERIFIED_FACTS_PATH = DATA_DIR / "verified_facts.json"
MAX_JSON_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BYTES = ((MAX_SOURCE_BYTES + 2) // 3) * 4 + 16 * 1024
VERIFIED_FACT_STATUSES = frozenset({"verified", "manual_primary_source"})

# These are application-owned state directories.  Request payloads never choose
# a filesystem destination; callers may only override them through deployment
# environment variables.
STATE_DIR = Path(os.environ.get("TMT_STATE_DIR", str(DATA_DIR / "workspace_state"))).expanduser()
COLLECTOR_STATE_DIR = Path(
    os.environ.get("TMT_COLLECTOR_STATE_DIR", str(DATA_DIR / "collector_state"))
).expanduser()
WORKSPACE_DB = WorkspaceDB(STATE_DIR)
COLLECTOR = Collector(COLLECTOR_STATE_DIR)


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def decode_upload_base64(value: Any) -> bytes:
    """Decode an upload using strict standard Base64 validation."""
    if not isinstance(value, str):
        raise ValueError("invalid_upload")
    try:
        encoded = value.encode("ascii")
        if len(encoded) > ((MAX_SOURCE_BYTES + 2) // 3) * 4:
            raise ValueError("invalid_upload")
        return base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError, TypeError):
        raise ValueError("invalid_upload")


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_verified_facts() -> list[dict[str, Any]]:
    """Load the curated corpus without mixing it into the demo store.

    The checked-in starter corpus predates an explicit status field; because the
    file itself is a manually curated primary-source corpus, that legacy shape is
    normalized to ``manual_primary_source``. Any explicit status still has to be
    one of the two verified states.
    """
    raw = load_json(VERIFIED_FACTS_PATH, [])
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        status = item.get("status", "manual_primary_source")
        if not isinstance(status, str) or status not in VERIFIED_FACT_STATUSES:
            continue
        fact = dict(item)
        fact["status"] = status
        source: dict[str, Any] = {}
        if "source_url" in item:
            source.update(url=item["source_url"], source_url=item["source_url"])
        if "source_title" in item:
            source.update(title=item["source_title"], source_title=item["source_title"])
        for key in ("published_at", "locator"):
            if key in item:
                source[key] = item[key]
        fact["source"] = source
        result.append(fact)
    return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def number(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def display_metric(metric: dict[str, Any]) -> str:
    value = metric.get("value")
    unit = metric.get("unit", "")
    if unit == "%":
        return pct(value)
    if unit == "USD bn":
        return f"${number(value)}bn"
    return f"{number(value)} {unit}".strip()


def display_delta(value: Any, unit: str = "%") -> str:
    if value is None:
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if unit == "%":
        return f"{n * 100:+.1f}pp"
    return f"{n:+.1f}"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class AtlasStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.seed = load_json(SEED_PATH, {"dataset": {}, "companies": [], "events": [], "research_prompts": []})
        self.source_registry = load_json(SOURCES_PATH, {"sources": []})
        self.settings = load_json(SETTINGS_PATH, {
            "provider": "none",
            "base_url": "",
            "model": "",
            "allow_external_llm": False,
            "allow_scheduled_collection": False,
            "last_updated": None,
        })
        self.local_sources = load_json(LOCAL_SOURCES_PATH, [])

    @property
    def dataset(self) -> dict[str, Any]:
        return self.seed.get("dataset", {})

    @property
    def companies(self) -> list[dict[str, Any]]:
        return self.seed.get("companies", [])

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.seed.get("events", [])

    @property
    def all_sources(self) -> list[dict[str, Any]]:
        return self.source_registry.get("sources", []) + self.local_sources

    def company(self, company_id: str) -> dict[str, Any] | None:
        return next((c for c in self.companies if c.get("id") == company_id), None)

    def source(self, source_id: str) -> dict[str, Any] | None:
        return next((s for s in self.all_sources if s.get("id") == source_id), None)

    def event(self, event_id: str) -> dict[str, Any] | None:
        return next((e for e in self.events if e.get("id") == event_id), None)

    def sectors(self) -> list[str]:
        return sorted({c.get("sector", "") for c in self.companies if c.get("sector")})

    def metric(self, company: dict[str, Any], key: str) -> dict[str, Any] | None:
        return next((m for m in company.get("metrics", []) if m.get("key") == key), None)

    def with_provenance(self, metric: dict[str, Any]) -> dict[str, Any]:
        source = self.source(metric.get("source_id", "")) or {}
        return {
            **metric,
            "display_value": display_metric(metric),
            "display_delta": display_delta(metric.get("trend"), metric.get("unit")),
            "source_title": source.get("title", "未配置来源"),
            "source_url": source.get("url", ""),
            "quality": "演示归一化值" if metric.get("status") == "demo" else "待审核",
            "trace": {
                "status": metric.get("status", "unknown"),
                "source_id": metric.get("source_id"),
                "evidence_locator": "演示数据；导入官方文件后替换为页码/段落",
            },
        }

    def snapshot(self, company_id: str) -> dict[str, Any] | None:
        company = self.company(company_id)
        if not company:
            return None
        metrics = [self.with_provenance(m) for m in company.get("metrics", [])]
        events = [self.event_view(e) for e in self.events if e.get("company_id") == company_id]
        return {
            "company": {
                k: company.get(k)
                for k in ["id", "name", "name_zh", "ticker", "country", "listing", "sector", "description"]
            },
            "metrics": metrics,
            "events": sorted(events, key=lambda x: x.get("date", ""), reverse=True),
            "data_notice": self.dataset.get("notice"),
            "as_of": self.dataset.get("as_of"),
            "source_count": len(company.get("source_ids", [])),
        }

    def event_view(self, event: dict[str, Any]) -> dict[str, Any]:
        company = self.company(event.get("company_id", "")) or {}
        source = self.source(event.get("source_id", "")) or {}
        return {
            **event,
            "company_name": company.get("name", "未知公司"),
            "company_name_zh": company.get("name_zh", ""),
            "source_title": source.get("title", "未配置来源"),
            "source_url": source.get("url", ""),
            "status_label": "演示事件" if event.get("demo_only") else "官方来源入口",
        }

    def sector_scan(self, sector: str) -> dict[str, Any]:
        selected = [c for c in self.companies if c.get("sector") == sector]
        keys = ["revenue", "revenue_growth", "gross_margin", "operating_margin", "rd_ratio", "capex"]
        rows = []
        for company in selected:
            row: dict[str, Any] = {
                "company_id": company["id"],
                "company": company["name"],
                "company_zh": company["name_zh"],
                "country": company["country"],
                "ticker": company["ticker"],
                "sector": company["sector"],
            }
            for key in keys:
                metric = self.metric(company, key)
                row[key] = self.with_provenance(metric) if metric else {
                    "key": key,
                    "label": key,
                    "value": None,
                    "display_value": "未披露",
                    "display_delta": "—",
                    "quality": "缺失/未披露",
                }
            row["comparability"] = "演示归一化；正式排名前需确认会计口径"
            rows.append(row)
        ranked = sorted(rows, key=lambda r: (r.get("revenue_growth", {}).get("value") is not None, r.get("revenue_growth", {}).get("value") or -999), reverse=True)
        for idx, row in enumerate(ranked, 1):
            row["growth_rank"] = idx
        return {
            "sector": sector,
            "rows": ranked,
            "columns": keys,
            "method": "按演示数据排序；可比性不是默认成立的，不同财年、会计准则和分部口径需要人工审核后再用于正式比较。",
            "as_of": self.dataset.get("as_of"),
        }

    def event_analysis(self, event_id: str) -> dict[str, Any] | None:
        event = self.event(event_id)
        if not event:
            return None
        company = self.company(event.get("company_id", "")) or {}
        snapshot = self.snapshot(company.get("id", "")) or {}
        metrics = {m.get("key"): m for m in snapshot.get("metrics", [])}
        direct = [
            f"事件主体：{company.get('name_zh')}（{company.get('name')}）",
            f"事件日期：{event.get('date')}",
            f"事件类型：{event.get('type')}",
            f"来源状态：{'演示事件，需替换为官方段落证据' if event.get('demo_only') else '已关联官方来源入口'}",
        ]
        transmission = []
        if metrics.get("revenue_growth"):
            transmission.append(f"收入增速当前为 {metrics['revenue_growth']['display_value']}；需要进一步确认事件是否改变收入确认、需求或客户预算。")
        if metrics.get("capex"):
            transmission.append(f"资本开支当前为 {metrics['capex']['display_value']}；需要验证事件是否影响产能、算力投入或现金流压力。")
        if metrics.get("operating_margin"):
            transmission.append(f"营业利润率当前为 {metrics['operating_margin']['display_value']}；不能仅凭事件标题推断利润率方向。")
        return {
            "event": self.event_view(event),
            "direct_facts": direct,
            "transmission_paths": transmission,
            "inferences": [
                "事件影响需要通过业务、指标和时间窗口传导；当前输出是研究框架，不是投资建议。",
                "如果来源只提供相对增长率或管理层表述，不能反推绝对金额。",
            ],
            "questions": [
                "是否有官方文件披露了受影响业务的绝对规模？",
                "事件发生前后的报告期是否可以直接比较？",
                "是否存在反向证据、管理层澄清或不同口径的披露？",
                "哪些结论需要用户先确认后才能进入研究备忘录？",
            ],
            "evidence_coverage": 0 if event.get("demo_only") else None,
            "evidence_coverage_status": "未建立页码/段落证据覆盖率；演示事件需核验" if event.get("demo_only") else "仅有关联来源入口，尚未完成段落级覆盖率核验",        }

    def search(self, query: str) -> dict[str, Any]:
        q = query.strip().lower()
        if not q:
            return {"companies": [], "events": [], "sources": []}
        companies = []
        for c in self.companies:
            haystack = " ".join(str(c.get(k, "")) for k in ["name", "name_zh", "ticker", "sector", "description"]).lower()
            if q in haystack:
                companies.append({"id": c["id"], "name": c["name"], "name_zh": c["name_zh"], "sector": c["sector"], "type": "company"})
        events = []
        for e in self.events:
            company = self.company(e.get("company_id", "")) or {}
            haystack = " ".join(str(e.get(k, "")) for k in ["title", "summary", "type", "date"]).lower()
            haystack += " " + " ".join(str(company.get(k, "")) for k in ["name", "name_zh", "ticker", "sector"]).lower()
            if q in haystack:
                events.append({**self.event_view(e), "type": "event"})
        sources = []
        for s in self.all_sources:
            source_company = self.company(s.get("company_id", "")) or {}
            haystack = " ".join(str(s.get(k, "")) for k in ["title", "company", "sector", "type", "notes"]).lower()
            haystack += " " + " ".join(str(source_company.get(k, "")) for k in ["name", "name_zh", "ticker", "sector"]).lower()
            if q in haystack:
                sources.append({**s, "type": "source"})
        return {"companies": companies[:12], "events": events[:12], "sources": sources[:12]}

    def add_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip()
        url = str(payload.get("url", "")).strip()
        if not title or not url:
            raise ValueError("title 和 url 为必填项")
        source_id = "local-" + hashlib.sha1((title + url).encode("utf-8")).hexdigest()[:10]
        item = {
            "id": source_id,
            "company_id": payload.get("company_id", "local"),
            "company": payload.get("company", "本地来源"),
            "sector": payload.get("sector", "未分类"),
            "type": payload.get("type", "用户授权来源"),
            "title": title,
            "url": url,
            "access": "user_added",
            "notes": payload.get("notes", "由用户在本地工作区添加；正式使用前请确认权限与来源。"),
            "added_at": utc_now(),
        }
        with self.lock:
            self.local_sources = [s for s in self.local_sources if s.get("id") != source_id]
            self.local_sources.append(item)
            save_json(LOCAL_SOURCES_PATH, self.local_sources)
        return item

    def memo(self, company_id: str | None, sector: str | None, event_id: str | None, notes: str = "") -> dict[str, Any]:
        snapshot = self.snapshot(company_id) if company_id else None
        scan = self.sector_scan(sector) if sector else None
        analysis = self.event_analysis(event_id) if event_id else None
        title_parts = []
        if snapshot:
            title_parts.append(snapshot["company"]["name_zh"] + "公司快照")
        if scan:
            title_parts.append(scan["sector"] + "赛道扫描")
        if analysis:
            title_parts.append("事件影响分析")
        title = "｜".join(title_parts) or "TMT Atlas 研究备忘录"
        lines = [
            f"# {title}",
            "",
            f"> 数据截至：{self.dataset.get('as_of', '未标注')} · 状态：演示工作区，导出前需复核引用",
            "",
            "## 研究结论",
            notes.strip() or "当前备忘录由已选工作区对象生成。请在正式使用前将演示归一化值替换为已定位到页码/段落的官方事实。",
            "",
            "## 关键事实与计算",
        ]
        evidence_count = 0
        if snapshot:
            lines.append(f"### {snapshot['company']['name_zh']}（{snapshot['company']['name']}）")
            for metric in snapshot["metrics"][:7]:
                lines.append(f"- {metric['label']}：{metric['display_value']}（{metric['period']}，{metric['quality']}；来源：{metric['source_title']}）")
                evidence_count += 1
        if scan:
            lines.append(f"### {scan['sector']}比较")
            for row in scan["rows"]:
                growth = row.get("revenue_growth", {}).get("display_value", "未披露")
                margin = row.get("operating_margin", {}).get("display_value", "未披露")
                lines.append(f"- {row['company_zh']}：收入增速 {growth}；营业利润率 {margin}；当前为演示归一化比较。")
                evidence_count += 1
        if analysis:
            lines.extend(["### 事件分析", f"- {analysis['event']['title']}", "- 直接事实：" + "；".join(analysis["direct_facts"]), "- 待验证问题：" + "；".join(analysis["questions"])])
            evidence_count += 1
        lines.extend([
            "",
            "## 研究边界",
            "本材料用于展示证据组织、口径管理和研究工作流，不构成投资建议；任何正式结论都应回到官方文件原文核验。",
            "",
            "## 来源",
        ])
        source_ids: list[str] = []
        if snapshot:
            source_ids.extend(snapshot["company"].get("source_ids", []))
        if analysis:
            source_ids.append(analysis["event"].get("source_id", ""))
        seen: set[str] = set()
        for source_id in source_ids:
            if source_id in seen:
                continue
            seen.add(source_id)
            source = self.source(source_id)
            if source:
                lines.append(f"- [{source['title']}]({source['url']})")
        return {
            "title": title,
            "markdown": "\n".join(lines) + "\n",
            "html": render_markdownish_html(title, lines),
            "evidence_count": evidence_count,
            "coverage": None,
            "coverage_status": "备忘录当前只保留来源入口和演示状态；段落级引用覆盖率需在事实审核后计算。",
            "generated_at": utc_now(),
        }


def render_markdownish_html(title: str, lines: list[str]) -> str:
    body: list[str] = []
    for line in lines:
        escaped = html.escape(line)
        if line.startswith("# "):
            body.append(f"<h1>{escaped[2:]}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{escaped[3:]}</h2>")
        elif line.startswith("### "):
            body.append(f"<h3>{escaped[4:]}</h3>")
        elif line.startswith("- "):
            body.append(f"<li>{escaped[2:]}</li>")
        elif line.startswith("> "):
            body.append(f"<blockquote>{escaped[2:]}</blockquote>")
        elif line.strip():
            body.append(f"<p>{escaped}</p>")
    return "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>{}</title><style>body{{font-family:system-ui;max-width:860px;margin:40px auto;padding:0 24px;line-height:1.7;color:#17202a}}h1{{color:#0f3d3e}}blockquote{{background:#eef7f5;padding:12px 16px;border-left:4px solid #35a69a}}li{{margin:6px 0}}</style><body>{}</body></html>".format(html.escape(title), "".join(body))


STORE = AtlasStore()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the terminal readable while preserving request visibility.
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} {format % args}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_JSON_RESPONSE_BYTES:
            raw = json.dumps({"error": "response_too_large"}, ensure_ascii=False).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            raise ValueError("invalid_request")
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request_too_large")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload_must_be_object")
        return payload

    def route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        return parsed.path.rstrip("/") or "/", parse_qs(parsed.query)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path, query = self.route()
        try:
            if path == "/api/health":
                self.send_json({"ok": True, "service": "tmt-atlas", "mode": "local-first", "time": utc_now()})
            elif path == "/api/config":
                self.send_json({
                    "product": "TMT Atlas",
                    "dataset": STORE.dataset,
                    "sectors": STORE.sectors(),
                    "companies": [{k: c.get(k) for k in ["id", "name", "name_zh", "ticker", "country", "listing", "sector"]} for c in STORE.companies],
                    "prompts": STORE.seed.get("research_prompts", []),
                    "llm": {k: v for k, v in STORE.settings.items() if k != "api_key"},
                })
            elif path == "/api/search":
                self.send_json(STORE.search((query.get("q") or [""])[0]))
            elif path == "/api/companies":
                self.send_json({"companies": [{k: c.get(k) for k in ["id", "name", "name_zh", "ticker", "country", "listing", "sector", "description"]} for c in STORE.companies]})
            elif path.startswith("/api/companies/") and path.endswith("/snapshot"):
                company_id = path.split("/")[3]
                result = STORE.snapshot(company_id)
                self.send_json(result or {"error": "company_not_found"}, 200 if result else 404)
            elif path == "/api/sectors":
                self.send_json({"sectors": STORE.sectors()})
            elif path.startswith("/api/sectors/"):
                sector = unquote(path.split("/", 3)[3])
                result = STORE.sector_scan(sector)
                self.send_json(result)
            elif path == "/api/events":
                events = [STORE.event_view(e) for e in STORE.events]
                self.send_json({"events": sorted(events, key=lambda x: x.get("date", ""), reverse=True)})
            elif path.startswith("/api/events/"):
                event_id = path.split("/")[3]
                result = STORE.event_analysis(event_id)
                self.send_json(result or {"error": "event_not_found"}, 200 if result else 404)
            elif path == "/api/sources":
                self.send_json({"sources": STORE.all_sources})
            elif path == "/api/workspace":
                self.send_json({
                    "sources": WORKSPACE_DB.sources(),
                    "evidence": WORKSPACE_DB.evidence(),
                    "facts": WORKSPACE_DB.facts(),
                    "audit": WORKSPACE_DB.audit(),
                    "stats": WORKSPACE_DB.stats(),
                })
            elif path == "/api/evidence":
                self.send_json({
                    "evidence": WORKSPACE_DB.evidence(
                        (query.get("q") or [""])[0],
                        (query.get("source_id") or [""])[0],
                    )
                })
            elif path == "/api/facts":
                self.send_json({"facts": WORKSPACE_DB.facts((query.get("company_id") or [""])[0])})
            elif path == "/api/audit":
                self.send_json({"audit": WORKSPACE_DB.audit()})
            elif path == "/api/stats":
                self.send_json(WORKSPACE_DB.stats())
            elif path == "/api/verified-facts":
                self.send_json({"facts": load_verified_facts()})
            elif path == "/api/collection/status":
                self.send_json(COLLECTOR.config())
            elif path == "/api/collection/inbox":
                status = (query.get("status") or [None])[0]
                self.send_json({"items": COLLECTOR.inbox(status)})
            elif path.startswith("/api/memos/"):
                self.send_json({"error": "memos are generated with POST /api/memos"}, 405)
            else:
                self.serve_static(path)
        except (ValueError, KeyError):
            self.send_json({"error": "validation_error"}, 400)
        except Exception:
            self.send_json({"error": "server_error"}, 500)

    def do_POST(self) -> None:
        path, _query = self.route()
        try:
            payload = self.read_json()
            if path == "/api/sources":
                item = STORE.add_source(payload)
                self.send_json({"source": item}, 201)
            elif path == "/api/ingest":
                self.handle_ingest(payload)
            elif path == "/api/facts":
                self.send_json({"fact": WORKSPACE_DB.add_fact(payload)}, 201)
            elif path == "/api/facts/review":
                self.send_json({
                    "fact": WORKSPACE_DB.review_fact(
                        payload.get("id"), payload.get("status"), payload.get("reason")
                    )
                })
            elif path == "/api/calculate":
                self.send_json(finance_engine.calculate(payload))
            elif path == "/api/memos":
                result = STORE.memo(payload.get("company_id"), payload.get("sector"), payload.get("event_id"), payload.get("notes", ""))
                self.send_json(result)
            elif path == "/api/settings/llm":
                # Never persist an API key. The runtime key comes from TMT_LLM_API_KEY.
                allowed_keys = {"provider", "base_url", "model", "allow_external_llm", "allow_scheduled_collection"}
                new_settings = {k: payload.get(k) for k in allowed_keys if k in payload}
                new_settings["last_updated"] = utc_now()
                with STORE.lock:
                    STORE.settings.update(new_settings)
                    save_json(SETTINGS_PATH, STORE.settings)
                self.send_json({"llm": {k: v for k, v in STORE.settings.items() if k != "api_key"}, "key_present": bool(os.environ.get("TMT_LLM_API_KEY"))})
            elif path == "/api/llm/draft":
                self.handle_llm_draft(payload)
            elif path == "/api/collection/config":
                self.send_json(COLLECTOR.configure(payload))
            elif path == "/api/collection/run":
                self.send_json({"items": COLLECTOR.run_once()})
            elif path == "/api/collection/review":
                self.send_json({"item": COLLECTOR.review(payload.get("id"), payload.get("status"))})
            elif path == "/api/collection/plan":
                self.handle_collection_plan(payload)
            else:
                self.send_json({"error": "not_found"}, 404)
        except json.JSONDecodeError:
            self.send_json({"error": "invalid_json"}, 400)
        except ValueError:
            self.send_json({"error": "validation_error"}, 400)
        except KeyError:
            self.send_json({"error": "not_found"}, 404)
        except Exception:
            self.send_json({"error": "server_error"}, 500)

    def handle_ingest(self, payload: dict[str, Any]) -> None:
        filename = payload.get("filename")
        content = decode_upload_base64(payload.get("content_base64"))
        result = WORKSPACE_DB.import_bytes(
            filename,
            content,
            ocr=payload.get("ocr", False),
            company_id=payload.get("company_id", ""),
        )
        self.send_json({
            "source": result["source"],
            "evidence": result["evidence"],
            "pending": result["pending_fact_count"],
        }, 201)

    def handle_collection_plan(self, payload: dict[str, Any]) -> None:
        if not STORE.settings.get("allow_scheduled_collection"):
            self.send_json({"error": "collection_disabled"}, 403)
            return
        selected_ids = payload.get("source_ids") or []
        sources = [s for s in STORE.all_sources if s.get("id") in selected_ids]
        plan = {
            "created_at": utc_now(),
            "frequency": payload.get("frequency", "weekly"),
            "lookback_days": int(payload.get("lookback_days", 7)),
            "sources": [{"id": s.get("id"), "title": s.get("title"), "url": s.get("url")} for s in sources],
            "steps": [
                "读取已授权来源的公开元数据/公告入口",
                "按标题、日期和 URL 去重",
                "进入待审核资讯箱，不自动写入确认事实",
                "用户确认后再进行事实抽取和事件分类",
            ],
            "status": "preview_only",
            "note": "当前版本只生成安全的审阅计划，不在没有明确连接器配置时自动抓取全网。",
        }
        self.send_json({"plan": plan})

    def handle_llm_draft(self, payload: dict[str, Any]) -> None:
        if not STORE.settings.get("allow_external_llm"):
            self.send_json({"error": "llm_disabled"}, 403)
            return
        api_key = os.environ.get("TMT_LLM_API_KEY")
        base_url = (STORE.settings.get("base_url") or os.environ.get("TMT_LLM_BASE_URL") or "").rstrip("/")
        model = STORE.settings.get("model") or os.environ.get("TMT_LLM_MODEL") or ""
        if not api_key or not base_url or not model:
            self.send_json({"error": "llm_not_configured"}, 400)
            return
        evidence = payload.get("evidence", [])
        if not isinstance(evidence, list) or len(evidence) > 12:
            self.send_json({"error": "evidence_scope_invalid"}, 400)
            return
        prompt = str(payload.get("instruction", "请根据证据生成一段研究备忘录草稿，并区分事实、计算和推断。"))
        context = "\n".join(f"[{i+1}] {str(item)[:1500]}" for i, item in enumerate(evidence))
        request_body = json.dumps({
            "model": model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": "你是 TMT 研究助理。只能基于用户选定证据回答；缺少证据时明确说不足以判断；不得把推断写成事实，不提供投资建议。"},
                {"role": "user", "content": prompt + "\n\n选定证据：\n" + context},
            ],
        }).encode("utf-8")
        request = urllib.request.Request(base_url + "/chat/completions", data=request_body, headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            self.send_json({"content": content, "model": model, "evidence_count": len(evidence), "generated": True})
        except urllib.error.HTTPError:
            self.send_json({"error": "llm_http_error"}, 502)
        except (urllib.error.URLError, TimeoutError, OSError):
            self.send_json({"error": "llm_network_error"}, 502)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.lstrip("/")
        candidate = (FRONTEND_DIR / relative).resolve()
        if FRONTEND_DIR.resolve() not in candidate.parents and candidate != FRONTEND_DIR.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        raw = candidate.read_bytes()
        content_type = "text/html; charset=utf-8"
        if candidate.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif candidate.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif candidate.suffix == ".json":
            content_type = "application/json; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    host = os.environ.get("TMT_HOST", "127.0.0.1")
    port = int(os.environ.get("TMT_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"TMT Atlas running at http://{host}:{port}")
    print("Local-first mode: no LLM, no external fetch, and no API key required.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping TMT Atlas")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
