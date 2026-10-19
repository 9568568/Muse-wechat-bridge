# 微信消息桥（wechat-bridge）

基于 [Tencent/openclaw-weixin](https://github.com/tencent/openclaw-weixin) 协议的微信私聊桥接工具。

**实现的效果**：微信私聊收到文字消息 → 本地 `serve` 服务长轮询收消息、写入 `inbox/` → 调度器扫描新消息并派发具备工具能力的执行者（agent）实际执行任务 → 执行结果写入 `outbox/` → 经 `wx_bridge.py send` 回传微信 → 状态置为 `sent`。

本 README 面向**另一个 agent**：照着下面的步骤走，能顺利安装、登录、运行、收发消息。

---

## 1. 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Linux / macOS（需常驻进程；Windows 可跑但推荐 Linux） |
| Python | 3.9+（已在 3.12 实测通过） |
| 网络 | 能访问 `https://ilinkai.weixin.qq.com` 及微信 CDN（`*.cdn.weixin.qq.com`） |
| 依赖 | 见 `requirements.txt`：`qrcode[pil]`（扫码登录用）、`pycryptodome`（发送图片用） |

核心功能（扫码登录的二维码生成、图片发送除外）**只依赖 Python 标准库**，无依赖即可运行。

## 2. 安装

```bash
# 2.1 克隆项目
git clone https://github.com/9568568/Muse-wechat-bridge.git
cd Muse-wechat-bridge

# 2.2（可选）创建虚拟环境
python3 -m venv .venv && source .venv/bin/activate

# 2.3 安装依赖
pip install -r requirements.txt
```

## 3. 项目结构

```
wechat-bridge/
├── wx_bridge.py        # 主程序：login / wait-login / serve / send / send-image / status
├── dispatcher.py       # 任务调度器：扫描 inbox/ 中 status=received 的消息并标记派发
├── requirements.txt    # 依赖列表
├── examples/
│   ├── wechat-bridge.service   # systemd 常驻 serve 服务模板
│   └── crontab.example         # cron 调度示例
├── state/              # 登录态：session.json（600 权限）、cursor.txt、二维码
├── inbox/              # 收到的消息，每条一个 <message_id>.json
├── outbox/             # 回复记录，每条一个 <message_id>.json
└── logs/               # 运行日志（bridge.log 等）
```

**注意**：`state/`、`inbox/`、`outbox/`、`logs/` 由 `.gitignore` 排除，绝不提交到仓库（内含登录凭据、私聊记录、会话 token）。

## 4. 配置与登录（扫码）

本项目**没有配置文件**。全部状态保存在项目目录下的 `state/` 中。

```bash
# 4.1 生成登录二维码（需要依赖 qrcode[pil]）
python3 wx_bridge.py login
# 输出类似：二维码已生成: .../state/qrcode.png

# 4.2 把 state/qrcode.png 展示出来（在支持图片的终端/界面中打开该文件），
#     用绑定的微信账号扫码确认。

# 4.3 轮询扫码状态（最长 5 分钟）
python3 wx_bridge.py wait-login
```

扫码成功后，凭据会**原子写入** `state/session.json`（文件权限自动设为 600），包含：

- `bot_token`：接口鉴权令牌（核心凭据，切勿外泄）
- `ilink_bot_id`：机器人 ID
- `ilink_user_id`：绑定的微信用户 ID（只收发该用户的私聊）
- `baseurl`：服务端地址

> 扫码状态说明：`wait`（待扫）、`scaned`（已扫待确认）、`confirmed`（成功）、`expired`（二维码过期，需重新 login）、`need_verifycode`（需在手机微信按提示完成验证后继续等待）、`verify_code_blocked`（验证失败次数过多，需重新 login）。

## 5. 启动收消息服务（常驻）

```bash
# 简单后台方式
nohup python3 wx_bridge.py serve > logs/serve.log 2>&1 &

# 推荐方式：systemd（生产/长期运行）
# 1) 复制 examples/wechat-bridge.service 到 /etc/systemd/system/（或 ~/.config/systemd/user/）
# 2) 把文件里的 User、WorkingDirectory、路径换成实际值
# 3) sudo systemctl daemon-reload && sudo systemctl enable --now wechat-bridge
```

`serve` 做的事情：
1. 用 `state/session.json` 中的 `bot_token` 鉴权，向服务端发 `notifyStart`；
2. 长轮询 `getupdates`，用 `state/cursor.txt` 保存游标（断线续传不丢消息）；
3. **只处理绑定用户（`ilink_user_id`）的私聊消息**，群聊和他人消息一律忽略；
4. 每条新消息按 `message_id` 去重后写入 `inbox/<message_id>.json`，`status` 初始为 `received`。

> 若某次返回 `ret/errcode=-14`，表示会话被服务端暂停 1 小时，程序会自动休眠 1 小时后继续（不是错误）。

## 6. 任务调度（dispatcher）

`dispatcher.py` 负责把收到的消息派发给实际的任务执行者。**重要**：本仓库的 `dispatcher.py` 只做"扫描 + 状态标记 + 持久化去重"，真正的执行派发由运行它的调度主机（例如一个 cron agent）完成——本仓库这样设计，是为了适配"agent 拥有完整工具能力"的调度模型。

### 6.1 调度器运行方式

```bash
# 方式一：cron 每分钟触发（见 examples/crontab.example）
* * * * * /usr/bin/python3 /path/to/wechat-bridge/dispatcher.py >> /path/to/wechat-bridge/logs/dispatcher.log 2>&1

# 方式二：systemd timer / 常驻循环（略）
```

### 6.2 调度协议（调度主机必须实现的逻辑）

调度主机在每次触发时执行以下流程（`dispatcher.py` 的 `dispatch_message()` 注释中也有说明）：

1. 遍历 `inbox/*.json`，读取每条记录的 `status` 字段；
2. 对 `status == "received"` 的消息：
   - 若 `has_non_text == true` 且 `text` 为空（图片/语音等非文字消息）：直接用 `wx_bridge.py send` 回复固定文本 `"这条通道暂时没有配置这类内容处理，请先用文字描述"`，并把该消息 `status` 置为 `sent`；
   - 否则：将该消息 `status` 改写为 `executing`，并派发一个**子执行者（subagent）**，任务描述必须包含：`message_id`、`from_user_id`、文字内容 `text`、`context_token`。明确指示子执行者：
     1. 它拥有完整工具能力，必须**实际调用工具执行**任务（不能只生成看起来像结果的文字）；
     2. 任务完成后，把回复文本写入 `outbox/<message_id>.json`，格式见下节；
     3. 运行 `python3 wx_bridge.py send --to <from_user_id> --text <回复文本> --ctx <context_token>` 把结果发回微信；
     4. 发送成功后，把 `inbox/<message_id>.json` 的 `status` 更新为 `sent`；
3. 同一轮多条消息各自独立派发，不要合并不同指令；
4. 本轮无新消息时不做任何事。

`dispatcher.py` 内部维护 `state/dispatched.json` 做去重：已经派发过的 `message_id` 不会重复派发。

## 7. 数据格式说明

### 7.1 inbox 消息（`inbox/<message_id>.json`）

由 `wx_bridge.py serve` 写入，字段如下：

| 字段 | 类型 | 说明 |
|---|---|---|
| `message_id` | string | 微信消息 ID，文件名即 `<message_id>.json` |
| `from_user_id` | string | 发送者用户 ID（恒等于绑定的 `ilink_user_id`） |
| `text` | string | 文字内容（非文字消息为空） |
| `context_token` | string | 会话 token，`send` 回复时必须原样带回 `--ctx` |
| `session_id` | string | 会话 ID |
| `create_time_ms` | int | 消息创建时间（毫秒时间戳） |
| `has_non_text` | bool | 是否包含非文字内容（图片/语音/视频等） |
| `status` | string | 状态机：`received` → `executing` → `sent` |
| `status_at` | string | 状态更新时间（ISO8601，由调度器写入） |
| `raw` | object | 服务端原始消息报文（调试用） |

状态机说明：`received`（刚收到，待派发）→ `executing`（已派发执行者）→ `sent`（回复已发出）。

### 7.2 outbox 回复（`outbox/<message_id>.json`）

由子执行者写入，发送后更新状态：

```json
{
  "message_id": "7517891720357917960",
  "to": "o9cq804120CxyZZm03JTohf8sRkw@im.wechat",
  "text": "回复给用户的文本",
  "ctx": "<context_token，打码示例：AARzJWAFAAA...实际发送时原样带回>",
  "status": "pending",
  "created_at": "2026-10-19T18:18:00+08:00",
  "sent_at": "2026-10-19T18:18:05+08:00"
}
```

`status` 取值：`pending`（待发送）→ `sent`（已发送）/`failed`（3 次重试后仍失败）。

> **安全红线**：`context_token` 是会话凭据。文档和示例中只放打码/占位符；真实 token、登录态文件、`inbox/` 私聊记录绝不写入日志、记忆文件或 git 仓库。`wx_bridge.py status` 输出已做脱敏处理。

## 8. 命令参考

```bash
python3 wx_bridge.py login        # 生成登录二维码（state/qrcode.png）
python3 wx_bridge.py wait-login   # 轮询扫码状态，最长 5 分钟
python3 wx_bridge.py serve        # 启动收消息服务（常驻）
python3 wx_bridge.py status       # 状态检查（登录态/服务进程/收件箱统计，凭据脱敏输出 JSON）
python3 wx_bridge.py send --to <user_id> --text "文本" --ctx <context_token>
                                 # 发送文字消息（--ctx 缺省也能发，但建议带上）
python3 wx_bridge.py send-image --to <user_id> --file <本地图片> [--text "说明"] --ctx <context_token>
                                 # 发送图片（需 pycryptodome；走 CDN 加密上传流程）
```

`send` / `send-image` 内置 3 次重试，成功返回码 0。

## 9. 完整部署检查清单

按顺序确认，每一步都应成功：

1. `pip install -r requirements.txt` 无报错
2. `python3 wx_bridge.py login` 生成 `state/qrcode.png`
3. 手机微信扫码 → `python3 wx_bridge.py wait-login` 输出"登录成功"
4. `python3 wx_bridge.py status` 显示 `logged_in: true`，`bound_user` 为扫码账号（脱敏）
5. 启动 `serve`，`status` 显示 `serving: true`
6. 用扫码的微信给自己发一条文字消息，`inbox/` 出现新的 `<message_id>.json` 且 `status=received`
7. 运行一次 `dispatcher.py`（或等 cron），消息 `status` 变为 `executing`，子执行者完成后微信收到回复，`status` 变为 `sent`

## 10. 常见问题排查（FAQ）

| 现象 | 原因 / 处理 |
|---|---|
| `login` 报 `ModuleNotFoundError: qrcode` | 依赖没装：`pip install -r requirements.txt`（只需二维码生成用到） |
| `wait-login` 输出 `expired` | 二维码过期（有效期约 5 分钟），重新运行 `login` |
| `wait-login` 输出 `need_verifycode` | 微信风控要求手机端二次验证，在手机微信按提示完成，程序会继续等待 |
| `wait-login` 输出 `verify_code_blocked` | 验证失败次数过多，重新运行 `login` 取新二维码 |
| `wait-login` 5 分钟超时（返回码 4） | 未扫码或网络问题，重新 `login` |
| `serve` 启动报"未登录" | `state/session.json` 不存在或损坏，重新走第 4 节登录流程 |
| `getupdates` 返回 `ret/errcode=-14` | 会话被服务端暂停 1 小时，程序自动休眠后继续，无需人工干预；频繁出现说明轮询过于激进 |
| `getupdates` 连续失败后退出 | 网络不通或服务端地址变更；检查能否访问 `https://ilinkai.weixin.qq.com`，确认无代理劫持 |
| 收到了微信消息但 `inbox/` 没文件 | 检查三点：① `serve` 是否在运行（`status` 看 `serving`）；② 消息是否来自绑定用户私聊（群聊/他人会被忽略）；③ `state/cursor.txt` 游标是否卡住（删掉游标文件会从头拉取，注意去重） |
| `send` 返回码非 0 / `outbox` 状态 `failed` | 检查登录态是否过期（重新登录）、`--ctx` 是否带对、网络是否正常；程序已内置 3 次重试 |
| `send-image` 报 `ModuleNotFoundError: Crypto` | 安装 `pycryptodome`：`pip install pycryptodome` |
| VM/机器重启后服务没了 | `serve` 是前台/后台进程，重启不自启——用 `examples/wechat-bridge.service` 做成 systemd 服务并 `enable` |
| 调度器重复派发同一条消息 | 检查 `state/dispatched.json` 是否被误删；`inbox/<id>.json` 的 `status` 必须在派发时先改为 `executing` |
| `status` 输出的 `inbox_pending` 一直增长 | 调度器（cron）没在跑，或子执行者没把 `status` 置为 `sent`；按第 6 节检查调度链路 |

## 11. 安全注意事项

1. `state/session.json` 包含 `bot_token`（核心凭据），权限固定为 600，原子写入；**绝不**提交到 git（已在 `.gitignore` 中排除）、**绝不**贴到聊天记录、日志、记忆文件或 issue 里。
2. `status` 命令和日志中的用户 ID、token 均已脱敏（`mask()` 只保留首尾各 2 字符）。
3. 只处理扫码绑定用户的私聊：`serve` 中 `from_user_id != ilink_user_id` 或带 `group_id` 的消息直接丢弃，不写入 `inbox/`。
4. 传输走 HTTPS；`Authorization: Bearer <bot_token>` + `AuthorizationType: ilink_bot_token` 双头鉴权（见 `wx_bridge.py` 的 `base_headers()`）。
5. 如怀疑凭据外泄：在手机微信解除该 bot 绑定后重新走第 4 节扫码，旧 `bot_token` 即失效。

## 12. 限制与已知问题

- 目前只处理**文字**私聊消息；图片/语音/文件等非文字消息只回复固定提示文本（见 6.2）。
- `send-image` 已支持（CDN 加密上传流程：`getUploadUrl` → AES-128-ECB 加密上传 → `sendMessage` 图片项），但语音/文件发送暂不支持。
- 微信消息不会自动同步到 Muse 网页主聊天；本项目是独立的"微信私聊 → agent 执行 → 微信回复"通道。
- 运行环境被回收/重启后，`serve` 与调度器需重新拉起（建议 systemd + 健康检查）。
- 本仓库 `dispatcher.py` 的派发动作需要调度主机具备"生成子执行者"的能力（如 cron agent）；它本身不执行任务。

## 13. 协议来源

实现遵循 Tencent/openclaw-weixin 协议文档：`https://github.com/tencent/openclaw-weixin/blob/HEAD/docs/protocol_zh_CN.md`
（二维码登录、getupdates 长轮询、sendMessage、CDN 媒体上传流程均按该文档实现）
