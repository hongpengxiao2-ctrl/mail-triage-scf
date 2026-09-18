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

    # ==== 真实邮件回归样本（2026-09-17 用户邮箱实测，主题/发件人原样照抄）====
    # 平台营销推送的典型特征：主题很"干净"（不含促销词汇），
    # 靠「批量发件人特征 + 广告/运营词」组合判定才能识别。
    ("Gate 积分活动（真实）", "SPAM", c(
        subject="第四期事件积分活动正式开启，交易即得刮刮卡，解锁多重奖励",
        from_addr="no-reply@notice.gate.com", from_name="gate")),
    ("Gate 资金就绪（真实）", "SPAM", c(
        subject="资金已准备就绪 — 立即开始您的首笔现货交易",
        from_addr="no-reply@remind.gate.com", from_name="gate",
        body_text="若不想再收到此类邮件，请点击下方链接取消订阅")),
    ("Gate 新手奖励到期（真实）", "SPAM", c(
        subject="您的新手奖励即将到期", from_addr="no-reply@remind.gate.com",
        from_name="gate", body_text="如需退订请联系客服")),
    ("Gate 任务奖池（真实）", "SPAM", c(
        subject="Gate Booster：参与发帖 & 预测任务，瓜分 44,800 CNPY 奖池",
        from_addr="no-reply@notice.gate.com", from_name="gate")),
    ("Gate 邮箱验证链接（真实）→过期即清", "CODE_EXPIRED", c(
        subject="[Gate Card]邮箱验证", from_addr="no-reply@alert.gate.com",
        from_name="gate", age_min=9999)),
    ("Instagram 关注建议（真实）", "SPAM", c(
        subject="user123，在动态中查看 arifjanivich.16 、 zzqi1956 和更多账户",
        from_addr="follow-suggestions@mail.instagram.com", from_name="instagram")),
    ("Instagram Reels 回顾（真实）", "SPAM", c(
        subject="user123，查看来自 therepostreels 和其他人的 Reels",
        from_addr="posts-recap@mail.instagram.com", from_name="instagram")),
    ("Instagram 精彩时刻（真实）", "SPAM", c(
        subject="user123，快来看看你错过的精彩时刻",
        from_addr="posts-recaps@mail.instagram.com", from_name="instagram")),
    ("EA 问卷邀约（真实）", "SPAM", c(
        subject="《战地风云》工作室期待您的反馈", from_addr="ea@e.ea.com",
        from_name="ea", list_unsub="<https://x.com/u>")),

    # 🔴 以下三条是最关键的护栏：这些邮件同样来自批量地址，
    #    若组合判定缺少「强重要词」保护就会被误删。
    ("Google 安全提醒（真实）→不得判广告", "IMPORTANT", c(
        subject="user@example.com 的安全提醒",
        from_addr="no-reply@accounts.google.com", from_name="google")),
    (" Instagram 账号安全（真实）→不得判广告", "IMPORTANT", c(
        subject="有人尝试登录你的 Instagram 账号",
        from_addr="security@mail.instagram.com", from_name="instagram")),
    ("真人来信（真实）", "IMPORTANT", c(
        subject="[API VibeCoding] 通知", from_addr="sender@qq.com",
        from_name="api vibecoding")),

    # ==== 验证码规则 ====
    ("验证码已过期→回收站", "CODE_EXPIRED", c(
        subject="【验证码】您的登录验证码为 8842", from_addr="no-reply@security.com",
        age_min=45)),
    ("验证码未过期→重要", "IMPORTANT", c(
        subject="【验证码】您的登录验证码为 8842", from_addr="no-reply@security.com",
        age_min=5)),
    ("数字在前也识别（验证码）", "CODE_EXPIRED", c(
        subject="8842 是您的登录验证码", from_addr="no-reply@security.com", age_min=60)),
    ("验证码+保护域→跳过清理", "IMPORTANT", c(
        subject="【验证码】您的支付验证码 668899", from_addr="service@icbc.com.cn",
        age_min=180)),
    ("正文顺带提及验证码→不判", "NORMAL", c(
        subject="请查收本月的使用报告", from_addr="no-reply@shop-example.com",
        body_text="请勿将验证码告知他人，谨防诈骗")),
    ("验证链接已过期→回收站", "CODE_EXPIRED", c(
        subject="请完成邮箱验证以激活账号", from_addr="no-reply@some-service.com",
        age_min=600)),
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
