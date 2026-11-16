"""Explicit initial people import. Does not enable monitoring or fetch posts."""

import json
from uuid import UUID, uuid5

from sqlalchemy import Connection, create_engine, text

from app.core.config import Settings
from app.core.database import database_url

NAMESPACE = UUID("037c8b31-06bd-4e1d-8953-57af13c12627")
INITIAL_PEOPLE = (
    {
        "person_id": uuid5(NAMESPACE, "donald-trump"),
        "name_original": "Donald J. Trump",
        "name_zh": "唐纳德·特朗普",
        "aliases": ["Donald Trump", "特朗普", "川普"],
        "country_codes": ["US"],
    },
    {
        "person_id": uuid5(NAMESPACE, "christine-lagarde"),
        "name_original": "Christine Lagarde",
        "name_zh": "克里斯蒂娜·拉加德",
        "aliases": ["拉加德", "拉加德女士"],
        "country_codes": ["FR"],
    },
)


def seed_people(connection: Connection, people=INITIAL_PEOPLE) -> dict[str, int]:
    # Serialize explicit imports and concurrent table writes without changing
    # the general rule that different people can have the same name.
    connection.execute(text("LOCK TABLE people IN SHARE ROW EXCLUSIVE MODE"))
    inserted = 0
    skipped = 0
    for person in people:
        exists = connection.execute(
            text("SELECT person_id FROM people WHERE person_id=:person_id"),
            {"person_id": person["person_id"]},
        ).scalar_one_or_none()
        if exists is not None:
            # Keep all existing edits, timestamps and monitoring preferences.
            skipped += 1
            continue
        matching_name = connection.execute(
            text("SELECT person_id FROM people WHERE name_original=:name_original LIMIT 1"),
            {"name_original": person["name_original"]},
        ).scalar_one_or_none()
        if matching_name is not None:
            raise ValueError("发现同名的已有记录，请先核对身份；本次导入已停止。")
        connection.execute(text(
            "INSERT INTO people "
            "(person_id, name_original, name_zh, aliases, country_codes, monitoring_enabled) "
            "VALUES (:person_id, :name_original, :name_zh, :aliases, :country_codes, false)"
        ), person)
        inserted += 1
    return {"inserted": inserted, "skipped": skipped}


def main() -> int:
    engine = None
    try:
        engine = create_engine(
            database_url(Settings()), hide_parameters=True,
            connect_args={"connect_timeout": 5, "options": "-c lock_timeout=5000 -c statement_timeout=10000"},
        )
        with engine.begin() as connection:
            result = seed_people(connection)
    except ValueError:
        print("导入未完成：请检查配置或核对已有同名人物。整个导入事务已取消。")
        return 1
    except Exception:
        print("导入失败：请检查数据库连接、账号权限及人物表迁移。未显示连接凭证。")
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
