"""Apply migrations explicitly: python -m app.migrate. Never called on startup."""

from alembic import command
from alembic.config import Config

from app.core.config import PROJECT_ROOT


def main() -> int:
    try:
        command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    except Exception:
        # Avoid including credentials or connection parameters in terminal output.
        print("迁移失败，已停止。请检查配置、数据库服务、账号权限及是否已有同名表。")
        return 1
    print("数据库迁移完成：已到当前版本；未添加人物、来源或公开内容数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
