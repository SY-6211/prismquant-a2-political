# 部署与运行

## 环境与配置

支持Windows、Python 3.12和PostgreSQL 18。生产运行管理采用单API进程，避免重复启动内置提醒和本地索引循环。默认仅监听127.0.0.1，由同源代理提供平台访问；本模块未配置公网入口或TLS。

安装依赖及初始化见README.md。运行管理所需模块均在requirements-runtime.lock.txt；开发检查另安装requirements-dev.lock.txt。数据库运行角色只需对业务库具有建表和读写权限，无需超级用户或CREATEDB。

本机.env填写PQ_DB_HOST、PQ_DB_PORT、PQ_DB_NAME、PQ_DB_USER和PQ_DB_PASSWORD。在线模型凭证仅保存在服务端PQ_OPENROUTER_API_KEY。关键词管理员、来源管理员、独立标注员和会话用户使用不同凭证。

## 后台启动

```powershell
.\deploy.ps1 -Action Check
.\deploy.ps1 -Action Start
.\deploy.ps1 -Action Status
```

API监督进程以production/research模式启动子进程，关闭开发热重载，隐藏诊断区并启用运维接口权限检查。API日志轮转保存在.runtime/stack/api.log。已有其他进程占用端口时启动失败，不接管或终止该进程。

API子进程异常退出后按有界退避重新启动；健康探测失败不会直接强杀活进程。监督进程同时检查已明确启用的采集守护，守护异常退出时重新启动；主动关闭的采集不会被自动重开。API监督不负责重新启动PostgreSQL服务或监督进程自身。

```powershell
.\deploy.ps1 -Action Stop
```

该命令停止API监督及其子进程，保留独立采集。加入-Collector可在启动/停止时同时操作采集守护；不会修改来源许可、监控开关或模型额度。

## 内容导入与持续采集

```powershell
.\.venv\Scripts\python.exe -X utf8 -m app.preview_batch --source ecb --limit 3
.\.venv\Scripts\python.exe -X utf8 -m app.import_batch --source ecb --limit 3
```

source可选ecb或trump，limit是尝试上限。已有完整记录跳过，未通过归属或类型检查的内容不强制入库。手动预览与入库不调用模型。

持续采集需先审核来源许可、配置和已有暂停原因，再明确启用来源、人物及关联开关。HTML快发现另有独立控制，使用app.monitor_speed status查看。来源、网页或模型均不会因目录导入自动获准。

```powershell
.\deploy.ps1 -Action Start -Collector
```

启动后核对采集运行记录及app.scheduler_service status。配置允许不等于进程在线；配置间隔不等于发布至页面的延迟。

## 向量检索与问答

```powershell
.\.venv\Scripts\python.exe -X utf8 -m app.knowledge download
.\.venv\Scripts\python.exe -X utf8 -m app.knowledge enable
.\.venv\Scripts\python.exe -X utf8 -m app.knowledge build --limit 300
```

download从公开模型发布源下载固定校验版本，使用本地CPU，不调用付费生成模型。新内容持续索引还需PQ_KNOWLEDGE_INDEXING_ENABLED=true并重启API。没有向量索引时检索可能明确降级，不把降级视为语义检索已完成。

独立测试身份通过app.chat_admin create-user签发，令牌写入私有文件：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m app.chat_admin create-user --name research-user --credential-file .runtime/chat-credentials/local-user.json
```

已有文件不会覆盖。生成令牌不授予模型消费额度。网页生成仍需独立设置金额、次数和有效期；交付脚本不创建额度、不启动持续付费分析。

## 登录时启动（可选）

```powershell
.\deploy.ps1 -Action RegisterStartup
.\deploy.ps1 -Action UnregisterStartup
```

该操作仅为当前Windows用户注册登录触发任务，不保存账户密码，不提升运行权限，不覆盖已有同名任务；加入-Collector可同时启动已审核的采集。默认不安装，本轮未执行跨重启验收。关机、休眠或注销期间不能采集，无法替代常开服务器。

## 备份与恢复验证

```powershell
.\.venv\Scripts\python.exe -X utf8 -m app.backup_database create
.\.venv\Scripts\python.exe -X utf8 -m app.restore_check --out .runtime/restore-report.json
```

restore_check创建备份，并用随机密码和空闲回环端口初始化独立PostgreSQL实例，恢复全部归档并检查迁移及业务样本。通过后停止并清理临时实例，不恢复或覆盖源数据库，不要求运行角色具备CREATEDB权限。已有输出文件拒绝覆盖；清理失败时报告cleanup_needs_review，不能当作完整通过。

本机自动备份不能防止设备丢失或磁盘损坏；异地保管和目标服务器实际恢复需在部署环境另行验收。
