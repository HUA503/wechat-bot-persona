# 微信群 AI 自动回复机器人（Windows）

一个面向 Windows 微信桌面端的示例机器人。配置 OpenAI 兼容接口、模型、群名并在本机输入 API Key 后，可对目标群的新文字消息生成回复。角色设定在 `persona.txt` 中。

## 快速开始

1. 安装 64 位 Python 3.11 和 Windows 微信，并登录准备作为机器人的微信账号。让该账号加入目标群。
2. 编辑 `config.json`：

   ```json
   {
     "base_url": "https://你的接口域名/v1",
     "model": "服务商提供的模型 ID",
     "group_name": "目标群名",
     "bot_name": "机器人在群里的昵称",
     "trigger": "all",
     "prefix": "小鲸",
     "history_messages": 12
   }
   ```

   `base_url` 填 API 根地址，不要附加 `/chat/completions`；程序会自动拼接。接口需要兼容 `POST /chat/completions`。

3. 双击 `install_env.bat` 安装依赖。
4. 在当前 Windows 用户的环境变量中配置 `OLLAMA_CF_ACCESS_CLIENT_ID` 和 `OLLAMA_CF_ACCESS_CLIENT_SECRET`，用于 Cloudflare Access。需要 Bearer API Key 的服务还可运行 `设置密钥.ps1`，将密钥加密保存在本机。设置后重新登录 Windows，使环境变量生效。
5. 在微信中打开目标群，确认群聊在会话列表第一位、输入框没有草稿，再双击 `启动后台.vbs`。
6. 双击 `停止后台.vbs` 停止机器人。运行记录在 `bot.log`，错误在 `error.log`。

## 触发方式

- `all`：回复启动后目标群收到的每条新文字消息。
- `mention`：只回复 @机器人群昵称的消息，并在 `bot_name` 填写昵称。
- `prefix`：只回复以 `prefix` 配置值开头的消息。

修改 `persona.txt` 后，新的设定会用于下一条消息。修改 `config.json` 后需要重启机器人。

## 长期记忆

机器人只会保存成员明确要求记住的内容，不会自动归档群聊。记忆按群和发言者的微信账号隔离；不过这是群聊机器人，命令回复和群内内容对群成员可见，请勿保存不适合公开的信息。可在目标群使用：

- `记住：我喜欢喝无糖拿铁`：保存一条只对当前群内该成员生效的记忆。
- `记忆`：查看自己在这个群保存的内容。
- `忘记：我喜欢喝无糖拿铁`：删除完全匹配的一条。
- `忘记：全部`：清空自己在这个群的记忆。

每人每群最多 30 条、每条 200 字。疑似密码、密钥、验证码、证件号或银行卡信息不会保存。记忆保存在机器人本机 `cache/memory.sqlite3`；生成回复时，当前成员的相关记忆会随请求发给你配置的模型服务，不会自动上传到其他存储服务。不要把数据库文件发给他人，备份机器人目录时也应妥善保护它。

## 文件说明

- `bot.py`：消息监听、模型请求与回复发送。
- `config.json`：接口地址、模型与群聊设置模板。
- `设置密钥.ps1`：安全输入并加密保存 API Key。
- `persona.txt`：角色提示词，可自行修改。
- `memory_store.py`：本地 SQLite 长期记忆，按群和成员隔离。
- `manager.ps1`、`启动后台.vbs`、`停止后台.vbs`：后台启动和停止脚本。
- `install_env.bat`、`requirements.txt`：Python 环境和依赖安装。

## 安全与已知限制

- 不要把真实 API Key 写进 `config.json`，也不要分享 `secret.txt`。`secret.txt` 只能由创建它的同一个 Windows 用户解密；换电脑或 Windows 账号后需重新运行设置脚本。
- 请勿提交微信数据、运行日志、缓存、虚拟环境或密钥文件。仓库的 `.gitignore` 已排除这些本地文件。
- 电脑、微信和网络必须保持运行。该程序使用第三方微信自动化库，微信更新可能导致程序失效，也可能触发微信平台限制。
- 回复发送依赖桌面界面操作，并要求目标群位于会话列表第一位。它可能切换微信窗口和输入焦点，不能保证后台静默运行或完全不打断键鼠操作。
- 仅使用你有权管理的账号和群聊，并遵守微信和模型服务商的条款。

## 依赖

Python 3.11，Windows 10/11，已登录的微信桌面端，以及 `requirements.txt` 中的依赖。若模型服务商不兼容 OpenAI Chat Completions API，请使用其兼容端点或调整 `bot.py` 中的请求适配。

