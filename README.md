# AstrBot 本地与 WebDAV 备份

本地和 WebDAV 都收录下表中的常规项目。每个目标分别设置是否包含 `temp` 目录、执行时间和时区。两种任务都生成 AstrBot 原生 ZIP，可由 AstrBot 的备份页面导入。

## 安装和首次备份

1. 在 AstrBot WebUI 的插件页选择通过仓库地址安装，粘贴 `https://github.com/saitenasuk/astrbot_plugin_webdav_backup`。
2. 打开插件详情页的 **备份仪表盘**，选择「本地」或「WebDAV」。两个目标默认都跳过 `temp`，定时备份默认关闭。
3. 在所选目标下设置「备份 temp 目录」。需要定时执行时打开开关，填写五段 Cron 和 IANA 时区。默认本地每天 02:00，WebDAV 每天 03:00，时区均为 `Asia/Shanghai`。星期可写 `mon` 到 `sun`。
4. 填写 WebDAV 服务地址、账号和远程目录。远程目录默认为 `/`，表示服务根目录。填写完毕后，点击「测试连接」验证当前输入。测试会写入、校验并删除一个小 ZIP。测试通过后点击「保存 WebDAV 设置」，再点击「立即备份到 WebDAV」。在「最近运行」可以查看最终文件的远端地址。

任务在 AstrBot 运行期间执行。两个目标同时触发时会排队，同一目标正在运行时会跳过重复触发。每次生成独立 ZIP，历史记录数只限制仪表盘显示，存储空间按实际保留的 ZIP 累计。

## 备份范围

升级时，原「本地备份项」和「WebDAV备份项」各自对 `temp` 的选择会迁移到对应开关。此前取消勾选的其他项目会随下次备份一并导出。

| 项目 | 包含的内容 |
| --- | --- |
| 主数据库（必选） | 当前 AstrBot 原生导出表中的会话、消息、人格、附件记录等。原生导入以替换模式恢复整份主库。 |
| 配置文件 | `cmd_config.json`、`mcp_server.json` 和 `data/config`。本插件导出的自身配置会移除 WebDAV 密码。 |
| 知识库 | 元数据、文档块、FAISS 向量索引和媒体，与原生备份格式一致。 |
| 附件文件 | 主库附件记录引用且仍存在的本地文件。 |
| 插件本体、插件数据、技能 | 对应 `data/plugins`、`data/plugin_data`、`data/skills`。 |
| WebChat、文生图模板 | `data/webchat` 和 `data/t2i_templates`。 |
| 临时目录 `temp` | `data/temp` 下的临时媒体、下载件、语音等缓存。WebUI 维护页把日志、缓存与追踪列在同一组。日志和追踪库默认位于 `data/logs`。 |

目录遍历跳过符号链接、`.git`、`__pycache__` 和 `.pyc`。运行期间数据库与文件可以继续变化，知识库批量导入等写入任务结束后生成的备份更适合迁移。

[本地与 WebDAV 设置截图](docs/zh/local-backup.md) · [Backup settings guide](docs/en/local-backup.md)

## 本地、WebDAV 与恢复

- **本地**：完成的 ZIP 出现在 `data/backups/`，可在 AstrBot 原生备份列表中查看和下载。
- **WebDAV**：先在本地暂存，流式 `PUT` 到唯一命名的最终 ZIP，再以 `PROPFIND` 查询该文件。若文件的属性响应缺少可用的路径或大小，会在父目录列表中核对。只有路径和字节数都匹配才记为成功。服务器需支持上述两个方法并返回 `getcontentlength`，连接测试还需要 `DELETE` 权限。请在「最近运行」显示校验成功后使用该 ZIP。上传中断或校验失败时，远端可能保留未完成的 ZIP。运行时要给本地留出一个 ZIP 的空间。建议使用 HTTPS，也可以显式启用可信内网 HTTP。
- **恢复**：从远端取回 ZIP，使用原生页面上传，或用 SFTP 等方式将完整文件放到 `data/backups/`。在原生备份列表中选择对应文件并按版本提示导入。原生导入会替换主库及包中包含的目录，先核对目标实例并备份现有数据。
- **凭据**：支持用环境变量 `ASTRBOT_WEBDAV_PASSWORD` 提供应用密码，优先于配置值。ZIP 未加密，主库、其他插件配置和主配置可能含凭据及私人消息，请限制备份目录的访问权限。恢复包含本插件配置的 ZIP 后，请重新填写应用密码或设置环境变量。

## 兼容性和开发

已在 AstrBot 4.29.3 和 4.30.0-beta.1 上验证原生导入。仪表盘采用官方 `plugin_page`、`astrbot.api.web` 和 `AstrBotPluginView` Bridge，页面资源随插件一起安装。

安装 AstrBot 及插件依赖后，在插件目录运行 `python -m unittest discover -s tests -v`。测试使用临时数据库和本地 WebDAV 测试服务。

许可证为 AGPL-3.0。参见 [LICENSE](LICENSE)。
