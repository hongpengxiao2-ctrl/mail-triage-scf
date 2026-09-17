"""
把 qq-mail-triage 部署为腾讯云函数（新建独立函数，可与既有函数共存互不影响）。

用法:
  python deploy_scf.py                       # 部署代码 + 触发器等（DRY_RUN 保持现值）
  python deploy_scf.py --dry-run 1           # 开启演练模式（只判定不移动邮件）
  python deploy_scf.py --dry-run 0           # 关闭演练模式（真正移入回收站）
  python deploy_scf.py --account x@qq.com --auth-code XXXXXXXXXXXX   # 写入邮箱凭证
  python deploy_scf.py --invoke              # 调用一次函数并打印返回
  python deploy_scf.py --status              # 查看函数与触发器状态

凭据来源（按优先级）：
  1. 环境变量 TENCENT_SECRET_ID / TENCENT_SECRET_KEY（推荐）
  2. 环境变量 TENCENT_CRED_FILE 指向的凭据文件
  3. 项目目录下自动查找 tencent_credentials.txt
凭据文件格式为每行 `KEY: VALUE`，需包含 COS_SECRET_ID / COS_SECRET_KEY /
COS_REGION / COS_BUCKET 四项。**凭据文件不要提交到版本库**（已在 .gitignore 中排除）。

可选：设置环境变量 SOURCE_FUNCTION 指向一个既有函数，
则从它的环境变量继承企业微信 Webhook 与 COS 配置，避免在多处重复维护同一份配置。
"""
import argparse
import base64
import json
import os
import re
import sys
import time

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.scf.v20180416 import scf_client, models


def _load_local_env():
    """加载同目录下的 `.env`（已 gitignore），用于本机运行配置。

    只在环境变量尚未存在时填充，因此命令行 `export` 的优先级更高。
    典型用途：在本文件里写 `TENCENT_CRED_FILE=<凭据文件路径>`，
    这样凭据文件可以留在项目之外，仓库里不含任何明文密钥。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_local_env()

FUNCTION_NAME = "qq-mail-triage"
REGION = os.environ.get("SCF_REGION", "ap-guangzhou")
NAMESPACE = "default"
HANDLER = "handler.main_handler"
RUNTIME = "Python3.10"
MEMORY_MB = 256          # TLS 握手与邮件解析偏 CPU，256MB 显著快于 128MB
TIMEOUT_S = 60           # 单轮最多处理 25 封邮件，需留足时间
CRON = os.environ.get("TRIAGE_CRON", "0 */5 * * * * *")   # 每 5 分钟

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 凭据文件候选位置：环境变量指定 > 项目根目录 > scf 目录
CRED_CANDIDATES = [
    os.environ.get("TENCENT_CRED_FILE", ""),
    os.path.join(PROJECT_ROOT, "tencent_credentials.txt"),
    os.path.join(PROJECT_ROOT, "scf", "tencent_credentials.txt"),
]
CRED_FILE = next((p for p in CRED_CANDIDATES if p and os.path.exists(p)), "")

# 可选：从既有函数继承共享配置（企业微信 Webhook / COS）。留空则不继承。
SOURCE_FUNCTION = os.environ.get("SOURCE_FUNCTION", "")

ZIP_PATH = os.path.join(PROJECT_ROOT, "qq-mail-triage-scf-deploy.zip")


def load_creds():
    """读取腾讯云密钥。优先环境变量，其次凭据文件。"""
    if os.environ.get("TENCENT_SECRET_ID") and os.environ.get("TENCENT_SECRET_KEY"):
        return (os.environ["TENCENT_SECRET_ID"],
                os.environ["TENCENT_SECRET_KEY"],
                os.environ.get("COS_REGION", REGION),
                os.environ.get("COS_BUCKET", ""))
    if not CRED_FILE:
        raise SystemExit(
            "❌ 未找到腾讯云凭据。请任选一种方式：\n"
            "   1) 设置环境变量 TENCENT_SECRET_ID / TENCENT_SECRET_KEY\n"
            "   2) 设置环境变量 TENCENT_CRED_FILE 指向凭据文件\n"
            f"   3) 在 {PROJECT_ROOT} 下创建 tencent_credentials.txt\n"
            "   凭据文件需含 COS_SECRET_ID / COS_SECRET_KEY / COS_REGION / COS_BUCKET，格式为 `KEY: 值`")
    txt = open(CRED_FILE, encoding="utf-8").read()

    def grab(key):
        m = re.search(rf"{key}:\s*(\S+)", txt)
        return m.group(1) if m else ""

    sid, skey = grab("COS_SECRET_ID"), grab("COS_SECRET_KEY")
    if not (sid and skey):
        raise SystemExit(f"❌ {CRED_FILE} 中未解析到 COS_SECRET_ID / COS_SECRET_KEY")
    return sid, skey, grab("COS_REGION") or REGION, grab("COS_BUCKET")


def client():
    sid, skey, _, _ = load_creds()
    hp = HttpProfile(endpoint="scf.tencentcloudapi.com", reqTimeout=60)
    return scf_client.ScfClient(credential.Credential(sid, skey), REGION,
                                ClientProfile(httpProfile=hp))


def env_as_dict(env):
    """GetFunction 返回的 Environment.Variables 可能是 dict 也可能是 [{Key,Value}]。"""
    if env is None:
        return {}
    v = getattr(env, "Variables", None)
    if v is None:
        return {}
    if isinstance(v, dict):
        return dict(v)
    out = {}
    for item in v:
        k = getattr(item, "Key", None) or (item.get("Key") if isinstance(item, dict) else None)
        val = getattr(item, "Value", None) or (item.get("Value") if isinstance(item, dict) else None)
        if k:
            out[k] = val
    return out


def fetch_source_env(c):
    """读取 SOURCE_FUNCTION 的环境变量用于继承共享配置；未配置则返回空。"""
    if not SOURCE_FUNCTION:
        return {}
    req = models.GetFunctionRequest()
    req.FunctionName = SOURCE_FUNCTION
    try:
        resp = c.GetFunction(req)
        return env_as_dict(getattr(resp, "Environment", None))
    except TencentCloudSDKException as e:
        print(f"⚠️ 无法读取 {SOURCE_FUNCTION} 的环境变量：{e}")
        return {}


def function_exists(c, name):
    req = models.ListFunctionsRequest()
    req.Limit = 50
    resp = c.ListFunctions(req)
    for f in resp.Functions or []:
        if f.FunctionName == name:
            return True
    return False


def function_status(c, name):
    req = models.GetFunctionRequest()
    req.FunctionName = name
    try:
        return c.GetFunction(req).Status
    except TencentCloudSDKException:
        return None


def delete_function(c, name):
    req = models.DeleteFunctionRequest()
    req.FunctionName = name
    req.Namespace = NAMESPACE
    c.DeleteFunction(req)


def build_env(c, args, current=None):
    src = fetch_source_env(c)
    base = dict(current or {})

    def pick(key, default=""):
        return base.get(key) or src.get(key) or default

    # COS 配置回退顺序：既有函数环境变量 > 本地凭据（环境变量或凭据文件）
    try:
        cred_sid, cred_skey, cred_region, cred_bucket = load_creds()
    except SystemExit:
        cred_sid = cred_skey = cred_region = cred_bucket = ""

    env = {
        "WECOM_WEBHOOK": pick("WECOM_WEBHOOK"),
        "WECOM_WEBHOOK_REPORT": args.report_webhook or pick("WECOM_WEBHOOK_REPORT"),
        "COS_BUCKET": pick("COS_BUCKET") or cred_bucket,
        "COS_REGION": pick("COS_REGION") or cred_region,
        "COS_SECRET_ID": pick("COS_SECRET_ID") or cred_sid,
        "COS_SECRET_KEY": pick("COS_SECRET_KEY") or cred_skey,
        "QQ_EMAIL_ACCOUNT": args.account or pick("QQ_EMAIL_ACCOUNT"),
        "QQ_EMAIL_AUTH_CODE": args.auth_code or pick("QQ_EMAIL_AUTH_CODE"),
        "IMAP_HOST": args.imap_host or base.get("IMAP_HOST") or "imap.qq.com",
        "IMAP_PORT": base.get("IMAP_PORT", "993"),
        "IMAP_NEED_ID": base.get("IMAP_NEED_ID", "auto"),
        "SCAN_DAYS": base.get("SCAN_DAYS", "7"),
        "QR_EXPIRE_MINUTES": base.get("QR_EXPIRE_MINUTES", "30"),
        "MAX_PROCESS_PER_RUN": base.get("MAX_PROCESS_PER_RUN", "25"),
        "MAX_TRASH_PER_RUN": base.get("MAX_TRASH_PER_RUN", "60"),
        "IMPORTANT_DEDUP_HOURS": base.get("IMPORTANT_DEDUP_HOURS", "24"),
        "DRY_RUN": base.get("DRY_RUN", "1"),
        "STATE_KEY": base.get("STATE_KEY", "qq-mail-triage-state.json"),
        "DIGEST_KEY": base.get("DIGEST_KEY", "qq-mail-digest.json"),
        "TZ": "Asia/Shanghai",
    }
    if args.dry_run is not None:
        env["DRY_RUN"] = "1" if args.dry_run == "1" else "0"
    return env


def as_variables(env):
    """腾讯云 SCF 的 Environment.Variables 要求 array 类型 [{Key,Value}]，不是 dict。"""
    return [{"Key": k, "Value": v} for k, v in env.items()]


def mask(env):
    sensitive = ("COS_SECRET_ID", "COS_SECRET_KEY", "QQ_EMAIL_AUTH_CODE")
    safe = {}
    for k, v in env.items():
        if k in sensitive:
            safe[k] = (v[:6] + "…(已脱敏)") if v else "(空)"
        elif k in ("WECOM_WEBHOOK", "WECOM_WEBHOOK_REPORT") and v:
            safe[k] = v.split("key=")[0] + "key=***"
        else:
            safe[k] = v
    return safe


def wait_active(c, name, timeout=180):
    """等待函数进入 Active。SCF 创建/更新是异步的，未就绪时建触发器会报 FailedOperation。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        st = function_status(c, name)
        if st != last:
            print(f"   函数状态：{st}")
            last = st
        if st == "Active":
            return True
        if st in ("CreateFailed", "UpdateFailed"):
            req = models.GetFunctionRequest()
            req.FunctionName = name
            r = c.GetFunction(req)
            print(f"❌ 函数进入 {st}：{getattr(r, 'StatusReasons', None)}")
            return False
        time.sleep(4)
    print(f"⚠️ 等待 {timeout}s 后函数仍未就绪（当前 {last}）")
    return False


def create_or_update(c, args):
    if not os.path.exists(ZIP_PATH):
        print(f"❌ 部署包不存在：{ZIP_PATH}（请先运行 build_scf.py）")
        return 1
    zip_b64 = base64.b64encode(open(ZIP_PATH, "rb").read()).decode()
    size_mb = os.path.getsize(ZIP_PATH) / 1024 / 1024

    exists = function_exists(c, FUNCTION_NAME)
    # 创建失败态（例如未开通 CLS）无法更新代码，必须先删除重建
    if exists and function_status(c, FUNCTION_NAME) == "CreateFailed":
        print("→ 检测到函数处于 CreateFailed 状态，删除后重建…")
        delete_function(c, FUNCTION_NAME)
        exists = False

    current = {}
    if exists:
        req = models.GetFunctionRequest()
        req.FunctionName = FUNCTION_NAME
        try:
            current = env_as_dict(getattr(c.GetFunction(req), "Environment", None))
        except TencentCloudSDKException:
            pass

    env = build_env(c, args, current)
    missing = [k for k, v in env.items() if k in ("WECOM_WEBHOOK",) and not v]
    print(f"部署包 {size_mb:.2f} MB  ->  函数 {FUNCTION_NAME} ({REGION}/{NAMESPACE})")
    print("环境变量：" + json.dumps(mask(env), ensure_ascii=False, indent=2))
    if not env.get("QQ_EMAIL_ACCOUNT") or not env.get("QQ_EMAIL_AUTH_CODE"):
        print("⚠️ QQ 邮箱凭证未配置：函数可部署，但在补齐 QQ_EMAIL_ACCOUNT / QQ_EMAIL_AUTH_CODE 前"
              "每次调用会安全退出且不会连接邮箱。")
    if missing:
        print(f"⚠️ 缺少继承项：{missing}")

    try:
        if exists:
            print("→ 更新函数代码…")
            ucode = models.UpdateFunctionCodeRequest()
            ucode.FunctionName = FUNCTION_NAME
            ucode.Namespace = NAMESPACE
            ucode.Handler = HANDLER
            ucode.ZipFile = zip_b64
            ucode.InstallDependency = "FALSE"
            ucode.Publish = "FALSE"
            c.UpdateFunctionCode(ucode)
            if not wait_active(c, FUNCTION_NAME):
                return 1

            print("→ 更新函数配置…")
            uconf = models.UpdateFunctionConfigurationRequest()
            uconf.FunctionName = FUNCTION_NAME
            uconf.Namespace = NAMESPACE
            uconf.MemorySize = MEMORY_MB
            uconf.Timeout = TIMEOUT_S
            uconf.Description = "QQ邮箱巡检：广告/垃圾/过期二维码移入回收站，重要邮件即时推送，保留邮件写入日报暂存"
            env_obj = models.Environment()
            env_obj.Variables = as_variables(env)
            uconf.Environment = env_obj
            c.UpdateFunctionConfiguration(uconf)
        else:
            print("→ 新建函数…")
            req = models.CreateFunctionRequest()
            req.FunctionName = FUNCTION_NAME
            req.Namespace = NAMESPACE
            req.Runtime = RUNTIME
            req.Handler = HANDLER
            req.Role = ""
            req.MemorySize = MEMORY_MB
            req.Timeout = TIMEOUT_S
            req.InstallDependency = "FALSE"
            # 账号未开通 CLS 日志服务时，必须关闭自动创建日志主题，否则函数会进入 CreateFailed
            req.AutoCreateClsTopic = "FALSE"
            req.Description = "QQ邮箱巡检：广告/垃圾/过期二维码移入回收站，重要邮件即时推送，保留邮件写入日报暂存"
            code = models.Code()
            code.ZipFile = zip_b64
            req.Code = code
            env_obj = models.Environment()
            env_obj.Variables = as_variables(env)
            req.Environment = env_obj
            c.CreateFunction(req)
    except TencentCloudSDKException as e:
        print(f"❌ 部署失败：{e}")
        return 1

    print("✅ 代码与配置已提交")

    if not wait_active(c, FUNCTION_NAME):
        return 1

    # 触发管理
    try:
        tq = models.ListTriggersRequest()
        tq.FunctionName = FUNCTION_NAME
        tq.Namespace = NAMESPACE
        trs = c.ListTriggers(tq).Triggers or []
    except TencentCloudSDKException as e:
        print(f"⚠️ 触发器查询失败：{e}")
        trs = []
    timer_trs = [t for t in trs if t.Type == "timer"]
    if not timer_trs:
        print(f"→ 创建定时触发器（{CRON}）…")
        treq = models.CreateTriggerRequest()
        treq.FunctionName = FUNCTION_NAME
        treq.Namespace = NAMESPACE
        treq.TriggerName = "qq-mail-timer"
        treq.Type = "timer"
        # 注意两个坑（腾讯云 API，非直觉）：
        #   1) timer 的 TriggerDesc 直接就是 cron 表达式本身，不能包成 {"cron": ...}，
        #      否则服务端把整个 JSON 当 cron 解析 → 报 "cron is invalid"
        #   2) Enable 取值是字符串 "OPEN" / "CLOSE"，不是 1/0
        treq.TriggerDesc = CRON
        treq.Enable = "OPEN"
        try:
            c.CreateTrigger(treq)
            print("✅ 定时触发器已创建")
        except TencentCloudSDKException as e:
            print(f"❌ 触发器创建失败：{e}")
            return 1
    else:
        for t in timer_trs:
            print(f"• 已存在定时触发器 {t.TriggerName} cron={getattr(t, 'TriggerDesc', '')} enable={t.Enable}")
    return 0


def invoke(c, payload=None, wait=90):
    req = models.InvokeRequest()
    req.FunctionName = FUNCTION_NAME
    req.Namespace = NAMESPACE
    req.InvocationType = "RequestResponse"
    req.ClientContext = json.dumps({"action": ""})
    if payload:
        req.ClientContext = json.dumps(payload)
    t0 = time.time()
    resp = c.Invoke(req)
    dt = time.time() - t0
    print(f"调用完成，耗时 {dt:.1f}s  status={getattr(resp, 'Status', '?')} "
          f"errCode={getattr(resp, 'ErrCode', 0)}")
    raw = getattr(resp, "Result", "")
    # SDK 有时把 Result 直接解析成对象，有时保留原始字符串，两种都要能打印
    try:
        if isinstance(resp, models.InvokeResponse):
            payload = json.loads(resp.to_json_string()).get("Result", raw)
        else:
            payload = raw
        if isinstance(payload, str):
            print(json.dumps(json.loads(payload), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    except Exception:
        print(f"原始结果：{str(raw)[:4000]}")
    return resp


def show_status(c):
    req = models.GetFunctionRequest()
    req.FunctionName = FUNCTION_NAME
    try:
        resp = c.GetFunction(req)
    except TencentCloudSDKException as e:
        print(f"函数不存在或读取失败：{e}")
        return 1
    print(f"函数 {FUNCTION_NAME}：runtime={resp.Runtime} mem={resp.MemorySize}MB "
          f"timeout={resp.Timeout}s status={resp.Status}")
    print("环境变量：" + json.dumps(mask(env_as_dict(getattr(resp, 'Environment', None))),
                                ensure_ascii=False, indent=2))
    tq = models.ListTriggersRequest()
    tq.FunctionName = FUNCTION_NAME
    tq.Namespace = NAMESPACE
    for t in (c.ListTriggers(tq).Triggers or []):
        print(f"触发器 {t.TriggerName} type={t.Type} enable={t.Enable} desc={getattr(t, 'TriggerDesc', '')}")
    return 0


def push_markdown(c, text, title="", channel="report"):
    """把 Markdown 推到企业微信群机器人（Webhook 从云函数环境变量取，不在本地留存）。

    channel="report" → 日报专用机器人 WECOM_WEBHOOK_REPORT（优先于本地 invoke）
    channel="alert"  → 告警机器人 WECOM_WEBHOOK
    """
    key = "WECOM_WEBHOOK_REPORT" if channel == "report" else "WECOM_WEBHOOK"
    url = ""
    # 先查本函数，再查 SOURCE_FUNCTION（用于从既有函数继承 Webhook）
    lookup = [FUNCTION_NAME] + ([SOURCE_FUNCTION] if SOURCE_FUNCTION else [])
    for fn in lookup:
        req = models.GetFunctionRequest()
        req.FunctionName = fn
        try:
            env = env_as_dict(getattr(c.GetFunction(req), "Environment", None))
        except TencentCloudSDKException:
            env = {}
        url = env.get(key, "")
        if not url and channel == "report":
            url = env.get("WECOM_WEBHOOK", "")   # 与函数内一致的回退逻辑
        if url:
            break
    if not url:
        print(f"❌ 未能取到 {key}")
        return 1
    import requests as _rq
    if title:
        text = f"## {title}\n\n{text}"
    body = text.encode("utf-8")
    if len(body) > 4000:
        text = body[:3900].decode("utf-8", "ignore") + "\n\n…(已截断)"
    try:
        resp = _rq.post(url, json={"msgtype": "markdown", "markdown": {"content": text}}, timeout=20)
        data = resp.json()
        if data.get("errcode") == 0:
            print(f"✅ 企业微信推送成功（通道：{channel}）")
            return 0
        print(f"❌ 企业微信推送失败：{data}")
        return 1
    except Exception as e:
        print(f"❌ 企业微信推送异常：{e}")
        return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", help="邮箱地址")
    ap.add_argument("--auth-code", help="邮箱授权码")
    ap.add_argument("--imap-host", help="IMAP 服务器，如 imap.163.com / imap.qq.com")
    ap.add_argument("--probe", action="store_true", help="连接诊断：登录并列出文件夹，不移动邮件")
    ap.add_argument("--probe-host", help="诊断时覆盖服务器，如 imap.163.com / pop.163.com")
    ap.add_argument("--probe-protocol", choices=["imap", "pop3"], help="诊断协议，默认 imap")
    ap.add_argument("--probe-port", type=int, help="诊断端口，如 143/993/995/110")
    ap.add_argument("--probe-account", help="诊断时覆盖账号")
    ap.add_argument("--probe-code", help="诊断时覆盖授权码，用于比对新旧码")
    ap.add_argument("--dry-run", choices=["0", "1"], help="1=只判定不移动邮件；0=真正移入回收站")
    ap.add_argument("--invoke", action="store_true", help="部署后调用一次")
    ap.add_argument("--preview", action="store_true", help="以演练模式调用一次（不改动邮箱）")
    ap.add_argument("--status", action="store_true", help="只查看状态")
    ap.add_argument("--digest", action="store_true", help="读取日报暂存区")
    ap.add_argument("--stats", action="store_true", help="查看云端运行状态与去重记录")
    ap.add_argument("--clear-digest", action="store_true", help="清空日报暂存区")
    ap.add_argument("--push-md", metavar="FILE", help="把 Markdown 文件推送到企业微信日报机器人")
    ap.add_argument("--title", default="", help="配合 --push-md 使用的标题")
    ap.add_argument("--channel", choices=["report", "alert"], default="report",
                    help="推送通道：report=日报机器人(默认)；alert=告警机器人")
    ap.add_argument("--via-cloud", action="store_true",
                    help="经云函数推送（国内出口 IP，与生产路径一致）")
    ap.add_argument("--report-webhook", help="日报专用企业微信机器人 Webhook（写入 WECOM_WEBHOOK_REPORT）")
    ap.add_argument("--channels", action="store_true", help="查看双通道配置状态")
    args = ap.parse_args()

    c = client()
    if args.status:
        return show_status(c)
    if args.channels:
        invoke(c, {"action": "channels"})
        return 0
    if args.probe:
        payload = {"action": "probe"}
        if args.probe_host:
            payload["host"] = args.probe_host
        if args.probe_port:
            payload["port"] = args.probe_port
        if args.probe_protocol:
            payload["protocol"] = args.probe_protocol
        if args.probe_code:
            payload["auth_code"] = args.probe_code
        if args.probe_account:
            payload["account"] = args.probe_account
        invoke(c, payload)
        return 0
    if args.digest:
        invoke(c, {"action": "get_digest"})
        return 0
    if args.stats:
        invoke(c, {"action": "stats"})
        return 0
    if args.clear_digest:
        invoke(c, {"action": "clear_digest"})
        return 0
    if args.push_md:
        text = open(args.push_md, encoding="utf-8").read()
        if args.via_cloud:
            # 经云函数推送：出口为国内 IP，与生产路径一致
            if args.title:
                text = f"## {args.title}\n\n{text}"
            action = "push_report" if args.channel == "report" else "push_alert"
            invoke(c, {"action": action, "markdown": text})
            return 0
        return push_markdown(c, text, args.title, channel=args.channel)

    rc = create_or_update(c, args)
    if rc != 0:
        return rc
    if args.preview:
        print("\n=== 演练调用（不改动邮箱）===")
        invoke(c, {"action": "preview"})
    elif args.invoke:
        print("\n=== 正式调用 ===")
        invoke(c, {"action": ""})
    return 0


if __name__ == "__main__":
    sys.exit(main())
