"""Bounded manual Trump archive batch preview; no database writes."""

import argparse
import json
import time
from datetime import datetime, timezone
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml.common import DefusedXmlException
from sqlalchemy.exc import SQLAlchemyError

from app.batch_trump import load_context, prepare_candidates, read_existing, select_unseen, classify, bounded
from app.preview_trump import PERSON_ID, SOURCE_ID, fetch_feed_items
from app.core.config import Settings
from app.preview_trump_post import fetch_post


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_preview(settings: Settings, limit: int, client: httpx.Client, *, pause=None) -> dict:
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Batch limit must be 1–5")
    sleep = pause if pause is not None else time.sleep
    report = {
        "mode": "manual_batch_preview", "source": "trump", "source_id": str(SOURCE_ID),
        "person_id": str(PERSON_ID), "provenance_type": "third_party_archive", "limit": limit, "saved": False,
        "status": "completed", "started_at": now_text(), "finished_at": None, "stop_reason": None,
        "source_enabled": None, "monitoring_enabled": None, "link_enabled": None,
        "selection": {}, "precheck_results": [], "items": [],
        "summary": {"attempted": 0, "ready_for_import": 0, "skipped_unsupported": 0, "needs_review": 0, "failed": 0,
                    "interrupted": 0, "not_attempted": 0, "inserted": 0},
    }
    report["attribution"] = "第三方归档预览；非特朗普或 Truth Social 官方接口，未访问原帖。主帖、引用、图片说明及视频转写分别展示，不合并为本人发言。"
    counts = report["summary"]
    try:
        context = load_context(settings)
        report.update(source_name=context["name"], publisher_name=context["publisher_name"], **{
            key: context[key] for key in ("source_enabled", "monitoring_enabled", "link_enabled")
        })
        prepared = prepare_candidates(fetch_feed_items(client))
        existing = read_existing(settings, [item["url"] for item in prepared["candidates"]])
        selected = select_unseen(prepared, existing, limit)
        report.update(selection=selected["selection"], precheck_results=selected["precheck_results"])
        report["items"] = [{"url": item["url"], "rss_title": item.get("title"),
                            "rss_published_at": item.get("published_at"), "rss_published_raw": item.get("published_raw"),
                            "rss_item": item, "status": "not_attempted", "reason": "尚未开始详情读取。"}
                           for item in selected["candidates"]]
        for item in report["items"]:
            begun = False
            try:
                # There was one RSS GET; each detail GET waits at least one second.
                sleep(1.0)
                counts["attempted"] += 1
                begun = True
                detail = fetch_post(client, item["rss_item"], include_full=True)
                status, reason = classify(detail, item["rss_item"])
                item.update(status=status, reason=reason, detail=bounded(detail))
                counts[item["status"]] += 1
            except KeyboardInterrupt:
                if begun:
                    item.update(status="interrupted", reason="用户中断了当前详情读取，未保存内容。")
                    counts["interrupted"] += 1
                report.update(status="interrupted", stop_reason="用户中断；未继续请求后续候选，未保存内容。")
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                item.update(status="failed", reason=f"详情返回 HTTP {status}；未保存内容。")
                counts["failed"] += 1
                if status not in (404, 410):
                    report.update(status="stopped", stop_reason=f"来源返回 HTTP {status}，已停止，不自动重试或跟随重定向。")
                    break
            except httpx.RequestError:
                item.update(status="failed", reason="详情连接失败或超时；未保存内容。")
                counts["failed"] += 1
                report.update(status="stopped", stop_reason="来源连接失败或超时，已停止，不自动重试。")
                break
            except ValueError:
                item.update(status="needs_review", reason="详情地址、格式或大小不符合完整预览要求；未保存内容。")
                counts["needs_review"] += 1
    except KeyboardInterrupt:
        report.update(status="interrupted", stop_reason="用户中断；未保存内容。")
    except httpx.HTTPStatusError as exc:
        report.update(status="stopped", stop_reason=f"RSS 返回 HTTP {exc.response.status_code}；未请求候选详情或保存内容。")
    except httpx.RequestError:
        report.update(status="stopped", stop_reason="RSS 连接失败或超时；未请求候选详情或保存内容。")
    except SQLAlchemyError:
        report.update(status="stopped", stop_reason="本地只读数据库检查失败，未显示连接凭证；未保存内容。")
    except (ValueError, ParseError, DefusedXmlException):
        report.update(status="stopped", stop_reason="来源配置、人物关联或 RSS 格式需核对；未保存内容。")
    counts["not_attempted"] = sum(item["status"] == "not_attempted" for item in report["items"])
    if report["status"] not in ("stopped", "interrupted"):
        if report["selection"].get("precheck_needs_review", 0) or counts["needs_review"] or counts["failed"]:
            report["status"] = "completed_with_issues"
        elif not report["items"]:
            report["status"] = "no_new_candidates" if report["selection"].get("skipped_existing", 0) else "no_candidates"
    report["finished_at"] = now_text()
    return report


