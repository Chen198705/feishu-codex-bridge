#!/usr/bin/env python3
"""feishu-codex-bridge: 飞书机器人 ⇄ 本机 Codex CLI 的最小桥.

原理：飞书开放平台 WebSocket 长连接（免公网）收私聊消息 → 在本机工作区执行
`codex exec`（或 resume 同一会话）→ 把最终回复以消息发回。

安全默认：
  * 白名单 open_id，空名单 = 全部拒绝（并把对方 open_id 告诉它以便加白）
  * 不传任何跳过审批/沙箱的 flag；要放宽请在 config 里显式写 codex_flags
  * 超时兜底，输出截断
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
    ReplyMessageRequest, ReplyMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.ws import Client as WSClient

MAX_REPLY = 3800  # 飞书单条文本上限约 4k 字符，留余量分条


# ── config ────────────────────────────────────────────────────────────────

def load_config(path: Path, require_feishu: bool = True) -> dict:
    cfg = json.loads(path.read_text("utf-8"))
    if require_feishu:
        for k in ("app_id", "app_secret"):
            if not cfg.get(k):
                sys.exit(f"config 缺少 {k}")
    cfg.setdefault("codex_bin", "codex")
    cfg.setdefault("workspace", str(Path.home()))
    cfg.setdefault("codex_flags", [])           # 如 ["--sandbox","workspace-write"]
    cfg.setdefault("timeout_sec", 600)
    cfg.setdefault("allowed_open_ids", [])      # 空 = 拒绝所有人
    cfg.setdefault("default_model", None)
    cfg.setdefault("reply_header", False)       # True 则附耗时/会话等调试信息
    return cfg


# ── codex executor（不依赖飞书，可独立自测）────────────────────────────────

class CodexResult:
    def __init__(self, text, session_id=None, elapsed=None, exit_code=0):
        self.text, self.session_id = text, session_id
        self.elapsed, self.exit_code = elapsed, exit_code


def run_codex(cfg, prompt, session_id=None):
    """跑一次 codex exec。session_id 非空则 resume 该会话。返回 CodexResult。"""
    bin_ = shutil.which(cfg["codex_bin"]) or cfg["codex_bin"]
    cmd = [bin_, "exec"]
    if session_id:
        # exec resume 不认 -C/--sandbox（会话自带工作区），只走 -c 配置覆盖
        cmd += ["resume", session_id, "--json", "--skip-git-repo-check"]
    else:
        cmd += ["-C", cfg["workspace"], "--json", "--skip-git-repo-check"]
    if cfg.get("default_model"):
        cmd += ["--model", cfg["default_model"]]
    flags = cfg.get("codex_flags") or []
    if isinstance(flags, str):
        flags = shlex.split(flags)
    cmd += list(flags) + [prompt]

    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=cfg["timeout_sec"], cwd=cfg["workspace"])
    except subprocess.TimeoutExpired:
        return CodexResult(f"⏱ 超时（>{cfg['timeout_sec']}s），任务已终止", session_id, time.time() - t0)
    except FileNotFoundError:
        return CodexResult(f"❌ 找不到可执行文件：{bin_}（先 brew install codex 并 codex login）",
                           session_id, time.time() - t0)
    elapsed = time.time() - t0
    out, sid, msgs = _parse_jsonl(p.stdout), None, []
    for ev in out:
        t = str(ev.get("type", ""))
        # 事件字段名跨版本不同，全部宽松匹配
        sid = ev.get("thread_id") or ev.get("session_id") or sid
        if t in ("item.completed", "response.output_item.done") or "agent_message" in t or t == "message":
            it = ev.get("item") or ev
            if str(it.get("type", "")) in ("agent_message", "output_text", "message") or t in ("agent_message", "message"):
                txt = it.get("message") or it.get("text") or it.get("content") or ""
                if isinstance(txt, str) and txt.strip():
                    msgs.append(txt.strip())
        elif t in ("error",):
            err = ev.get("message") or ev.get("error") or ""
            if isinstance(err, str) and err.strip() and "approval" not in err.lower():
                msgs.append(f"⚠️ {err.strip()}")
    text = "\n\n".join(dict.fromkeys(msgs)).strip()  # 去重拼接
    if not text:
        text = p.stdout.strip() or p.stderr.strip() or "(无输出)"
        if len(text) > MAX_REPLY:
            text = text[:MAX_REPLY] + "\n…（截断）"
    if p.returncode != 0:
        text = f"⚠️ codex 退出码 {p.returncode}\n\n{text}"
    return CodexResult(text, sid, elapsed, p.returncode)


def _parse_jsonl(raw):
    evs = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                evs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return evs


def selftest(cfg, prompt):
    r = run_codex(cfg, prompt)
    print(f"[selftest] exit={r.exit_code} elapsed={r.elapsed:.1f}s session={r.session_id}")
    print(r.text)
    return r.exit_code == 0


# ── feishu glue ───────────────────────────────────────────────────────────

class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.api = lark.Client.builder().app_id(cfg["app_id"]).app_secret(cfg["app_secret"]).build()
        self._tok, self._tok_exp = None, 0.0
        self.lock = threading.Lock()
        self.sessions = {}  # chat_id -> codex session_id
        sp = Path(cfg["workspace"]) / ".feishu-codex-sessions.json"
        try:
            self.sessions.update(json.loads(sp.read_text("utf-8")))
        except Exception:
            pass
        self._sp = sp

    def _save(self):
        self._sp.write_text(json.dumps(self.sessions, ensure_ascii=False, indent=1), "utf-8")

    # -- outbound
    def _token(self):
        """tenant_access_token，带进程内缓存（飞书给 2h，提前 5 分钟续）。"""
        if self._tok and time.time() < self._tok_exp:
            return self._tok
        import urllib.request
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        body = json.dumps({"app_id": self.cfg["app_id"], "app_secret": self.cfg["app_secret"]}).encode()
        d = json.loads(urllib.request.urlopen(req, body, timeout=15).read())
        self._tok, self._tok_exp = d["tenant_access_token"], time.time() + d.get("expire", 7200) - 300
        return self._tok

    def _api(self, path, method="POST", body=None):
        import urllib.request
        req = urllib.request.Request("https://open.feishu.cn/open-apis" + path, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Authorization", "Bearer " + self._token())
        data = json.dumps(body).encode() if body is not None else None
        d = json.loads(urllib.request.urlopen(req, data, timeout=15).read())
        if d.get("code") not in (0, None):
            print(f"[api-err] {method} {path} -> {d.get('code')} {d.get('msg')}", flush=True)
        return d

    def react(self, message_id, emoji, remove_id=None):
        if remove_id:
            self._api(f"/im/v1/messages/{message_id}/reactions/{remove_id}", "DELETE")
            return None
        d = self._api(f"/im/v1/messages/{message_id}/reactions",
                      body={"reaction_type": {"emoji_type": emoji}})
        return (d.get("data") or {}).get("reaction_id")

    def send(self, chat_id, text, reply_to=None):
        for i in range(0, len(text), MAX_REPLY):
            chunk = text[i:i + MAX_REPLY]
            if reply_to:  # 结果贴回用户消息串；失败自动降级为普通发送
                try:
                    self._api(f"/im/v1/messages/{reply_to}/reply",
                              body={"msg_type": "text",
                                    "content": json.dumps({"text": chunk}, ensure_ascii=False)})
                    continue
                except Exception as e:
                    print(f"[reply-err] {e} -> fallback create", flush=True)
            req = (CreateMessageRequest.builder().receive_id_type("chat_id")
                   .request_body(CreateMessageRequestBody.builder().receive_id(chat_id)
                   .msg_type("text").content(json.dumps({"text": chunk})).build()).build())
            try:
                self.api.im.v1.message.create(req)
            except Exception as e:
                print(f"[send-err] {e}", flush=True)

    # -- inbound
    def on_message(self, data: P2ImMessageReceiveV1):
        try:
            ev = data.event
            msg, sender = ev.message, ev.sender
            if msg.message_type != "text":
                self.send(msg.chat_id, "目前只处理文本消息（文件/图片请先放到工作区）")
                return
            text = (json.loads(msg.content).get("text") or "").strip()
            open_id = (sender.sender_id.open_id or "") if sender and sender.sender_id else ""
            chat_id = msg.chat_id
            print(f"[msg] chat={chat_id[:12]}… open_id={open_id} text={text[:60]!r}", flush=True)

            allowed = self.cfg["allowed_open_ids"]
            if allowed and open_id not in allowed:
                print(f"[rejected] open_id={open_id} text={text[:40]!r}", flush=True)
                return
            if not allowed:
                self.send(chat_id, f"未配置白名单，你的 open_id 是：\n{open_id}\n（写进 config 的 allowed_open_ids 后重启）")
                return

            text = re.sub(r"^@_user_\d+\s*", "", text).strip()
            # 内置命令
            low = text.lower()
            if low in ("/new", "/reset", "新会话"):
                with self.lock:
                    self.sessions.pop(chat_id, None); self._save()
                self.send(chat_id, "🆕 已开新会话"); return
            if low in ("/whoami", "id"):
                self.send(chat_id, f"open_id: {open_id}\nchat_id: {chat_id}"); return
            if low in ("/status", "状态"):
                sid = self.sessions.get(chat_id)
                self.send(chat_id, "\n".join([
                    f"工作区: {self.cfg['workspace']}",
                    f"codex: {self.cfg['codex_bin']}",
                    f"flags: {self.cfg['codex_flags'] or '默认（沙箱读+审批）'}",
                    f"本会话: {sid or '（未开始，下条消息即新建）'}"]))
                return
            if not text:
                return

            sid = self.sessions.get(chat_id)
            mid = msg.message_id
            rmid = None  # 「进行中」表情的 reaction_id，完工后揭掉
            try:
                rmid = self.react(mid, self.cfg.get("processing_emoji", "OnIt"))
            except Exception as e:
                print(f"[react-err] {e}", flush=True)

            def worker():
                r = run_codex(self.cfg, text, sid)
                if r.session_id and r.session_id != sid:
                    with self.lock:
                        self.sessions[chat_id] = r.session_id; self._save()
                tail = ""
                if self.cfg["reply_header"] or r.exit_code != 0:
                    tail = f"\n\n⚙️ {r.elapsed:.0f}s · session {str(r.session_id)[:8]}"
                self.send(chat_id, r.text + tail, reply_to=mid)
                if rmid:
                    try:
                        self.react(mid, None, remove_id=rmid)
                    except Exception:
                        pass
            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:
            print(f"[handler-err] {e}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    ap.add_argument("--selftest", metavar="PROMPT",
                    help="不连飞书，直接跑一次 codex exec 验证执行链路")
    args = ap.parse_args()

    cfg = load_config(Path(args.config), require_feishu=not args.selftest)
    if args.selftest:
        sys.exit(0 if selftest(cfg, args.selftest) else 1)

    bridge = Bridge(cfg)
    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(bridge.on_message).build())
    ws = WSClient(cfg["app_id"], cfg["app_secret"], event_handler=handler)
    print(f"[bridge] 已启动（长连接模式） 工作区={cfg['workspace']} 白名单={len(cfg['allowed_open_ids'])}人", flush=True)
    ws.start()  # 阻塞


if __name__ == "__main__":
    main()
