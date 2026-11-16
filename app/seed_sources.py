"""Save initial source settings only. Never download RSS or import statements."""

import json
from uuid import uuid5

from sqlalchemy import Connection, create_engine, text

from app.core.config import Settings
from app.core.database import database_url
from app.seed_people import NAMESPACE

INITIAL_SOURCES = (
    {
        "source_id": uuid5(NAMESPACE, "ecb-press-rss"),
        "person_id": uuid5(NAMESPACE, "christine-lagarde"),
        "name": "欧洲央行官方公开信息 RSS",
        "publisher_name": "European Central Bank",
        "provenance_type": "official",
        "fetch_method": "rss",
        "feed_url": "https://www.ecb.europa.eu/rss/press.html",
        "homepage_url": "https://www.ecb.europa.eu/home/html/rss.en.html",
        "poll_interval_seconds": 300,
    },
    {
        "source_id": uuid5(NAMESPACE, "trumps-truth-rss"),
        "person_id": uuid5(NAMESPACE, "donald-trump"),
        "name": "Trump’s Truth 第三方归档 RSS",
        "publisher_name": "Trump’s Truth (Defending Democracy Together)",
        "provenance_type": "third_party_archive",
        "fetch_method": "rss",
        "feed_url": "https://www.trumpstruth.org/feed",
        "homepage_url": "https://www.trumpstruth.org/about",
        "poll_interval_seconds": 300,
    },
)


def seed_sources(connection: Connection, sources=INITIAL_SOURCES) -> dict[str, int]:
    connection.execute(text("LOCK TABLE sources, person_sources IN SHARE ROW EXCLUSIVE MODE"))
    # Validate all targets before inserting anything. The caller owns the transaction.
    for source in sources:
        person = connection.execute(
            text("SELECT person_id FROM people WHERE person_id=:person_id"), source,
        ).scalar_one_or_none()
        if person is None:
            raise ValueError("关联人物不存在，请先核对人物资料。")

    counts = {"sources_inserted": 0, "sources_skipped": 0, "links_inserted": 0, "links_skipped": 0}
    for source in sources:
        existing = connection.execute(
            text("SELECT source_id FROM sources WHERE source_id=:source_id"), source,
        ).scalar_one_or_none()
        if existing is not None:
            # Do not overwrite edits, enabled flags, intervals or timestamps.
            counts["sources_skipped"] += 1
        else:
            same_url = connection.execute(
                text("SELECT source_id FROM sources WHERE feed_url=:feed_url LIMIT 1"), source,
            ).scalar_one_or_none()
            if same_url is not None:
                raise ValueError("发现相同入口的已有来源，请先人工核对。")
            connection.execute(text(
                "INSERT INTO sources (source_id, name, publisher_name, provenance_type, fetch_method, "
                "feed_url, homepage_url, poll_interval_seconds, enabled) "
                "VALUES (:source_id, :name, :publisher_name, :provenance_type, :fetch_method, "
                ":feed_url, :homepage_url, :poll_interval_seconds, false)"
            ), source)
            counts["sources_inserted"] += 1
        inserted_link = connection.execute(text(
            "INSERT INTO person_sources (person_id, source_id, enabled) "
            "VALUES (:person_id, :source_id, false) "
            "ON CONFLICT (person_id, source_id) DO NOTHING RETURNING source_id"
        ), source).scalar_one_or_none()
        counts["links_inserted" if inserted_link is not None else "links_skipped"] += 1
    return counts


def main() -> int:
    engine = None
    try:
        engine = create_engine(
            database_url(Settings()), hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": "-c lock_timeout=5000 -c statement_timeout=10000"},
        )
        with engine.begin() as connection:
            result = seed_sources(connection)
    except ValueError:
        print("来源配置导入已取消，请核对数据库配置、关联人物或已有同入口来源。")
        return 1
    except Exception:
        print("来源配置导入失败，请检查数据库连接、账号权限及表结构。未显示连接凭证。")
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
