"""Inspect automatic eligibility without network requests, writes or scheduling."""

import argparse
import json

from sqlalchemy.exc import SQLAlchemyError

from app.collection_rules import check_rules
from app.core.config import Settings


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读检查自动采集配置和开关，不开启监控或执行采集')
    parser.add_argument('--source', choices=('ecb', 'trump'), help='留空检查两个已实现来源')
    args = parser.parse_args(argv)
    try:
        result = check_rules(Settings(), args.source)
    except (SQLAlchemyError, ValueError):
        print('规则检查失败，请核对本机数据库和配置；未采集、未修改开关，未显示连接凭证。')
        return 1
    print(json.dumps(result.model_dump(mode='json'), ensure_ascii=False, indent=2))
    return 2 if any(item.status == 'invalid_configuration' for item in result.items) else 0


if __name__ == '__main__':
    raise SystemExit(main())
