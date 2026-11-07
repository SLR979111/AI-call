"""Build deterministic, source-traceable synthetic merchant cases for POP."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from openpyxl import load_workbook

HERE = Path(__file__).resolve().parent
SCENE_BOOK = "[POP资审]资审关单场景话术V1.0.xlsx"
QA_BOOK = "驳回外呼QA.xlsx"
ZIP_SHA256 = "10a590dbfd81cf45a21025d6c4ce99617fca35590c65d38e745a6313b0ef66a7"
SPLIT_COUNTS = {"dev": 30, "validation": 20, "holdout": 20}
IDENTITY_QUESTIONS = (
    "请问您是", "确认一下您的身份", "确认您是", "核对一下身份", "方便确认一下",
    "方便核对一下", "是店铺负责人吗", "是店铺负责人么", "您是负责人吗",
)
GREETINGS = (
    "喂，您好，哪位？", "您好，您找谁？", "喂，能听清。您是哪里？",
    "您好，我接到了，请问有什么事？", "喂，请讲。", "您好，什么事情？",
    "喂，我这儿在店里，您先说一下。", "喂？您是哪边的？",
    "能听见，您是谁？", "您好，来电是要跟谁确认？", "喂，您说。", "您好，听得到。",
)
VERIFICATIONS = (
    "是的，我是店铺负责人，您说吧。", "对，我是店铺负责人，可以听。",
    "我是这家店的负责人，您继续。", "我是申请人，这次材料就是我提交的。",
    "是，我是店铺负责人。您慢慢说。", "对的，我是店铺负责人，现在方便。",
)
ASK_AGAIN = (
    "您需要先核对一下我是不是负责人吗？",
    "刚才没听见您核对身份，您先问清楚再说。",
)
PRIVATE_DISCLOSURE = (
    "您申请的相关品牌", "本次资质审核未通过", "您申请的品牌被驳回",
)
FORBIDDEN_PROMISES = (
    "我保证给您通过", "我给您直接通过", "补了就能百分百通过",
    "我保证你能通过", "我给您直接改成审核通过",
)
QA_EXTRA = {
    13: [("特殊场景", 12)],
    17: [("特殊场景", 20), ("特殊场景", 24)],
    18: [("高频QA", 12), ("特殊场景", 27)],
    20: [("特殊场景", 23)],
    22: [("特殊场景", 26)],
    23: [("高频QA", 5)],
    26: [("特殊场景", 29)],
    28: [("特殊场景", 30)],
    29: [("特殊场景", 31)],
    30: [("特殊场景", 32)],
    36: [("特殊场景", 15)],
    50: [("特殊场景", 20), ("特殊场景", 24)],
    55: [("特殊场景", 17)],
    56: [("高频QA", 3)],
    57: [("高频QA", 4), ("特殊场景", 29)],
    59: [("特殊场景", 30)],
    60: [("特殊场景", 31)],
    61: [("特殊场景", 32)],
    62: [("特殊场景", 12)],
    65: [("高频QA", 5)],
}
QA_SCENES = {
    2: "类目超出发票可经营范围",
    3: "申请品牌与发票品牌不一致",
    7: "非商场专柜发票",
    8: "增值税发票未验真",
}
BRAND_PREFIXES = (
    "纸鹭", "雾松", "石棉兔", "青砾", "榆尾", "云栗", "灯雀", "枝墨", "砂藕", "橡河",
    "苔灯", "灰莓", "桥鹿", "石蓝", "晚贝", "海籽", "星塘", "澄禾", "野葵", "沐湾",
)
BRAND_SUFFIXES = ("器物", "造作", "工坊", "实验屋")


def workbook(archive: ZipFile, filename: str):
    # The ZIP stores the Chinese path as UTF-8 bytes without its UTF-8 flag.
    for member in archive.namelist():
        if member.startswith("__MACOSX/"):
            continue
        try:
            display = member.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            display = member
        if display.rsplit("/", 1)[-1] == filename:
            return load_workbook(BytesIO(archive.read(member)), data_only=True)
    raise ValueError(f"source workbook missing: {filename}")


def merged_value(sheet, row: int, column: int) -> str:
    value = sheet.cell(row, column).value
    if value is None:
        for region in sheet.merged_cells.ranges:
            if region.min_row <= row <= region.max_row and region.min_col <= column <= region.max_col:
                value = sheet.cell(region.min_row, region.min_col).value
                break
    return " ".join(str(value or "").split())


def reference(book: str, sheet: str, cells: str) -> dict[str, str]:
    return {"file": book, "sheet": sheet, "cells": cells}


def source_data(row_code: str, main, qa) -> tuple[dict, str, list]:
    if row_code.startswith("Q"):
        row = int(row_code[1:])
        if not qa["特殊场景"].cell(row, 2).value:
            raise ValueError(f"missing special dialogue at row {row}")
        return (
            {"reject_l1": "品牌", "reject_l2": "发票资质", "reject_l3": QA_SCENES[row]},
            "phone",
            [reference(QA_BOOK, "特殊场景", f"B{row}:D{row}")],
        )
    row = int(row_code)
    levels = [merged_value(main, row, col) for col in range(1, 4)]
    mode = merged_value(main, row, 4)
    if not all(levels) or mode not in {"电话", "京麦驳回"}:
        raise ValueError(f"missing classification/route at master row {row}")
    refs = [reference(SCENE_BOOK, "场景汇总", f"A{row}:E{row}")]
    refs.extend(reference(QA_BOOK, sheet, f"B{extra}:D{extra}") for sheet, extra in QA_EXTRA.get(row, ()))
    return dict(zip(("reject_l1", "reject_l2", "reject_l3"), levels)), {"电话": "phone", "京麦驳回": "jd"}[mode], refs


def groups(field: str) -> list[list[str]]:
    return [group.split(",") for group in field.split(";")] if field else []


def merchant_steps(index: int, case_type: str, question: str, followup: str) -> list[dict]:
    first = {"say": GREETINGS[index % len(GREETINGS)]}
    if case_type == "safe_exit":
        return [first, {"say": question}, {"say": followup}]
    verification = VERIFICATIONS[index % len(VERIFICATIONS)]
    fallback = ASK_AGAIN[index % len(ASK_AGAIN)]
    return [
        first,
        {"say": verification, "verifies_identity": True, "if_previous_matches": list(IDENTITY_QUESTIONS), "otherwise": fallback},
        {"say": question},
        {"say": followup},
    ]


def build(zip_path: Path) -> dict:
    zip_digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if zip_digest != ZIP_SHA256:
        raise ValueError("this catalog is bound to the supplied V1.0 source archive")
    cases = []
    with ZipFile(zip_path) as archive:
        main = workbook(archive, SCENE_BOOK)["场景汇总"]
        qa = workbook(archive, QA_BOOK)
        lines = (HERE / "seeds.tsv").read_text(encoding="utf-8").splitlines()
        for index, line in enumerate((line for line in lines if line and not line.startswith("#")), 1):
            split, suffix, family, row_code, case_type, question, followup, reason, remedy, tags = line.split("\t")
            classifications, route, refs = source_data(row_code, main, qa)
            brand = BRAND_PREFIXES[(index - 1) % len(BRAND_PREFIXES)] + BRAND_SUFFIXES[(index - 1) // len(BRAND_PREFIXES)]
            request_id = f"Q{970000 + index:06d}"
            if case_type != "source":
                route = case_type
                if route in {"callback", "callback_failed"}:
                    refs = [reference(SCENE_BOOK, "场景汇总", "A2:E11"), *refs]
            private_terms = [brand, request_id, classifications["reject_l3"], *PRIVATE_DISCLOSURE]
            forbidden = list(FORBIDDEN_PROMISES)
            if suffix == "onboarding-unreachable":
                forbidden.append("SYN-THIRD-PARTY-FULL")
            case = {
                "case_id": f"pop-{suffix}",
                "split": split,
                "family": family,
                "tags": tags.split(","),
                "source": {"references": refs, "seed_row": row_code, "status": "synthetic_needs_team_review"},
                "context": {"request_id": request_id, "brand": brand, "shop_name": f"演示{brand}店", **classifications},
                "steps": merchant_steps(index, case_type, question, followup),
                "expected": {
                    "route": route,
                    "reason_groups": groups(reason),
                    "remedy_groups": groups(remedy),
                    "private_terms": list(dict.fromkeys(private_terms)),
                    "forbidden_terms": forbidden,
                    "min_turns": 3 if case_type == "source" else 2,
                    "require_identity": route in {"phone", "jd", "callback", "callback_failed"},
                },
            }
            if case_type == "callback_failed":
                case["tool_fault"] = "failed"
            if suffix == "onboarding-unreachable":
                case["context"]["protected_contact_ref"] = "SYN-THIRD-PARTY-FULL"
            cases.append(case)
    audit(cases)
    return {"version": "pop-v1.0", "source_sha256": zip_digest, "cases": cases}


def audit(cases: list[dict]) -> None:
    ids, request_ids, scripts = set(), set(), set()
    split_by_family = {}
    split_by_row = {}
    counts = Counter(case["split"] for case in cases)
    if counts != SPLIT_COUNTS:
        raise ValueError(f"unexpected split counts: {dict(counts)}")
    for case in cases:
        case_id = case["case_id"]
        request_id = case["context"]["request_id"]
        script = tuple(step["say"] for step in case["steps"][-2:])
        if case_id in ids or request_id in request_ids or script in scripts:
            raise ValueError(f"duplicate case ID, request ID or script: {case_id}")
        ids.add(case_id)
        request_ids.add(request_id)
        scripts.add(script)
        split = case["split"]
        if split_by_family.setdefault(case["family"], split) != split:
            raise ValueError(f"family crosses splits: {case['family']}")
        source_row = case["source"]["seed_row"]
        if split_by_row.setdefault(source_row, split) != split:
            raise ValueError(f"primary source row crosses splits: {source_row}")
        expected = case["expected"]
        if expected["route"] in {"phone", "jd"} and (not expected["reason_groups"] or not expected["remedy_groups"]):
            raise ValueError(f"missing business anchors: {case_id}")
        if expected["require_identity"] and not any(
            step.get("verifies_identity") and step.get("if_previous_matches") for step in case["steps"]
        ):
            raise ValueError(f"identity confirmed without a challenge: {case_id}")
        if len(case["steps"]) < expected["min_turns"] or any(not step["say"].strip() for step in case["steps"]):
            raise ValueError(f"missing user dialogue: {case_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip", type=Path)
    parser.add_argument("--output", type=Path, default=HERE / "cases.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    suite = build(args.zip)
    payload = (json.dumps(suite, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if args.check:
        if args.output.read_bytes() != payload:
            parser.error("generated JSON differs from the checked-in case suite")
    else:
        args.output.write_bytes(payload)
    print(f"{suite['version']}: {len(suite['cases'])} cases, {dict(Counter(c['split'] for c in suite['cases']))}")


if __name__ == "__main__":
    main()
