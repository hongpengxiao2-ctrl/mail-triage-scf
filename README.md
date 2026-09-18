# mail-triage-scf —— 邮箱 7×24 自动巡检

在腾讯云函数（SCF）上 7×24 运行：自动读取邮箱收件箱，**把广告、垃圾邮件和已失效的二维码邮件移入回收站**（不永久删除，可恢复），重要邮件立刻推送企业微信，其余邮件写入云端暂存区，由每日 10:00 的日报任务汇总成中文摘要推送。

> 云函数名为 `qq-mail-triage`，仓库名为 `mail-triage-scf`。本项目对任意 IMAP 服务商通用，
> 名字里的 `qq` 属于早期命名，不代表只支持 QQ 邮箱。

**纯 Python 标准库 + 少量依赖实现，无需服务器**：`imaplib` 收信、MIME 解析、加权计分分类、COS 状态存储、企业微信推送，全部跑在腾讯云函数上，本地只做部署与日报生成。

## 项目亮点

| 亮点 | 说明 |
| --- | --- |
| **无服务器 7×24** | 部署在腾讯云 SCF，每 5 分钟触发一次，不依赖本机开机 |
| **不硬编码密钥** | 所有凭证走环境变量；本机专属配置放 gitignore 的 `.env`，仓库零明文密钥 |
| **破坏性操作可恢复** | 只移入回收站（`UID MOVE`），绝不永久删除；找不到回收站则整体不动 |
| **绝不改已读状态** | 读取统一用 `BODY.PEEK`，不会把未读邮件悄悄标成已读 |
| **演练模式** | `DRY_RUN=1` 只判定不动作，且**不写去重状态**，避免正式运行时把邮件误判为「已处理」 |
| **防批量误伤** | 单轮处理量与移入回收站数量双上限；回收站定位失败则不退化为删除 |
| **加权计分分类** | 多信号累加（广告词/运营词/发件人特征/批量头/退订指引/`Precedence`），普通域与免费邮箱门槛不同 |
| **组合判定识别平台营销** | 「批量发件人特征 + 广告或退订特征」即可判定，覆盖主题不露声色的平台推送；并用强重要词护栏防止误杀账号安全邮件 |
| **一次性凭证自动清理** | 验证码（关键词 + 邻近数字）与验证链接邮件过期即清，避免收件箱被一次性码堆满 |
| **多邮箱合并日报** | `MAIL_ACCOUNTS` 一次巡检多个邮箱，状态按账号隔离，单账号失败不影响其他账号 |
| **跨国链路可靠** | 显式连接超时 + 带退避重试 + 按账号分配时间预算，覆盖「国内连 Gmail 约一半概率首次超时」的抖动 |
| **清理可审计** | 每日日报单列「已清理」段落，列出被移入回收站的邮件与判定原因 |
| **双信号二维码判定** | 必须「含图片」且「命中关键词」才判过期，避免把账单、验证码误删 |
| **硬保护域** | `edu.cn` / `gov.cn` / 银行 / 12306 / 支付宝等命中即永不清理 |
| **推送去重** | 同一「发件人+主题」在窗口内只推一次，杜绝同一告警连发 9 封刷屏 |
| **零日志可调试** | 未开通 CLS 时控制台无日志，因此把诊断信息全部编码进函数返回值 |
| **可复用诊断工具** | `diagnose.py` 分层定位 DNS/TCP/TLS/认证故障；`--probe` 支持参数覆盖，一次部署比对多组凭证 |
| **有测试** | 17 条分类规则样本 + 6 个去重场景，均为不连邮箱的离线单测 |

## 接入的邮箱

本项目对**任意 IMAP 服务商**通用，服务商由 `IMAP_HOST` 决定（如 `imap.qq.com`、`imap.163.com`）。

> ⚠️ **账号必须与服务器匹配**，否则报错会把人带偏：拿 `@163.com` 账号去连 `imap.qq.com`
> 会得到「Account is abnormal, service is not open, password is incorrect…」，
> 而拿 `@qq.com` 账号去连 `imap.163.com` 会得到「Login error or password error」——
> 两者看起来都像「密码错」，实际是**连错了服务器**。
>
> 服务商可从登录横幅辨认：`Coremail System` = 网易系，`QQMail XMIMAP4Server` = 腾讯系。
> **但只有在账号正确的前提下，横幅才有诊断意义。**

**授权码格式各家不同，不要互相套用**：

| 服务商 | 授权码格式 | 备注 |
| --- | --- | --- |
| QQ 邮箱 | 16 位**纯字母** | 服务默认关闭，须手动开启 POP3/IMAP/SMTP |
| 网易 163 | 16 位**大写字母**（可能含数字） | 认证后必须发 `ID` 命令，否则报 `Unsafe Login`（本项目自动处理） |

授权码均**只显示一次**，与登录密码不同；重置授权码或修改密码会让旧码立即失效。

## 推送双通道

| 通道 | 环境变量 | 用途 |
| --- | --- | --- |
| 告警 | `WECOM_WEBHOOK` | 即时重要邮件提醒 |
| 日报 | `WECOM_WEBHOOK_REPORT` | 每日 10:00 摘要日报（独立机器人） |

`WECOM_WEBHOOK_REPORT` 留空时**自动回退到 `WECOM_WEBHOOK`**，不会静默丢消息。可用 `--channels` 查看当前生效状态。

## 多邮箱支持

用 `MAIL_ACCOUNTS` 可让一次巡检覆盖**多个邮箱**，结果合并到同一份日报：

```
MAIL_ACCOUNTS=QQ|me@qq.com|授权码|imap.qq.com;Gmail|me@gmail.com|应用专用密码|imap.gmail.com|993|0|[Gmail]/Trash
```

分号分隔记录，字段用 `|` 分隔，字段顺序为
`名称|邮箱地址|授权码|IMAP服务器|端口|是否发ID命令|回收站文件夹`，后四项可省略。
未配置 `MAIL_ACCOUNTS` 时自动回退到单账号环境变量（向后兼容）。

- **状态按账号隔离**：`state.handled = {账号名: [uid...]}`，账号之间不会互相干扰；
  旧版扁平结构会在首次运行时自动迁移到第一个账号名下。
- **单账号失败不影响其他账号**：每个账号独立连接、独立报错，返回体里逐账号给出明细。
- **Gmail 注意事项**：
  - 认证需要 **应用专用密码**（Google 账号需先开启两步验证：账号 → 安全性 → 应用专用密码），
    不能使用登录密码；
  - 需在 Gmail 设置里启用 IMAP；
  - 回收站不是默认命名，**必须显式指定** `[Gmail]/Trash`（本项目已内置该名称的自动识别兜底，
    但显式配置更稳妥）；
  - 只扫描 `INBOX`，不会触碰已归档的 `[Gmail]/All Mail`；
  - **链路不稳定，务必依赖重试**：实测国内云服务器连 `imap.gmail.com` **约一半概率首次超时**
    （可达性没问题，是抖动）。因此 `IMAP_RETRIES`（默认 3）与 `IMAP_TIMEOUT`（默认 20 秒）
    是 Gmail 场景的必需配置，不是可选优化。

## 连接可靠性

多账号场景下最容易踩的坑是**一个账号卡死拖垮全部**。本项目的三条约束：

1. **显式连接超时**：`imaplib.IMAP4_SSL` 默认**没有超时**，一次卡住会一路阻塞到云函数
   被强杀。`IMAP_TIMEOUT` 强制每次尝试都有上限。
2. **带退避的重试**：`IMAP_RETRIES` 次尝试，每次失败后 `1.5s / 3s` 退避。对跨国链路
   （国内 → Gmail）这种「约一半概率首次超时」的抖动，重试把整体失败率降到约 12%，
   配合 5 分钟一轮的调度，实际不会漏处理。
3. **按账号分配时间预算**：每个账号拿到 `(FUNC_TIMEOUT_S - 8) / 账号数` 的预算
   （可用 `ACCOUNT_BUDGET_S` 覆盖）。每次尝试的超时取「基础超时」与「剩余预算」的较小值，
   预算耗尽即放弃该账号——**其余账号照常完成**，失败账号在下一轮（5 分钟后）自然重试。

状态按账号分区，因此某个账号失败**不会影响其他账号的去重进度**。

## 运行架构

```
腾讯云函数 qq-mail-triage（ap-guangzhou · Python3.10 · 每 5 分钟）
  ├─ 遍历 MAIL_ACCOUNTS 中的每个邮箱（QQ / Gmail / …）
  │    ├─ IMAP 读取收件箱近 7 天邮件
  │    ├─ 四分类：SPAM / CODE_EXPIRED / QR_EXPIRED → 移入该账号的回收站
  │    │            IMPORTANT → 立刻推送「告警机器人」
  │    │            NORMAL    → 写入 COS 日报暂存
  │    └─ 去重状态按账号分区写入 COS
  │
WorkBuddy 每日 10:00 ─┴─→ 读取暂存（保留 + 已清理）→ 生成中文摘要日报
                            → 推送「日报机器人」→ 清空暂存
```

四类标签：`SPAM`（广告营销）、`CODE_EXPIRED`（失效验证码/验证链接）、
`QR_EXPIRED`（失效二维码）、`IMPORTANT`、`NORMAL`（保留）。

本函数与既有函数**完全独立**，只共用一个 COS 存储桶（键名不同），互不影响。
若希望从某个既有函数继承企业微信 Webhook 与 COS 配置，设置环境变量
`SOURCE_FUNCTION=<函数名>` 即可，无需在多处重复维护同一份配置。

## 文件结构

| 文件 | 说明 |
| --- | --- |
| `scf/handler.py` | 全部业务逻辑。同时是云函数入口与本地运行入口 |
| `scf/deploy_scf.py` | 部署 / 状态查询 / 演练调用 / 连接诊断 / 读取与清空暂存 / 双通道推送 |
| `scf/build_scf.py` | 打包 `qq-mail-triage-scf-deploy.zip` |
| `scf/.env.example` | 本地运行配置模板（复制为 `.env`） |
| `diagnose.py` | 邮箱链路诊断：分层定位 DNS / TCP / TLS / 认证故障 |
| `show_digest.py` | 按账号分组查看云端日报暂存区（保留 + 已清理） |
| `test_rules.py` | 判定规则单元测试（17 条样本，不连邮箱） |
| `test_dedup.py` | 重要邮件去重逻辑单元测试（6 个场景，不连邮箱） |
| `channel_test.md` | 推送通道自检用的消息体 |
| `qq-mail-triage-scf-deploy.zip` | 部署包（约 1.5 MB，由 `build_scf.py` 生成，不入库） |

## 先决条件

```bash
pip install requests cos-python-sdk-v5 tencentcloud-sdk-python
```

**腾讯云凭据**按以下优先级获取，仓库内不含任何明文密钥：

1. 环境变量 `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY`（推荐）
2. 环境变量 `TENCENT_CRED_FILE` 指向凭据文件
3. 项目根目录下的 `tencent_credentials.txt`（**已 gitignore，请勿提交**）

凭据文件格式为每行 `KEY: 值`，需含 `COS_SECRET_ID` / `COS_SECRET_KEY` / `COS_REGION` / `COS_BUCKET`。

本机专属配置可写进 `scf/.env`（同样已 gitignore），例如把凭据文件指向项目之外的路径：

```
TENCENT_CRED_FILE=/path/to/tencent_credentials.txt
SOURCE_FUNCTION=                            # 可选：从既有函数继承 Webhook 与 COS 配置
```

## 云端部署与运维

```bash
cd scf

# 查看函数与触发器状态
python deploy_scf.py --status

# 连接诊断：登录并列出文件夹，不移动任何邮件
python deploy_scf.py --probe
# 诊断时可临时覆盖参数，便于比对新旧凭证
python deploy_scf.py --probe --probe-host imap.163.com --probe-code 新授权码

# 查看双通道配置状态（不回显 URL）
python deploy_scf.py --channels

# 查看已配置的邮箱账号（不回显凭证）
python deploy_scf.py --accounts

# 规则调优：忽略去重、按当前规则重判全部邮件（只读，不改动邮箱）
python deploy_scf.py --rescan

# 重新清理：按当前规则重判并真正移入回收站（会改动邮箱）——改完规则后让存量邮件也生效
python deploy_scf.py --reclaim

# 多邮箱配置（QQ + Gmail 合并到同一份日报）
python deploy_scf.py --mail-accounts "QQ|me@qq.com|授权码|imap.qq.com;Gmail|me@gmail.com|应用专用密码|imap.gmail.com|993|0|[Gmail]/Trash"

# 演练：只判定、不移动邮件（首次验证务必先跑这个）
python deploy_scf.py --preview

# 部署 + 指定邮箱凭证与服务商
python deploy_scf.py --account you@163.com --auth-code 你的授权码 --imap-host imap.163.com

# 单独写入日报机器人 Webhook
python deploy_scf.py --report-webhook "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"

# 切换为正式模式（真正移入回收站）
python deploy_scf.py --dry-run 0
# 切回演练模式
python deploy_scf.py --dry-run 1

# 读取 / 清空日报暂存区
python deploy_scf.py --digest
python deploy_scf.py --clear-digest

# 查看云端运行状态与去重记录
python deploy_scf.py --stats

# 把一份 Markdown 推到日报机器人（经云函数推送，出口为国内 IP）
python deploy_scf.py --push-md ../channel_test.md --channel report --via-cloud
# 推给告警机器人（用于对照排查）
python deploy_scf.py --push-md ../channel_test.md --channel alert --via-cloud
```

> `--push-md` 默认在**本机**发请求，若本机代理走了海外节点，企业微信会返回
> `errcode 93000 invalid webhook url`——这是**出口 IP 问题，不是 key 无效**。
> 加 `--via-cloud` 改用云函数出口即可排除该干扰。

## 故障排查

先用 `python diagnose.py` 分层定位，各层含义：

| 现象 | 含义 | 处理方向 |
| --- | --- | --- |
| 裸 TCP 收到 0 字节 / `TLS=FAIL(SSLEOFError)` | 中间层把连接掐断了，**不是邮箱故障** | 检查本机代理的 DNS 与分流规则 |
| 横幅是 `Coremail System` | 服务器是网易系 | 163/126/yeah/188 应用 `imap.163.com` |
| 横幅是 `QQMail XMIMAP4Server` | 服务器是腾讯系 | qq.com / foxmail.com 应用 `imap.qq.com` |
| `LOGIN Login error or password error` | 服务器对了，凭证不对**或**该 IP 被风控 | 核对授权码；换网络验证以区分 |
| `Account is abnormal, service is not open…` | 腾讯侧的通用拒绝 | 同上 |

授权码的格式差异与失效原因见上文「接入的邮箱」一节。以下是两个最容易踩的坑：

- **手打授权码极易出错**。它是 16 位随机串且只显示一次，务必**复制粘贴**。
  可用 `--probe` 的返回体里的 `auth_code_len` 立刻判断是否粘贴不全。
- **重置授权码会让旧码立即失效**，**修改账号密码也会触发授权码过期**——
  这是「码看着没错但登录失败」的常见原因。

**已知风险：网易对云服务器 IP 有风控。** 社区有明确案例：同一套凭证本地正常、云端报认证失败，原因是云端 IP 被判定为高风险登录。若凭证确认无误但云端仍被拒，需改用其他架构（例如让 QQ 邮箱代收 163 邮件，云函数只连 `imap.qq.com`）。


## 本地运行（同一份逻辑，改规则后先在本机验证）

```bash
cd scf
cp .env.example .env      # 填入凭证
python handler.py --once --dry-run     # 演练
python handler.py --once               # 正式执行
python handler.py --test-rules         # 规则自检
```

## 判定逻辑

### 硬保护（命中即永不清理）
`edu.cn` / `gov.cn` / 银行 / 学信网 / 12306 / 支付宝 / 微信支付，以及 `PROTECT_EXTRA` 中自定义的豁免子串。

### 广告计分（未达门槛一律保留）

| 信号 | 分值 |
| --- | --- |
| 主题命中**广告词**（促销语义，每个 +1，上限 2 分） | +1 / 个 |
| 主题命中**运营词**（平台互动语义，上限 1 分） | +1 |
| 发件人为批量特征（`no-reply` / `posts-recap` / `follow-suggestions` 等）或域名含营销子域 | +1 |
| Message-ID 含 `bulk` / `mailchimp` / `sendgrid` / `klaviyo` 等 | +1 |
| 含 `List-Unsubscribe` 一键退订头 | +1 |
| **正文**含退订措辞（取消订阅 / unsubscribe / manage preferences） | +1 |
| `Precedence: bulk/list/junk` | +2 |

**门槛**：普通域名 ≥ 2 分；免费邮箱（qq / 163 / gmail 等，视作真人来信）≥ 4 分。

### 组合判定（识别平台营销推送）

平台营销邮件的主题往往很干净——「快来看看你错过的精彩时刻」不含任何促销词汇，
单靠计分永远达不到门槛。但它们有一个共同特征：**发件人一定是批量地址**。

因此增加一条组合规则：

> **批量发件人特征 +（任一广告词 / 运营词 / 退订头 / 正文退订措辞）→ 判为广告**

这条规则能稳定识别 Gate 活动推送、Instagram 关注建议与回顾、EA 问卷邀约这类邮件。

**必要的护栏**：`STRONG_IMPORTANT_KEYWORDS`（验证码、安全提醒、异常登录、账单、订单、
面试、offer 等）命中时**禁止判为广告**。否则 Google 安全提醒会被误杀——它正是由
`no-reply@accounts.google.com` 发出的，满足「批量发件人特征」。
护栏还会排除与广告词重叠的强重要词：英文 `offer` 既是招聘录用也是促销用语，
出现在 `exclusive offer` 中时不再触发保护。

### 验证码与验证链接邮件

| 类型 | 判定条件 | 行为 |
| --- | --- | --- |
| 验证码 | 关键词 + **邻近的 4~8 位数字**（两种书写顺序都识别） | 超过 `CODE_EXPIRE_MINUTES`（默认 30 分钟）→ 移入回收站；未超时 → 重要 |
| 验证链接 | 邮箱验证 / 激活账号 / confirm your email 等 | 同上（本身即一次性凭证，无需数字） |

要求「关键词 + 邻近数字」是为了避免把正文里「请勿将验证码告知他人」这类顺带提及误判。

**保护域例外**：银行 / 政务 / 支付宝等保护域内的邮件默认**不清理**，即使内容是验证码。
如需一并清理，设 `CODE_TRASH_PROTECTED=1`。

### 重要邮件即时推送去重

同一「**发件人 + 规范化主题**」在 `IMPORTANT_DEDUP_HOURS`（默认 24）小时内**只即时推送一次**，
避免「同一条安全提醒连发 9 封」这类噪音淹没推送通道。

- 主题会先去掉 `Re:` / `Fw:` / `回复:` / `转发:` 前缀并小写化，同一会话归并为一个键；
- 去重**只影响即时推送**，日报中该邮件仍然完整保留；
- 过滤在推送**成功后**才写入云端状态——推送失败不会被误记为「已推过」；
- 实例：9 封 `user@example.com 的安全提醒` → 推送 **1** 封、合并 **8** 封
  （`important_push: 1` / `important_deduped: 8`）；
- 设 `IMPORTANT_DEDUP_HOURS=0` 可关闭。

### 二维码邮件
需同时满足两个条件才判定为失效二维码（双信号，避免误判）：

1. **含图片**——邮件中存在 `image/*` 部件（内嵌图或图片附件）；
2. **命中关键词**——主题或正文出现「二维码、扫码、扫一扫、收款码、付款码、取件码、登录确认、扫码登录、qrcode」等。

且邮件时间超过 `QR_EXPIRE_MINUTES`（默认 30）分钟 → 移入回收站。
未超时则标记为 IMPORTANT（仍有效）；只有一个条件满足则保守保留，不动。

**已知边界**：超过 1 MB 的邮件只取头部、不做二维码判定（保守放行）；远程外链图片不算「含图片」（不下载邮件内图片，避免隐私与超时问题）。

## 重要参数（云函数环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QQ_EMAIL_ACCOUNT` | — | 邮箱完整地址（变量名沿用历史命名，与邮箱服务商无关） |
| `QQ_EMAIL_AUTH_CODE` | — | 邮箱授权码（**非**登录密码） |
| `IMAP_HOST` | `imap.qq.com` | IMAP 服务器；网易邮箱填 `imap.163.com` |
| `IMAP_PORT` | 993 | IMAP 端口 |
| `IMAP_NEED_ID` | `auto` | 网易系邮箱必须先发 `ID` 命令；`auto` 按主机名自动判断 |
| `MAIL_ACCOUNTS` | 空 | **多邮箱配置**（详见上文）；留空则用上面的单账号变量 |
| `WECOM_WEBHOOK` | — | 告警机器人 Webhook |
| `WECOM_WEBHOOK_REPORT` | 空 | 日报机器人 Webhook；留空回退到告警机器人 |
| `SCAN_DAYS` | 7 | 扫描最近多少天（含已读） |
| `QR_EXPIRE_MINUTES` | 30 | 二维码失效阈值（分钟） |
| `CODE_EXPIRE_MINUTES` | 30 | 验证码 / 验证链接失效阈值（分钟） |
| `CODE_TRASH_PROTECTED` | 0 | 1=验证码在保护域内也清理；0=保护域跳过 |
| `AD_DOMAINS` | 空 | 显式营销域名（逗号分隔子串），命中即判广告 |
| `AD_EXTRA_KEYWORDS` | 空 | 自定义补充广告词（逗号分隔） |
| `FUNC_TIMEOUT_S` | 60 | 云函数总时限，用于按账号均分时间预算 |
| `IMAP_TIMEOUT` | 20 | 单次 IMAP 连接/登录超时（秒），**必须设置** |
| `IMAP_RETRIES` | 3 | 连接/登录失败重试次数（跨国链路必需） |
| `ACCOUNT_BUDGET_S` | 空 | 单账号时间预算（秒）；留空按账号数均分 |
| `MAX_PROCESS_PER_RUN` | 25 | 单轮处理上限，防止超时 |
| `MAX_TRASH_PER_RUN` | 60 | 单轮最多移入回收站，防止批量误伤 |
| `IMPORTANT_DEDUP_HOURS` | 24 | 重要邮件即时推送的去重窗口（小时）；`0` 关闭去重 |
| `DRY_RUN` | 1 | 1=只判定不移动 |
| `PROTECT_EXTRA` | 空 | 自定义豁免，逗号分隔子串 |
| `TRASH_FOLDER` | 空 | 回收站文件夹名，留空自动识别 |
| `STATE_KEY` / `DIGEST_KEY` | — | COS 中的状态与暂存键名 |

## 安全性说明

- **移入回收站，不是永久删除**。采用 IMAP `UID MOVE`（不支持时回退 `COPY` + `\Deleted` + `UID EXPUNGE`），邮件可在邮箱「已删除」中找回。
- **绝不修改已读状态**：读取统一使用 `BODY.PEEK`，不会把未读邮件标成已读。
- **凭证缺失时安全退出**：未配置 `QQ_EMAIL_ACCOUNT` / `QQ_EMAIL_AUTH_CODE` 时直接返回错误并结束，不连接邮箱、不做任何变更。
- **演练模式不写状态**：`DRY_RUN` 下不写去重状态，避免把邮件标记成「已处理」而在正式运行时被跳过。
- **回收站定位失败则不动作**：找不到回收站文件夹时跳过全部移动并回报错误，不会退化为删除。
- **单轮上限双保险**：`MAX_PROCESS_PER_RUN` 与 `MAX_TRASH_PER_RUN` 限制单次动作规模，异常时不会一次性清空收件箱。
- **仓库零明文密钥**：凭证只走环境变量或 gitignore 的本地文件；`.gitignore` 已覆盖 `.env`、`tencent_credentials.txt`、构建产物等。

## 作者与许可

由 **hongpengxiao2-ctrl** 构建与维护，基于 [MIT License](LICENSE) 开源。
