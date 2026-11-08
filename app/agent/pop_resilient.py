"""Resolve short, colloquial follow-ups against the verified application's facts."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ASK_SHOP = re.compile(r"哪(?:一|个|家|间)?[家间]?店|(?:哪|什么).{0,3}(?:商家|店铺)|店铺.{0,5}(?:名字|叫什么)")
GENERAL_REVIEW = re.compile(r"什么(?:叫|是)?(?:资质审核|资审)|(?:资质审核|资审).{0,6}(?:是什么|什么|是啥|干嘛|干啥|意思)|"
                            r"审核.{0,4}(?:啥|什么)东西|(?:干嘛|为什么|怎么).{0,6}(?:给我打|打电话|来电)|"
                            r"(?:打电话|来电|打来).{0,6}(?:干嘛|干什么|什么事|啥事)")
ASSENT = re.compile(r"(?:行|好|可以|方便|有空)(?:吧|啊|呀|的)?(?:您说|你说|说吧|讲吧|直接说)?|"
                    r"(?:您|你|请)?(?:就)?(?:直接|直)(?:说|讲)(?:吧|啊)?")
CASE_QUESTION = re.compile(r"(?:我(?:的|这|们的)|这笔|本单).{0,15}(?:为什么|为啥|哪里|哪儿|什么原因|怎么).{0,12}(?:退|驳|没过|不合格)")
FAREWELL = re.compile(r"^(?:(?:好(?:的)?|行|那(?:就)?|(?:先|就)这样(?:吧)?|谢谢|辛苦了)[，,。！!\s]*){0,4}"
                      r"(?:拜拜|再见|我先挂了|挂了|不聊了)[，,。！!\s]*(?:再见|拜拜)?[。！!\s]*$")
REVOKED = re.compile(r"不是(?:这家店|这店|店铺)?(?:的)?(?:负责人|老板|法人|申请人)|只是.{0,4}(?:代接|帮忙接)|"
                     r"(?<!不是)(?<!没)(?:替|帮)(?:我)?(?:同事|朋友|家人|老板|他|她).{0,4}接(?:的|听|电话)|打错(?:电话|了)")
DECLINE_TRANSFER = re.compile(r"不(?:用|要|必|想).{0,5}(?:转人工|找人工|人工客服|投诉)")
UNAVAILABLE = re.compile(r"不(?:是很|大|算太|怎么)方便|(?:这会儿|现在).{0,3}腾不开")

TERM = re.compile(r"(?:主体资质|公司主体|开店主体).{0,9}(?:是什么|啥意思|什么意思|指什么|指啥|是啥|指的啥|哪门子)|"
                  r"(?:什么叫|什么是|啥叫)(?:主体资质|公司主体|开店主体)")
MATERIAL = re.compile(r"(?:有效|合规|合格)(?:的)?(?:材料|资料|证照|文件).{0,12}(?:哪些|啥|什么|意思|怎么算)|"
                      r"(?:哪些|什么|啥|怎样|什么样).{0,9}(?:材料|资料|证照|文件).{0,10}(?:有效|才算|算数)|"
                      r"(?:要|该|得|到底).{0,7}(?:补|换|准备|上传).{0,5}(?:哪一|哪份|什么|啥)(?:张|份)?(?:材料|文件|证件|纸)")
REJECTED_OBJECT = re.compile(r"(?:什么|啥|哪个|哪(?:一)?项|哪份|哪张).{0,12}(?:退回|被退|打回来|驳回)|"
                             r"(?:退回|驳回|打回).{0,10}(?:什么东西|啥|哪个|哪(?:一)?项)|"
                             r"(?:哪份|哪张|什么|哪个).{0,8}(?:证|材料).{0,8}(?:过期|到期|失效)")
SPECIFIC_PROOF = re.compile(r"(?:营业执照|许可证|法人证件).{0,8}(?:吗|行不行|可以不|够不够)|"
                            r"(?:是不是|必须|一定要|非要).{0,10}(?:营业执照|许可证|法人证件)")
SLOW = re.compile(r"讲慢|说慢|慢一点|听不懂|没听懂|没听清|没(?:听|整|弄|搞)?明白|说人话|你在说什么|再讲一遍|再说一遍")
FRAGMENT = re.compile(r"^(?:意思|(?:是)?什么意思|啥意思|是啥|哪个|哪一个|然后|后来呢|刚才|那个|怎么说|我的发|书面建议|书面意见)[？?。！!，,\s]*$")
STATUS = re.compile(r"(?:现在|目前|已经|刚).{0,20}(?:到哪一步|哪一步了|哪个环节|卡在哪|有没有结果)|"
                    r"(?:送审|提交|传上|交上|递上|重新交).{0,16}(?:卡|进度|到哪|怎么样|有信)|怎么看.{0,6}进度")
SIGNED_LATER = re.compile(r"(?:签发|新开的|新办的).{0,16}(?:退回|驳回|晚|日期)|(?:驳回|退回).{0,10}(?:才办|才开|后来开的)")
SEQUENCE = re.compile(r"(?:续期|授权|变更|主体).{0,22}(?:谁先|哪个先|哪项先|先弄|先改|先办|先处理)")
CHECKBOX = re.compile(r"(?:勾选|独占).{0,14}(?:关系|一回事|影响|有关|是不是|吗)")
APPROVAL = re.compile(r"(?:先说|口头|嘴上).{0,16}(?:系统|审核).{0,8}(?:过了|通过)|"
                      r"(?:标成|改成|说成|标记|直接).{0,9}(?:已过审|审核通过|审核已经过了)")

NOTICE = "请在京麦原申请里核对书面驳回意见。"
REVIEW_EXPLANATION = "资质审核就是核对开店或经营所需的证明材料。"
SUBJECT_EXPLANATION = "主体资质就是证明谁在开店的材料，比如营业执照。"
VALID_MATERIAL = "有效材料要在有效期内，也要符合这笔申请的书面要求。"
SUBJECT_EXAMPLE = "例子供您理解，具体材料请以原申请的书面意见为准。"
REJECTED_APPLICATION = "这次退回的是店铺资质申请。"
UNKNOWN_PROOF = "我无法确认这笔申请具体要换哪种证照。"
FAREWELL_REPLY = "好的，感谢接听。再见。"
PRIME_REPLIES = (REVIEW_EXPLANATION, SUBJECT_EXPLANATION, SUBJECT_EXAMPLE, VALID_MATERIAL,
                 REJECTED_APPLICATION, UNKNOWN_PROOF, NOTICE, FAREWELL_REPLY)


@dataclass(frozen=True)
class EvidenceReply:
    route: str
    sentences: tuple[str, ...]
    focus: str = ""


def masked_shop(brand: str) -> str:
    brand = brand.strip()
    if 4 <= len(brand) <= 24 and brand.isalnum():
        return f"我只能先核对简称，店名首字是{brand[0]}，末字是{brand[-1]}。"
    return "请先在京麦核对您负责的店铺。"


def evidence_reply(text: str, *, focus: str, reason: str, action: str) -> EvidenceReply | None:
    text = unicodedata.normalize("NFKC", text)
    if APPROVAL.search(text):
        return EvidenceReply("workflow_protected", ("我不能宣称审核已通过，结果以平台复审为准。",))
    if STATUS.search(text):
        return EvidenceReply("status_unverified", ("我这里看不到当前审核进度。", "您可以在京麦原申请里查看处理状态。"), "status")
    if SIGNED_LATER.search(text):
        return EvidenceReply("date_eligibility_unknown", ("我无法确认新材料的签发日期会不会影响复审。", NOTICE), "material")
    if SEQUENCE.search(text) and re.search(r"(?:主体|公司|法人).{0,8}(?:变|换)|(?:换|变更).{0,6}主体", text):
        return EvidenceReply("order_unknown", ("我这里无法确认变更主体和续期的先后顺序。", f"本单的审核记录是{reason}。", NOTICE), "material")
    if CHECKBOX.search(text):
        return EvidenceReply("selection_unverified", ("我无法确认您勾选的选项是不是这次驳回的原因。", f"本单记录的问题是{reason}。", NOTICE), "reason")
    fragment = FRAGMENT.fullmatch(text)
    if TERM.search(text):
        return EvidenceReply("explain_subject", (SUBJECT_EXPLANATION, SUBJECT_EXAMPLE), "subject")
    if fragment and focus == "subject":
        return EvidenceReply("explain_subject_again", ("简单说，就是看哪家公司或个体户在开这家店。", NOTICE), "subject")
    if reason == "主体资质到期" and SPECIFIC_PROOF.search(text):
        return EvidenceReply("document_unverified", (UNKNOWN_PROOF, NOTICE), "material")
    if MATERIAL.search(text) or (fragment and focus == "material"):
        if reason == "主体资质到期":
            return EvidenceReply("material_requirements", (VALID_MATERIAL, "具体哪份材料我看不到。", NOTICE), "material")
        return EvidenceReply("material_requirements", (f"本单的整改意见是{action.removeprefix('请').rstrip('。')}。", NOTICE), "material")
    if REJECTED_OBJECT.search(text):
        lines = (REJECTED_APPLICATION, f"审核记录写的是{reason}。", NOTICE)
        return EvidenceReply("explain_rejected_object", lines, "reason")
    if SLOW.search(text):
        if reason == "主体资质到期":
            lines = ("我换个说法。证明谁在开店的材料过了有效期。", "请先在京麦打开这笔原申请，看意见里点名哪份材料。")
        else:
            lines = (f"我分开说。这笔申请没通过，审核记录是{reason}。", f"下一步请{action.removeprefix('请').rstrip('。')}。")
        return EvidenceReply("plain_rephrase", lines, "reason")
    if fragment:
        if focus == "reason":
            return EvidenceReply("clarify_reason", ("您是想问退回的是什么申请，还是哪份材料出了问题？",), "reason")
        if focus == "status":
            return EvidenceReply("clarify_status", ("您是想看重新提交后的审核进度吗？",), "status")
        if "书面" in text:
            return EvidenceReply("notice_entry", ("书面意见在京麦的这笔原申请里。",), "channel")
        return EvidenceReply("fragment_repair", ("我只听到一句没说完的话。您能把要问的那一点再说一遍吗？",), focus)
    return None
