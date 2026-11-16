# API与身份集成

前端任务和后端待补接口清单见[CHAT_FRONTEND_HANDOFF.md](CHAT_FRONTEND_HANDOFF.md)。模型Key网页配置、动态切换模型和网页自助开额度尚需后端开发，不属于纯前端页面工作。

2026-11-16行业范围与来源扩展更新：详见[RESEARCH_FOCUS.md](RESEARCH_FOCUS.md)。新准备使用synthesis_v3，旧回答不改写。

2026-11-16 新增人物原文与官方背景综合研究。开启混合检索及 `read_official_sources` 后，研究、归纳与比较问题可使用 `person_research_synthesis_v2`；前端需要支持 `answer_mode=synthesis` 的直接回答、分开引用与条件分析。旧记录继续按原契约展示。完整字段、调用上限及适配方式见 [RESEARCH_SYNTHESIS.md](RESEARCH_SYNTHESIS.md)。

## 接入约定

基础前缀/api/v1，独立页面/monitor/。推荐总平台通过同源反向代理转发API和静态资源，保留路径。API默认仅监听回环地址。跨域只读查询可配置精确PQ_CORS_ORIGINS；私有写入和凭证传输保持同源，不使用通配来源。

GET /health仅表示API进程可响应。GET /api/v1/module-info提供模块版本、界面模式、权限边界和运行实例标识；不证明数据库、采集或模型已就绪。

## 功能接口

| 功能 | 路径 |
| --- | --- |
| 人物 | GET /api/v1/people、/people/{person_id} |
| 发言与正文 | GET /api/v1/public-records、/public-records/{record_id} |
| 原文事件与版本 | GET /api/v1/events/{event_id}、/events/{event_id}/versions/{version_id} |
| 已保存分析 | GET /api/v1/agent-analyses、/agent-analyses/{run_id} |
| 内容更新 | GET /api/v1/content-stream、/content-stream/state |
| 人物来源简要状态 | GET /api/v1/monitoring-status?person_id=<UUID>；公开只读，不触发采集 |
| 提醒 | GET /api/v1/reminders、/reminders/summary |
| 语义检索 | POST /api/v1/knowledge/search |
| 本人身份 | GET /api/v1/agent-chat/me |
| 会话及问题 | GET/POST /api/v1/agent-chat/conversations、/conversations/{id}/messages |
| 回答准备及历史 | GET/POST /api/v1/agent-chat/conversations/{id}/answers |
| 明确执行生成 | POST /api/v1/agent-chat/conversations/{id}/answers/{run_id}/execute |
| 本人反馈及报告 | GET/POST .../answers/{run_id}/feedback；GET /api/v1/agent-chat/quality-report |

完整字段、分页和错误结构以交付包openapi.json及运行环境/openapi.json为准。列表通常使用items/next_cursor。SSE支持断线补读与版本标识；连接成功不表示外部来源已实时发布。

## 身份边界

2026-11-15新增研究分析契约：research_mode=research，输出区分原文事实、条件推断、情景和资料缺口；详见RESEARCH_ANALYSIS.md。旧模式及历史回答继续兼容。分析序号与引句位置校验不代表推理质量已通过人工认证。

未开启官方背景的研究问答兼容research_v4；开启官方背景的新综合研究使用person_research_synthesis_v3。context.research_task区分impact_analysis与method_checklist。research_steps返回明确标注的一般方法或原文相关方法，analysis及方法可包含服务端派生的evidence_citations。无原文的合法方法回答可没有points，不应据此当作引用损坏；仅在method_checklist且方法结构通过时接受此情况。完整约束见RESEARCH_WORKFLOW.md。

公开人物内容和私有研究历史分开。私有请求使用Authorization: Bearer <MODULE_USER_TOKEN>，由服务端映射到会话所有者。前端不能自报user_id或reviewer_id；模型Key不能作为用户令牌。

独立页面使用已有开发凭证，无新增账号密码系统。正式接入时，平台服务端必须先验证平台会话，再建立稳定的外部用户与模块用户映射，签发或代理模块用户凭证，并处理到期和撤销。本轮未获得平台身份协议，因此没有实现或宣称平台单点登录已接通。

frontend/chat-api.js可作为同源私有传输适配器，支持凭证验证、no-store、取消与明确写操作；集成方复用其契约即可，无需复制整套页面。Bearer不写入浏览器持久存储，不传至外部来源。

## 管理边界

research界面保留人物动态、分析、问答、知识检索和提醒；运维日志、来源目录、网页试接、备份、详细延迟与完整质量报告隐藏。production环境同时在服务端保护运维接口，隐藏按钮不作为权限控制。

完整运行状态、采集记录、详细来源检查、发现调度、任务队列和自动分析运行状态需要关键词管理员令牌；来源目录也接受独立来源管理员令牌，其写入继续接受原有角色校验。公开monitoring-status只提供白名单字段构成的简要状态，不包含管理报告或操作。私有会话、标注和模型执行保持原有权限。开发环境仍可使用operations界面诊断，仅限可信本机环境。

## 前端接入检查

1. 显示最新内容、完整正文和来源性质；正文以文本呈现，引用与附件说明分开。
2. 验证两名用户分别建立会话，交叉读取均拒绝；刷新后能重新打开本人历史。
3. 问题保存、资料准备、真实生成分开处理，未授权时显示明确状态。
4. 失败后使用原client_request_id核对与重试，不自动重复模型请求。
5. 引用绑定event/version及Unicode字符位置，检索分数不当作事实置信度。
6. 页面离开时关闭SSE、定时器并取消过期请求；迟到结果不能覆盖其他会话。

## 待提供的环境信息

平台代码与构建方式、同源代理入口、服务端身份协议、目标运行环境、数据源许可和付费预算。接入后按ACCEPTANCE.md重新执行环境验收，不复用本机结果冒充目标平台结果。

## 来源导航结果

新增 read_official_sources（默认 false）和 local_source_navigation 结果类型。answer_mode=source_navigation 时使用 source_items、source_resources 和 summary_zh 展示来源卡片；points 可以为空，不应当作模型回答失败。来源导航不需要模型额度、不请求模型。接入前应用迁移 0027_source_navigation，字段、状态和边界详见 [SOURCE_NAVIGATION.md](SOURCE_NAVIGATION.md)。

## 可选补充搜索接口

已提供 SearXNG 接入和受控官方正文读取。当前主机未部署搜索服务，默认继续使用官方目录。部署方式、限制与状态含义见 [SEARCH_CONNECTOR.md](SEARCH_CONNECTOR.md)。

## 分析缺口补查

支持从研究回答的待核对资料直接填写补查问题，并在同会话承接结构化缺口编号。不自动生成或覆盖历史回答，详见 [RESEARCH_GAP_FLOW.md](RESEARCH_GAP_FLOW.md)。

## 金融行业 AI 补充正文

来源导航可读取两份美国官方背景正文并展示原文片段，补充应用案例与风险资料；不代表具体公司投资额已取得。见 [FINANCIAL_AI_SOURCES.md](FINANCIAL_AI_SOURCES.md)。
