#!/usr/bin/env python3
"""Warmup cache with common FAQ responses."""

import asyncio
import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.cache import CacheManager


async def main():
    """Load FAQ data and warmup the cache."""
    cache_manager = CacheManager()
    
    # Load FAQ data
    faq_path = Path(__file__).parent.parent / "tasks" / "faq-cache-warmup.json"
    if not faq_path.exists():
        print("❌ FAQ warmup file not found:", faq_path)
        return
    
    faq_data = json.loads(faq_path.read_text(encoding="utf-8"))
    
    # Warmup LLM cache with common Q&A pairs
    system_prompt = "你是京东资质审核助手，负责解答商家关于资质、授权、材料等问题。"
    model = "deepseek-flash"
    temperature = 0.2
    
    for item in faq_data["common_qa"]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": item["question"]},
        ]
        cache_manager.llm.put(messages, model, temperature, item["answer"])
        print(f"✓ Cached: {item['question']}")
    
    print(f"\n✅ Warmed up {len(faq_data['common_qa'])} FAQ entries")
    print(f"LLM cache: {len(cache_manager.llm.cache)} entries, "
          f"{cache_manager.llm.cache_bytes / 1024:.1f} KB")
    
    # Show cache stats
    stats = cache_manager.report()
    print("\n📊 Cache Statistics:")
    print(f"  LLM: {stats['llm']['entries']} entries, "
          f"{stats['llm']['size_mb']:.2f} MB")
    print(f"  ASR: {stats['asr']['entries']} entries, "
          f"{stats['asr']['size_mb']:.2f} MB")


if __name__ == "__main__":
    asyncio.run(main())
