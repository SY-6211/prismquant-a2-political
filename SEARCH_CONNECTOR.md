# 补充搜索接入

日期：2026-11-16。

## 完成范围与当前状态

已接入可选的本机 SearXNG JSON 接口、候选链接筛选、获准官方正文读取、冻结结果保存和搜索状态展示。单轮最多一次搜索请求、两条搜索候选、三次网页读取；搜索与目录读取共用上限。搜索或正文不可用时保留目录与明确失败状态，不自动重试，不调用模型。

本机未安装 Docker，WSL 运行环境未就绪，SearXNG 服务未启动。当前真实搜索尚未验收，不能表述为已启用联网搜索。模拟服务的请求与网页提取测试不等同于真实上游搜索验收。

当前候选正文读取仅限此前已支持的美联储讲话、美国财政部声明与讲话、EIA 文章路径。搜索排名与摘要不作为事实依据；候选标题不会成为所选人物的署名。目录仍保留 SEC、BLS、BEA 等入口，新增网站需另行适配。

## 配置与启用

部署文件可重复准备，已有配置不会覆盖：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m app.search_service prepare
```

配置位于 .runtime/searxng，包含 compose.json 和 config/settings.yml，部署模板使用官方镜像，仅将端口映射到本机 127.0.0.1:8888，启用 html 和 json 输出。随机服务密钥保留在运行目录，不进入交付包。模板未在当前主机运行验收；部署环境应记录实际镜像版本或摘要。

安装并启动 Docker 后，按顺序执行：

```powershell
docker compose -f .runtime/searxng/compose.json up -d
```

在后端 .env 设置 PQ_SEARXNG_URL=http://127.0.0.1:8888，再检查：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m app.research_search
```

配置只接受带显式端口的本机 HTTP 地址；不接受前端传入服务地址。成功后重启后端，在人物问答中保留“寻找资料来源时，读取少量官方入口页”选项并提交来源问题。SearXNG 403 通常需要检查 JSON 输出配置，不能直接视为没有搜索结果。

## 页面与兼容

新结果显示未配置、未开启、搜索失败、没有获准候选或已取得候选等状态。已取得候选不表示正文读取成功，正文读取不表示内容事实已经核验。旧问答结果继续按冻结输入展示，不自动补查、不覆盖旧结果。

保持既有官方来源导航 API 和本地结果类型，无数据库迁移。默认配置为空，不会新增搜索请求。模型自主规划、生成回答中的多轮工具调用、公司财务数值提取、监控速度优化尚未因本轮改动完成。

参考：

- [SearXNG 搜索 API](https://docs.searxng.org/dev/search_api.html)
- [官方容器安装说明](https://docs.searxng.org/admin/installation-docker.html)
