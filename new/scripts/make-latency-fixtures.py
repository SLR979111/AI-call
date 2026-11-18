"""Generate synthetic microphone inputs for the voice latency acceptance test."""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.providers import Cloud, Settings


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dialogues", action="store_true", help="Generate two multi-turn test calls")
    args = parser.parse_args()
    directory = ROOT / "runtime" / "validation" / ("latency-dialogues" if args.dialogues else "latency-inputs")
    cloud = Cloud(Settings.load())
    cases = [
        ("01-owner", "是的，这次的资料是我负责的。"),
        ("02-documents", "方便，请直接告诉我要补什么。"),
        ("03-screenshot", "商标截图上具体要看清哪些信息？"),
        ("04-repeat", "不好意思，刚才没听清，您再说一遍。"),
        ("05-submitted", "我已经重新提交了，多久能看到结果？"),
        ("06-company", "授权文件上的公司名称写错了，我该怎么改？"),
        ("07-chain", "缺少的那段授权，应该是谁给谁的？"),
        ("08-busy", "我现在在开会，不方便继续聊。"),
    ]
    if args.dialogues:
        cases = [
            ("authorization/01", "是我负责。我们已经改成由品牌方直接授权了。"),
            ("authorization/02", "那新的授权书上，被授权方应该填哪家公司？"),
            ("submission/01", "截图已经传上去了，其他两项我还没有改。"),
            ("submission/02", "如果只是公司的名字写错了，能自己在原件上改吗？"),
        ]
    try:
        for name, text in cases:
            path = directory / (name + ".pcm")
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                pcm = b"".join([chunk async for chunk in cloud.speech(text)])
                path.write_bytes(pcm)
    finally:
        await cloud.close()
    print(directory)


if __name__ == "__main__":
    asyncio.run(main())
