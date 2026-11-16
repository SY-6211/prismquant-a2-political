"""Explicit Trump archive batch import; each post and its details commit together."""

import argparse
import json
import time
from datetime import datetime, timezone
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml.common import DefusedXmlException
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import SQLAlchemyError

from app.batch_trump import load_context, prepare_candidates, read_existing, select_unseen, classify
from app.preview_trump import PERSON_ID, SOURCE_ID, fetch_feed_items
from app.extract_trump_post import prepare_extraction
from app.core.config import Settings
from app.collection_guard import CollectionBlocked
from app.collection_failures import http_failure
from app.core.database import database_url
from app.import_trump_post import save_post
from app.preview_batch import batch_limit, now_text
from app.preview_trump_post import fetch_post
from app.timing import elapsed_ms, item_timing, measure_stage, report_timing


def save_one(settings: Settings, extraction: dict) -> dict:
    """Return only after commit; network work has already finished."""
    engine = create_engine(
        database_url(settings), poolclass=NullPool, hide_parameters=True,
        connect_args={"connect_timeout": 5, "options": "-c lock_timeout=5000 -c statement_timeout=10000"},
    )
    try:
        with engine.begin() as connection:
            result = save_post(connection, extraction, datetime.now(timezone.utc))
        return result
    finally:
        engine.dispose()


def run_import(settings: Settings, limit: int, client: httpx.Client, *, pause=None, before_request=None, save=None, feed_items=None) -> dict:
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Batch limit must be 1–5")
    sleep = pause if pause is not None else time.sleep
    report = {
        "mode": "manual_batch_import", "source": "trump", "source_id": str(SOURCE_ID),
        "person_id": str(PERSON_ID), "provenance_type": "third_party_archive", "limit": limit, "saved": False,
        "status": "completed", "started_at": now_text(), "finished_at": None, "stop_reason": None,
        "source_enabled": None, "monitoring_enabled": None, "link_enabled": None,
        "selection": {}, "precheck_results": [], "items": [],
        "timing": report_timing(provided_items=feed_items is not None),
        "summary": {"attempted": 0, "already_exists": 0, "skipped_unsupported": 0, "needs_review": 0, "failed": 0,
                    "interrupted": 0, "not_attempted": 0, "inserted": 0},
    }
    report["attribution"] = "第三方归档；非官方接口，未访问原帖。主帖文字与引用及归档附件说明分开保存，不合并为本人发言。"
    counts = report["summary"]
    try:
        context = load_context(settings)
        report.update(source_name=context["name"], publisher_name=context["publisher_name"], **{
            key: context[key] for key in ("source_enabled", "monitoring_enabled", "link_enabled")
        })
        if before_request is not None:
            before_request()
        with measure_stage(report["timing"], "list") as list_clock:
            prepared = prepare_candidates(fetch_feed_items(client) if feed_items is None else feed_items)
        existing = read_existing(settings, [item["url"] for item in prepared["candidates"]])
        selected = select_unseen(prepared, existing, limit)
        report.update(selection=selected["selection"], precheck_results=selected["precheck_results"])
        report["items"] = [{"url": item["url"], "rss_title": item.get("title"),
                            "rss_published_at": item.get("published_at"), "rss_published_raw": item.get("published_raw"),
                            "timing": item_timing(report["timing"]["input_kind"], report["timing"]["list_finished_at"]),
                            "rss_item": item, "status": "not_attempted", "reason": "尚未开始详情读取。"}
                           for item in selected["candidates"]]
        for item in report["items"]:
            begun = False
            writing = False
            try:
                # There was one RSS GET; each detail GET waits at least one second.
                sleep(1.0)
                if before_request is not None:
                    before_request()
                counts["attempted"] += 1
                begun = True
                with measure_stage(item["timing"], "detail"):
                    detail = fetch_post(client, item["rss_item"], include_full=True)
                status, reason = classify(detail, item["rss_item"])
                item.update(status=status, reason=reason)
                if status == "ready_for_import":
                    extraction = prepare_extraction(detail, item["rss_item"])
                    writing = True
                    with measure_stage(item["timing"], "save", success_only=True) as save_clock:
                        result = (save or save_one)(settings, extraction)
                    item["timing"]["observed_to_save_ms"] = elapsed_ms(list_clock.finished_counter, save_clock.finished_counter)
                    item.update(status=result["action"], reason=result["message"], record_id=result["record_id"],
                                body_characters=result["body_characters"], media_note_count=result["media_note_count"])
                    writing = False
                counts[item["status"]] += 1
            except CollectionBlocked:
                if begun:
                    item.update(status="needs_review", reason="执行锁或自动采集许可已变化，当前项未继续保存。")
                    counts["needs_review"] += 1
                report.update(status="stopped", stop_reason="执行锁或自动采集许可已变化，停止后续请求和保存；已提交内容保留。")
                break
            except KeyboardInterrupt:
                if begun:
                    item.update(status="interrupted", reason="用户中断当前项；如处于写入或提交期间，保存结果需按地址复核。")
                    item["write_outcome_unknown"] = writing
                    counts["interrupted"] += 1
                report.update(status="interrupted", stop_reason="用户中断；已提交内容保留，不再请求后续候选。")
                break
            except httpx.HTTPStatusError as exc:
                report.update(http_failure(exc))
                status = exc.response.status_code
                item.update(status="failed", reason=f"详情返回 HTTP {status}；未保存内容。")
                counts["failed"] += 1
                if status not in (404, 410):
                    report.update(status="stopped", stop_reason=f"来源返回 HTTP {status}，已停止，不自动重试或跟随重定向。")
                    break
            except httpx.RequestError:
                report["failure_kind"] = "network"
                item.update(status="failed", reason="详情连接失败或超时；未保存内容。")
                counts["failed"] += 1
                report.update(status="stopped", stop_reason="来源连接失败或超时，已停止，不自动重试。")
                break
            except SQLAlchemyError:
                if not begun:
                    report.update(status="stopped", stop_reason="请求前的数据库锁或配置检查失败；已提交内容保留。")
                    break
                item.update(status="failed", reason="数据库操作失败；当前项是否提交需按地址复核，未显示连接凭证。",
                            write_outcome_unknown=True)
                counts["failed"] += 1
                report.update(status="stopped", stop_reason="数据库操作失败，停止后续处理；先前已提交内容保留。")
                break
            except ValueError:
                if writing:
                    item.update(status="needs_review", reason="保存前的来源、人物关联或完整字段需核对；未写入当前项。")
                    counts["needs_review"] += 1
                    report.update(status="stopped", stop_reason="保存检查未通过，停止后续处理；先前已提交内容保留。")
                    break
                item.update(status="needs_review", reason="详情地址、格式或大小不符合完整预览要求；未保存内容。")
                counts["needs_review"] += 1
    except CollectionBlocked:
        report.update(status="stopped", stop_reason="执行锁或自动采集许可已变化，未继续请求来源。")
    except KeyboardInterrupt:
        report.update(status="interrupted", stop_reason="用户中断；已提交内容保留。")
    except httpx.HTTPStatusError as exc:
        report.update(http_failure(exc))
        report.update(status="stopped", stop_reason=f"RSS 返回 HTTP {exc.response.status_code}；未开始详情处理。")
    except httpx.RequestError:
        report["failure_kind"] = "network"
        report.update(status="stopped", stop_reason="RSS 连接失败或超时；未请求候选详情或保存内容。")
    except SQLAlchemyError:
        report.update(status="stopped", stop_reason="数据库前置检查失败，未显示连接凭证；未开始详情处理。")
    except (ValueError, ParseError, DefusedXmlException):
        report.update(status="stopped", stop_reason="来源配置、人物关联或 RSS 格式需核对；未保存内容。")
    counts["not_attempted"] = sum(item["status"] == "not_attempted" for item in report["items"])
    if report["status"] not in ("stopped", "interrupted"):
        if report["selection"].get("precheck_needs_review", 0) or counts["needs_review"] or counts["failed"]:
            report["status"] = "completed_with_issues"
        elif not report["items"]:
            report["status"] = "no_new_candidates" if report["selection"].get("skipped_existing", 0) else "no_candidates"
    for item in report["items"]:
        item.pop("rss_item", None)
    report["saved"] = counts["inserted"] > 0
    report["finished_at"] = now_text()
    return report


