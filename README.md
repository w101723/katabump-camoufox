# Katabump Auto Renew - Camoufox Edition

这是 `katabump` 的 Camoufox 精简版，面向 GitHub Actions 自动续期场景。

## 设计

```text
GitHub Actions
    ↓
Python 3.11
    ↓
Camoufox
    ↓
Playwright
    ↓
Katabump Dashboard
```

本版本不再使用：

- Google Chrome
- Chrome DevTools Protocol / 9222 调试端口
- Node.js / playwright-extra
- puppeteer-extra-plugin-stealth
- sing-box
- PROXY_URL / HTTP_PROXY
- `proxy_handler.py`
- 0~3 小时随机 sleep

保留：

- 多账号
- 登录
- Turnstile 检测与 Playwright 原生点击
- ALTCHA 状态检测与点击
- Renew 流程
- 截图 Artifact
- Telegram 可选通知
- 账号级独立 Browser Context

## GitHub Actions 使用

### 1. 配置 `USERS_JSON`

仓库进入：

`Settings -> Secrets and variables -> Actions -> New repository secret`

名称：

```text
USERS_JSON
```

值示例：

```json
[{"username":"your_email@example.com","password":"your_password"}]
```

多个账号：

```json
[
  {"username":"user1@example.com","password":"password1"},
  {"username":"user2@example.com","password":"password2"}
]
```

建议 Secret 中压成一行。

### 2. Telegram（可选）

可配置：

```text
TG_BOT_TOKEN
TG_CHAT_ID
```

不配置不会影响续期。

### 3. 运行

进入 GitHub 仓库：

`Actions -> Katabump Auto Renew (Camoufox) -> Run workflow`

Workflow 默认每天 UTC 00:00 自动执行。

截图会上传到本次 Workflow Run 的 `screenshots` Artifact。

## 本地运行

要求 Python 3.10+。

### 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m camoufox fetch
```

Linux 无桌面环境需要 Xvfb：

```bash
sudo apt-get install -y xvfb
```

### 配置账号

```bash
cp login.json.template login.json
```

编辑 `login.json`。

### 执行

```bash
python action_renew.py
```

本地默认使用可见浏览器。如果希望 headless：

```bash
CAMOUFOX_HEADLESS=true python action_renew.py
```

Linux 使用虚拟显示：

```bash
CAMOUFOX_HEADLESS=virtual python action_renew.py
```

## 环境变量

| 变量 | 必需 | 默认 | 说明 |
|---|---:|---|---|
| `USERS_JSON` | Actions 必需 | - | 账号 JSON 数组 |
| `TG_BOT_TOKEN` | 否 | - | Telegram Bot Token |
| `TG_CHAT_ID` | 否 | - | Telegram Chat ID |
| `CAMOUFOX_HEADLESS` | 否 | Actions=`virtual` | `true` / `false` / `virtual` |
| `MAX_RENEW_ATTEMPTS` | 否 | `8` | Renew 最大重试次数 |
| `DEFAULT_TIMEOUT_MS` | 否 | `60000` | Playwright 默认超时 |

## 结果状态

脚本按账号输出三种状态：

- `success`：续期成功
- `skipped`：当前无需/不能续期，例如尚未到时间
- `failed`：登录、服务器定位、Captcha 或续期流程最终失败

只要存在 `failed`，GitHub Actions 会以非零退出码结束，方便直接从 Actions 页面发现异常。

## 项目结构

```text
katabump-camoufox/
├── .github/
│   └── workflows/
│       └── renew.yml
├── .gitignore
├── action_renew.py
├── login.json.template
├── requirements.txt
└── README.md
```

## 从旧版迁移

旧仓库中的以下文件不再需要，可以删除：

```text
action_renew.js
renew.js
package.json
proxy_handler.py
start_chrome.bat
```

旧 Secrets 中以下项也不再使用：

```text
PROXY_URL
HTTP_PROXY
```

只保留 `USERS_JSON`，以及可选的 `TG_BOT_TOKEN` / `TG_CHAT_ID`。
