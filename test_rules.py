"""
云函数判定规则单元测试（不连接邮箱、不读环境变量）
用法: python test_rules.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scf"))

# 屏蔽环境变量依赖，避免导入期报错
os.environ.setdefault("COS_BUCKET", "")
os.environ.setdefault("COS_REGION", "")

from handler import classify  # noqa: E402


def c(**kw):
    """classify(subject, from_addr, from_name, message_id, list_unsub, precedence, body_text, image_count, age_min)"""
    return classify(
        kw.get("subject", ""), kw.get("from_addr", ""), kw.get("from_name", ""),
        kw.get("message_id", ""), kw.get("list_unsub", ""), kw.get("precedence", ""),
        kw.get("body_text", ""), kw.get("image_count", 0), kw.get("age_min"),
    )


CASES = [
    ("典型电商大促", "SPAM", c(
        subject="限时秒杀｜优惠券已到账，满减立减，点击领取好物好价",
        from_addr="promo@edm.taobao.com", from_name="淘宝",
        message_id="<x@mailchimp.com>", list_unsub="<mailto:u@x.com>", precedence="bulk")),
    ("双关键词促销", "SPAM", c(
        subject="年终清仓，全场折扣低至三折", from_addr="no-reply@shop.com", from_name="某某商城")),
    ("电商促销+退订头", "SPAM", c(
        subject="会员日专属优惠，券后到手价更低", from_addr="edm@shopmail.com",
        from_name="商城", list_unsub="<mailto:x@x.com>")),
    ("英文newsletter", "SPAM", c(
        subject="Limited time: exclusive offer, save now", from_addr="newsletter@brand.com",
        from_name="Brand", list_unsub="<https://x.com/u>")),
    ("仅发件人特征→保守保留", "NORMAL", c(
        subject="您关注的话题有新回复", from_addr="noreply@forum.com", from_name="Forum")),
    ("银行账单→保护域", "IMPORTANT", c(
        subject="您的信用卡账单已生成", from_addr="service@icbc.com.cn", from_name="工商银行")),
    ("面试通知", "IMPORTANT", c(
        subject="面试邀请：运营实习生岗位面试安排", from_addr="hr@somecompany.com", from_name="hr")),
    ("验证码", "IMPORTANT", c(
        subject="【验证码】您的登录验证码为 8842", from_addr="no-reply@security.com")),
    ("学校邮件→保护域", "IMPORTANT", c(
        subject="关于本学期选课与考试安排的通知", from_addr="jwc@gzhu.edu.cn", from_name="广州大学教务处")),
    ("物流签收", "IMPORTANT", c(
        subject="您的快递已签收", from_addr="service@sf-express.com", from_name="顺丰速运")),
    ("免费邮箱真人来信", "NORMAL", c(subject="周末一起打球吗", from_addr="friend@163.com")),
    ("免费邮箱单信号→保守保留", "NORMAL", c(
        subject="限时优惠分享给你", from_addr="someone@qq.com")),
    # ---- 二维码规则 ----
    ("过期二维码→回收站", "QR_EXPIRED", c(
        subject="登录确认", from_addr="no-reply@game.com", from_name="某游戏",
        body_text="请使用微信扫描下方二维码完成登录确认", image_count=1, age_min=45)),
    ("二维码未过期→重要", "IMPORTANT", c(
        subject="登录确认", from_addr="no-reply@game.com", from_name="某游戏",
        body_text="请使用微信扫描下方二维码完成登录确认", image_count=1, age_min=5)),
    ("二维码但无图片→不判过期(保守保留)", "IMPORTANT", c(
        subject="登录确认", from_addr="no-reply@game.com", body_text="请扫描二维码", image_count=0,
        age_min=120)),
    ("有图片但无二维码关键词→不判过期(保守保留)", "IMPORTANT", c(
        subject="本月电子对账单", from_addr="service@somebank-example.com", body_text="详见附件",
        image_count=3, age_min=9999)),
    ("保护域含二维码也不清理", "IMPORTANT", c(
        subject="登录确认二维码", from_addr="service@icbc.com.cn", body_text="扫描二维码",
        image_count=1, age_min=120)),
]


def main():
    passed = 0
    print("🧪 云函数判定规则测试（不连接邮箱）\n")
    for name, expect, (label, reason) in CASES:
        ok = label == expect
        passed += ok
        print(f"{'✅' if ok else '❌'} [{name}] 期望={expect} 实际={label}")
        print(f"    {reason}")
    print(f"\n结果：{passed}/{len(CASES)} 通过")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
