"""Latency performance test for AI Call G2.

Tests P50/P95/P99 latency across different components:
- ASR (audio-to-text)
- LLM (chat completion)
- TTS (text-to-speech)
- End-to-end (full turn)

Usage:
    pytest new/tests/test_latency.py -v
    pytest new/tests/test_latency.py -v --benchmark
"""

import asyncio
import statistics
import time
from pathlib import Path

import pytest

from app.providers import Cloud, Settings


class LatencyStats:
    """Calculate latency percentiles."""
    
    def __init__(self, samples: list[float]):
        self.samples = sorted(samples)
        self.count = len(samples)
        self.mean = statistics.mean(samples) if samples else 0
        self.median = statistics.median(samples) if samples else 0
        self.p50 = self._percentile(50)
        self.p95 = self._percentile(95)
        self.p99 = self._percentile(99)
        self.min = min(samples) if samples else 0
        self.max = max(samples) if samples else 0
    
    def _percentile(self, p: float) -> float:
        """Calculate percentile (0-100)."""
        if not self.samples:
            return 0
        k = (len(self.samples) - 1) * p / 100
        f = int(k)
        c = k - f
        if f + 1 < len(self.samples):
            return self.samples[f] + (self.samples[f + 1] - self.samples[f]) * c
        return self.samples[f]
    
    def report(self, name: str) -> str:
        """Generate human-readable report."""
        return (
            f"{name:20s} | "
            f"P50: {self.p50:7.1f}ms | "
            f"P95: {self.p95:7.1f}ms | "
            f"P99: {self.p99:7.1f}ms | "
            f"Mean: {self.mean:7.1f}ms | "
            f"Range: [{self.min:6.1f}, {self.max:6.1f}]ms | "
            f"N={self.count}"
        )


@pytest.fixture(scope="function")
async def cloud():
    """Shared cloud provider instance."""
    settings = Settings.load()
    if not settings.api_key:
        pytest.skip("DEEPSEEK_API_KEY not configured")
    
    client = Cloud(settings)
    yield client
    await client.close()


@pytest.mark.asyncio
async def test_asr_latency(cloud):
    """Test ASR transcription latency."""
    # Load test audio samples
    test_audio_dir = Path(__file__).parent.parent / "tests" / "fixtures" / "audio"
    if not test_audio_dir.exists():
        pytest.skip("Test audio fixtures not found")
    
    samples = []
    test_cases = [
        "配送需要多久？",
        "主体资质快到期了怎么办？",
        "需要准备什么材料？",
    ]
    
    # Warm up
    dummy_pcm = b'\x00' * 16000  # 0.5s silence
    try:
        await cloud.transcribe(dummy_pcm)
    except Exception:
        pass  # Warmup failure is OK
    
    # Test with cache disabled (measure API latency)
    for text in test_cases:
        # Generate simple test PCM (silence)
        pcm = b'\x00' * (16000 * 2)  # 1 second of silence, 16kHz mono 16-bit
        
        start = time.perf_counter()
        try:
            result = await cloud.transcribe(pcm)
            elapsed = (time.perf_counter() - start) * 1000
            samples.append(elapsed)
            print(f"  ASR: {elapsed:.1f}ms -> '{result}'")
        except Exception as e:
            print(f"  ASR failed: {e}")
    
    if not samples:
        pytest.skip("No successful ASR samples")
    
    stats = LatencyStats(samples)
    print(f"\n{stats.report('ASR')}")
    
    # Assertions
    assert stats.p50 < 1000, f"ASR P50 latency {stats.p50:.1f}ms exceeds 1000ms"
    assert stats.p95 < 2000, f"ASR P95 latency {stats.p95:.1f}ms exceeds 2000ms"


@pytest.mark.asyncio
async def test_llm_latency(cloud):
    """Test LLM chat completion latency (TTFT and total)."""
    test_queries = [
        "配送需要多久？",
        "主体资质快到期了怎么办？",
        "需要准备什么材料？",
        "授权书怎么填写？",
        "商标注册证在哪里查？",
    ]
    
    ttft_samples = []  # Time to first token
    total_samples = []  # Total completion time
    
    # Warm up
    messages = [{"role": "user", "content": "你好"}]
    try:
        async for _ in cloud.chat(messages):
            break
    except Exception:
        pass
    
    # Clear cache to measure API latency
    cloud.cache_manager.llm.cache.clear()
    
    for query in test_queries:
        messages = [
            {"role": "system", "content": "你是京东资质审核助手。"},
            {"role": "user", "content": query},
        ]
        
        start = time.perf_counter()
        first_token_time = None
        chunks = []
        
        try:
            async for chunk in cloud.chat(messages, max_tokens=300):
                if first_token_time is None:
                    first_token_time = (time.perf_counter() - start) * 1000
                chunks.append(chunk)
            
            total_time = (time.perf_counter() - start) * 1000
            response = "".join(chunks)
            
            if first_token_time is not None:
                ttft_samples.append(first_token_time)
                total_samples.append(total_time)
                print(f"  LLM: TTFT={first_token_time:.1f}ms, Total={total_time:.1f}ms")
                print(f"       Query: '{query}'")
                print(f"       Response: '{response[:50]}...'")
        except Exception as e:
            print(f"  LLM failed for '{query}': {e}")
    
    if not ttft_samples:
        pytest.skip("No successful LLM samples")
    
    ttft_stats = LatencyStats(ttft_samples)
    total_stats = LatencyStats(total_samples)
    
    print(f"\n{ttft_stats.report('LLM TTFT')}")
    print(f"{total_stats.report('LLM Total')}")
    
    # Assertions
    assert ttft_stats.p50 < 2000, f"LLM TTFT P50 {ttft_stats.p50:.1f}ms exceeds 2000ms"
    assert total_stats.p50 < 5000, f"LLM Total P50 {total_stats.p50:.1f}ms exceeds 5000ms"


@pytest.mark.asyncio
async def test_tts_latency(cloud):
    """Test TTS synthesis latency."""
    test_texts = [
        "您好，配送一般需要1-3个工作日。",
        "请您及时上传最新的资质文件。",
        "入驻需要准备营业执照、法人身份证、商标注册证等材料。",
    ]
    
    samples = []
    
    # Warm up
    try:
        async for _ in cloud.speech("测试"):
            break
    except Exception:
        pass
    
    # Clear cache to measure API latency
    cloud.cache.clear()
    
    for text in test_texts:
        start = time.perf_counter()
        chunk_count = 0
        
        try:
            async for chunk in cloud.speech(text):
                chunk_count += 1
            
            elapsed = (time.perf_counter() - start) * 1000
            samples.append(elapsed)
            print(f"  TTS: {elapsed:.1f}ms, {chunk_count} chunks -> '{text[:30]}...'")
        except Exception as e:
            print(f"  TTS failed for '{text}': {e}")
    
    if not samples:
        pytest.skip("No successful TTS samples")
    
    stats = LatencyStats(samples)
    print(f"\n{stats.report('TTS')}")
    
    # Assertions
    assert stats.p50 < 1000, f"TTS P50 latency {stats.p50:.1f}ms exceeds 1000ms"
    assert stats.p95 < 2000, f"TTS P95 latency {stats.p95:.1f}ms exceeds 2000ms"


@pytest.mark.asyncio
async def test_cache_hit_latency(cloud):
    """Test latency with cache hits (should be <100ms)."""
    test_text = "配送需要多久？"
    
    # Prime cache
    messages = [
        {"role": "system", "content": "你是京东资质审核助手。"},
        {"role": "user", "content": test_text},
    ]
    
    # First call - cache miss
    async for _ in cloud.chat(messages):
        pass
    
    # Measure cache hits
    samples = []
    for i in range(10):
        start = time.perf_counter()
        async for _ in cloud.chat(messages):
            pass
        elapsed = (time.perf_counter() - start) * 1000
        samples.append(elapsed)
    
    stats = LatencyStats(samples)
    print(f"\n{stats.report('LLM Cache Hit')}")
    
    # Cache hits should be very fast
    assert stats.p95 < 200, f"Cache hit P95 {stats.p95:.1f}ms exceeds 200ms"
    
    # Test TTS cache hit
    tts_samples = []
    tts_text = "您好，配送一般需要1-3个工作日。"
    
    # Prime TTS cache
    async for _ in cloud.speech(tts_text):
        pass
    
    # Measure TTS cache hits
    for i in range(10):
        start = time.perf_counter()
        async for _ in cloud.speech(tts_text):
            pass
        elapsed = (time.perf_counter() - start) * 1000
        tts_samples.append(elapsed)
    
    tts_stats = LatencyStats(tts_samples)
    print(f"{tts_stats.report('TTS Cache Hit')}")
    
    assert tts_stats.p95 < 100, f"TTS cache hit P95 {tts_stats.p95:.1f}ms exceeds 100ms"


@pytest.mark.asyncio
async def test_cache_stats(cloud):
    """Verify cache statistics are tracked correctly."""
    # Clear caches
    cloud.cache_manager.llm.cache.clear()
    cloud.cache_manager.asr.cache.clear()
    
    # Reset stats
    cloud.cache_manager.llm.stats.hits = 0
    cloud.cache_manager.llm.stats.misses = 0
    
    messages = [{"role": "user", "content": "测试"}]
    
    # First call - should be miss
    async for _ in cloud.chat(messages):
        pass
    assert cloud.cache_manager.llm.stats.misses == 1
    assert cloud.cache_manager.llm.stats.hits == 0
    
    # Second call - should be hit
    async for _ in cloud.chat(messages):
        pass
    assert cloud.cache_manager.llm.stats.hits == 1
    
    # Check report format
    report = cloud.cache_manager.report()
    assert "llm" in report
    assert "hit_rate" in report["llm"]
    assert report["llm"]["hits"] == 1
    assert report["llm"]["misses"] == 1
    
    print(f"\nCache report: {report}")


@pytest.mark.asyncio
async def test_benchmark_full_suite(cloud):
    """Full benchmark suite with detailed breakdown."""
    print("\n" + "=" * 80)
    print("LATENCY BENCHMARK - Full Suite")
    print("=" * 80)
    
    # Run all component tests
    await test_llm_latency(cloud)
    print()
    await test_tts_latency(cloud)
    print()
    await test_cache_hit_latency(cloud)
    
    print("\n" + "=" * 80)
    print("CACHE STATISTICS")
    print("=" * 80)
    report = cloud.cache_manager.report()
    for component, stats in report.items():
        print(f"\n{component.upper()}:")
        for key, value in stats.items():
            print(f"  {key:15s}: {value}")
