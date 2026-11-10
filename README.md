# 微信群 AI 自动回复机器人（Windows）

一个面向 Windows 微信桌面端的示例机器人：监听指定群聊（以及可选的私聊），把新消息交给 OpenAI 兼容接口的模型，按 `persona.txt` 里的人格生成回复，再通过桌面界面把文字、表情包或语音发回去。

除了纯文字回复，当前版本还包含：

- **多会话监听**：目标群 + 白名单私聊，统一按同一套人格回复。
- **表情包**：模型在回复里写 `[[sticker:分类]]`，机器人从对应分类随机发一张真实图片，标记本身不进正文。
- **收图识图**：收到的表情包会从微信 CDN 回源校验 md5，照片解密后一并交给支持视觉的模型。
- **真·语音气泡**：需要语音时把文字合成为微信语音消息（走 WhaleGirl + VB-CABLE 虚拟声卡链路），失败自动回退文字。
- **热更新配置**：`config.json` 的开关、`persona.txt` 的设定、贴纸图库的增删都在运行时重新加载。

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
4. 右键 `设置密钥.ps1`，选择“使用 PowerShell 运行”，在提示中输入 API Key。密钥会使用 Windows DPAPI 加密保存在本机的 `secret.txt`。
5. 在微信中打开目标群，确认群聊在会话列表第一位、输入框没有草稿，再双击 `启动后台.vbs`。
6. 双击 `停止后台.vbs` 停止机器人。运行记录在 `bot.log`，错误在 `error.log`。

## 触发方式

- `all`：回复启动后目标会话收到的每条新文字消息。
- `mention`：只回复 @机器人群昵称的消息，并在 `bot_name` 填写昵称。
- `prefix`：只回复以 `prefix` 配置值开头的消息。

修改 `persona.txt` 后，新的设定会用于下一条消息。修改 `config.json` 里的开关类参数后立即生效；`trigger`、群名、模型等启动参数需要重启。

## 可选功能

以下功能在 `config.json` 里开关，模板中默认关闭，按需打开。

### 私聊回复

`private_chats` 填备注名或昵称（精确匹配），例如 `["联系人A", "联系人B"]`。留空表示不回复任何私聊。`private_trigger` 可设为 `inherit`（跟随群聊的触发方式）。

### 表情包

```json
"sticker": {
  "enabled": true,
  "dir": "D:\\wechat-bot\\stickers",
  "chance": 0.6,
  "fast": true
}
```

`dir` 指向图库根目录，**一级子目录名就是分类**（例如 `开心/`、`傲娇/`、`炸毛/`），分类名会被写进提示词让模型挑选。`chance` 是每次回复主动配图的概率，`chance_by_chat` 可按会话单独覆盖；用户明确说“发个表情包”时必定发图。`fast=true` 走快速落库路径，失败自动回退标准发送。

### 收图识图

```json
"vision": {
  "enabled": true,
  "allow_network": true,
  "photos": true,
  "reply_on_fail": true
}
```

`allow_network` 控制表情包是否从微信 CDN 回源（只允许 `*.tc.qq.com`，且下载内容必须与消息里的 md5 一致才采纳）。`photos` 控制普通照片是否解密后识别。图片会发送给模型服务商，请自行确认合规性；`reply_on_fail=true` 时读不到图会回一句提示，而不是装没看见。

### 语音回复

```json
"voice": {
  "enabled": true,
  "whalegirl_dir": "D:\\WhaleGirl",
  "chance": 0.2,
  "max_chars": 120,
  "groups": true,
  "private": true
}
```

语音走 WhaleGirl 的 `WhaleLauncher.exe --voice-reply-job` 协议：合成音频 → 播到虚拟声卡 → 在微信窗口按住录音热键 → 松开发送。需要：

- 本机有包含 `WhaleLauncher.exe` 与 `bridge/voice_reply.py` 的 WhaleGirl 目录，路径填进 `whalegirl_dir`（留空则该功能不可用，不会报错崩溃）；
- 已安装 VB-Audio Virtual Cable，默认播放设备为 `CABLE Input`、默认录音设备为 `CABLE Output`；
- 微信「发送语音」快捷键与 WhaleGirl 的 `data/voice-send.json` 中 `RecordingHotkey` 一致。

`chance` 是随机语音概率；对方明确要“发语音”时必定语音。`max_chars` 限制朗读长度。`groups` / `private` 分别控制群聊和私聊是否允许。语音失败会自动回退为文字，不会一声不吭。

## 文件说明

- `bot.py`：消息监听、模型请求，以及表情包 / 识图 / 语音的发送编排。
- `sticker.py`：图库扫描与选图逻辑（纯逻辑，无 GUI、无网络，便于离线测试）。
- `vision.py`：表情包回源校验与照片预处理。
- `voice.py`：真·语音气泡发送，复用 WhaleGirl 的录制链路。
- `config.json`：接口地址、模型与功能开关模板。
- `persona.txt`：角色提示词，可自行修改。
- `设置密钥.ps1`：安全输入并加密保存 API Key。
- `设置发图几率.bat`：交互式修改表情包概率。
- `manager.ps1`、`启动后台.vbs`、`停止后台.vbs`：后台启动和停止脚本。
- `install_env.bat`、`requirements.txt`：Python 环境和依赖安装。

## 安全与已知限制

- 不要把真实 API Key 写进 `config.json`，也不要分享 `secret.txt`。`secret.txt` 只能由创建它的同一个 Windows 用户解密；换电脑或 Windows 账号后需重新运行设置脚本。
- 请勿提交微信数据、运行日志、缓存、虚拟环境或密钥文件。仓库的 `.gitignore` 已排除这些本地文件。
- 电脑、微信和网络必须保持运行。该程序使用第三方微信自动化库，微信更新可能导致程序失效，也可能触发微信平台限制。
- 回复发送依赖桌面界面操作，并要求目标群位于会话列表第一位。它可能切换微信窗口和输入焦点，不能保证后台静默运行或完全不打断键鼠操作。
- 语音功能依赖第三方项目与虚拟声卡，链路比文字长，失败时会有可见的降级行为（回退文字）。
- 仅使用你有权管理的账号和群聊，并遵守微信和模型服务商的条款。

## 依赖

Python 3.11，Windows 10/11，已登录的微信桌面端，以及 `requirements.txt` 中的依赖（`requests`、`wechatauto-replica[guia]`、`pillow`）。若模型服务商不兼容 OpenAI Chat Completions API，请使用其兼容端点或调整 `bot.py` 中的请求适配。语音功能另需 WhaleGirl 与 VB-Audio Virtual Cable，见上文。
