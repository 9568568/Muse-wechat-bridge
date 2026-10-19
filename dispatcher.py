#!/usr/bin/env python3
"""
微信任务调度器
- 扫描 inbox/ 中的新消息（status=received）
- 对每条消息派发一个具备 Muse 工具能力的子代理执行
- 子代理完成后，结果写入 outbox/ 并经 wx_bridge.py send 发回微信

由 cron 每分钟触发（或 systemd 常驻轮询）。
"""
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INBOX_DIR = BASE_DIR / "inbox"
OUTBOX_DIR = BASE_DIR / "outbox"
STATE_DIR = BASE_DIR / "state"

# 去重：记录已派发的 message_id
DISPATCHED_FILE = STATE_DIR / "dispatched.json"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [dispatcher] {msg}"
    print(line, flush=True)


def load_dispatched():
    if DISPATCHED_FILE.exists():
        try:
            return set(json.loads(DISPATCHED_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_dispatched(s):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DISPATCHED_FILE.write_text(json.dumps(sorted(s), ensure_ascii=False), encoding="utf-8")


def mark_status(mid, status):
    """更新消息状态"""
    f = INBOX_DIR / f"{mid}.json"
    if not f.exists():
        return
    try:
        rec = json.loads(f.read_text(encoding="utf-8"))
        rec["status"] = status
        rec["status_at"] = datetime.now().isoformat()
        f.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"更新状态失败 {mid}: {e}")


def dispatch_message(rec):
    """
    派发一条消息给任务执行者。
    返回 True 表示已派发。
    """
    mid = rec["message_id"]
    text = rec.get("text", "")
    from_uid = rec.get("from_user_id", "")
    ctx = rec.get("context_token", "")
    has_non_text = rec.get("has_non_text", False)

    log(f"派发消息 {mid}: {text[:50]}")

    # 非文字消息直接回复固定文本
    if has_non_text and not text:
        reply = "这条通道暂时没有配置这类内容处理，请先用文字描述"
        send_reply(from_uid, ctx, reply, mid)
        mark_status(mid, "sent")
        return True

    # 构造子代理任务
    # 注意：实际执行由 cron agent 通过 subagent.spawn 完成，
    # 这里只做标记，真正的派发在 cron 任务体中进行。
    mark_status(mid, "executing")
    return True


def send_reply(to_uid, ctx, text, mid):
    """经 wx_bridge.py 发送回复"""
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    # 记录待发送
    out = {
        "message_id": mid,
        "to": to_uid,
        "text": text,
        "ctx": ctx,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
    }
    (OUTBOX_DIR / f"{mid}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    cmd = [sys.executable, str(BASE_DIR / "wx_bridge.py"), "send",
           "--to", to_uid, "--text", text, "--ctx", ctx]
    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                out["status"] = "sent"
                out["sent_at"] = datetime.now().isoformat()
                (OUTBOX_DIR / f"{mid}.json").write_text(
                    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
                log(f"回复已发送 {mid}")
                return True
            log(f"发送失败 (尝试 {attempt+1}): {r.stderr[:200]}")
        except Exception as e:
            log(f"发送异常 (尝试 {attempt+1}): {e}")
        time.sleep(2)
    out["status"] = "failed"
    (OUTBOX_DIR / f"{mid}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return False


def main():
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    dispatched = load_dispatched()
    new_count = 0

    for f in sorted(INBOX_DIR.glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        mid = rec.get("message_id", "")
        status = rec.get("status", "received")
        if status != "received" or mid in dispatched:
            continue
        if dispatch_message(rec):
            dispatched.add(mid)
            new_count += 1

    save_dispatched(dispatched)
    if new_count:
        log(f"本轮派发 {new_count} 条新消息")
    return 0


if __name__ == "__main__":
    sys.exit(main())
