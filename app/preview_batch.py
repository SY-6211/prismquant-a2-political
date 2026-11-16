"""Manual ECB batch preview only. No imports, writes, background work or retries."""

import argparse
import json
import time
from datetime import datetime, timezone
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml.common import DefusedXmlException
from sqlalchemy.exc import SQLAlchemyError

from app.batch_ecb import PERSON_ID, SOURCE_ID, load_context, prepare_candidates, read_existing, select_unseen
from app.core.config import Settings
from app.preview_ecb import fetch_feed_items
from app.preview_ecb_speech import fetch_speech


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_preview(settings: Settings, limit: int, client: httpx.Client, *, pause=None) -> dict:
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Batch limit must be 1–5")
    sleep = pause if pause is not None else time.sleep
    report = {
        "mode": "manual_batch_preview", "source": "ecb", "source_id": str(SOURCE_ID),
        "person_id": str(PERSON_ID), "provenance_type": "official", "limit": limit, "saved": False,
        "status": "completed", "started_at": now_text(), "finished_at": None, "stop_reason": None,
        "source_enabled": None, "monitoring_enabled": None, "link_enabled": None,
        "selection": {}, "precheck_results": [], "items": [],
        "summary": {"attempted": 0, "ready_for_import": 0, "needs_review": 0, "failed": 0,
                    "interrupted": 0, "not_attempted": 0, "inserted": 0},
    }
    counts = report["summary"]
    try:
        context = load_context(settings)
        report.update(source_name=context["name"], **{
            key: context[key] for key in ("source_enabled", "monitoring_enabled", "link_enabled")
        })
        prepared = prepare_candidates(fetch_feed_items(client))
        existing = read_existing(settings, [item["url"] for item in prepared["candidates"]])
        selected = select_unseen(prepared, existing, limit)
        report.update(selection=selected["selection"], precheck_results=selected["precheck_results"])
        report["items"] = [{"url": item["url"], "rss_title": item.get("title"),
                            "rss_published_at": item.get("published_at"), "rss_published_raw": item.get("published_raw"),
                            "status": "not_attempted", "reason": "尚未开始详情读取。"}
                           for item in selected["candidates"]]
        for item in report["items"]:
            begun = False
            try:
                # There was one RSS GET; each detail GET waits at least one second.
                sleep(1.0)
                counts["attempted"] += 1
                begun = True
                detail = fetch_speech(client, item["url"], include_body=True)
                if detail["verification_status"] == "verified_page_attribution":
                    body = detail.get("body_text")
                    valid = (detail.get("person_id") == str(PERSON_ID) and isinstance(body, str)
                             and len(body.strip()) >= 80 and len(body) == detail.get("body_characters"))
                else:
                    valid = False
                item.update(status="ready_for_import" if valid else "needs_review", reason=detail["reason"],
                            detail={key: value for key, value in detail.items() if key != "body_text"})
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


def batch_limit(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit 必须为 1–5 的整数") from None
    if not 1 <= result <= 5:
        raise argparse.ArgumentTypeError("limit 必须为 1–5 的整数")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="人物批量预览，只读、不入库")
    parser.add_argument("--source", required=True, choices=("ecb", "trump"), help="ecb 官方讲话或 trump 第三方归档")
    parser.add_argument("--limit", type=batch_limit, default=3, help="最多尝试的详情数，默认 3，范围 1–5")
    args = parser.parse_args(argv)
    try:
        settings = Settings()
    except ValueError:
        print("本地配置不符合要求，未读取来源或保存内容。")
        return 1
    with httpx.Client(
        timeout=httpx.Timeout(15.0, connect=10.0), follow_redirects=False,
        headers={"User-Agent": f"PrismQuant-FYP/0.1 (manual {args.source} batch preview)"},
    ) as client:
        if args.source == "trump":
            from app.preview_trump_batch import run_preview as run_trump_preview
            report = run_trump_preview(settings, args.limit, client)
        else:
            report = run_preview(settings, args.limit, client)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {"stopped": 1, "interrupted": 130, "completed_with_issues": 2}.get(report["status"], 0)


if __name__ == "__main__":
    raise SystemExit(main())
