"""验证重要邮件去重逻辑（不连邮箱、不写状态）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scf"))
os.environ.setdefault("QQ_EMAIL_ACCOUNT", "x@qq.com")
os.environ.setdefault("QQ_EMAIL_AUTH_CODE", "x")
os.environ.setdefault("IMPORTANT_DEDUP_HOURS", "24")

import handler as h  # noqa: E402

GOOGLE = {"subject": "user@example.com 的安全提醒",
          "from": "no-reply@accounts.google.com", "date": "2026-09-13 18:49"}
GATE = {"subject": "Gate CandyDrop 500,000 MCAT 限时狂欢瓜分中",
        "from": "no-reply@notice.gate.com", "date": "2026-09-15 13:09"}
REPLY = {"subject": "Re: 面试安排通知", "from": "hr@company.com", "date": "2026-09-16 10:00"}
REPLY2 = {"subject": "回复: 面试安排通知", "from": "HR@Company.com", "date": "2026-09-16 10:05"}

state = {}
print("=== 场景1：同一主题连发 9 封（首次运行）===")
batch = [dict(GOOGLE, subject=f"user@example.com 的安全提醒") for _ in range(9)]
to_push, deduped, hist = h._split_dedup(batch, state, 24)
print(f"  待推送 {len(to_push)}，去重 {len(deduped)}，历史键 {len(hist)}")
assert len(to_push) == 1 and len(deduped) == 8, "应只推 1 封、合并 8 封"
print("  ✅ 正确：9 封合并为 1 封推送")

print("\n=== 场景2：下一轮又来了同主题的 3 封 ===")
state["pushed_important"] = hist
batch2 = [dict(GOOGLE) for _ in range(3)]
to_push2, deduped2, hist2 = h._split_dedup(batch2, state, 24)
print(f"  待推送 {len(to_push2)}，去重 {len(deduped2)}")
assert len(to_push2) == 0 and len(deduped2) == 3, "窗口内应全部去重"
print("  ✅ 正确：窗口内不再重复推送")

print("\n=== 场景3：不同邮件不受影响 ===")
to_push3, deduped3, _ = h._split_dedup([GATE, REPLY], {"pushed_important": hist}, 24)
print(f"  待推送 {len(to_push3)}，去重 {len(deduped3)}")
assert len(to_push3) == 2 and len(deduped3) == 0, "不同邮件不应被误去重"
print("  ✅ 正确：不同发件人/主题正常推送")

print("\n=== 场景4：Re: 与 回复: 前缀归一化（同一会话）===")
k1, k2 = h._dedup_key(REPLY), h._dedup_key(REPLY2)
print(f"  key1 = {k1}")
print(f"  key2 = {k2}")
assert k1 == k2, "Re:/回复: 应归一化为同一个键"
print("  ✅ 正确：中英文回复前缀均被归一化")

print("\n=== 场景5：窗口过期后恢复推送 ===")
old = {"pushed_important": {"no-reply@accounts.google.com|user@example.com 的安全提醒":
                            "2026-09-10 10:00:00"}}
to_push5, deduped5, hist5 = h._split_dedup([GOOGLE], old, 24)
print(f"  待推送 {len(to_push5)}，去重 {len(deduped5)}")
assert len(to_push5) == 1, "超过 24 小时的记录应失效，允许再次推送"
print("  ✅ 正确：过期记录被清理，恢复推送")

print("\n=== 场景6：关闭去重（hours=0）===")
to_push6, deduped6, _ = h._split_dedup([dict(GOOGLE) for _ in range(3)], {}, 0)
print(f"  待推送 {len(to_push6)}，去重 {len(deduped6)}")
assert len(to_push6) == 3 and len(deduped6) == 0, "关闭去重应每封都推"
print("  ✅ 正确：去重关闭时全部推送")

print("\n全部 6 个场景通过 ✅")
