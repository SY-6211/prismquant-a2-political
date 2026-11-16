"""Explicit, read-only database check: python -m app.check_database."""

import json

import psycopg
from pydantic import ValidationError

from app.core.config import Settings


def check_database(settings: Settings) -> dict[str, str]:
    if not settings.db_password.get_secret_value():
        raise ValueError("请在项目 .env 中填写 PQ_DB_PASSWORD。")

    # Keyword arguments avoid URI escaping problems with special characters.
    # Connect only when explicitly requested, never during API startup.
    with psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password.get_secret_value(),
        connect_timeout=5,
        options="-c default_transaction_read_only=on -c statement_timeout=5000",
        autocommit=True,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), current_user, "
                "current_setting('transaction_read_only')"
            )
            row = cursor.fetchone()
    if row is None or row[2] != "on":
        raise RuntimeError("未能确认只读连接，检查未通过。")
    return {"status": "ok", "database": row[0], "user": row[1], "read_only": row[2]}


def main() -> int:
    try:
        result = check_database(Settings())
    except ValidationError:
        print("配置格式无效，请检查 .env 中的端口和配置值；未显示配置内容。")
        return 2
    except ValueError:
        print("数据库密码未配置，请在项目 .env 中填写 PQ_DB_PASSWORD。")
        return 2
    except psycopg.Error:
        # Do not print driver exceptions: they may contain connection details.
        print("数据库连接检查失败，请核对数据库服务、地址、端口、账号、数据库名称及密码。")
        return 1
    except RuntimeError:
        print("未能确认只读连接，检查未通过。")
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
