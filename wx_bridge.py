#!/usr/bin/env python3
"""
微信消息桥 - 基于 Tencent/openclaw-weixin 协议
https://github.com/tencent/openclaw-weixin/blob/HEAD/docs/protocol_zh_CN.md

子命令:
  login       获取二维码并显示（不轮询）
  wait-login  轮询二维码状态，成功后原子写入 state/session.json
  serve       长轮询收消息，写入 inbox/（后台常驻）
  send        发送文字消息：send --to <user_id> --text <文本> --ctx <context_token>
  send-image  发送图片消息：send-image --to <user_id> --file <本地图片> [--text <说明>] --ctx <context_token>
  status      检查登录态和服务状态（凭据脱敏）

只处理扫码绑定用户的私聊消息，忽略群聊和他人。
"""
import argparse
import base64
import hashlib
import json
import os
import random
import struct
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
INBOX_DIR = BASE_DIR / "inbox"
OUTBOX_DIR = BASE_DIR / "outbox"
LOG_DIR = BASE_DIR / "logs"
SESSION_FILE = STATE_DIR / "session.json"
CURSOR_FILE = STATE_DIR / "cursor.txt"

API_BASE = "https://ilinkai.weixin.qq.com"
# 默认 CDN 地址（协议文档；服务端返回完整 URL 时优先使用）
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
APP_ID = "bot"
# 版本号 2.4.8 -> 0x00020408 -> 十进制
CLIENT_VERSION = str(0x00020408)
BOT_AGENT = "MuseWeChatBridge/1.0"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "bridge.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def mask(s, keep=4):
    """脱敏：只保留首尾各 keep/2 个字符"""
    if not s or len(s) <= keep:
        return "***"
    h = keep // 2
    return s[:h] + "***" + s[-h:]


def random_uin():
    v = random.randint(1, 2**32 - 1)
    return base64.b64encode(str(v).encode()).decode()


def base_headers(auth=True):
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": random_uin(),
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": CLIENT_VERSION,
    }
    if auth:
        sess = load_session()
        if sess and sess.get("bot_token"):
            h["Authorization"] = "Bearer " + sess["bot_token"]
    return h


def base_info():
    return {"channel_version": "2.4.8", "bot_agent": BOT_AGENT}


def http_post(path, body, auth=True, base_url=None, timeout=40):
    url = (base_url or API_BASE) + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=base_headers(auth), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:200]}")
    except Exception as e:
        raise RuntimeError(f"请求失败: {e}")


def http_get(path, params, timeout=40):
    url = API_BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": CLIENT_VERSION,
    }, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"请求失败: {e}")


def load_session():
    if not SESSION_FILE.exists():
        return None
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_session_atomic(data):
    """原子写入 session.json"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SESSION_FILE)
    os.chmod(SESSION_FILE, 0o600)


def load_cursor():
    if CURSOR_FILE.exists():
        return CURSOR_FILE.read_text(encoding="utf-8").strip()
    return ""


def save_cursor(buf):
    if buf:
        CURSOR_FILE.write_text(buf, encoding="utf-8")


# ============ 子命令 ============

def cmd_login(args):
    """获取二维码并显示"""
    log("请求登录二维码...")
    # 二维码请求不带 Authorization，但带 AuthorizationType 和 X-WECHAT-UIN
    resp = http_post("/ilink/bot/get_bot_qrcode?bot_type=3",
                     {"local_token_list": []}, auth=False)
    qrcode_val = resp.get("qrcode", "")
    img_content = resp.get("qrcode_img_content", "")
    if not qrcode_val:
        log(f"获取二维码失败: {resp}")
        return 1

    # 保存二维码值供 wait-login 使用
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "pending_qrcode.txt").write_text(qrcode_val, encoding="utf-8")

    # 生成二维码图片
    import qrcode as qr_lib
    # 优先用服务端返回的展示内容，否则用 qrcode 值
    qr_data = img_content if img_content.startswith("http") else qrcode_val
    img = qr_lib.make(qr_data)
    qr_path = STATE_DIR / "qrcode.png"
    img.save(str(qr_path))
    log(f"二维码已生成: {qr_path}")
    log("请用微信扫码。扫码后运行: python3 wx_bridge.py wait-login")
    # 同时输出路径，方便展示
    print(f"QRCODE_PATH={qr_path}")
    return 0


def cmd_wait_login(args):
    """轮询二维码状态"""
    pf = STATE_DIR / "pending_qrcode.txt"
    if not pf.exists():
        log("没有待处理的二维码，请先运行 login")
        return 1
    qrcode_val = pf.read_text(encoding="utf-8").strip()
    log("开始轮询扫码状态（最长 5 分钟）...")
    deadline = time.time() + 300
    base_url = API_BASE
    while time.time() < deadline:
        try:
            resp = http_get("/ilink/bot/get_qrcode_status",
                            {"qrcode": qrcode_val})
        except RuntimeError as e:
            log(f"轮询异常，转为 wait: {e}")
            time.sleep(3)
            continue

        status = resp.get("status", "wait")
        log(f"状态: {status}")

        if status == "confirmed":
            sess = {
                "bot_token": resp.get("bot_token", ""),
                "ilink_bot_id": resp.get("ilink_bot_id", ""),
                "ilink_user_id": resp.get("ilink_user_id", ""),
                "baseurl": resp.get("baseurl", API_BASE),
                "bound_at": datetime.now().isoformat(),
            }
            if not sess["bot_token"]:
                log("confirmed 但缺少 bot_token")
                return 1
            save_session_atomic(sess)
            pf.unlink(missing_ok=True)
            log(f"登录成功！绑定用户: {mask(sess['ilink_user_id'])}")
            log("凭据已原子写入 state/session.json（权限 600）")
            return 0
        elif status == "expired":
            log("二维码已过期，请重新运行 login")
            return 2
        elif status == "need_verifycode":
            log("需要手机验证码：请在微信手机端按提示完成验证后继续等待")
        elif status == "verify_code_blocked":
            log("验证失败次数过多，请重新运行 login 获取新二维码")
            return 3
        elif status == "scaned_but_redirect" and resp.get("redirect_host"):
            base_url = "https://" + resp["redirect_host"]
            log(f"切换地址继续轮询: {base_url}")
        # wait / scaned 继续轮询
        time.sleep(3)

    log("轮询超时（5 分钟），请重新运行 login")
    return 4


def extract_text(msg):
    """从消息中提取文字"""
    for item in msg.get("item_list", []):
        if item.get("type") == 1:
            ti = item.get("text_item", {})
            if isinstance(ti, dict):
                return ti.get("text", "")
    return ""


def cmd_serve(args):
    """长轮询收消息（后台常驻）"""
    sess = load_session()
    if not sess:
        log("未登录，请先运行 login + wait-login")
        return 1
    bound_user = sess.get("ilink_user_id", "")
    log(f"启动消息服务，绑定用户: {mask(bound_user)}")
    base_url = sess.get("baseurl", API_BASE)

    # 通知后端启动
    try:
        http_post("/ilink/bot/msg/notifystart", {"base_info": base_info()},
                  base_url=base_url)
    except Exception as e:
        log(f"notifyStart 警告（不阻断）: {e}")

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    cursor = load_cursor()
    consecutive_errors = 0

    while True:
        try:
            resp = http_post("/ilink/bot/getupdates",
                             {"get_updates_buf": cursor, "base_info": base_info()},
                             base_url=base_url, timeout=60)
        except RuntimeError as e:
            consecutive_errors += 1
            log(f"getupdates 失败 ({consecutive_errors}): {e}")
            if consecutive_errors > 10:
                log("连续失败过多，退出")
                return 1
            time.sleep(5)
            continue

        consecutive_errors = 0
        ret = resp.get("ret", 0)
        errcode = resp.get("errcode", 0)
        if ret == -14 or errcode == -14:
            log("会话被暂停 1 小时（ret/errcode=-14）")
            time.sleep(3600)
            continue
        if ret != 0:
            log(f"getupdates 业务错误 ret={ret} errcode={errcode}")
            time.sleep(5)
            continue

        # 更新游标
        new_buf = resp.get("get_updates_buf", "")
        if new_buf:
            cursor = new_buf
            save_cursor(cursor)

        for msg in resp.get("msgs", []):
            # 只处理用户发来的消息（state 0=新消息，2=已完成，均接受）
            if msg.get("message_type") != 1 or msg.get("message_state") not in (0, 2):
                continue
            from_uid = msg.get("from_user_id", "")
            # 只处理绑定用户的私聊，忽略群聊和他人
            if from_uid != bound_user or msg.get("group_id"):
                continue
            mid = str(msg.get("message_id", ""))
            # 去重
            msg_file = INBOX_DIR / f"{mid}.json"
            if msg_file.exists():
                continue
            text = extract_text(msg)
            # 非文字消息暂不支持
            has_non_text = any(i.get("type") != 1 for i in msg.get("item_list", []))
            record = {
                "message_id": mid,
                "from_user_id": from_uid,
                "text": text,
                "context_token": msg.get("context_token", ""),
                "session_id": msg.get("session_id", ""),
                "create_time_ms": msg.get("create_time_ms", 0),
                "has_non_text": has_non_text,
                "status": "received",
                "raw": msg,
            }
            msg_file.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            log(f"收到新消息 id={mid} 长度={len(text)}")

        # 短轮询间隔由服务端建议决定，无消息时快速继续
        time.sleep(1)


def cmd_send(args):
    """发送文字消息"""
    sess = load_session()
    if not sess:
        log("未登录")
        return 1
    import uuid
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": args.to,
            "client_id": "muse-" + uuid.uuid4().hex[:16],
            "message_type": 2,
            "message_state": 2,
            "context_token": args.ctx,
            "item_list": [{"type": 1, "text_item": {"text": args.text}}],
        },
        "base_info": base_info(),
    }
    if not args.ctx:
        log("警告：缺少 context_token，仍尝试发送")

    base_url = sess.get("baseurl", API_BASE)
    return send_message_body(body, base_url)


def send_message_body(body, base_url):
    """统一的消息发送（带 3 次重试）"""
    for attempt in range(3):
        try:
            resp = http_post("/ilink/bot/sendmessage", body, base_url=base_url)
            if resp.get("ret", 0) == 0:
                log(f"发送成功 (尝试 {attempt + 1})")
                return 0
            log(f"发送业务失败 ret={resp.get('ret')} errmsg={resp.get('errmsg')}")
        except RuntimeError as e:
            log(f"发送网络失败 (尝试 {attempt + 1}): {e}")
        time.sleep(2)
    log("发送失败，已重试 3 次")
    return 1


# ============ 图片发送（CDN 媒体流程） ============

def aes_ecb_padded_size(plaintext_size):
    return ((plaintext_size + 1 + 15) // 16) * 16


def encrypt_aes_ecb(plaintext, key):
    """AES-128-ECB 加密，PKCS#7 填充"""
    from Crypto.Cipher import AES
    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad]) * pad
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.encrypt(padded)


def http_post_binary(url, data, headers, timeout=60):
    """二进制 POST，返回 (响应头字典, 响应体)"""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:200]}")
    except Exception as e:
        raise RuntimeError(f"请求失败: {e}")


def upload_image(path, to_user_id):
    """上传图片到微信 CDN，返回 (download_param, aeskey_hex, ciphertext_size)

    流程遵循 Tencent/openclaw-weixin 协议的 CDN 媒体流程：
    getUploadUrl → AES-128-ECB 加密上传 → 取响应头 x-encrypted-param
    """
    sess = load_session()
    if not sess:
        raise RuntimeError("未登录")
    base_url = sess.get("baseurl", API_BASE)

    with open(path, "rb") as f:
        plaintext = f.read()
    rawsize = len(plaintext)
    if rawsize == 0:
        raise RuntimeError("图片文件为空")
    rawfilemd5 = hashlib.md5(plaintext).hexdigest()
    filesize = aes_ecb_padded_size(rawsize)
    filekey = os.urandom(16).hex()
    aeskey = os.urandom(16)

    log(f"请求上传参数: rawsize={rawsize} filesize={filesize} md5={rawfilemd5[:8]}***")
    resp = http_post("/ilink/bot/getuploadurl", {
        "filekey": filekey,
        "media_type": 1,  # 1=图片
        "to_user_id": to_user_id,
        "rawsize": rawsize,
        "rawfilemd5": rawfilemd5,
        "filesize": filesize,
        "no_need_thumb": True,
        "aeskey": aeskey.hex(),
        "base_info": base_info(),
    }, base_url=base_url)
    if resp.get("ret", 0) != 0:
        raise RuntimeError(f"getUploadUrl 业务失败 ret={resp.get('ret')} errmsg={resp.get('errmsg')}")

    upload_full_url = (resp.get("upload_full_url") or "").strip()
    upload_param = resp.get("upload_param", "")
    if upload_full_url:
        upload_url = upload_full_url
    elif upload_param:
        upload_url = (f"{CDN_BASE}/upload?encrypted_query_param="
                      f"{urllib.parse.quote(upload_param)}&filekey="
                      f"{urllib.parse.quote(filekey)}")
    else:
        raise RuntimeError("getUploadUrl 未返回 upload_full_url 或 upload_param")

    ciphertext = encrypt_aes_ecb(plaintext, aeskey)
    assert len(ciphertext) == filesize, "密文大小与 filesize 不一致"

    download_param = None
    last_err = None
    for attempt in range(3):
        try:
            headers = {"Content-Type": "application/octet-stream"}
            resp_headers, _ = http_post_binary(upload_url, ciphertext, headers)
            # 响应头大小写不敏感
            for k, v in resp_headers.items():
                if k.lower() == "x-encrypted-param" and v:
                    download_param = v
                    break
            if not download_param:
                raise RuntimeError("CDN 上传响应缺少 x-encrypted-param 响应头")
            break
        except RuntimeError as e:
            last_err = e
            msg = str(e)
            if msg.startswith("HTTP 4") or "client error" in msg:
                raise
            log(f"CDN 上传失败 (尝试 {attempt + 1}): {e}")
            time.sleep(2)
    if not download_param:
        raise RuntimeError(f"CDN 上传失败，已重试 3 次: {last_err}")
    log("图片已上传到 CDN")
    return download_param, aeskey.hex(), filesize


def cmd_send_image(args):
    """发送图片消息：send-image --to <user_id> --file <本地图片> [--text <说明文字>] [--ctx <token>]"""
    sess = load_session()
    if not sess:
        log("未登录")
        return 1
    import uuid
    if not os.path.exists(args.file):
        log(f"图片文件不存在: {args.file}")
        return 1
    base_url = sess.get("baseurl", API_BASE)

    try:
        download_param, aeskey_hex, filesize = upload_image(args.file, args.to)
    except RuntimeError as e:
        log(f"图片上传失败: {e}")
        return 1

    # 官方实现：说明文字和媒体内容拆成独立请求，各自独立 client_id
    if args.text:
        text_body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": args.to,
                "client_id": "muse-" + uuid.uuid4().hex[:16],
                "message_type": 2,
                "message_state": 2,
                "context_token": args.ctx,
                "item_list": [{"type": 1, "text_item": {"text": args.text}}],
            },
            "base_info": base_info(),
        }
        if send_message_body(text_body, base_url) != 0:
            log("说明文字发送失败，终止图片发送")
            return 1

    # 官方行为：aes_key 取十六进制密钥字符串再做 base64
    aes_key_b64 = base64.b64encode(aeskey_hex.encode()).decode()
    image_body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": args.to,
            "client_id": "muse-" + uuid.uuid4().hex[:16],
            "message_type": 2,
            "message_state": 2,
            "context_token": args.ctx,
            "item_list": [{
                "type": 2,
                "image_item": {
                    "media": {
                        "encrypt_query_param": download_param,
                        "aes_key": aes_key_b64,
                        "encrypt_type": 1,
                    },
                    "mid_size": filesize,
                },
            }],
        },
        "base_info": base_info(),
    }
    if not args.ctx:
        log("警告：缺少 context_token，仍尝试发送")
    return send_message_body(image_body, base_url)


def cmd_status(args):
    """状态检查（凭据脱敏）"""
    sess = load_session()
    if not sess:
        print(json.dumps({"logged_in": False}, ensure_ascii=False))
        return 0
    # 检查服务进程
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", "wx_bridge.py serve"],
                             capture_output=True, text=True)
        pids = [p for p in out.stdout.strip().split("\n") if p]
        # 排除 pgrep 自身
        serving = len(pids) > 0
    except Exception:
        serving = False
    # 收件箱统计
    inbox_count = len(list(INBOX_DIR.glob("*.json"))) if INBOX_DIR.exists() else 0
    info = {
        "logged_in": True,
        "bot_id": mask(sess.get("ilink_bot_id", "")),
        "bound_user": mask(sess.get("ilink_user_id", "")),
        "bound_at": sess.get("bound_at", ""),
        "serving": serving,
        "inbox_pending": inbox_count,
        "cursor_saved": CURSOR_FILE.exists(),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def main():
    ap = argparse.ArgumentParser(description="微信消息桥")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="获取登录二维码")
    sub.add_parser("wait-login", help="轮询扫码状态")
    sub.add_parser("serve", help="启动消息收发服务")
    sp = sub.add_parser("send", help="发送文字消息")
    sp.add_argument("--to", required=True, help="目标用户 ID")
    sp.add_argument("--text", required=True, help="文字内容")
    sp.add_argument("--ctx", default="", help="会话 context_token")
    si = sub.add_parser("send-image", help="发送图片消息")
    si.add_argument("--to", required=True, help="目标用户 ID")
    si.add_argument("--file", required=True, help="本地图片路径")
    si.add_argument("--text", default="", help="可选说明文字（先于图片单独发送）")
    si.add_argument("--ctx", default="", help="会话 context_token")
    sub.add_parser("status", help="状态检查")

    args = ap.parse_args()
    cmds = {
        "login": cmd_login,
        "wait-login": cmd_wait_login,
        "serve": cmd_serve,
        "send": cmd_send,
        "send-image": cmd_send_image,
        "status": cmd_status,
    }
    return cmds[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
