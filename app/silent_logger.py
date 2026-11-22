"""Silent logging system for debugging and performance analysis."""

import json
import time
from pathlib import Path
from typing import Any


class SilentLogger:
    """Thread-safe JSON Lines logger for silent backend logging."""
    
    def __init__(self, log_dir: Path | None = None):
        self.log_dir = log_dir or Path("runtime/debug")
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        
        # Create timestamped log file
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.log_file = self.log_dir / f"debug_{timestamp}.jsonl"
        
        # Write header
        self._write({
            "event": "logger_started",
            "timestamp": time.time(),
            "log_file": str(self.log_file),
        })
    
    def _write(self, entry: dict[str, Any]):
        """Write a JSON line to the log file."""
        try:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            # Silent failure - don't crash on logging errors
            pass
    
    def log(self, event: str, **kwargs):
        """Log an event with arbitrary fields."""
        entry = {
            "event": event,
            "timestamp": time.time(),
            **kwargs,
        }
        self._write(entry)
    
    def log_decision(self, decision_type: str, **details):
        """Log a decision event."""
        self.log("decision", decision_type=decision_type, **details)
    
    def log_cache_event(self, cache_type: str, hit: bool, **details):
        """Log a cache hit/miss event."""
        self.log("cache", cache_type=cache_type, hit=hit, **details)
    
    def log_latency(self, component: str, latency_ms: float, **details):
        """Log a latency measurement."""
        self.log("latency", component=component, latency_ms=latency_ms, **details)
    
    def log_error(self, error_type: str, message: str, **details):
        """Log an error."""
        self.log("error", error_type=error_type, message=message, **details)
    
    def log_performance(self, **metrics):
        """Log performance metrics."""
        self.log("performance", **metrics)


# Global singleton instance
_logger: SilentLogger | None = None


def get_logger() -> SilentLogger:
    """Get the global logger instance."""
    global _logger
    if _logger is None:
        _logger = SilentLogger()
    return _logger


def silent_log(event: str, **kwargs):
    """Convenience function for logging."""
    get_logger().log(event, **kwargs)
