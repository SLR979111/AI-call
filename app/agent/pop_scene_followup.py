"""Local follow-up replies with facts scoped to a single verified phone case."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256

# Each entry is a paraphrase of the corresponding phone row in knowledge/pop_audit.json.
# Unknown details are absent from that row (including any blank placeholders).
SCENE_FACTS = {
    "007": ("提交的品牌持有人与商标网公示的持有人不一致", "哪一个名字或哪份证明有问题",
            "持有人|注册人|商标网|公示|权利人|转让|更名"),
    "009": ("这笔申请的授权时间已经失效，需要重新开具授权书", "哪份授权书在哪天失效",
            "授权书|授权时间|授权期|日期|换新|续签|过期"),
    "023": ("这笔新增产品线申请用了图形商标，这个场景暂不支持图形商标", "您手头那件商标是否符合要求",
            "商标|图形|图案|图标|标志|文字|标识|组合标"),
    "027": ("这笔全球购申请需要境外的有效商标注册证，当前提交的是境内注册证", "另一份证书能不能用于本单",
            "商标|境外|境内|海外|国内|注册证|注册地"),
    "034": ("这笔申请所提交的开店主体资质已到期失效", "哪份证照在哪天失效",
            "营业执照|主体资质|有效期|证照|到期|过期"),
    "035": ("审核意见指向营业执照号码或公司名称填写有误，大小写和名称末尾多出的点也需要核对",
            "具体是号码还是公司名称有误，或者错在哪个字符",
            "营业执照|执照号|公司名|主体名称|字号|抬头|大小写|括号|标点|小数点|空格|信用代码|店名"),
    "039": ("这笔申请提交的授权关系没有形成完整链路", "具体缺少哪两方之间的授权",
            "授权链|授权|上游|下游|经销商|代理商|品牌方|链路|哪一环|哪一段"),
    "043": ("这笔申请在核实授权时没有收到授权方的回复", "此前联系的是谁、用的什么方式",
            "授权方|授权|品牌方|回信|回复|回邮件|接电话|联系|验真"),
    "054": ("这笔申请提交的行业资质范围与所申请的类目不一致", "哪份材料、哪个具体类目需要调整",
            "资质|类目|经营范围|许可|范围|证书|品类"),
    "061": ("这笔申请中填写的法人身份证信息有误", "究竟是哪一项证件信息填错了",
            "法人|证件|身份证|证号|姓名|有效期|证件号"),
}
POLICY_INTENTS = ("explain", "detail", "action")
ACTIVE_SCENE_POLICY = "data/pop-scene-rsi/active.json"
BUNDLED_SCENE_POLICY = "eval/pop/scene_policy.json"
DETAIL = re.compile(r"到底|具体|究竟|哪(?:一|几|份|个|位|张|天|处|家|段|项|种)|哪儿|哪边|是不是|该不会|是因为|吗[？?。]?$|么[？?。]?$")
EXPLAIN = re.compile(r"为什么|为啥|咋会|怎么会|怎么就|啥原因|什么原因|有啥问题|出了什么问题|啥意思|什么意思|有什么问题")
ACTION = re.compile(r"怎么(?:改|补|填|弄|修|核对|处理|办|重来)|咋(?:改|补|填|弄|办)|改哪|从哪.{0,6}(?:改|核对|下手)|"
                    r"要.{0,4}(?:改|补|核对)什么|下一步|先.{0,5}(?:核对|比对)")
CONTINUE = re.compile(r"^(?:然后呢|接着呢|继续|接着说|下一步呢)[。？！!?\s]*$")
CASE_CAUSE = re.compile(r"(?:为啥|为什么|怎么就|到底).{0,12}(?:驳回|打回|没过|不通过|退回来)|"
                        r"(?:驳回|没过|退回来).{0,8}(?:原因|为啥|为什么)")
VALID_TERM = re.compile(r"[\u3400-\u9fff]{3,10}")


def normalize(text):
    return unicodedata.normalize("NFKC", text).strip()


def load_scene_policy(root, explicit=None):
    paths = (explicit,) if explicit is not None else (
        root / ACTIVE_SCENE_POLICY, root / BUNDLED_SCENE_POLICY,
    )
    for path in paths:
        if explicit is not None or path.is_file():
            return ScenePolicy.from_json(json.loads(path.read_text(encoding="utf-8")))
    return None


@dataclass(frozen=True)
class ScenePolicy:
    phrases: dict

    @classmethod
    def from_json(cls, payload):
        if not isinstance(payload, dict) or payload.get("schema") != 1 or set(payload) != {"schema", "phrases"}:
            raise ValueError("Invalid scene policy format")
        phrases = payload["phrases"]
        if not isinstance(phrases, dict) or not set(phrases) <= set(SCENE_FACTS):
            raise ValueError("Unknown scene policy ID")
        for rules in phrases.values():
            if not isinstance(rules, dict) or not set(rules) <= set(POLICY_INTENTS):
                raise ValueError("Unknown scene policy intent")
            seen = set()
            for terms in rules.values():
                if (not isinstance(terms, list) or len(terms) > 8 or
                        not all(isinstance(term, str) and VALID_TERM.fullmatch(term) for term in terms)
                        or len(terms) != len(set(terms)) or seen.intersection(terms)):
                    raise ValueError("Invalid, duplicate or excessive routing phrases")
                seen.update(terms)
        return cls(phrases)

    def to_json(self):
        return {"schema": 1, "phrases": self.phrases}

    @property
    def version(self):
        payload = json.dumps(self.to_json(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return "scene-rsi-" + sha256(payload).hexdigest()[:12]

    def reply(self, doc_id, text, action, focus):
        key = doc_id.removeprefix("pop-scene-")
        facts = SCENE_FACTS.get(key)
        if not facts:
            return None
        cause, unknown, anchors = facts
        text = normalize(text)
        anchor = re.search(anchors, text)
        if focus in {"scene_detail", "scene_explain"} and CONTINUE.fullmatch(text):
            intent = "action"
        elif anchor and ACTION.search(text):
            intent = "action"
        elif anchor and DETAIL.search(text):
            intent = "detail"
        elif (anchor and EXPLAIN.search(text)) or CASE_CAUSE.search(text):
            intent = "explain"
        else:
            matches = [intent for intent, terms in self.phrases.get(key, {}).items()
                       if any(term in text for term in terms)]
            if len(matches) != 1:
                return None
            intent = matches[0]
        if intent == "action":
            return intent, [f"您可以先{action.removeprefix('请').rstrip('。')}。",
                            "如果页面还有具体要求，请核对京麦原申请的书面意见。"]
        if intent == "detail":
            return intent, [f"我这里无法确认{unknown}。", f"{cause}。",
                            "请在京麦原申请核对书面驳回意见。"]
        return intent, [f"{cause}。", f"我这里无法确认{unknown}。",
                        f"您可以{action.removeprefix('请').rstrip('。')}。"]
