# WeChat AutoChat

微信 AI 自动回复机器人。监听微信新消息通知，使用 AI 自动回复，支持多模型切换、不同好友不同语气、联网搜索、Token 用量自动切换。

## 功能

- 监听微信新消息通知，自动 AI 回复
- 桌面 GUI 程序，系统托盘后台运行，支持单实例
- 支持多个 API 预设，运行时一键切换，Token 超限自动切备用
- 针对不同好友配置不同回复语气
- 联网搜索（可选），消息含链接或查实时信息时自动搜
- 上下文持久化，重启程序保留聊天记忆（最多10条）
- 免 Python 环境，下载即用

## 下载与使用

### 方式一：GUI 版本（推荐）

下载整个项目仓库，双击 `WeChatAutoChat.exe` 运行。

> exe 已内置 `wxauto4`、`openai`、`pystray` 等所有依赖，不需要安装 Python 环境。

**使用步骤：**
1. 安装微信 PC 版并登录
2. 双击 `WeChatAutoChat.exe` 启动
3. 在程序界面的"API 设置"中填入你的 API Key、Base URL 和模型名称，点击"保存"
4. 点击"修改"（回复对象），输入要自动回复的好友昵称
5. 程序会自动开始监听，收到新消息即自动回复

> 注意：只下载 exe 文件无法运行，必须连同 `config.json` 和 `prompts/` 目录一起下载。

### 方式二：命令行版本（需 Python 环境）

```bash
# 先安装依赖
pip install wxauto4 openai

# 再运行
python wechat_auto_reply.py
```

## 界面功能

| 按键 | 功能 |
|---|---|
| `▶ 开始监听` | 开始监控微信新消息 |
| `■ 停止监听` | 暂停监控 |
| 模型下拉框 + `切换` | 在多个 API 之间切换 |
| `新增/删除` | 管理 API 配置 |
| API 设置 + `保存` | 修改当前模型的 Key / URL / 模型名 |
| `修改`（回复对象） | 设置要自动回复的好友名单 |
| 语气编辑框 + `保存` | 自定义 AI 回复风格 |

## 运行时指令（仅命令行版本）

| 按键 | 功能 |
|---|---|
| `1` | 开始监听 |
| `2` | 停止监听 |
| `d` | 切换到 DeepSeek |
| `a` | 切换到 Agnes |
| `h` | 显示帮助 |
| `q` | 退出程序 |

## 配置说明

编辑 `config.json`：

| 字段 | 说明 |
|---|---|
| `system_prompt` | AI 回复语气设定 |
| `auto_reply_friends` | 要自动回复的好友昵称列表 |
| `friend_prompts` | 针对特定好友的语气覆盖 |
| `blacklist` | 黑名单，永不回复 |
| `api_presets` | API 配置列表 |
| `api_presets[].api_key` | **必须填写**，你的 API Key |
| `token_budget` | 默认模型 Token 上限，超出自动切备用 |

### 最少配置

```json
{
    "api_presets": [
        {
            "name": "Agnes",
            "api_key": "sk-你的API密钥",
            "api_base_url": "https://apihub.agnes-ai.com/v1",
            "api_model": "agnes-2.0-flash"
        }
    ],
    "auto_reply_friends": ["好友昵称1", "好友昵称2"]
}
```

## 推荐接口

| 接口 | 模型 | 特点 |
|---|---|---|
| [Agnes](https://agnes-ai.com) | `agnes-2.0-flash` | **免费**，回复较慢，适合日常聊天 |
| [DeepSeek](https://platform.deepseek.com/api_keys) | `deepseek-v4-flash` | Token 消耗极小，价格极低，**速度快** |

两者不冲突，`config.json` 里可同时配置，运行中一键切换。

## 联网搜索（可选功能）

脚本支持通过 MCP 工具 `websearch-deepseek` 进行联网搜索，消息含链接或搜索意图时自动触发。

**安装依赖：**

```bash
# 安装 Node.js（如已装则跳过）
# 下载：https://nodejs.org/

# 全局安装搜索工具
npm install -g websearch-deepseek
```

不安装不影响 AI 回复功能，仅无法联网搜索。

## 系统要求

- Windows 10 / 11
- 微信 PC 版（推荐 [4.1.8.107](https://github.com/SiverKing/wechat4.0-windows-versions/releases/tag/v4.1.8.107)）
- Python 3.8+（仅命令行版本需要）
- Node.js（仅联网搜索需要）

## 注意事项

- 只回复文本消息，自动跳过图片、视频、表情包、语音
- 首次使用建议先给自己发条测试消息确认运行正常
- 日志保存在 `logs/` 目录
- 程序第二次双击会自动切换到已有窗口，不会重复启动
