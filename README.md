# feishu-codex-bridge

飞书机器人 ⇄ 本机 Codex CLI 的最小桥。WebSocket 长连接模式，**无需公网 IP**。

```
你在飞书私聊机器人 ──长连接──▶ bridge.py ──▶ codex exec（本机工作区）
        ▲                                        │
        └────────────── 文本回复（≤3800 字自动分条）◀┘
```

## 一、建飞书应用（5 分钟）

1. https://open.feishu.cn/app → 创建**企业自建应用**
2. 「添加应用能力」→ **机器人**
3. 「权限管理」搜并开通：`im:message`（或 `im:message:send_as_bot` + 接收事件）；
   想要「进行中」表情状态再开 `im:message.reaction:write`（消息表情回复）
4. 「事件订阅」→ 订阅方式选 **使用长连接接收事件**（这就是免公网的关键）→ 添加事件 `message.im_v1`（接收消息）
5. 「凭证与基础信息」拿 **App ID / App Secret**
6. 「版本管理与发布」→ 创建版本 → **发布**（企业自建应用走管理员审核，个人版直接可用）
7. 在飞书里搜应用名，给机器人发条消息

## 二、装与跑（Mac 上）

```bash
brew install codex          # 没装的话
codex login                 # 或 codex --api-key 方式，见 codex 文档
cd feishu-codex-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json   # 填 app_id / app_secret / workspace
# 先不连飞书，验证 codex 链路：
.venv/bin/python bridge.py --selftest "用一句话回答：1+1=?"
# 正式跑：
.venv/bin/python bridge.py
```

## 三、白名单

首次启动后私聊机器人任意消息，它会回你的 `open_id`；
填进 `config.json` 的 `allowed_open_ids` 重启。**空名单 = 拒绝所有人**。

## 四、聊天命令

| 消息 | 作用 |
|---|---|
| 任意文本 | 发给 codex（同一私聊默认续同一会话） |
| `/new` | 开新会话 |
| `/whoami` | 查自己的 open_id / chat_id |
| `/status` | 查工作区、flags、当前 codex 会话 |

## 五、安全须知

- 默认沙箱 = codex 默认（读 + 审批）。`codex_flags` 里写 `--full-auto` /
  `--yolo` 等于把这台 Mac 交给能碰到你微信/飞书的人，**三思**。
- 白名单务必只放自己的 open_id。
- 首次 selftest 若报未登录，先在终端 `codex login`。

## 六、常驻（可选）

测试通过后用 launchd 常驻（见 `com.feishu.codex-bridge.plist.example`），
日志写在 `bridge.log`。

## 五、交互细节

- 收到任务：在**你的消息上贴一个表情**（默认 `OnIt`，config 可换 `processing_emoji`）表示执行中，完工自动揭掉
- 结果以**回复**形式贴回你那条消息，不再单发「收到…」
- 沙箱默认只读；要允许写工作区，config 加 `"codex_flags": ["-c", "sandbox_mode=\"workspace-write\""]`
  （注意：`codex exec resume` 不认 `-C`/`--sandbox`，只能走 `-c` 配置覆盖）

## 六、开机自启（macOS launchd）

```bash
cp com.feishu.codex-bridge.plist.example ~/Library/LaunchAgents/com.feishu.codex-bridge.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.feishu.codex-bridge.plist
# 改配置后重启：launchctl kickstart -k gui/$(id -u)/com.feishu.codex-bridge
# 停用：launchctl bootout gui/$(id -u)/com.feishu.codex-bridge
```

## 七、安全边界

- 白名单为空 = 拒绝所有人；只处理文本；只干活于 `workspace` 目录
- macOS 对 Desktop/Documents/Downloads 等目录有 TCC 弹窗授权：人不在电脑前时
  跨目录读取会被拒（这是特性不是 bug）；信任后去「隐私与安全性」里放行
