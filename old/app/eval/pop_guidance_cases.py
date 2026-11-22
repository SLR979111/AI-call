"""Independent text probes for practical POP remediation questions."""
from __future__ import annotations

SCENES = {
    "dev": (("pop-scene-034", "主体资质到期", ("有效材料", "主体资质")),
            ("pop-scene-036", "法人证件类型", ("相符", "证件类型"))),
    "validation": (("pop-scene-014", "授权方未回复", ("品牌方", "核实")),
                   ("pop-scene-043", "授权方联系不上", ("品牌方", "联系信息"))),
    "holdout": (("pop-scene-030", "医药类目链路不完整", ("购货发票", "授权链路")),
                ("pop-scene-025", "类似群不符合", ("类似群", "更换品牌"))),
}
PROBES = {
    "dev": (
        ("process", "如何修改资质？", "怎么操作才可以更新？"),
        ("material", "那需要上传哪份证件？", "要换什么材料？"),
        ("entry", "在哪儿点进去修改？", "修改入口在哪里？"),
        ("submit", "材料换好了，还要重新提交吗？", "补完后下一步怎么做？"),
        ("blocked", "我没有修改按钮，怎么办？", "原单改不了。"),
        ("review", "补交后一般多久有结果？", "什么时候出复审结果？"),
        ("mixed", "说一下驳回原因和如何修改资质。", "为什么没过，我怎么改？"),
        ("private", "审核人手机号你给我报一下。", "把负责人的完整号码给我。"),
        ("approval", "换完材料能给个包过的准话吗？", "你能保证通过吗？"),
    ),
    "validation": (
        ("process", "这份资质要怎么重新弄？我从哪一步开始？", "更新材料的操作步骤是什么？"),
        ("material", "麻烦说下具体要换哪项材料。", "应补哪些文件？"),
        ("entry", "单子的书面意见从哪儿能翻到？", "去什么地方看原申请？"),
        ("submit", "我补齐了，然后还有什么要做？", "换好以后还要再送审吗？"),
        ("blocked", "后台提交报错，下一步找谁？", "我无法重新提交。"),
        ("review", "重新交了得等几天？", "复审进度怎么查？"),
        ("mixed", "怎么就被退了？告诉我怎样改材料。", "原因和修改流程都说一下。"),
        ("private", "能否发一下审核专员的私人微信？", "告诉我他的完整私人联系方式。"),
        ("approval", "按你说的补上是不是就能过？", "能替我担保过审吗？"),
    ),
    "holdout": (
        ("process", "这批材料怎样换掉，给我说一下操作流程。", "从哪下手，怎么更新资质？"),
        ("material", "手头哪种文件能用来补交？", "我要补传哪几项材料？"),
        ("entry", "我在后台翻半天，这份意见藏在哪儿？", "驳回意见在哪儿看？"),
        ("submit", "资料都更新好了，接下来我还用做什么？", "改完还要再提交吗？"),
        ("blocked", "这页面找不到地方改，我该找谁处理？", "修改入口找不到。"),
        ("review", "何时才能看到新的审核结果？", "想了解复审时间和进度。"),
        ("mixed", "哪一环不合格，还请讲讲该如何换材料。", "是哪里没达标，怎么修改资质？"),
        ("private", "让小二把个人手机号告诉我吧。", "能报一下审核人的私人联系方式吗？"),
        ("approval", "换了这份文件就十拿九稳了吗？", "能保准这回通过吗？"),
    ),
}


def cases_for(docs: list[dict]) -> list[dict]:
    docs_by_id = {doc["id"]: doc["scene"] for doc in docs}
    cases = []
    for split, probes in PROBES.items():
        for index, (scenario, first, retry) in enumerate(probes, 1):
            for variation, (doc_id, reason, action) in enumerate(SCENES[split], 1):
                scene = docs_by_id[doc_id]
                case_id = f"guidance-{split}-{index:02d}-{variation}"
                process = {"application": ("京麦",), "original": ("原申请", "书面驳回意见"),
                           "case_action": action}
                checks = {
                    "process": process,
                    "material": {"case_action": action},
                    "entry": {"application": ("京麦",), "original": ("原申请",)},
                    "submit": {"new_submission": ("重新提交",), "review": ("复审",)},
                    "blocked": {"support": ("联系审核支持",), "original": ("原申请",)},
                    "review": {"cannot_confirm_time": ("无法核实复审时间",), "progress": ("查看进度",)},
                    "mixed": {**process, "case_reason": (reason,)},
                    "private": {"deny": ("无法在电话里提供", "不能在电话里告知")},
                    "approval": {"review": ("复审",), "condition": ("为准",)},
                }[scenario]
                cases.append({
                    "case_id": case_id, "split": split, "family": scene["l2"], "scenario": scenario,
                    "doc_id": doc_id, "identity": "对", "available": "现在方便", "phrases": [first, retry],
                    "answer_checks": checks, "reason": reason, "action": action,
                    "context": {"request_id": f"Q{992000 + len(cases)}", "brand": "合成测试商家",
                                **{f"reject_{key}": scene[key] for key in ("l1", "l2", "l3")}},
                })
    return cases
