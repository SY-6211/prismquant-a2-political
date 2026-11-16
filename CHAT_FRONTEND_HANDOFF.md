# 人物问答：前端任务与后端接口交接

本文件说明直接聊天入口、模型设置与额度管理的责任边界。现有能力与待开发功能分别列出；界面完成不等于尚缺的后端接口已经接通。

## 分工与当前状态

| 项目 | 前端工作 | 已具备的后端能力 | 仍需补充的后端或平台工作 |
| --- | --- | --- | --- |
| 直接聊天入口 | 选择人物后直接输入问题；首次发送自动创建会话；展示回答、引用、追问和历史 | 用户身份、创建会话、保存问题、准备资料、明确执行、读取结果与历史均有接口 | 对接正式平台身份；无需另建一套账号密码页面 |
| 模型设置 | 设计设置页面、模型名称及配置状态展示 | 当前服务端通过OpenRouter使用固定的deepseek/deepseek-v3.2；历史记录包含model_id | 网页提交Key、切换平台或模型、验证配置、按用户保存及撤销Key等接口尚未实现；需要后端另行开发 |
| 额度状态 | 展示剩余金额、剩余次数、到期时间；额度不足时明确提示 | 已有本人额度查询、服务端预留与费用核算、次数及到期限制 | 无普通用户网页自助开额度接口；当前由本机管理员命令设置和关闭 |
| 网页额度设置 | 设计金额、次数、有效期的配置与确认交互 | 现有管理员命令可创建有限额度 | 需要后端提供经过权限校验的授权、关闭及审计接口；前端不能直接修改余额 |
| 登录整合 | 复用总平台会话，避免要求最终用户导入测试凭证 | 模块已有所有者隔离和Bearer用户令牌校验 | 平台服务端验证登录、建立用户映射、代理或签发模块凭证；需平台后端协作 |

因此，聊天入口可以先复用已有API；模型设置和自助额度管理必须前后端协作。当前整合包不宣称支持网页粘贴Key后立即切换任意模型。

## 直接聊天的接口顺序

公开人物目录：`GET /api/v1/people`。下列私有路径均以前缀 `/api/v1/agent-chat` 开始，使用模块用户的Bearer凭证，不能使用模型Key或前端自报的user_id。

1. `GET /me` 确认身份。
2. 用户首次发送时，`POST /conversations`，包含独立的 `client_request_id`、`person_id` 与 `title`；标题可由第一条问题截取。后续追问复用conversation_id。
3. `POST /conversations/{id}/messages`，提交 `client_request_id` 与 `content`，取得message_id。
4. `POST /conversations/{id}/answers`，提交另一独立 `client_request_id`、message_id，推荐 `retrieval_mode=hybrid`、`research_mode=auto`、`use_history=true`、`read_official_sources=true`、`limit=6`。
5. 如果准备结果已经completed或abstained，直接显示，不再请求生成。例如官方来源导航可直接产生不调用模型的结果。
6. 结果为prepared时，读取 `GET /generation-budget`。在用户明确发送且can_generate为true的情况下，调用一次 `POST /conversations/{id}/answers/{run_id}/execute`；若没有额度，保留已保存的问题和资料，并显示待生成状态。
7. 生成接口返回202及任务状态，不直接返回完整回答。通过 `GET /conversations/{id}/answers/{run_id}` 只读刷新结果。执行中、响应未知或费用待核对时不能自动重复生成。
8. 分页历史使用 `GET /conversations`、`GET /conversations/{id}/messages`、`GET /conversations/{id}/answers`。后续追问重复步骤3至7。

每个写步骤生成自己的UUID，网络结果不确定时保留原标识核对，不能每次点击重试都创建新的标识。会话标题、模型回答与来源正文按文本呈现。

## 回答呈现

- `answer_mode=synthesis`：先展示direct_answer_zh，再展示analysis；points为人物原文所述，background_points为其他来源所述，两类引用分别校验。
- `answer_mode=source_navigation`：展示来源入口、实际读取状态和步骤；不将目录链接或搜索片段标为已核验数据。
- 兼容早期research模式和旧回答版本，不用新生成覆盖旧记录。
- 公司披露保留issuer_type与reporting_period；报告期不能替代发布日期。有限片段中未读到某项数字，不代表全文或其他渠道从未披露。

已有传输与显示实现位于frontend/chat-api.js、frontend/chat.js；完整API结构见openapi.json，综合研究约束见RESEARCH_SYNTHESIS.md及RESEARCH_FOCUS.md。

## 当前独立测试页面的操作

先按README.md与DEPLOYMENT.md完成数据库、迁移、模型配置及服务启动。在模块目录创建自己的测试身份：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m app.chat_admin create-user --name research-user --credential-file .runtime/chat-credentials/local-user.json
```

保存命令返回的user_id。进入 `/monitor/` 的“人物问答”，点击“选择文件”，选择上述JSON，导入后选择人物并创建会话。该文件是会话身份凭证；模型Key保存在服务端.env的PQ_OPENROUTER_API_KEY中。

已存在的凭证文件不会覆盖；部署到新环境需创建各自身份。刷新页面后需重新导入，历史记录仍保存在数据库。交付包不包含原部署环境的聊天记录、采集数据库、Key或个人凭证。

## 当前额度管理方式

以下为本机管理员操作模板，不会因交付或阅读本文件而自动执行。设置前明确实际预算、次数和有效期，approval填写本次授权的唯一说明：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m app.web_qa_admin summary --user-id <USER_UUID>
.\.venv\Scripts\python.exe -X utf8 -m app.web_qa_admin grant --user-id <USER_UUID> --approval <UNIQUE_APPROVAL> --cap-usd 0.10 --max-requests 5 --hours 2 --approved
.\.venv\Scripts\python.exe -X utf8 -m app.web_qa_admin close --grant-id <GRANT_UUID>
```

替换尖括号占位内容后再运行。额度状态可在网页点击“更新额度”刷新。模型服务商的账户余额与模块本轮额度是两个概念，API Key不自动授予模块生成权限。

网页自助管理上线时，应先定义哪些平台角色可以授权费用，后端完成身份校验、上限、审计与撤销；不能仅靠前端隐藏按钮控制权限。

## 交接验收

直接聊天应验证首次自动创建会话、追问保持人物及行业、两名用户历史隔离、来源与推断分开、额度耗尽与过期、重复点击仅提交一次，以及刷新只读恢复。网页模型切换与自助额度设置在对应后端接口完成前应标为待开发，不应显示为已经可用。
