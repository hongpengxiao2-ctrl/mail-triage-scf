"""邮箱链路诊断工具（本地运行，只读不改动邮箱）

用途：当邮箱收信链路出问题时，快速定位故障发生在哪一层——
  DNS 解析 / TCP 连通 / TLS 握手 / 认证。

用法：
  python diagnose.py                          # 全量诊断（用 .env 里的配置）
  python diagnose.py --host imap.163.com      # 只测指定服务器
  python diagnose.py --no-login               # 只测网络，不试登录

常见结论对照：
  裸 TCP 收到 0 字节      → 中间层（代理/DNS）把连接掐断了，不是邮箱问题
  TLS = SSLEOFError       → 同上，多为代理 DNS 返回了错误 IP
  Coremail System 横幅    → 服务器是网易系（163/126/yeah/188）
  QQMail XMIMAP4Server    → 服务器是腾讯系（qq.com/foxmail.com）
  Login error or password → 服务器对了，但凭证不对或被风控
"""
import argparse
import imaplib
import os
import poplib
import socket
import ssl
import sys

# 常见邮箱服务商：主机 -> (说明, 协议)
SERVERS = [
    ("imap.163.com", "网易163 IMAP"),
    ("pop.163.com", "网易163 POP3"),
    ("smtp.163.com", "网易163 SMTP"),
    ("imap.qq.com", "腾讯QQ IMAP"),
    ("imap.126.com", "网易126 IMAP"),
    ("www.163.com", "网易门户(对照)"),
    ("www.baidu.com", "外部对照"),
]
PORT_BY_KIND = {"IMAP": 993, "POP3": 995, "SMTP": 465, "对照": 443, "门户(对照)": 443}


def guess_port(name):
    if "IMAP" in name:
        return 993
    if "POP3" in name:
        return 995
    if "SMTP" in name:
        return 465
    return 443


def probe_network(host, port):
    """返回 (dns_ip, tcp_ok, tls_result, note)"""
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:
        return None, False, f"DNS失败:{type(e).__name__}", ""
    try:
        s = socket.create_connection((host, port), timeout=12)
    except Exception as e:
        return ip, False, f"TCP失败:{type(e).__name__}", ""
    note = ""
    tls = "未测"
    try:
        s.settimeout(6)
        try:
            raw = s.recv(64)
            if raw == b"":
                note = "裸TCP收0字节(链路被掐断)"
            else:
                note = f"裸TCP有数据:{raw[:40]!r}"
        except socket.timeout:
            note = "裸TCP静默(正常)"
        except Exception as e:
            note = f"裸TCP异常:{type(e).__name__}"
    finally:
        try:
            s.close()
        except Exception:
            pass
    try:
        s = socket.create_connection((host, port), timeout=12)
        ctx = ssl.create_default_context()
        ss = ctx.wrap_socket(s, server_hostname=host)
        tls = f"OK({ss.version()})"
        ss.close()
    except Exception as e:
        tls = f"FAIL({type(e).__name__})"
    return ip, True, tls, note


def probe_login(host, account, code, protocol):
    """尝试认证，返回结果字符串。"""
    try:
        if protocol == "pop3":
            c = poplib.POP3_SSL(host, 995, timeout=25)
            c.user(account)
            c.pass_(code)
            n = c.stat()[0]
            c.quit()
            return f"✅ 认证成功，{n} 封邮件"
        c = imaplib.IMAP4_SSL(host, 993, timeout=25)
        banner = c.welcome.decode("utf-8", "replace") if isinstance(c.welcome, bytes) else str(c.welcome)
        try:
            c.login(account, code)
        except imaplib.IMAP4.error as e:
            c.logout()
            return f"❌ 认证被拒 (服务器横幅: {banner[:60]}) :: {e}"
        if "163" in host or "126" in host or "yeah" in host:
            try:
                c.xatom("ID", '("name" "diagnose" "version" "1.0" "vendor" "wb" '
                              '"support-email" "a@b.com")')
            except Exception:
                pass
        typ, rows = c.list()
        folders = [r.decode("utf-8", "replace") if isinstance(r, bytes) else str(r)
                   for r in (rows or [])]
        c.logout()
        return f"✅ 认证成功，{len(folders)} 个文件夹 :: {banner[:60]}"
    except Exception as e:
        return f"❌ 连接异常: {type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser(description="邮箱链路诊断")
    ap.add_argument("--host", help="只诊断指定主机")
    ap.add_argument("--account", help="覆盖账号")
    ap.add_argument("--auth-code", help="覆盖授权码")
    ap.add_argument("--no-login", action="store_true", help="只测网络，不试登录")
    args = ap.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))
    env = {}
    envfile = os.path.join(base, "scf", ".env")
    if os.path.exists(envfile):
        for line in open(envfile, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    account = args.account or env.get("QQ_EMAIL_ACCOUNT") or os.environ.get("QQ_EMAIL_ACCOUNT", "")
    code = args.auth_code or env.get("QQ_EMAIL_AUTH_CODE") or os.environ.get("QQ_EMAIL_AUTH_CODE", "")

    targets = [t for t in SERVERS if not args.host or t[0] == args.host]
    print("=" * 74)
    print("第一层：网络可达性")
    print("=" * 74)
    print(f"{'主机':<20}{'端口':<7}{'解析IP':<18}{'TCP':<6}{'TLS':<22}{'备注'}")
    for host, label in targets:
        port = guess_port(label)
        ip, tcp_ok, tls, note = probe_network(host, port)
        ip_s = ip or "-"
        print(f"{host:<20}{port:<7}{ip_s:<18}{'OK' if tcp_ok else 'FAIL':<6}{tls:<22}{note}")
    print()
    if args.no_login:
        return 0
    print("=" * 74)
    print(f"第二层：认证测试    账号={account or '(未配置)'}  授权码长度={len(code)}")
    print("=" * 74)
    if not account or not code:
        print("账号或授权码未配置，跳过认证测试。")
        return 2
    for host, label in targets:
        if not any(k in label for k in ("IMAP", "POP3")):
            continue
        proto = "pop3" if "POP3" in label else "imap"
        print(f"[{label}] {host}: {probe_login(host, account, code, proto)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
