"""局域网剪贴板中转站。

电脑和手机打开同一个网页，一端输入另一端实时出现，点一下就复制。
文字之外也能互传文件 —— 截图、apk、PDF、压缩包都行。

它必须监听 0.0.0.0 手机才连得上 —— 也就是同一个 WiFi 下的任何人
都能看到这里的内容。别往里贴密码。真要收回来，加 --host 127.0.0.1。

启动：python server.py
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import socket
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_DATA_DIR = BASE_DIR / ".data"

# 收件箱：传上来的文件照原名再落一份到这儿，省掉「在电脑上再下载一次」。
# 默认就是系统下载目录 —— 跟手点下载的落点一致，找文件不用改习惯。
DEFAULT_INBOX = Path.home() / "Downloads"

# 收件流水账，就放在收件箱里。网页上那份列表是内存里的，进程一重启就空了，
# 这个文件是唯一能长期回答「那东西什么时候收到的、原名叫什么、落在哪」的地方。
INBOX_LOG_NAME = "收件记录.txt"
INBOX_LOG_HEADER = "# 局域网剪贴板收件记录 —— 每收到一个文件追加一行，只追加，不会自己清理\n"

DEFAULT_PORT = 8900

# 一次贴进来 20 万字已经不是「复制一段文字」了，多半是误操作，别让它把内存撑爆
MAX_TEXT_CHARS = 200_000

# 文件：单个上限、保留个数、磁盘总量。三条谁先到都会把最早的挤掉。
# 单文件上限可以用 --max-file-mb 调大，内存不受影响 —— 上传下载都是分块流式的。
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_FILES = 20
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

# 流式搬运的块大小，256 KB 够用了
CHUNK_BYTES = 256 * 1024

# 拒收一个文件时，顺手把已经在路上的请求体吞掉 —— 不吞的话直接关连接，
# 对面看到的是「连接被重置」而不是我们写的那句人话。但也不能几百兆全读完，
# 那跟没拒绝一样，读到 1 MB 就撒手，剩下的靠断连收场。
DRAIN_LIMIT = 1024 * 1024

# 没数据的时候每 20 秒给 SSE 连接发个心跳，顺便探一下对端还在不在
HEARTBEAT_SECONDS = 20

# 只有这几种类型敢原样 inline 回去（点开直接看图）。
# 其余一律当附件下发并改成 octet-stream —— 不然传个 .html / .svg 上来，
# 点开就是一个同源页面在跑脚本。
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}

FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Windows 存不下这些字符，其中 : 尤其要掐 —— a:b.txt 会被当成备用数据流写进去
ILLEGAL_NAME_CHARS = '<>:"/\\|?*'

# 这些名字在 Windows 上是设备，不是文件。带扩展名也一样（CON.txt 照样是 CON），
# 建出来的东西打不开也删不掉，所以一律加个前缀躲开。
WIN_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def safe_name(raw: str) -> str:
    """把客户端报上来的文件名收拾干净 —— 只保留最后一段，掐掉换行和超长。"""
    name = urllib.parse.unquote(raw or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:120] or "文件"


def inbox_name(raw: str) -> str:
    """收件箱专用：比 safe_name 再狠一道。

    .data 里落盘一律是 {id}.bin，客户端报的名字压根碰不到文件系统。
    收件箱不一样 —— 那是真拿原名往你下载目录里写，所以这里得把
    Windows 那套额外规矩（非法字符、设备名、结尾的点和空格）全兜住。
    """
    name = safe_name(raw)
    name = "".join("_" if ch in ILLEGAL_NAME_CHARS else ch for ch in name)
    name = name.rstrip(". ")  # 结尾的点和空格会被 Windows 悄悄吃掉，自己先去干净
    if not name:
        return "文件"
    if name.split(".")[0].upper() in WIN_RESERVED_NAMES:
        name = "_" + name
    return name[:120]


def human_size(n: int) -> str:
    """给人看的大小。流水账里要对齐，所以宽度自己在调用处补。"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1048576:.1f} MB"


def unique_path(folder: Path, name: str) -> Path:
    """重名就往后排 —— 下载目录里已经有的东西，谁也别想悄悄替掉。"""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:  # 没扩展名，或者 .gitignore 这种整个都是「扩展名」
        stem, ext = name, ""
    suffix = f".{ext}" if ext else ""
    candidate = folder / name
    i = 2
    while candidate.exists():
        candidate = folder / f"{stem}-{i}{suffix}"
        i += 1
    return candidate


# ─── 共享白板 ────────────────────────────────────────────────


@dataclass
class FileMeta:
    id: str
    name: str
    size: int
    content_type: str
    at: float
    by: str

    def as_json(self) -> dict:
        """字段名转驼峰 —— 前端按 contentType 判断能不能当图片预览，
        直接 asdict 出来是 content_type，前端读到 undefined 就全当成普通文件了。"""
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "contentType": self.content_type,
            "at": self.at,
            "by": self.by,
        }


class Board:
    """一块共享白板：一段文字 + 最近若干个文件。

    rev 是全局版本号，任何变更都 +1。前端靠它判断自己是不是落后了。
    """

    def __init__(self, data_dir: Path, inbox: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._data_dir = data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self.inbox = inbox
        self.rev = 0
        self.text = ""
        self.updated_by = ""
        self.updated_at = 0.0
        self._files: list[FileMeta] = []

    # 读 ─────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "rev": self.rev,
                "text": self.text,
                "updatedBy": self.updated_by,
                "updatedAt": self.updated_at,
                "files": [m.as_json() for m in reversed(self._files)],
                # 只报目录名，不报完整路径 —— 路径里带着本机用户名，
                # 没必要发给局域网上的每一台设备
                "inbox": {
                    "enabled": self.inbox is not None,
                    "name": self.inbox.name if self.inbox is not None else "",
                },
                # 前端拿这个做上传前预检，省得传完几百兆才被拒
                "limits": {
                    "maxFileBytes": MAX_FILE_BYTES,
                    "maxFiles": MAX_FILES,
                    "maxTextChars": MAX_TEXT_CHARS,
                },
            }

    def file_meta(self, file_id: str) -> FileMeta | None:
        if not FILE_ID_RE.match(file_id or ""):
            return None
        with self._lock:
            for meta in self._files:
                if meta.id == file_id:
                    return meta
        return None

    def file_path(self, meta: FileMeta | str) -> Path:
        """落盘名一律 {id}.bin —— 原始文件名只存在元数据里，碰不到文件系统。"""
        file_id = meta if isinstance(meta, str) else meta.id
        return self._data_dir / f"{file_id}.bin"

    # 写 ─────────────────────────────────────────────────────

    def set_text(self, text: str, client_id: str) -> dict:
        with self._lock:
            self.text = text
            self.updated_by = client_id
            self.updated_at = time.time()
            self.rev += 1
            return self.snapshot()

    def new_upload(self) -> tuple[str, Path]:
        """发一个 id 和落盘路径，调用方把字节流写进去，写完再 commit。"""
        file_id = uuid.uuid4().hex
        return file_id, self.file_path(file_id)

    def commit_upload(
        self, file_id: str, name: str, content_type: str, size: int, client_id: str
    ) -> tuple[dict, FileMeta]:
        meta = FileMeta(
            id=file_id,
            name=name or "文件",
            size=size,
            content_type=content_type or "application/octet-stream",
            at=time.time(),
            by=client_id,
        )
        with self._lock:
            self._files.append(meta)
            self._evict()
            self.updated_by = client_id
            self.updated_at = meta.at
            self.rev += 1
            return self.snapshot(), meta

    def deliver_to_inbox(self, meta: FileMeta) -> Path | None:
        """把文件送进收件箱，顺手在流水账上记一笔。落不进去就什么都不记。"""
        dest = self._place_in_inbox(meta)
        if dest is not None:
            self._log_to_inbox(meta, dest)
        return dest

    def _log_to_inbox(self, meta: FileMeta, dest: Path) -> None:
        """往收件箱里的流水账追加一行。

        网页上那份列表只活在内存里，进程一重启就空了 —— 这个文件是唯一能长期
        回答「那东西什么时候收到的、原名叫什么、落在哪」的地方，所以只追加不清理。
        记不上也不算事故，文件该在哪还在哪。
        """
        log = self.inbox / INBOX_LOG_NAME
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta.at))
        line = f"{stamp}  {human_size(meta.size):>9}  {dest}"
        if dest.name != meta.name:
            # 重名排到了 -2，或者名字里有 Windows 存不下的字符被换掉了
            line += f"   (原名 {meta.name})"
        try:
            with self._lock:
                fresh = not log.exists()
                with log.open("a", encoding="utf-8") as fp:
                    if fresh:
                        fp.write(INBOX_LOG_HEADER)
                    fp.write(line + "\n")
        except OSError as exc:
            print(f"  收件记录写入失败（{exc}）—— 不影响文件本身")

    def _place_in_inbox(self, meta: FileMeta) -> Path | None:
        """把刚收到的文件照原名送进收件箱，省掉「在电脑上再下载一次」那一步。

        收件箱这份不参与淘汰 —— .data 里那份将来被挤掉，这份照样在。
        同盘先试硬链接（不占额外空间），跨盘链不了就复制。
        两条路都用「目标已存在就换名字」的原子方式建文件，绝不覆盖已有的东西。

        送不进去不算上传失败：网页上还能下载，不该因为下载目录写不了就把文件退回去。
        """
        if self.inbox is None:
            return None
        src = self.file_path(meta)
        base = inbox_name(meta.name)
        try:
            self.inbox.mkdir(parents=True, exist_ok=True)
            for _ in range(5):
                dest = unique_path(self.inbox, base)
                try:
                    os.link(src, dest)
                    return dest
                except FileExistsError:
                    continue  # 这一瞬间名字被别人占了，换一个重来
                except OSError:
                    break  # 跨盘，或者文件系统不支持硬链接 —— 改成复制
            for _ in range(5):
                dest = unique_path(self.inbox, base)
                try:
                    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
                except FileExistsError:
                    continue
                with os.fdopen(fd, "wb") as out, src.open("rb") as fp:
                    shutil.copyfileobj(fp, out, CHUNK_BYTES)
                return dest
        except OSError as exc:
            print(f"  收件箱写入失败（{exc}）—— 文件还在网页上，可以手动下载")
        return None

    def remove_file(self, file_id: str, client_id: str) -> dict | None:
        with self._lock:
            meta = self.file_meta(file_id)
            if meta is None:
                return None
            self._drop(meta)
            self.updated_by = client_id
            self.updated_at = time.time()
            self.rev += 1
            return self.snapshot()

    def clear_files(self, client_id: str) -> dict:
        with self._lock:
            for meta in list(self._files):
                self._drop(meta)
            self.updated_by = client_id
            self.updated_at = time.time()
            self.rev += 1
            return self.snapshot()

    def _evict(self) -> None:
        """超量就把最早的连文件一起扔掉，不然 .data 会一直长。调用方须持锁。

        个数和总量两条线分开砍；刚传上来的那个永远留着，
        不然传一个超过总量上限的大文件会被自己挤掉，看起来像传失败了。
        """
        while len(self._files) > MAX_FILES:
            self._drop(self._files[0])
        while len(self._files) > 1 and sum(m.size for m in self._files) > MAX_TOTAL_BYTES:
            self._drop(self._files[0])

    def _drop(self, meta: FileMeta) -> None:
        """从列表里摘掉并删文件。调用方须持锁。"""
        self._files.remove(meta)
        self.file_path(meta).unlink(missing_ok=True)


# ─── SSE 广播 ───────────────────────────────────────────────


class Broadcaster:
    """每个 SSE 连接一个队列，白板一变就往所有队列里塞快照。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: set[queue.Queue] = set()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=8)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, payload: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # 这个客户端卡住了，丢掉它积压的最老一条再塞 —— 快照是全量的，丢中间帧无所谓
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except (queue.Empty, queue.Full):
                    pass

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._subs)


# ─── HTTP ───────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LanClipboard/1.1"

    # 由 create_server 注入
    board: Board
    bus: Broadcaster
    verbose: bool = False

    # 回包 ───────────────────────────────────────────────────

    def _send_json(self, payload: dict, status: int = 200, *, close: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        if close:
            # body 还没读完就回包了，这条连接上剩下的字节会被当成下一个请求，必须断掉
            self.close_connection = True

    def _send_error_json(self, status: int, message: str, *, close: bool = False) -> None:
        self._send_json({"ok": False, "error": message}, status, close=close)

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # 路由 ───────────────────────────────────────────────────

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_page()
        elif path == "/api/state":
            self._send_json(self.board.snapshot())
        elif path == "/api/events":
            self._serve_events()
        elif path.startswith("/api/files/"):
            self._serve_file(path.rsplit("/", 1)[-1])
        elif path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send_error_json(404, "没有这个地址")

    def do_PUT(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/text":
            self._handle_put_text()
        else:
            self._send_error_json(404, "没有这个地址")

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/files":
            self._handle_post_file()
        else:
            self._send_error_json(404, "没有这个地址")

    def do_DELETE(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        client_id = self._query_param("clientId")
        if path == "/api/files":
            self._publish(self.board.clear_files(client_id))
            self._send_json({"ok": True})
        elif path.startswith("/api/files/"):
            state = self.board.remove_file(path.rsplit("/", 1)[-1], client_id)
            if state is None:
                self._send_error_json(404, "这个文件已经不在了")
                return
            self._publish(state)
            self._send_json({"ok": True})
        else:
            self._send_error_json(404, "没有这个地址")

    # 具体处理 ───────────────────────────────────────────────

    def _serve_page(self) -> None:
        page = STATIC_DIR / "index.html"
        if not page.exists():
            self._send_error_json(500, "页面文件丢了：static/index.html")
            return
        self._send_bytes(page.read_bytes(), "text/html; charset=utf-8")

    def _serve_file(self, file_id: str) -> None:
        meta = self.board.file_meta(file_id)
        if meta is None:
            self._send_error_json(404, "这个文件已经不在了")
            return
        path = self.board.file_path(meta)
        if not path.exists():
            self._send_error_json(404, "这个文件已经不在了")
            return

        inline = meta.content_type in INLINE_TYPES
        quoted = urllib.parse.quote(meta.name)
        self.send_response(200)
        self.send_header("Content-Type", meta.content_type if inline else "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Disposition",
            f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quoted}",
        )
        self.end_headers()
        try:
            with path.open("rb") as fp:
                while True:
                    chunk = fp.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 对面下到一半取消了，正常
            self.close_connection = True

    def _handle_put_text(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        text = payload.get("text")
        if not isinstance(text, str):
            self._send_error_json(400, "text 必须是字符串")
            return
        if len(text) > MAX_TEXT_CHARS:
            self._send_error_json(413, f"文字太长了，上限 {MAX_TEXT_CHARS} 字")
            return
        state = self.board.set_text(text, str(payload.get("clientId") or ""))
        self._publish(state)
        self._send_json({"ok": True, "rev": state["rev"]})

    def _handle_post_file(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_error_json(400, "没收到文件内容")
            return
        if length > MAX_FILE_BYTES:
            # 别把这几百兆读完再拒绝 —— 吞掉开头一截就回错，剩下的断连甩掉
            drained = self._drain(length)
            self._send_error_json(
                413, f"文件太大了，单个上限 {MAX_FILE_BYTES // 1024 // 1024} MB", close=not drained
            )
            return

        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        name = safe_name(self.headers.get("X-File-Name") or "")
        file_id, path = self.board.new_upload()

        written = 0
        try:
            with path.open("wb") as fp:
                while written < length:
                    chunk = self.rfile.read(min(CHUNK_BYTES, length - written))
                    if not chunk:
                        break
                    fp.write(chunk)
                    written += len(chunk)
        except OSError:
            path.unlink(missing_ok=True)
            self._send_error_json(500, "写入磁盘失败，看一下磁盘还有没有空间", close=True)
            return

        if written != length:
            # 半截的文件留着没意义，而且登记进去别人下下来也是坏的
            path.unlink(missing_ok=True)
            self._send_error_json(400, "文件没传完", close=True)
            return

        state, meta = self.board.commit_upload(
            file_id, name, content_type, written, self._query_param("clientId")
        )
        # 先送进收件箱再广播 —— 网页上看见这个文件时，下载目录里那份已经就位了
        self.board.deliver_to_inbox(meta)
        self._publish(state)
        self._send_json({"ok": True, "id": meta.id, "rev": state["rev"]})

    def _serve_events(self) -> None:
        q = self.bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self._write_event(self.board.snapshot())
            while True:
                try:
                    payload = q.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._write_event(payload)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass  # 手机息屏、切后台、关页面都是这么断的，正常
        finally:
            self.bus.unsubscribe(q)

    def _write_event(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
        self.wfile.flush()

    # 小工具 ─────────────────────────────────────────────────

    def _publish(self, state: dict) -> None:
        self.bus.publish(state)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_error_json(400, "请求体是空的")
            return None
        if length > MAX_TEXT_CHARS * 4 + 1024:  # UTF-8 最多 4 字节一个字，再给点余量
            self._send_error_json(413, "请求体太大了", close=True)
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "请求体不是合法 JSON")
            return None

    def _drain(self, length: int) -> bool:
        """把不打算收的请求体吞掉，返回是否吞干净了（没吞干净的连接只能断掉）。"""
        if length > DRAIN_LIMIT:
            return False
        left = length
        try:
            while left > 0:
                chunk = self.rfile.read(min(CHUNK_BYTES, left))
                if not chunk:
                    return False
                left -= len(chunk)
        except OSError:
            return False
        return True

    def _query_param(self, key: str) -> str:
        query = urllib.parse.urlparse(self.path).query
        return urllib.parse.parse_qs(query).get(key, [""])[0]

    def log_message(self, fmt, *args):  # noqa: A003
        if self.verbose:
            super().log_message(fmt, *args)


def create_server(host: str, port: int, data_dir: Path, verbose: bool = False, inbox: Path | None = None):
    """装好一台服务器，返回 (httpd, board, bus) 方便测试直接拿来用。"""
    board = Board(data_dir, inbox)
    bus = Broadcaster()
    handler = type("BoundHandler", (Handler,), {"board": board, "bus": bus, "verbose": verbose})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd, board, bus


def lan_ip() -> str:
    """本机在局域网里的地址。

    连一个外网地址只是为了让系统挑出默认出口网卡，UDP 不握手，不会真发包。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main() -> None:
    global MAX_FILE_BYTES

    parser = argparse.ArgumentParser(description="局域网剪贴板中转站")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0", help="默认对局域网开放；只想自己用就填 127.0.0.1")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="文件落盘目录")
    parser.add_argument(
        "--max-file-mb",
        type=int,
        default=MAX_FILE_BYTES // 1024 // 1024,
        help="单个文件上限（MB），默认 200",
    )
    parser.add_argument(
        "--inbox",
        default=str(DEFAULT_INBOX),
        help="收到的文件照原名再落一份到这个目录，默认是系统下载目录",
    )
    parser.add_argument("--no-inbox", action="store_true", help="不要往收件箱落，只留在网页上")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="打印每条请求日志")
    args = parser.parse_args()

    MAX_FILE_BYTES = max(1, args.max_file_mb) * 1024 * 1024
    inbox = None if args.no_inbox else Path(args.inbox).expanduser()

    httpd, _, _ = create_server(args.host, args.port, Path(args.data_dir), args.verbose, inbox)
    local_url = f"http://127.0.0.1:{args.port}"

    print("局域网剪贴板已启动")
    print(f"  本机   {local_url}")
    if args.host == "0.0.0.0":
        print(f"  手机   http://{lan_ip()}:{args.port}     ← 手机连同一个 WiFi 后打开这个")
        print("")
        print("  注意：没有口令。同一 WiFi 下任何人打开这个地址都能看到你贴的内容、")
        print("        下载你传的文件，也能改掉它们。别往里放生产密码和客户数据 ——")
        print("        只想自己用就加 --host 127.0.0.1。")
    print("")
    print(f"  文件：单个上限 {MAX_FILE_BYTES // 1024 // 1024} MB，保留最近 {MAX_FILES} 个")
    if inbox is not None:
        print(f"  收件箱：传上来的文件照原名直接落到 {inbox}，不用再下载一次")
    print("")
    print("按 Ctrl+C 停止。")

    if not args.no_browser:
        webbrowser.open(local_url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
