# PrismQuant 人物监控与研究 Agent

前端与平台接入从[CHAT_FRONTEND_HANDOFF.md](CHAT_FRONTEND_HANDOFF.md)开始，包含聊天、模型设置、额度管理的分工，以及测试凭证与页面操作。

2026-11-16行业范围与来源扩展更新：详见[RESEARCH_FOCUS.md](RESEARCH_FOCUS.md)。新准备使用synthesis_v3，旧回答不改写。

2026-11-16 综合研究更新：人物原文与实际读取的官方背景可共同参与回答，支持补查后继续分析；直接结论、事实来源和条件推断分开呈现。接口和验收边界见 [RESEARCH_SYNTHESIS.md](RESEARCH_SYNTHESIS.md)。

独立模块交付版本 0.1.0，2026-11-14。用于采集公开人物发言、保留来源与版本、检索原文，以及开展带引用的研究问答。

## 功能范围

| 能力 | 状态 |
| --- | --- |
| 原文采集、去重、版本与归属保存 | 已实现；来源覆盖有限 |
| 关键词提醒、向量知识库与混合RAG | 已实现；需要数据、索引和配置 |
| 私有问答、追问、引用、历史记录 | 已实现；在线生成需要有效额度授权 |
| 研究分析模式 | 已接通结构化条件推断及离线验证；真实模型效果待验收 |
| 人物来源状态卡 | 显示最近成功检查及入库，可打开原文；不代表发布时效达标 |
| 新内容自动分析 | 队列与预算已实现，持续付费任务未启用 |
| 反馈、独立复核与质量报告 | 工具已实现；真实人工评测未完成 |
| Windows API监督、采集守护、备份恢复 | 支持独立本机运行；验收见ACCEPTANCE.md |
| 总平台身份集成、远程持续部署 | 需要平台代码和目标环境，尚未接入 |
| M2数据、模型微调 | 不在本轮交付范围 |

## 安装与启动

环境要求：Windows、Python 3.12、PostgreSQL 18。所有命令在模块目录执行。源码不包含数据库、历史业务数据、凭证或本地模型文件。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.lock.txt
Copy-Item .env.example .env
```

填写本机数据库配置后执行；已有.env时保留原配置，不重复复制。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m app.check_database
.\.venv\Scripts\python.exe -X utf8 -m app.migrate
.\.venv\Scripts\python.exe -X utf8 -m app.seed_people
.\.venv\Scripts\python.exe -X utf8 -m app.seed_sources
.\deploy.ps1 -Action Check
.\deploy.ps1 -Action Start
```

访问人物页面 http://127.0.0.1:8000/monitor/ 。初始数据库没有发言记录，需按DEPLOYMENT.md导入内容；来源初始化不会自动开启监控。向量模型需要显式下载和建索引。启动不会授权付费生成。

## 交付文档

- [部署与运行](DEPLOYMENT.md)：配置、采集、索引、进程与备份恢复。
- [API与身份集成](INTEGRATION.md)：同源接入、权限、引用和事件更新。
- [研究分析与来源状态](RESEARCH_ANALYSIS.md)：新增契约、使用方式和本轮验证边界。
- [问答稳定性与原文补读](RESEARCH_WORKFLOW.md)：任务继承、方法清单、邻段读取及引用关联。
- [验收记录](ACCEPTANCE.md)：测试证据与未完成验收项。
- [已知限制](KNOWN_LIMITS.md)：时效、来源、评测、部署和模型边界。
- [集成交付说明](TEAM_HANDOFF.md)：接入顺序与交付物检查。

公开内容的来源归属核对与内容事实核实分别记录。研究输出需要回看原文，不代表已经接入行情或完成投资效果验证。

## 官方来源导航更新

迁移 0027_source_navigation 新增不消耗模型额度的官方来源导航，可承接研究清单并查看对应入口、步骤及实际读取状态。范围和 API 说明见 [SOURCE_NAVIGATION.md](SOURCE_NAVIGATION.md)。

## 可选补充搜索接口

已提供 SearXNG 接入和受控官方正文读取。当前主机未部署搜索服务，默认继续使用官方目录。部署方式、限制与状态含义见 [SEARCH_CONNECTOR.md](SEARCH_CONNECTOR.md)。

## 分析缺口补查

支持从研究回答的待核对资料直接填写补查问题，并在同会话承接结构化缺口编号。不自动生成或覆盖历史回答，详见 [RESEARCH_GAP_FLOW.md](RESEARCH_GAP_FLOW.md)。

## 金融行业 AI 补充正文

来源导航可读取两份美国官方背景正文并展示原文片段，补充应用案例与风险资料；不代表具体公司投资额已取得。见 [FINANCIAL_AI_SOURCES.md](FINANCIAL_AI_SOURCES.md)。
