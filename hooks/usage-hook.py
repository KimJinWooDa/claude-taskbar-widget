# -*- coding: utf-8 -*-
"""Claude Code Stop 훅에서 사용량(rate_limits)을 뽑아 위젯용 파일로 남긴다.

API 경로가 막힌 계정에서의 폴백 데이터원. 대화 내용은 저장하지 않는다.
구조 진단 덤프는 환경변수 CLAUDE_WIDGET_DEBUG=1 일 때만 남긴다.
"""
import json
import os
import sys
import time

HOME = os.path.expanduser("~")
OUT = os.path.join(HOME, ".claude", "usage-widget.json")
DUMP = os.path.join(HOME, ".claude", "usage-hook-debug.json")

try:
    raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
    data = json.loads(raw)
except Exception as e:
    data = {"_parse_error": str(e)}


def find_rate_limits(o, depth=0):
    """중첩 어디에 있든 rate_limits 유사 구조를 찾는다."""
    if depth > 6 or not isinstance(o, dict):
        return None
    if any(k in o for k in ("five_hour", "seven_day")):
        return o
    rl = o.get("rate_limits")
    if isinstance(rl, dict) and rl:
        return rl
    for v in o.values():
        if isinstance(v, dict):
            hit = find_rate_limits(v, depth + 1)
            if hit:
                return hit
    return None


def shape(o, depth=0):
    if depth > 3:
        return "..."
    if isinstance(o, dict):
        return {k: shape(v, depth + 1) for k, v in list(o.items())[:40]}
    if isinstance(o, list):
        return [shape(v, depth + 1) for v in o[:2]]
    if isinstance(o, str):
        return o[:60]
    return o


if os.environ.get("CLAUDE_WIDGET_DEBUG") == "1":
    try:
        with open(DUMP, "w", encoding="utf-8") as f:
            json.dump({"at": time.time(), "top_keys": sorted(data.keys())
                       if isinstance(data, dict) else None,
                       "shape": shape(data)}, f, ensure_ascii=False, indent=1)
    except OSError:
        pass

rl = find_rate_limits(data)
if rl:
    def norm(v):
        if not isinstance(v, dict):
            return None
        pct = v.get("used_percentage")
        if pct is None and v.get("utilization") is not None:
            u = float(v["utilization"])
            pct = u * 100 if u <= 1 else u
        if pct is None:
            return None
        return {"used_percentage": float(pct), "resets_at": v.get("resets_at")}

    windows = {k: norm(v) for k, v in rl.items() if isinstance(v, dict)}
    windows = {k: v for k, v in windows.items() if v}
    if windows:
        try:
            tmp = f"{OUT}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"written_at": time.time(), "source": "hook",
                           "rate_limits": windows}, f)
            os.replace(tmp, OUT)
        except OSError:
            pass
