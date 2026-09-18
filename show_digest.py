"""查看云端日报暂存区，按账号分组展示（辅助脚本，不属于交付物）。

用法:
    python show_digest.py [账号名]

说明：deploy_scf.py 输出的是 {Log, RetMsg, ErrMsg, ...} 形式，
其中 RetMsg 是一层被转义的 JSON 字符串，需要解两层。
"""
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run_deploy(args):
    out = subprocess.run([PY, os.path.join(BASE, "scf", "deploy_scf.py")] + args,
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = out.stdout or ""
    start = text.find("{")
    if start < 0:
        raise SystemExit(f"未拿到 JSON 输出：\n{text[-800:]}\n{out.stderr[-400:]}")
    outer = json.loads(text[start:])
    return json.loads(outer["RetMsg"])


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    d = run_deploy(["--digest"]).get("digest", {})
    entries, trashed = d.get("entries", []), d.get("trashed", [])
    print(f"日期 {d.get('date') or '(空)'} ｜ 保留 {len(entries)} 封 ｜ 已清理 {len(trashed)} 封")
    accts = sorted({e.get("account") or "?" for e in entries + trashed})
    print(f"涉及账号：{', '.join(accts) if accts else '(无)'}\n")

    for label, rows in (("保留", entries), ("已清理", trashed)):
        sel = [x for x in rows if not want or (x.get("account") or "") == want]
        if not sel:
            continue
        print(f"---- {label}（{len(sel)} 封）----")
        for x in sel:
            print(f"[{x.get('account')}] {x.get('subject', '')[:56]}")
            print(f"    {x.get('from', '')} · {x.get('date', '')} · {x.get('label', '')}")
            print(f"    判定：{x.get('reason', '')}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
