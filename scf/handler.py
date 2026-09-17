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
MAX_PROCESS = int(os.environ.get("MAX_PROCESS_PER_RUN", "25"))
MAX_TRASH = int(os.environ.get("MAX_TRASH_PER_RUN", "60"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
# 重要邮件即时推送去重窗口（小时）。同一「发件人+主题」在窗口内只推一次；
# 设为 0 关闭去重（每封都推）。注意：去重只影响即时推送，日报中仍然全部保留。
IMPORTANT_DEDUP_HOURS = float(os.environ.get("IMPORTANT_DEDUP_HOURS", "24"))

MAX_BODY_BYTES = 1_000_000  # 超过 1MB 的邮件只取头部，避免超时
DIGEST_MAX_ENTRIES = 400

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
]

IMPORTANT_SUBJECT_KEYWORDS = [
    "验证码", "校验码", "动态密码", "登录提醒", "异常登录", "密码", "安全提醒", "安全验证",
    "账单", "发票", "报销", "对账", "报表",
    "面试", "笔试", "offer", "录用", "录取", "复试", "宣讲", "招聘", "实习", "兼职",
    "通知", "提醒", "确认", "合同", "承诺书", "截止", "deadline",
    "订单", "发货", "物流", "签收", "退款", "支付", "扣款", "还款", "逾期", "转账",
    "报名", "成绩", "奖学金", "答辩", "学籍", "选课", "考试", "分数线",
    "会议", "邀请函", "中签", "纳税", "汇算", "医保", "社保", "公积金", "信用卡", "银行",
    "结算", "结算单", "工资", "入职",
]

# 二维码类邮件的识别关键词（主题或正文命中，且邮件内含图片 → 视为二维码邮件）
QR_KEYWORDS = [
    "二维码", "扫码", "扫一扫", "收款码", "付款码", "取件码", "登录确认", "扫码登录",
    "扫描下方", "长按识别", "qrcode", "qr code", "scan the code", "scan to",
]

AD_SENDER_RE = re.compile(
    r"(no-?reply|noreply|newsletter|marketing|promo|edm|mailer|mailer-daemon|broadcast"
    r"|notify|notification|service-?mail|advert)", re.I)
AD_MESSAGEID_RE = re.compile(
    r"(bulk|massmail|newsletter|campaign|edm|marketing|mailchimp|sendcloud|sendgrid|mailgun)", re.I)
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


def classify(subject, from_addr, from_name, message_id, list_unsub, precedence,
             body_text="", image_count=0, age_min=None):
    """返回 (label, reason)。label ∈ {SPAM, QR_EXPIRED, IMPORTANT, NORMAL}"""
    domain = from_addr.split("@")[-1] if "@" in from_addr else ""
    subj = (subject or "").lower()
    haystack = f"{subj} {from_name}"

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

    # --- 重要判定 ---
    important_hits = [k for k in IMPORTANT_SUBJECT_KEYWORDS if k in subj]
    important_domain = next((d for d in IMPORTANT_DOMAINS if d in domain), None)

    if protected:
        label = "IMPORTANT" if (important_hits or important_domain) else "NORMAL"
        return label, f"受保护域({protected})，永不清理"

    # --- 广告计分 ---
    score = 0
    signals = []
    hits = [k for k in AD_SUBJECT_KEYWORDS if k in subj]
    if hits:
        score += min(len(hits), 2)
        signals.append(f"主题词x{len(hits)}({','.join(hits[:3])})")
    if AD_SENDER_RE.search(from_addr):
        score += 1
        signals.append("发件人批量特征")
    if AD_MESSAGEID_RE.search(message_id or ""):
        score += 1
        signals.append("批量发信ID")
    if list_unsub:
        score += 1
        signals.append("含一键退订头")
    if (precedence or "").lower() in ("bulk", "list", "junk"):
        score += 2
        signals.append(f"Precedence:{precedence}")

    is_freemail = domain in FREEMAIL_DOMAINS
    threshold = 4 if is_freemail else 2

    if score >= threshold:
        return "SPAM", f"广告分{score}/门槛{threshold}：{' / '.join(signals[:3])}"

    # --- 二维码邮件（含图片 + 二维码关键词 + 已超时） ---
    qr_hit = next((k for k in QR_KEYWORDS if k in subj or k in (body_text or "").lower()), None)
    if qr_hit and image_count > 0:
        if age_min is not None and age_min >= QR_EXPIRE_MINUTES:
            return "QR_EXPIRED", f"含二维码图片(命中“{qr_hit}”)且已过 {int(age_min)} 分钟 > {QR_EXPIRE_MINUTES} 分钟"
        return "IMPORTANT", f"含二维码图片(命中“{qr_hit}”)且仍在有效期内"

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
    """重要邮件去重键 = 发件人 + 规范化主题。

    去掉 Re:/Fw:/回复/转发 等前缀与多余空白后小写化，
    使「同一主题连发多封」归并为同一个键。
    """
    subj = item.get("subject", "") or ""
    subj = re.sub(r"^\s*(re|fw|fwd|回复|答复|转发)\s*[:：]\s*", "", subj, flags=re.I)
    subj = re.sub(r"^(回复|转发)[:：]?\s*", "", subj)
    subj = re.sub(r"\s+", " ", subj).strip().lower()
    return f"{(item.get('from') or '').lower()}|{subj}"


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

def _should_send_id():
    if IMAP_NEED_ID == "1":
        return True
    if IMAP_NEED_ID == "0":
        return False
    return bool(re.search(r"163\.com|126\.com|yeah\.net|188\.com", IMAP_HOST, re.I))


def _imap_connect(readonly):
    if not (ACCOUNT and AUTH_CODE):
        raise RuntimeError("环境变量 QQ_EMAIL_ACCOUNT / QQ_EMAIL_AUTH_CODE 未配置")
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(ACCOUNT, AUTH_CODE)
    # 网易系邮箱必须先发 ID，再做任何邮箱操作，否则报 Unsafe Login
    if _should_send_id():
        try:
            imap.xatom("ID", IMAP_ID_CMD)
            log.info("已发送 IMAP ID 命令（网易系邮箱必需）")
        except Exception as e:
            log.warning(f"发送 IMAP ID 命令失败（将继续尝试）: {e}")
    imap.select("INBOX", readonly=readonly)
    return imap


def _find_trash(imap):
    """定位回收站文件夹，返回 IMAP 语法中可直接使用的名字（含引号）。"""
    if TRASH_FOLDER:
        return TRASH_FOLDER if TRASH_FOLDER.startswith('"') else f'"{TRASH_FOLDER}"'
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
    # 优先 \\Trash 特殊属性；其次常见命名
    for flags, name in candidates:
        if "\\Trash" in flags:
            return name
    for flags, name in candidates:
        if re.search(r"deleted|trash|已删除|垃圾", name, re.I):
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

def _run(force_dry=None):
    if not (COS_BUCKET and COS_REGION and COS_SECRET_ID and COS_SECRET_KEY):
        return {"code": 1, "msg": "COS 环境变量未配置完整"}

    dry = DRY_RUN if force_dry is None else force_dry
    state = _cos_get(STATE_KEY, {"handled_uids": [], "runs": 0, "last_run": ""})
    handled = set(str(x) for x in state.get("handled_uids", []))
    digest = _cos_get(DIGEST_KEY, {"date": "", "entries": [], "updated_at": ""})

    try:
        imap = _imap_connect(readonly=dry)
    except Exception as e:
        return {"code": 1, "msg": f"IMAP 连接失败: {e}"}

    result = {"code": 0, "dry_run": dry}
    try:
        since = _imap_date(datetime.now(BJT) - timedelta(days=SCAN_DAYS))
        typ, data = imap.uid("SEARCH", None, f"(SINCE {since})")
        if typ != "OK":
            return {"code": 2, "msg": f"IMAP SEARCH 失败: {data}"}
        all_uids = (data[0] or b"").split()
        result["scanned_total"] = len(all_uids)

        # 新的候选（未处理过的），按 UID 倒序 = 最新优先，保证即时告警及时
        fresh = [u for u in reversed(all_uids) if u.decode() not in handled][:MAX_PROCESS]
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
                "date": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                "from": from_addr,
                "from_name": from_name,
                "subject": subject[:120],
                "label": label,
                "reason": reason,
                "images": filenames[:3],
                "snippet": body_text[:160],
            }
            if label in ("SPAM", "QR_EXPIRED"):
                to_trash.append(item)
            elif label == "IMPORTANT":
                important.append(item)
                kept.append(item)
            else:
                kept.append(item)

        # 5) 移入回收站
        trash_name = None
        if to_trash and not dry:
            trash_name = _find_trash(imap)
            if trash_name is None:
                result["trash_error"] = "未能定位回收站文件夹，已跳过移动（未删除任何邮件）"
                to_trash, kept = [], kept + to_trash
            else:
                allowed = to_trash[:MAX_TRASH]
                deferred = to_trash[MAX_TRASH:]
                moved, failed = _move_to_trash(imap, [x["uid"] for x in allowed], trash_name)
                result["trash_folder"] = trash_name
                result["moved_to_trash"] = moved
                if failed:
                    result["trash_failed"] = failed
                # 未移动成功的下轮重试；被 deferred 的也留在待处理区
                ok_items = [x for x in allowed if x["uid"] not in failed]
                to_trash = deferred + [x for x in allowed if x["uid"] in failed]
        elif to_trash:
            result["trash_folder"] = _find_trash(imap) or "(dry-run 未解析)"

        result["trash_candidates"] = len(to_trash)
        result["by_label"] = {
            "spam": sum(1 for x in to_trash if x["label"] == "SPAM"),
            "qr_expired": sum(1 for x in to_trash if x["label"] == "QR_EXPIRED"),
            "important": len(important),
            "normal": sum(1 for x in kept if x["label"] == "NORMAL"),
        }
        result["trash_list"] = [{"subject": x["subject"], "from": x["from"],
                                "date": x["date"], "reason": x["reason"]} for x in to_trash[:40]]
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
        if not dry:
            for uid, _, _ in msgs:
                handled.add(uid)
            state["handled_uids"] = sorted(handled)[-3000:]
            state["runs"] = int(state.get("runs", 0)) + 1
            state["last_run"] = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
            state["last_result"] = {"by_label": result["by_label"], "dry_run": dry}
            _cos_put(STATE_KEY, state)

        # 7) 摘要暂存（供每日 10:00 生成中文日报）
        if not dry:
            today = datetime.now(BJT).strftime("%Y-%m-%d")
            if digest.get("date") != today:
                digest = {"date": today, "entries": [], "updated_at": ""}
            entries = digest.get("entries", [])
            existing = {e["uid"] for e in entries}
            for x in kept:
                if x["uid"] not in existing:
                    entries.append(x)
            digest["entries"] = entries[-DIGEST_MAX_ENTRIES:]
            digest["updated_at"] = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
            _cos_put(DIGEST_KEY, digest)
            result["digest_pending"] = len(digest["entries"])

        # 8) 重要邮件即时推送（已按去重窗口过滤）
        if important_push and not dry:
            lines = [f"## ⭐ QQ邮箱重要邮件 · {datetime.now(BJT).strftime('%m-%d %H:%M')}"]
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


def _probe(ev=None):
    """连接诊断：不移动任何邮件，只报告服务器、登录结果与文件夹结构。

    可通过 event 覆盖 host/port/protocol/account/auth_code，便于在不重新部署的情况下
    对比不同服务器或凭证组合。
    """
    ev = ev or {}
    host = ev.get("host") or IMAP_HOST
    port = int(ev.get("port") or IMAP_PORT)
    account = ev.get("account") or ACCOUNT
    code = ev.get("auth_code") or AUTH_CODE
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
    if action == "clear_digest":     # 清空摘要暂存区（日报发完后调用）
        _cos_put(DIGEST_KEY, {"date": "", "entries": [], "updated_at": ""})
        return {"code": 0, "msg": "摘要暂存已清空"}
    if action == "probe":            # 连接诊断：主机、登录、文件夹列表
        return _probe(event)
    if action == "get_digest":       # 读取摘要暂存区
        return {"code": 0, "digest": _cos_get(DIGEST_KEY, {"date": "", "entries": []})}
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
        return {"code": 0, "state": {k: v for k, v in st.items() if k != "handled_uids"},
                "handled_count": len(st.get("handled_uids", []))}
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
    g["MAX_PROCESS"] = int(os.environ.get("MAX_PROCESS_PER_RUN", "25"))
    g["MAX_TRASH"] = int(os.environ.get("MAX_TRASH_PER_RUN", "60"))
    g["DRY_RUN"] = os.environ.get("DRY_RUN", "0") == "1"
    g["IMPORTANT_DEDUP_HOURS"] = float(os.environ.get("IMPORTANT_DEDUP_HOURS", "24"))
    g["PROTECT_EXTRA"] = [s.strip().lower()
                          for s in os.environ.get("PROTECT_EXTRA", "").split(",") if s.strip()]


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
