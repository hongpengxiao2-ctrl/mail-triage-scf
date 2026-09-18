"""
QQ 邮箱巡检 — 腾讯云函数版（无服务器 7×24）
================================================================
入口：main_handler(event, context)，由定时触发器每 5 分钟调用一次。

工作流程：
  1. IMAP 连接 QQ 邮箱，扫描收件箱近 SCAN_DAYS 天（默认 7 天）的邮件
  2. 三分类：SPAM(广告/垃圾) / QR_EXPIRED(超时二维码) / IMPORTANT / NORMAL
  3. SPAM 与 QR_EXPIRED 通过 IMAP MOVE 移入「回收站」——不是永久删除，可恢复
  4. IMPORTANT 邮件立刻推送企业微信
  5. 保留的邮件写入 COS 摘要暂存区，供每日 10:00 生成中文摘要日报
  6. handled_uids 写入 COS 去重，同一封邮件不会重复处理或重复推送

依赖：requests、cos-python-sdk-v5（imaplib / email 为标准库）
环境变量：
  QQ_EMAIL_ACCOUNT     必填，邮箱完整地址
  QQ_EMAIL_AUTH_CODE   必填，邮箱授权码（非登录密码）
  IMAP_HOST            可选，默认 imap.qq.com；网易邮箱填 imap.163.com
  IMAP_PORT            可选，默认 993
  IMAP_NEED_ID         可选，auto/1/0；网易系邮箱必须发送 ID 命令，默认 auto 自动判断
  WECOM_WEBHOOK        告警通道：企业微信群机器人 Webhook（即时重要邮件提醒用）
  WECOM_WEBHOOK_REPORT 日报通道：企业微信群机器人 Webhook（每日摘要日报用）
                       留空时自动回退到 WECOM_WEBHOOK，不会静默丢消息
  COS_BUCKET           必填，如 my-bucket-1250000000
  COS_REGION           必填，如 ap-guangzhou
  COS_SECRET_ID        必填，腾讯云 API 密钥
  COS_SECRET_KEY       必填，腾讯云 API 密钥
  TRASH_FOLDER         可选，回收站文件夹名；留空则自动识别（\\Trash 属性）
  SCAN_DAYS            可选，默认 7
  QR_EXPIRE_MINUTES    可选，默认 30，二维码邮件超过该分钟数即视为失效
  MAX_PROCESS_PER_RUN  可选，默认 25，单轮最多处理的邮件数（防止超时）
  MAX_TRASH_PER_RUN    可选，默认 60，单轮最多移入回收站的邮件数（防误批量）
  IMPORTANT_DEDUP_HOURS 可选，默认 24；重要邮件即时推送的去重窗口（小时），0 = 关闭
                       同一「发件人+主题」在窗口内只推一次；日报中仍完整保留
  DRY_RUN              可选，1 = 只判定不移动，默认 0
  STATE_KEY            可选，默认 qq-mail-triage-state.json
  DIGEST_KEY           可选，默认 qq-mail-digest.json
"""
import email
import html as html_mod
import imaplib
import json
import logging
import os
import poplib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

import requests
from qcloud_cos import CosConfig, CosS3Client

log = logging.getLogger(__name__)

BJT = timezone(timedelta(hours=8))
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.qq.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
# 网易邮箱(163/126)要求认证后立即发送 ID 命令，否则后续命令报 "Unsafe Login"。
# auto = 主机名含 163/126/yeah 时自动发送；1 = 强制发送；0 = 不发送
IMAP_NEED_ID = os.environ.get("IMAP_NEED_ID", "auto")
IMAP_ID_CMD = ('("name" "qq-mail-triage" "version" "1.0.0" '
               '"vendor" "workbuddy" "support-email" "noreply@example.com")')

ACCOUNT = os.environ.get("QQ_EMAIL_ACCOUNT", "")
AUTH_CODE = os.environ.get("QQ_EMAIL_AUTH_CODE", "")
WECOM_WEBHOOK = os.environ.get("WECOM_WEBHOOK", "")
# 日报专用机器人；未单独配置时回退到告警机器人，保证日报不会静默丢失
WECOM_WEBHOOK_REPORT = os.environ.get("WECOM_WEBHOOK_REPORT", "") or WECOM_WEBHOOK

COS_BUCKET = os.environ.get("COS_BUCKET", "")
COS_REGION = os.environ.get("COS_REGION", "")
COS_SECRET_ID = os.environ.get("COS_SECRET_ID", "")
COS_SECRET_KEY = os.environ.get("COS_SECRET_KEY", "")
STATE_KEY = os.environ.get("STATE_KEY", "qq-mail-triage-state.json")
DIGEST_KEY = os.environ.get("DIGEST_KEY", "qq-mail-digest.json")

TRASH_FOLDER = os.environ.get("TRASH_FOLDER", "")
SCAN_DAYS = int(os.environ.get("SCAN_DAYS", "7"))
QR_EXPIRE_MINUTES = int(os.environ.get("QR_EXPIRE_MINUTES", "30"))
# 验证码 / 验证链接类邮件的失效阈值（分钟）：超过即移入回收站
CODE_EXPIRE_MINUTES = int(os.environ.get("CODE_EXPIRE_MINUTES", "30"))
# 验证码规则是否对「保护域」(银行/政务/支付宝等) 也生效。默认 0 = 保护域跳过，
# 保持「保护域永不清理」的安全承诺；设为 1 则验证码在保护域内同样会被清理。
CODE_TRASH_PROTECTED = os.environ.get("CODE_TRASH_PROTECTED", "0") == "1"
MAX_PROCESS = int(os.environ.get("MAX_PROCESS_PER_RUN", "25"))
MAX_TRASH = int(os.environ.get("MAX_TRASH_PER_RUN", "60"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
# 重要邮件即时推送去重窗口（小时）。同一「发件人+主题」在窗口内只推一次；
# 设为 0 关闭去重（每封都推）。注意：去重只影响即时推送，日报中仍然全部保留。
IMPORTANT_DEDUP_HOURS = float(os.environ.get("IMPORTANT_DEDUP_HOURS", "24"))

# 显式营销域名（逗号分隔子串）。命中即直接判为广告，且优先级高于「保护」以外的一切规则。
# 用于平台营销子域这类光靠主题词难以稳定识别的发件人，例：
#   AD_DOMAINS=notice.gate.com,mail.instagram.com
AD_DOMAINS = [s.strip().lower() for s in os.environ.get("AD_DOMAINS", "").split(",") if s.strip()]
# 自定义补充广告词（逗号分隔），与内置词库同等生效
AD_EXTRA_KEYWORDS = [s.strip().lower()
                     for s in os.environ.get("AD_EXTRA_KEYWORDS", "").split(",") if s.strip()]

# ---------------- 多邮箱账号 ----------------
# MAIL_ACCOUNTS 支持一次巡检多个邮箱，并把结果合并到同一份日报。
# 格式：分号分隔多条记录，每条记录用 `|` 分隔字段——
#   名称|邮箱地址|授权码|IMAP服务器|端口|是否发ID命令|回收站文件夹
# 后四项可省略。例：
#   QQ|me@qq.com|xxxxxxxxxxxxxxxx|imap.qq.com
#   Gmail|me@gmail.com|xxxxxxxxxxxxxxxx|imap.gmail.com|993|0|[Gmail]/Trash
# 未配置该项时，自动用上面的单账号变量合成一条记录（向后兼容）。
MAIL_ACCOUNTS_RAW = os.environ.get("MAIL_ACCOUNTS", "")
ACCOUNTS = []   # 由 _reload_accounts() 填充

MAX_BODY_BYTES = 1_000_000  # 超过 1MB 的邮件只取头部，避免超时
DIGEST_MAX_ENTRIES = 400
RESCAN_LIMIT = 120   # 规则调优时（rescan/ignore_handled）单轮最多重判的邮件数


def _acct_default_name(addr, host):
    """按域名推断一个可读的账号名，用于日报分组。"""
    d = (addr.split("@")[-1] if "@" in (addr or "") else (host or "")).lower()
    for key, label in (("qq.com", "QQ邮箱"), ("gmail.com", "Gmail"),
                       ("163.com", "网易163"), ("126.com", "网易126"),
                       ("outlook.com", "Outlook"), ("hotmail.com", "Outlook"),
                       ("foxmail.com", "Foxmail"), ("icloud.com", "iCloud")):
        if key in d:
            return label
    return d or "邮箱"


def _parse_accounts(raw, legacy):
    """解析 MAIL_ACCOUNTS；无配置或解析为空时，回退到单账号 legacy 记录。"""
    accts = []
    for rec in (raw or "").split(";"):
        rec = rec.strip()
        if not rec:
            continue
        f = [x.strip() for x in rec.split("|")]
        while len(f) < 7:
            f.append("")
        name, addr, code, host, port, need_id, trash = f[:7]
        if not addr:
            continue
        host = host or "imap.qq.com"
        accts.append({
            "name": name or _acct_default_name(addr, host),
            "account": addr,
            "auth_code": code,
            "host": host,
            "port": int(port) if port.isdigit() else 993,
            "need_id": need_id or "auto",
            "trash": trash,
        })
    if accts:
        return accts
    if legacy.get("account"):
        return [{
            "name": legacy.get("name") or _acct_default_name(legacy["account"], legacy.get("host", "")),
            "account": legacy["account"],
            "auth_code": legacy.get("auth_code", ""),
            "host": legacy.get("host") or "imap.qq.com",
            "port": int(legacy.get("port") or 993),
            "need_id": legacy.get("need_id", "auto"),
            "trash": legacy.get("trash", ""),
        }]
    return []


def _reload_accounts():
    """从环境变量重建 ACCOUNTS 列表（模块加载与 .env 载入后各调用一次）。"""
    g = globals()
    g["ACCOUNTS"] = _parse_accounts(MAIL_ACCOUNTS_RAW, {
        "account": ACCOUNT, "auth_code": AUTH_CODE,
        "host": IMAP_HOST, "port": IMAP_PORT,
        "need_id": IMAP_NEED_ID, "trash": TRASH_FOLDER,
    })


_reload_accounts()

# ---------------- 规则表（与本地版 triage.js 保持一致） ----------------

PROTECT_DOMAINS = [
    "edu.cn", "gov.cn", "ac.cn", "chsi.com.cn", "chsi.cn",
    "icbc.com.cn", "ccb.com", "abchina.com", "bankofchina.com", "boc.cn",
    "cmbchina.com", "bankcomm.com", "spdb.com.cn", "cib.com.cn", "psbc.com",
    "cmbc.com.cn", "citicbank.com", "cebbank.com", "hxb.com.cn",
    "alipay.com", "tenpay.com", "unionpay.com", "12306.cn", "chinatax.gov.cn",
]

# 自定义豁免：逗号分隔的子串，命中发件人地址、发件人显示名或主题即永不清理。
# 例：PROTECT_EXTRA="teacher@x.edu,某公司hr,成绩单"
PROTECT_EXTRA = [s.strip().lower() for s in os.environ.get("PROTECT_EXTRA", "").split(",") if s.strip()]

IMPORTANT_DOMAINS = [
    "github.com", "openai.com", "anthropic.com", "claude.ai",
    "apple.com", "microsoft.com", "google.com", "tencent.com", "aliyun.com",
    "zhaopin.com", "51job.com", "zhipin.com", "lagou.com", "liepin.com",
    "maimai.cn", "linkedin.com", "nowcoder.com", "shixiseng.com", "yingjiesheng.com",
    "sf-express.com", "sto.cn", "yto.net.cn", "yundaex.com", "zto.com",
    "ems.com.cn", "deppon.com", "10086.cn", "189.cn", "10010.com",
    "chinaunicom.com", "ctrip.com", "trip.com",
]

FREEMAIL_DOMAINS = [
    "qq.com", "163.com", "126.com", "gmail.com", "outlook.com",
    "hotmail.com", "foxmail.com", "sina.com", "sohu.com", "yeah.net", "139.com",
]

AD_SUBJECT_KEYWORDS = [
    "促销", "优惠券", "优惠", "限时", "折扣", "特价", "秒杀", "抢购", "满减", "立减",
    "免费领", "免费试", "会员日", "大促", "双11", "双十一", "618", "清仓", "甩卖",
    "返现", "直播预告", "新品上市", "内购", "福利", "抽奖", "试用", "招商", "加盟",
    "贷款", "借贷", "提额", "提额度", "中奖", "领取", "点击查看", "退订", "订阅",
    "好物", "榜单", "种草", "上新", "开抢", "到手价", "券后", "专属优惠", "尊享",
    "钜惠", "狂欢", "惊喜", "尾款", "预售", "送好礼", "免单", "补贴", "低价",
    "sale", "promo", "discount", "deal", "coupon", "newsletter", "unsubscribe",
    "webcast", "limited time", "black friday", "cyber monday", "free shipping",
    "exclusive offer", "save now", "% off",
    # 平台营销补充（2026-09 实测：Gate 活动、奖池瓜分类邮件原本只拿到 1 分）
    "奖励", "奖池", "瓜分", "积分", "解锁", "空投", "刮刮卡", "返佣", "返利",
    "领券", "开卡", "注册送", "礼包", "赢取", "免费领取", "限时狂欢", "交易即得",
]

# 弱广告词：平台运营 / 互动召回语义。单独出现不足以判定为广告（每个 +1，上限 1），
# 但与「批量发件人特征」组合时可直接判为广告——这条组合规则用于识别平台营销邮件
# （Gate 活动推送、Instagram 关注建议与回顾等），同时不会误伤同域的账号安全邮件
# （安全提醒 / 密码重置类不含任何广告词）。
AD_SOFT_KEYWORDS = [
    "活动", "任务", "打卡", "签到", "参与", "回顾", "关注建议", "推荐关注",
    "可能认识", "你可能认识", "更多账户", "更多好友", "精彩时刻", "你错过",
    "快来看看", "正式开启", "倒计时", "问卷", "调研", "期待您的", "回馈",
    "任务中心", "为你推荐", "猜你喜欢", "其他人",
    "recap", "suggestions", "you might know", "recommended for you",
    "we miss you", "come back", "your activity", "weekly digest",
]
# 刻意不收录「即将 / 上线 / 升级 / 预约 / 预告 / 反馈」这类词：它们在
# 正常的服务通知（会员到期、系统升级公告、预约提醒）里同样常见，
# 与批量发件人特征组合时会误杀真实通知。宁可漏判，不可误删。

IMPORTANT_SUBJECT_KEYWORDS = [
    "验证码", "校验码", "动态密码", "登录提醒", "异常登录", "尝试登录", "新设备",
    "密码", "安全提醒", "安全验证",
    "账单", "发票", "报销", "对账", "报表",
    "面试", "笔试", "offer", "录用", "录取", "复试", "宣讲", "招聘", "实习", "兼职",
    "通知", "提醒", "确认", "合同", "承诺书", "截止", "deadline",
    "订单", "发货", "物流", "签收", "退款", "支付", "扣款", "还款", "逾期", "转账",
    "报名", "成绩", "奖学金", "答辩", "学籍", "选课", "考试", "分数线",
    "会议", "邀请函", "中签", "纳税", "汇算", "医保", "社保", "公积金", "信用卡", "银行",
    "结算", "结算单", "工资", "入职",
]

# 强重要词：命中后禁止被判为垃圾。用于防止「账号安全提醒」「密码重置」这类
# 邮件被批量发件人特征误伤（Google 安全提醒就是 no-reply@ 发出的）。
STRONG_IMPORTANT_KEYWORDS = [
    "验证码", "校验码", "动态密码", "密码", "安全提醒", "安全验证", "异常登录",
    "登录提醒", "尝试登录", "新设备", "账单", "发票", "对账", "面试", "笔试", "offer", "录用", "录取",
    "复试", "合同", "订单", "发货", "物流", "签收", "退款", "支付", "扣款",
    "还款", "逾期", "转账", "成绩", "奖学金", "答辩", "学籍", "选课", "考试",
    "会议", "邀请函", "中签", "纳税", "汇算", "医保", "社保", "公积金",
    "信用卡", "银行", "结算", "工资", "入职", "身份证", "实名", "账号安全",
]

# 二维码类邮件的识别关键词（主题或正文命中，且邮件内含图片 → 视为二维码邮件）
QR_KEYWORDS = [
    "二维码", "扫码", "扫一扫", "收款码", "付款码", "取件码", "登录确认", "扫码登录",
    "扫描下方", "长按识别", "qrcode", "qr code", "scan the code", "scan to",
]

# 验证码类：必须「关键词 + 邻近的 4~8 位数字」同时命中，避免把正文里
# 「请不要把验证码告诉他人」这类顺带提及误判为验证码邮件。
CODE_KEYWORDS = [
    "验证码", "校验码", "动态密码", "短信验证码", "登录验证码", "验证代码",
    "动态码", "verification code", "one-time code", "one time code",
    "security code", "one-time password", "otp",
]
# 关键词在前、数字在后，或数字在前、关键词在后，两种书写顺序都要覆盖
_CODE_ALT = ("验证码|校验码|动态密码|验证代码|动态码|verification code"
             "|one[- ]time code|one[- ]time password|security code|otp")
CODE_NEAR_RE = re.compile(
    rf"(?:{_CODE_ALT})[^0-9\n]{{0,24}}(\d{{4,8}})"
    rf"|(?<!\d)(\d{{4,8}})(?!\d)[^0-9\n]{{0,12}}(?:{_CODE_ALT})",
    re.I)

# 验证链接类：本身就是一次性凭证，无需数字即可判定
CODE_LINK_KEYWORDS = [
    "邮箱验证", "验证邮箱", "验证您的邮箱", "验证邮件", "激活账号", "账号激活",
    "激活您的账号", "confirm your email", "verify your email",
    "verify your account", "activate your account", "email verification",
]

AD_SENDER_RE = re.compile(
    r"(no-?reply|noreply|newsletter|marketing|promo|edm|mailer|mailer-daemon|broadcast"
    r"|notify|notification|service-?mail|advert|recap|suggestions|updates?|digest"
    r"|notice|remind|alert|invite|events?|community|engage|campaign|blast|bulk"
    r"|donotreply|do-not-reply|automated|auto-?mail|info-?mail|noreply)", re.I)
# 批量 / 营销常用子域。例：no-reply@notice.gate.com、posts-recap@mail.instagram.com、
# ea@e.ea.com。仅作为「批量特征」，需与广告词或退订特征组合才判广告。
BULK_SUBDOMAINS = {
    "notice", "remind", "alert", "mail", "email", "news", "marketing", "promo",
    "edm", "campaign", "newsletter", "info", "notification", "notify", "updates",
    "digest", "noreply", "no-reply", "reply", "message", "messages", "go", "link",
    "click", "track", "send", "em", "e", "m", "t",
}
AD_MESSAGEID_RE = re.compile(
    r"(bulk|massmail|newsletter|campaign|edm|marketing|mailchimp|sendcloud|sendgrid|mailgun"
    r"|klaviyo|hubspot|braze|iterable|mailjet|postmark|amazonses|exacttarget|salesforce)", re.I)
# 正文里的退订措辞：不少营销邮件不带 List-Unsubscribe 头，但正文一定有退订链接
AD_BODY_UNSUB_RE = re.compile(
    r"(取消订阅|退订|不再接收|不想再收到|unsubscribe|opt[- ]?out"
    r"|manage (your )?preferences|email preferences)", re.I)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


# ---------------- 工具 ----------------

def _hdr(raw):
    """解码 MIME 编码的头部（如 =?UTF-8?B?...?=）。"""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _addr_of(msg):
    raw = msg.get("From", "") or ""
    m = re.search(r"<([^>]+)>", raw)
    return (m.group(1) if m else raw).strip().lower()


def _name_of(msg):
    raw = msg.get("From", "") or ""
    m = re.match(r"^(.*?)\s*<", raw)
    return _hdr(m.group(1)).strip().lower() if m else ""


def _html_to_text(s):
    return html_mod.unescape(WS_RE.sub(" ", TAG_RE.sub(" ", s or ""))).strip()


def _body_and_images(msg):
    """返回 (正文文本, 图片部件数, 图片文件名列表)。"""
    texts, images = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = (part.get_content_type() or "").lower()
        disp = (part.get("Content-Disposition") or "").lower()
        if ctype.startswith("image/"):
            images.append(_hdr(part.get_filename() or ""))
            continue
        if ctype in ("text/plain", "text/html") and "attachment" not in disp:
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(charset, errors="replace")
                except LookupError:
                    text = payload.decode("utf-8", errors="replace")
                texts.append(_html_to_text(text) if ctype == "text/html" else text)
            except Exception:
                continue
    return WS_RE.sub(" ", " ".join(texts)).strip(), len(images), [i for i in images if i]


def _age_minutes(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BJT)
    return (datetime.now(BJT) - dt.astimezone(BJT)).total_seconds() / 60.0


def _bulk_signal(from_addr):
    """批量 / 营销发件人特征。返回描述字符串；无特征返回空串。

    两个来源：本地部分命中批量命名（no-reply、posts-recap、follow-suggestions…），
    或域名含有营销子域（notice.gate.com、mail.instagram.com、e.ea.com）。
    注意：这**只是**一个特征，单独不足以判广告——否则会误伤
    no-reply@accounts.google.com 这类由批量地址发出的账号安全邮件。
    """
    addr = from_addr or ""
    m = AD_SENDER_RE.search(addr)
    if m:
        return f"批量发件人({m.group(0).lower()})"
    dom = addr.split("@")[-1] if "@" in addr else ""
    parts = dom.split(".")
    subs = set(parts[:-2]) if len(parts) > 2 else set()
    hit = sorted(subs & BULK_SUBDOMAINS)
    if hit:
        return f"批量子域({hit[0]})"
    return ""


def _code_signal(subject, body_text):
    """验证码 / 验证链接命中的关键词；无则返回 None。"""
    subj = (subject or "").lower()
    body = (body_text or "").lower()
    # 验证码要求「关键词 + 邻近数字」，避免顺带提及被误判
    if CODE_NEAR_RE.search(subj) or CODE_NEAR_RE.search(body):
        for k in CODE_KEYWORDS:
            if k in subj or k in body:
                return k
        return "验证码"
    for k in CODE_LINK_KEYWORDS:
        if k in subj or k in body:
            return k
    return None


def classify(subject, from_addr, from_name, message_id, list_unsub, precedence,
             body_text="", image_count=0, age_min=None):
    """返回 (label, reason)。

    label ∈ {SPAM, CODE_EXPIRED, QR_EXPIRED, IMPORTANT, NORMAL}

    判定优先级（从高到低）：
      1. 硬保护域        —— 永不清理（唯一例外：CODE_TRASH_PROTECTED=1 时的验证码）
      2. 显式营销域名     —— AD_DOMAINS 命中，直接判广告
      3. 验证码/验证链接  —— 过期即清；未过期判重要
      4. 二维码          —— 含图片且超时
      5. 广告            —— 加权计分，或「批量特征 + 广告词/退订特征」组合判定
      6. 重要 / 普通

    强重要词（STRONG_IMPORTANT_KEYWORDS）命中时禁止判为广告。这是必要的护栏：
    Google 安全提醒由 no-reply@accounts.google.com 发出，满足「批量特征」，
    若无此护栏就会被组合规则误杀。
    """
    domain = from_addr.split("@")[-1] if "@" in from_addr else ""
    subj = (subject or "").lower()
    body = (body_text or "").lower()

    # --- 硬保护 ---
    protected = None
    for d in PROTECT_DOMAINS:
        if d in domain:
            protected = d
            break
    if not protected:
        for s in PROTECT_EXTRA:
            if s in from_addr or s in from_name or s in subj:
                protected = f"自定义豁免:{s}"
                break

    # --- 广告词先算，供强重要词做冲突排除 ---
    strong_ad = [k for k in AD_SUBJECT_KEYWORDS if k in subj] + \
                [k for k in AD_EXTRA_KEYWORDS if k in subj]
    soft_ad = [k for k in AD_SOFT_KEYWORDS if k in subj]

    # --- 重要 / 强重要 ---
    # 排除与广告词重叠的强重要词：英文 "offer" 既是招聘录用、也是促销用语
    # （exclusive offer / special offer），出现在促销语境里时不应触发保护。
    _ad_flat = " ".join(strong_ad + soft_ad)
    strong_hits = [k for k in STRONG_IMPORTANT_KEYWORDS if k in subj and k not in _ad_flat]
    important_hits = [k for k in IMPORTANT_SUBJECT_KEYWORDS if k in subj]
    important_domain = next((d for d in IMPORTANT_DOMAINS if d in domain), None)

    # --- 验证码 / 验证链接 ---
    code_kw = _code_signal(subject, body_text)

    # --- 硬保护（唯一例外：允许清理保护域内的验证码）---
    if protected and not (CODE_TRASH_PROTECTED and code_kw):
        label = "IMPORTANT" if (important_hits or important_domain or code_kw) else "NORMAL"
        return label, f"受保护域({protected})，永不清理"

    # --- 显式营销域名（仅低于硬保护）---
    ad_domain = next((d for d in AD_DOMAINS if d in domain), None)
    if ad_domain:
        return "SPAM", f"命中营销域名({ad_domain})"

    # --- 验证码 / 验证链接：过期即清 ---
    if code_kw:
        if age_min is not None and age_min >= CODE_EXPIRE_MINUTES:
            return "CODE_EXPIRED", (f"验证码/验证链接(命中“{code_kw}”)已过 "
                                    f"{int(age_min)} 分钟 > {CODE_EXPIRE_MINUTES} 分钟")
        return "IMPORTANT", f"验证码/验证链接(命中“{code_kw}”)仍在有效期内"

    # --- 二维码邮件（含图片 + 二维码关键词 + 已超时）---
    qr_hit = next((k for k in QR_KEYWORDS if k in subj or k in body), None)
    if qr_hit and image_count > 0:
        if age_min is not None and age_min >= QR_EXPIRE_MINUTES:
            return "QR_EXPIRED", f"含二维码图片(命中“{qr_hit}”)且已过 {int(age_min)} 分钟 > {QR_EXPIRE_MINUTES} 分钟"
        return "IMPORTANT", f"含二维码图片(命中“{qr_hit}”)且仍在有效期内"

    # --- 广告计分 ---
    score = 0
    signals = []
    if strong_ad:
        score += min(len(strong_ad), 2)
        signals.append(f"广告词x{len(strong_ad)}({','.join(strong_ad[:3])})")
    if soft_ad:
        score += 1
        signals.append(f"运营词x{len(soft_ad)}({','.join(soft_ad[:2])})")
    bulk = _bulk_signal(from_addr)
    if bulk:
        score += 1
        signals.append(bulk)
    if AD_MESSAGEID_RE.search(message_id or ""):
        score += 1
        signals.append("批量发信ID")
    if list_unsub:
        score += 1
        signals.append("含一键退订头")
    body_unsub = bool(AD_BODY_UNSUB_RE.search(body))
    if body_unsub:
        score += 1
        signals.append("正文含退订指引")
    if (precedence or "").lower() in ("bulk", "list", "junk"):
        score += 2
        signals.append(f"Precedence:{precedence}")

    is_freemail = domain in FREEMAIL_DOMAINS
    threshold = 4 if is_freemail else 2

    # 组合判定：批量发件人特征 + 任一广告/退订特征 → 广告。
    # 专为「平台营销推送」设计：这类邮件主题往往很干净（「快来看看你错过的精彩时刻」），
    # 但发件人一定是批量地址；而同域的账号安全邮件既无广告词也无退订特征，不会被误伤。
    combo = bool(bulk) and bool(strong_ad or soft_ad or list_unsub or body_unsub)
    spam = (score >= threshold) or combo

    if spam and strong_hits:
        return "IMPORTANT", f"强重要词({'/'.join(strong_hits[:2])})优先于广告判定"
    if spam:
        why = "组合判定" if (combo and score < threshold) else f"广告分{score}/门槛{threshold}"
        return "SPAM", f"{why}：{' / '.join(signals[:3])}"

    if important_hits or important_domain:
        return "IMPORTANT", " / ".join(important_hits[:2]) or f"重要域:{important_domain}"

    if score > 0:
        return "NORMAL", f"广告分不足({score}/{threshold})，保守保留"
    return "NORMAL", "无广告特征"


# ---------------- COS ----------------

def _cos():
    cfg = CosConfig(Region=COS_REGION, SecretId=COS_SECRET_ID, SecretKey=COS_SECRET_KEY)
    return CosS3Client(cfg)


def _cos_get(key, default):
    try:
        resp = _cos().get_object(Bucket=COS_BUCKET, Key=key)
        return json.loads(resp["Body"].get_raw_stream().read().decode("utf-8"))
    except Exception:
        return default


def _cos_put(key, obj):
    _cos().put_object(Bucket=COS_BUCKET, Key=key,
                      Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                      ContentType="application/json")


# ---------------- 推送 ----------------

def _dedup_key(item):
    """重要邮件去重键 = 账号 + 发件人 + 规范化主题。

    去掉 Re:/Fw:/回复/转发 等前缀与多余空白后小写化，
    使「同一主题连发多封」归并为同一个键。前缀账号名以保证多邮箱互不干扰。
    """
    subj = item.get("subject", "") or ""
    subj = re.sub(r"^\s*(re|fw|fwd|回复|答复|转发)\s*[:：]\s*", "", subj, flags=re.I)
    subj = re.sub(r"^(回复|转发)[:：]?\s*", "", subj)
    subj = re.sub(r"\s+", " ", subj).strip().lower()
    return f"{item.get('account') or ''}|{(item.get('from') or '').lower()}|{subj}"


def _split_dedup(items, state, hours):
    """按去重窗口拆分重要邮件。

    返回 (待推送列表, 被去重列表, 新的历史记录 dict)。
    窗口内已推送过的键会被剔除；过期键会被清理。
    """
    hist = dict(state.get("pushed_important") or {})
    if hours <= 0:
        return list(items), [], hist
    now = datetime.now(BJT)
    fresh, expired = {}, 0
    for k, ts in hist.items():
        try:
            t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJT)
        except Exception:
            expired += 1
            continue
        if (now - t).total_seconds() < hours * 3600:
            fresh[k] = ts
        else:
            expired += 1
    to_push, deduped = [], []
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    for x in items:
        k = _dedup_key(x)
        if k in fresh:
            deduped.append(x)
            continue
        to_push.append(x)
        fresh[k] = now_str
    return to_push, deduped, fresh


def push_wecom(markdown, channel="alert"):
    """推送到企业微信。

    channel="alert"  → WECOM_WEBHOOK（即时重要邮件提醒）
    channel="report" → WECOM_WEBHOOK_REPORT（每日摘要日报）

    返回 (是否成功, 错误描述)。错误描述便于在无 CLS 日志时定位问题。
    """
    url = WECOM_WEBHOOK_REPORT if channel == "report" else WECOM_WEBHOOK
    if not url:
        return False, "webhook 未配置"
    content = markdown
    if len(content.encode("utf-8")) > 4000:
        content = content.encode("utf-8")[:3900].decode("utf-8", "ignore") + "\n\n…(已截断)"
    last_err = ""
    for attempt in range(3):
        try:
            resp = requests.post(url, json={"msgtype": "markdown",
                                            "markdown": {"content": content}}, timeout=15)
            data = resp.json()
            if data.get("errcode") == 0:
                return True, ""
            last_err = f"errcode={data.get('errcode')} errmsg={data.get('errmsg')}"
            if data.get("errcode") == 45008:
                import time as _t
                _t.sleep(min(int(resp.headers.get("Retry-After", "15")), 60))
                continue
            log.warning(f"企业微信推送失败({channel}): {data}")
            return False, last_err
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log.warning(f"企业微信推送异常({channel}): {e}")
    return False, last_err


# ---------------- IMAP ----------------

def _should_send_id(acct=None):
    """是否需要发送 IMAP ID 命令（网易系邮箱必需，否则报 Unsafe Login）。"""
    need = (acct or {}).get("need_id", IMAP_NEED_ID)
    if need == "1":
        return True
    if need == "0":
        return False
    host = (acct or {}).get("host") or IMAP_HOST
    return bool(re.search(r"163\.com|126\.com|yeah\.net|188\.com", host, re.I))


def _imap_connect(readonly, acct):
    """按账号配置建立 IMAP 连接并选中收件箱。"""
    addr = acct.get("account") or ""
    code = acct.get("auth_code") or ""
    if not (addr and code):
        raise RuntimeError(f"[{acct.get('name')}] 邮箱地址或授权码未配置")
    host = acct.get("host") or "imap.qq.com"
    port = int(acct.get("port") or 993)
    imap = imaplib.IMAP4_SSL(host, port)
    imap.login(addr, code)
    # 网易系邮箱必须先发 ID，再做任何邮箱操作，否则报 Unsafe Login
    if _should_send_id(acct):
        try:
            imap.xatom("ID", IMAP_ID_CMD)
            log.info("已发送 IMAP ID 命令（网易系邮箱必需）")
        except Exception as e:
            log.warning(f"发送 IMAP ID 命令失败（将继续尝试）: {e}")
    imap.select("INBOX", readonly=readonly)
    return imap


def _find_trash(imap, acct=None):
    """定位回收站文件夹，返回 IMAP 语法中可直接使用的名字（含引号）。"""
    # Gmail 这类服务商的回收站不在默认命名里，需要用显式配置
    # 例：[Gmail]/Trash
    configured = (acct or {}).get("trash") or TRASH_FOLDER
    if configured:
        return configured if configured.startswith('"') else f'"{configured}"'
    try:
        typ, rows = imap.list()
    except Exception:
        return None
    candidates = []
    for row in rows or []:
        line = row.decode("utf-8", "replace") if isinstance(row, bytes) else str(row)
        m = re.match(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$', line.strip())
        if not m:
            continue
        flags, name = m.group("flags"), m.group("name").strip()
        candidates.append((flags, name))
    # 优先 \Trash 特殊属性；其次常见命名
    for flags, name in candidates:
        if "\\Trash" in flags:
            return name
    for flags, name in candidates:
        if re.search(r"deleted|trash|已删除|垃圾|\[gmail\]", name, re.I):
            return name
    return None


def _to_tz(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=BJT)


def _month_name(month):
    return ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][month - 1]


def _imap_date(dt):
    """IMAP SINCE 需要英文月份缩写；不能用 strftime('%b')（受 locale 影响）。"""
    return f"{dt.day:02d}-{_month_name(dt.month)}-{dt.year}"


def _parse_fetch(data):
    """把 imaplib FETCH 返回的 data 规整为 [(meta_bytes, payload_bytes|None)]。"""
    out = []
    pending_meta = None
    for item in data or []:
        if isinstance(item, tuple):
            meta, payload = item[0], item[1]
            out.append((meta if isinstance(meta, bytes) else str(meta).encode(), payload))
            pending_meta = None
        elif isinstance(item, bytes):
            if pending_meta is None:
                pending_meta = item
            else:
                out.append((pending_meta, None))
                pending_meta = item
    return out


def _move_to_trash(imap, uids, trash):
    """优先 UID MOVE；不支持则 COPY + \\Deleted + UID EXPUNGE。返回 (成功数, 失败uid)"""
    moved, failed = 0, []
    for i in range(0, len(uids), 40):
        batch = uids[i:i + 40]
        uid_set = ",".join(batch)
        try:
            typ, _ = imap.uid("MOVE", uid_set, trash)
            if typ == "OK":
                moved += len(batch)
                continue
        except Exception as e:
            log.warning(f"UID MOVE 不可用，回退 COPY+DELETE：{e}")
        for uid in batch:
            try:
                typ, _ = imap.uid("COPY", uid, trash)
                if typ == "OK":
                    imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                    moved += 1
                else:
                    failed.append(uid)
            except Exception:
                failed.append(uid)
        try:
            imap.uid("EXPUNGE", uid_set)
        except Exception as e:
            log.warning(f"UID EXPUNGE 失败（邮件已复制到回收站，仅残留删除标记）：{e}")
    return moved, failed


# ---------------- 主流程 ----------------

def _run_account(acct, dry, handled, state, digest, ignore_handled=False):
    """巡检单个邮箱账号。

    handled 为该账号「已处理 UID」集合，函数内会就地更新（仅在非 dry 时持久化）。
    state / digest 为共享的云端状态对象，按账号分区写入。
    ignore_handled=True 用于规则调优：忽略去重、按当前规则重新判定全部邮件，
    但**始终不写状态、不移动邮件**。
    """
    result = {"account": acct.get("name"), "address": acct.get("account"),
              "host": acct.get("host"), "code": 0, "dry_run": dry}

    try:
        imap = _imap_connect(dry, acct)
    except Exception as e:
        result["code"] = 1
        result["msg"] = f"IMAP 连接失败: {e}"
        return result

    try:
        since = _imap_date(datetime.now(BJT) - timedelta(days=SCAN_DAYS))
        typ, data = imap.uid("SEARCH", None, f"(SINCE {since})")
        if typ != "OK":
            result["code"] = 2
            result["msg"] = f"IMAP SEARCH 失败: {data}"
            return result
        all_uids = (data[0] or b"").split()
        result["scanned_total"] = len(all_uids)

        # 新的候选（未处理过的），按 UID 倒序 = 最新优先，保证即时告警及时
        limit = RESCAN_LIMIT if ignore_handled else MAX_PROCESS
        if ignore_handled:
            fresh = list(reversed(all_uids))[:limit]
        else:
            fresh = [u for u in reversed(all_uids) if u.decode() not in handled][:limit]
        result["new_this_run"] = len(fresh)
        if not fresh:
            result["msg"] = "无新邮件待处理"
            return result

        # 1) 先取体积，决定哪些能安全下载全文
        sizes = {}
        typ, sdata = imap.uid("FETCH", ",".join(u.decode() for u in fresh), "(RFC822.SIZE)")
        if typ == "OK":
            for meta, _ in _parse_fetch(sdata):
                uid_m = re.search(rb"UID (\d+)", meta)
                size_m = re.search(rb"RFC822\.SIZE (\d+)", meta)
                if uid_m and size_m:
                    sizes[uid_m.group(1).decode()] = int(size_m.group(1))

        to_fetch = [u for u in fresh if sizes.get(u.decode(), 0) <= MAX_BODY_BYTES]
        headers_only = [u for u in fresh if u not in to_fetch]
        result["oversize_skipped_body"] = len(headers_only)

        msgs = []
        # 2) 正常邮件：取全文（BODY.PEEK 不改变已读状态）
        for i in range(0, len(to_fetch), 20):
            chunk = to_fetch[i:i + 20]
            typ, fdata = imap.uid("FETCH", ",".join(u.decode() for u in chunk),
                                  "(BODY.PEEK[] INTERNALDATE)")
            if typ != "OK":
                continue
            for meta, payload in _parse_fetch(fdata):
                if payload is None:
                    continue
                uid_m = re.search(rb"UID (\d+)", meta)
                if not uid_m:
                    continue
                msgs.append((uid_m.group(1).decode(), meta, payload))
        # 3) 超大邮件：仅取头部（保守，不做二维码判定）
        for i in range(0, len(headers_only), 20):
            chunk = headers_only[i:i + 20]
            typ, fdata = imap.uid("FETCH", ",".join(u.decode() for u in chunk),
                                  "(BODY.PEEK[HEADER] INTERNALDATE)")
            if typ != "OK":
                continue
            for meta, payload in _parse_fetch(fdata):
                if payload is None:
                    continue
                uid_m = re.search(rb"UID (\d+)", meta)
                if uid_m:
                    msgs.append((uid_m.group(1).decode(), meta, payload))

        # 4) 分类
        to_trash, important, kept, skipped = [], [], [], []
        for uid, meta, payload in msgs:
            try:
                msg = email.message_from_bytes(payload)
            except Exception:
                skipped.append(uid)
                continue
            subject = _hdr(msg.get("Subject", "")) or "(无主题)"
            from_addr = _addr_of(msg)
            from_name = _name_of(msg)
            message_id = msg.get("Message-ID", "")
            list_unsub = msg.get("List-Unsubscribe", "")
            precedence = msg.get("Precedence", "")
            is_oversize = sizes.get(uid, 0) > MAX_BODY_BYTES
            if is_oversize:
                body_text, image_count, filenames = "", 0, []
            else:
                body_text, image_count, filenames = _body_and_images(msg)
            dt = None
            try:
                dt = parsedate_to_datetime(msg.get("Date", "")) if msg.get("Date") else None
            except Exception:
                dt = None
            if dt is None:
                im = re.search(rb'INTERNALDATE "([^"]+)"', meta)
                if im:
                    try:
                        dt = parsedate_to_datetime(im.group(1).decode())
                    except Exception:
                        dt = None
            dt = _to_tz(dt)
            age = _age_minutes(dt)

            label, reason = classify(subject, from_addr, from_name, message_id,
                                     list_unsub, precedence, body_text, image_count, age)
            item = {
                "uid": uid,
                "account": acct.get("name"),
                "date": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                "from": from_addr,
                "from_name": from_name,
                "subject": subject[:120],
                "label": label,
                "reason": reason,
                "images": filenames[:3],
                "snippet": body_text[:160],
            }
            if label in ("SPAM", "QR_EXPIRED", "CODE_EXPIRED"):
                to_trash.append(item)
            elif label == "IMPORTANT":
                important.append(item)
                kept.append(item)
            else:
                kept.append(item)

        # 5) 移入回收站
        trash_name = None
        trashed_items = []
        all_trash = list(to_trash)   # 本轮判定为「该清理」的全部邮件，供统计与审计
        if to_trash and not dry:
            trash_name = _find_trash(imap, acct)
            if trash_name is None:
                result["trash_error"] = "未能定位回收站文件夹，已跳过移动（未删除任何邮件）"
                to_trash, kept = [], kept + to_trash
                all_trash = []
            else:
                allowed = to_trash[:MAX_TRASH]
                deferred = to_trash[MAX_TRASH:]
                moved, failed = _move_to_trash(imap, [x["uid"] for x in allowed], trash_name)
                result["trash_folder"] = trash_name
                result["moved_to_trash"] = moved
                if failed:
                    result["trash_failed"] = failed
                # 未移动成功的下轮重试；被 deferred 的也留在待处理区
                trashed_items = [x for x in allowed if x["uid"] not in failed]
                to_trash = deferred + [x for x in allowed if x["uid"] in failed]
        elif to_trash:
            result["trash_folder"] = _find_trash(imap, acct) or "(dry-run 未解析)"
            trashed_items = list(to_trash[:MAX_TRASH])

        result["trash_candidates"] = len(to_trash)
        result["trashed_count"] = len(trashed_items)
        result["by_label"] = {
            "spam": sum(1 for x in all_trash if x["label"] == "SPAM"),
            "code_expired": sum(1 for x in all_trash if x["label"] == "CODE_EXPIRED"),
            "qr_expired": sum(1 for x in all_trash if x["label"] == "QR_EXPIRED"),
            "important": len(important),
            "normal": sum(1 for x in kept if x["label"] == "NORMAL"),
        }
        result["trash_list"] = [{"subject": x["subject"], "from": x["from"],
                                "date": x["date"], "label": x["label"],
                                "reason": x["reason"]} for x in all_trash[:40]]
        result["important_list"] = [{"subject": x["subject"], "from": x["from"],
                                    "date": x["date"], "reason": x["reason"]} for x in important[:20]]

        # 7.5) 重要邮件去重：同一「发件人+主题」在窗口内只即时推一次。
        #      演练模式也计算并回报，便于事先确认规则效果；但不写状态。
        important_push, important_deduped, dedup_hist = _split_dedup(
            important, state, IMPORTANT_DEDUP_HOURS)
        result["important_push"] = len(important_push)
        result["important_deduped"] = len(important_deduped)
        result["dedup_window_hours"] = IMPORTANT_DEDUP_HOURS
        if important_deduped:
            result["deduped_list"] = [{"subject": x["subject"], "from": x["from"],
                                       "date": x["date"]} for x in important_deduped[:20]]

        # 6) 已处理标记：本轮全部计入（下轮不再重复判定）
        #    演练模式绝不写状态，否则下次正式运行会把这些邮件当作"已处理"而跳过。
        aname = acct.get("name") or "默认"
        if not dry and not ignore_handled:
            for uid, _, _ in msgs:
                handled.add(uid)
            handled_map = state.setdefault("handled", {})
            handled_map[aname] = sorted(handled)[-3000:]
            state.pop("handled_uids", None)   # 迁移：清掉旧版扁平字段
            state["runs"] = int(state.get("runs", 0)) + 1
            state["last_run"] = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
            state["last_result"] = {"account": aname, "by_label": result["by_label"],
                                    "dry_run": dry}
            _cos_put(STATE_KEY, state)

        # 7) 摘要暂存（供每日 10:00 生成中文日报）
        if not dry:
            today = datetime.now(BJT).strftime("%Y-%m-%d")
            if digest.get("date") != today:
                # 就地清空而非重新绑定：digest 由调用方共享，多账号时才能看到同一份
                digest.clear()
                digest.update({"date": today, "entries": [], "trashed": [], "updated_at": ""})
            entries = digest.setdefault("entries", [])
            existing = {(e.get("account"), e["uid"]) for e in entries}
            for x in kept:
                if (x.get("account"), x["uid"]) not in existing:
                    entries.append(x)
                    existing.add((x.get("account"), x["uid"]))
            digest["entries"] = entries[-DIGEST_MAX_ENTRIES:]

            # 记录本轮清理了哪些邮件：让「删除」这件事可审计、可追溯
            tlog = digest.setdefault("trashed", [])
            seen_t = {(t.get("account"), t.get("uid")) for t in tlog}
            for x in trashed_items:
                if (x.get("account"), x["uid"]) in seen_t:
                    continue
                tlog.append({"uid": x["uid"], "account": x.get("account"),
                             "subject": x["subject"], "from": x["from"],
                             "date": x["date"], "label": x["label"],
                             "reason": x["reason"]})
                seen_t.add((x.get("account"), x["uid"]))
            digest["trashed"] = tlog[-DIGEST_MAX_ENTRIES:]

            digest["updated_at"] = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
            _cos_put(DIGEST_KEY, digest)
            result["digest_pending"] = len(digest["entries"])
            result["digest_trashed"] = len(digest["trashed"])

        # 8) 重要邮件即时推送（已按去重窗口过滤）
        if important_push and not dry:
            lines = [f"## ⭐ {aname} 重要邮件 · {datetime.now(BJT).strftime('%m-%d %H:%M')}"]
            if important_deduped:
                lines.append(f"> 本轮扫描 {len(msgs)} 封，重要 **{len(important)}** 封"
                             f"（其中 {len(important_deduped)} 封与近期重复，已合并）")
            else:
                lines.append(f"> 本轮扫描 {len(msgs)} 封，重要 **{len(important)}** 封")
            lines.append("")
            for i, x in enumerate(important_push[:8], 1):
                lines.append(f"{i}. **{x['subject']}**")
                lines.append(f"   {x['from']} · {x['date']}")
                lines.append(f"   <font color=\"comment\">{x['reason']}</font>")
            if len(important_push) > 8:
                lines.append(f"…另有 {len(important_push) - 8} 封")
            _ok, _err = push_wecom("\n".join(lines), channel="alert")
            result["pushed"] = _ok
            if not _ok:
                result["push_error"] = _err
            else:
                # 仅在推送成功后才登记去重记录，避免推送失败却把邮件标记为"已推过"
                state["pushed_important"] = dedup_hist
                _cos_put(STATE_KEY, state)
                result["dedup_recorded"] = len(dedup_hist)
        return result
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _run(force_dry=None, ignore_handled=False):
    """巡检全部已配置账号，并把结果合并为一份报告。

    - 单个账号失败不影响其他账号；
    - 状态按账号分区存储（state.handled = {账号名: [uid...]}）；
    - ignore_handled=True 时忽略去重、按当前规则重判全部邮件。
      配合 force_dry=True 即为「规则调优」模式（只读）；
      配合 DRY_RUN=0 即为「重新清理」模式（会把已有邮件按新规则真正移入回收站）。
    """
    if not (COS_BUCKET and COS_REGION and COS_SECRET_ID and COS_SECRET_KEY):
        return {"code": 1, "msg": "COS 环境变量未配置完整"}
    if not ACCOUNTS:
        return {"code": 1, "msg": "未配置任何邮箱账号（MAIL_ACCOUNTS 或 QQ_EMAIL_ACCOUNT）"}

    dry = DRY_RUN if force_dry is None else force_dry

    state = _cos_get(STATE_KEY, {"handled": {}, "runs": 0, "last_run": ""})
    digest = _cos_get(DIGEST_KEY, {"date": "", "entries": [], "trashed": [], "updated_at": ""})

    # 迁移旧版扁平结构：把 handled_uids 归到第一个账号名下
    legacy = state.pop("handled_uids", None)
    handled_map = state.setdefault("handled", {})
    if legacy and ACCOUNTS:
        first = ACCOUNTS[0].get("name") or "默认"
        if not handled_map.get(first):
            handled_map[first] = [str(x) for x in legacy]

    per_account, warnings = [], []
    for acct in ACCOUNTS:
        name = acct.get("name") or "默认"
        if not (acct.get("account") and acct.get("auth_code")):
            msg = "未配置邮箱地址或授权码，已跳过"
            warnings.append(f"[{name}] {msg}")
            per_account.append({"account": name, "code": 3, "msg": msg})
            continue
        handled = set(str(x) for x in handled_map.get(name, []))
        try:
            per_account.append(
                _run_account(acct, dry, handled, state, digest, ignore_handled))
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            warnings.append(f"[{name}] {msg}")
            per_account.append({"account": name, "address": acct.get("account"),
                                "code": 9, "msg": msg})

    merged = {
        "code": 0 if any(r.get("code") == 0 for r in per_account) else 1,
        "dry_run": dry,
        "accounts": per_account,
    }
    if ignore_handled:
        merged["ignore_handled"] = True
    for key in ("scanned_total", "new_this_run", "trashed_count", "important_push",
                "important_deduped", "digest_pending", "digest_trashed"):
        vals = [r[key] for r in per_account if key in r]
        if vals:
            merged[key] = sum(vals)
    bools = [r["pushed"] for r in per_account if "pushed" in r]
    if bools:
        merged["pushed"] = any(bools)
    bl = {}
    for r in per_account:
        for k, v in (r.get("by_label") or {}).items():
            bl[k] = bl.get(k, 0) + v
    if bl:
        merged["by_label"] = bl
    if warnings:
        merged["warnings"] = warnings
    return merged


def _probe(ev=None):
    """连接诊断：不移动任何邮件，只报告服务器、登录结果与文件夹结构。

    可通过 event 覆盖 host/port/protocol/account/auth_code，便于在不重新部署的情况下
    对比不同服务器或凭证组合。
    """
    ev = ev or {}
    # 未显式传入时，回退到第一个已配置账号（多账号下 IMAP_HOST/ACCOUNT 可能为空）
    first = (ACCOUNTS[0] if ACCOUNTS else {})
    host = ev.get("host") or first.get("host") or IMAP_HOST
    port = int(ev.get("port") or first.get("port") or IMAP_PORT)
    account = ev.get("account") or first.get("account") or ACCOUNT
    code = ev.get("auth_code") or first.get("auth_code") or AUTH_CODE
    protocol = (ev.get("protocol") or "imap").lower()

    out = {"host": f"{host}:{port}", "protocol": protocol, "account": account,
           "auth_code_len": len(code),
           "send_id": _should_send_id() and protocol == "imap"}
    conn = None
    try:
        if protocol == "pop3":
            conn = poplib.POP3_SSL(host, port, timeout=25)
            out["banner"] = (conn.welcome or b"").decode("utf-8", "replace")
            conn.user(account)
            conn.pass_(code)
            out["login"] = "OK"
            n, size = conn.stat()
            out["message_count"] = n
            out["mailbox_size"] = size
        else:
            conn = imaplib.IMAP4_SSL(host, port, timeout=25)
            out["banner"] = (conn.welcome.decode("utf-8", "replace")
                             if isinstance(conn.welcome, bytes) else str(conn.welcome))
            conn.login(account, code)
            out["login"] = "OK"
            if _should_send_id():
                try:
                    conn.xatom("ID", IMAP_ID_CMD)
                    out["id_cmd"] = "sent"
                except Exception as e:
                    out["id_cmd"] = f"failed: {e}"
            typ, rows = conn.list()
            out["folders"] = [r.decode("utf-8", "replace") if isinstance(r, bytes) else str(r)
                              for r in (rows or [])]
            out["trash_folder"] = _find_trash(conn)
            typ, cnt = conn.select("INBOX", readonly=True)
            out["inbox_total"] = cnt[0].decode() if cnt and cnt[0] else "?"
    except Exception as e:
        out["login"] = "FAILED"
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
    return {"code": 0 if out.get("login") == "OK" else 1, "probe": out}


def main_handler(event, context):
    try:
        event = event if isinstance(event, dict) else {}
    except Exception:
        event = {}
    action = (event or {}).get("action", "")

    if action == "preview":          # 演练：只判定不移动
        return _run(force_dry=True)
    if action == "rescan":           # 规则调优：忽略去重重判全部邮件，只读不改动邮箱
        return _run(force_dry=True, ignore_handled=True)
    if action == "reclaim":          # 重新清理：按当前规则重判并真正移入回收站
        return _run(ignore_handled=True)
    if action == "accounts":         # 查看已配置的账号（不回显凭证）
        return {"code": 0, "count": len(ACCOUNTS),
                "accounts": [{"name": a.get("name"), "address": a.get("account"),
                              "host": a.get("host"), "port": a.get("port"),
                              "auth_code_set": bool(a.get("auth_code"))}
                             for a in ACCOUNTS]}
    if action == "clear_digest":     # 清空摘要暂存区（日报发完后调用）
        _cos_put(DIGEST_KEY, {"date": "", "entries": [], "trashed": [], "updated_at": ""})
        return {"code": 0, "msg": "摘要暂存已清空"}
    if action == "probe":            # 连接诊断：主机、登录、文件夹列表
        return _probe(event)
    if action == "get_digest":       # 读取摘要暂存区
        return {"code": 0, "digest": _cos_get(
            DIGEST_KEY, {"date": "", "entries": [], "trashed": []})}
    if action == "push_report":      # 把日报 Markdown 推到「日报专用机器人」
        md = (event or {}).get("markdown", "")
        if not md:
            return {"code": 1, "msg": "缺少 markdown 字段"}
        ok, err = push_wecom(md, channel="report")
        return {"code": 0 if ok else 1, "pushed": ok, "channel": "report",
                "error": err,
                "webhook_configured": bool(WECOM_WEBHOOK_REPORT),
                "dedicated": bool(os.environ.get("WECOM_WEBHOOK_REPORT", ""))}
    if action == "push_alert":       # 把消息推到「告警机器人」（自检用）
        md = (event or {}).get("markdown", "")
        ok, err = push_wecom(md, channel="alert")
        return {"code": 0 if ok else 1, "pushed": ok, "channel": "alert",
                "error": err,
                "webhook_configured": bool(WECOM_WEBHOOK)}
    if action == "channels":         # 查看当前双通道配置状态（不回显 URL）
        return {"code": 0,
                "alert_configured": bool(WECOM_WEBHOOK),
                "report_configured": bool(os.environ.get("WECOM_WEBHOOK_REPORT", "")),
                "report_dedicated": bool(os.environ.get("WECOM_WEBHOOK_REPORT", "")),
                "report_effective": bool(WECOM_WEBHOOK_REPORT)}
    if action == "stats":
        st = _cos_get(STATE_KEY, {})
        handled_map = st.get("handled", {}) or {}
        return {"code": 0,
                "state": {k: v for k, v in st.items()
                          if k not in ("handled", "handled_uids", "pushed_important")},
                "handled_by_account": {k: len(v) for k, v in handled_map.items()},
                "handled_total": sum(len(v) for v in handled_map.values()),
                "dedup_keys": len(st.get("pushed_important") or {})}
    return _run()


# ---------------- 本地运行（同一份逻辑，上云前先用它验证） ----------------
# 用法:
#   python handler.py --once --dry-run         本机演练，只判定不移动邮件
#   python handler.py --once                   本机正式执行
#   python handler.py --test-rules             规则自检（不连邮箱）
# 凭证与配置可放在同目录 .env（见 .env.example），也可用系统环境变量。

def _load_dotenv(path):
    if not os.path.exists(path):
        return False
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return True


def _reload_globals():
    """模块级常量在 import 时就固化了；加载 .env 后需要重新读取一次。"""
    g = globals()
    g["ACCOUNT"] = os.environ.get("QQ_EMAIL_ACCOUNT", "")
    g["AUTH_CODE"] = os.environ.get("QQ_EMAIL_AUTH_CODE", "")
    g["IMAP_HOST"] = os.environ.get("IMAP_HOST", "imap.qq.com")
    g["IMAP_PORT"] = int(os.environ.get("IMAP_PORT", "993"))
    g["IMAP_NEED_ID"] = os.environ.get("IMAP_NEED_ID", "auto")
    g["WECOM_WEBHOOK"] = os.environ.get("WECOM_WEBHOOK", "")
    g["WECOM_WEBHOOK_REPORT"] = (os.environ.get("WECOM_WEBHOOK_REPORT", "")
                                 or os.environ.get("WECOM_WEBHOOK", ""))
    g["COS_BUCKET"] = os.environ.get("COS_BUCKET", "")
    g["COS_REGION"] = os.environ.get("COS_REGION", "")
    g["COS_SECRET_ID"] = os.environ.get("COS_SECRET_ID", "")
    g["COS_SECRET_KEY"] = os.environ.get("COS_SECRET_KEY", "")
    g["STATE_KEY"] = os.environ.get("STATE_KEY", "qq-mail-triage-state.json")
    g["DIGEST_KEY"] = os.environ.get("DIGEST_KEY", "qq-mail-digest.json")
    g["TRASH_FOLDER"] = os.environ.get("TRASH_FOLDER", "")
    g["SCAN_DAYS"] = int(os.environ.get("SCAN_DAYS", "7"))
    g["QR_EXPIRE_MINUTES"] = int(os.environ.get("QR_EXPIRE_MINUTES", "30"))
    g["CODE_EXPIRE_MINUTES"] = int(os.environ.get("CODE_EXPIRE_MINUTES", "30"))
    g["CODE_TRASH_PROTECTED"] = os.environ.get("CODE_TRASH_PROTECTED", "0") == "1"
    g["MAX_PROCESS"] = int(os.environ.get("MAX_PROCESS_PER_RUN", "25"))
    g["MAX_TRASH"] = int(os.environ.get("MAX_TRASH_PER_RUN", "60"))
    g["DRY_RUN"] = os.environ.get("DRY_RUN", "0") == "1"
    g["IMPORTANT_DEDUP_HOURS"] = float(os.environ.get("IMPORTANT_DEDUP_HOURS", "24"))
    g["PROTECT_EXTRA"] = [s.strip().lower()
                          for s in os.environ.get("PROTECT_EXTRA", "").split(",") if s.strip()]
    g["AD_DOMAINS"] = [s.strip().lower()
                       for s in os.environ.get("AD_DOMAINS", "").split(",") if s.strip()]
    g["AD_EXTRA_KEYWORDS"] = [s.strip().lower()
                              for s in os.environ.get("AD_EXTRA_KEYWORDS", "").split(",")
                              if s.strip()]
    g["MAIL_ACCOUNTS_RAW"] = os.environ.get("MAIL_ACCOUNTS", "")
    _reload_accounts()


if __name__ == "__main__":
    import argparse
    import sys

    _load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    _reload_globals()

    ap = argparse.ArgumentParser(description="QQ 邮箱巡检（本地运行模式）")
    ap.add_argument("--once", action="store_true", help="执行一轮巡检")
    ap.add_argument("--dry-run", action="store_true", help="只判定不移动邮件，也不推送")
    ap.add_argument("--days", type=int, help=f"扫描天数，默认 {SCAN_DAYS}")
    ap.add_argument("--limit", type=int, help=f"单轮处理上限，默认 {MAX_PROCESS}")
    ap.add_argument("--test-rules", action="store_true", help="规则自检（不连邮箱）")
    args = ap.parse_args()

    if args.test_rules:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from test_rules import main as rules_main
        sys.exit(rules_main())

    if args.days:
        SCAN_DAYS = args.days
        globals()["SCAN_DAYS"] = args.days
    if args.limit:
        globals()["MAX_PROCESS"] = args.limit

    if not args.once and not args.dry_run:
        ap.print_help()
        sys.exit(0)

    print(f"配置：邮箱={'已设置' if ACCOUNT else '未设置'}  授权码={'已设置' if AUTH_CODE else '未设置'}  "
          f"告警机器人={'已设置' if WECOM_WEBHOOK else '未设置'}  "
          f"日报机器人={'独立' if os.environ.get('WECOM_WEBHOOK_REPORT') else '回退告警'}")
    out = _run(force_dry=True if args.dry_run else None)
    print(json.dumps(out, ensure_ascii=False, indent=2))
