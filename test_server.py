"""局域网剪贴板服务端测试。

起一台真的 server 在随机端口上打，不 mock —— 这东西的坑全在 HTTP 这一层
（SSE 流、文件字节、路径穿越、超限断连），mock 掉就什么都测不到了。

跑：python -m pytest test_server.py -v
或：python test_server.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import server as srv

# 1x1 的 PNG，够当测试素材了
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "05570c2a0000000049454e44ae426082"
)


class ClipboardServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(tempfile.mkdtemp(prefix="clipboard-test-"))
        cls.inbox = Path(tempfile.mkdtemp(prefix="clipboard-inbox-"))
        cls.httpd, cls.board, cls.bus = srv.create_server("127.0.0.1", 0, cls.data_dir, inbox=cls.inbox)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)
        shutil.rmtree(cls.inbox, ignore_errors=True)

    def clear_inbox(self):
        for p in self.inbox.iterdir():
            p.unlink()

    # 请求小工具 ─────────────────────────────────────────────

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, method: str, path: str, data: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(self.url(path), data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def get_json(self, path: str) -> dict:
        status, body, _ = self.request("GET", path)
        self.assertEqual(200, status)
        return json.loads(body)

    def put_text(self, text: str, client_id: str = "tester"):
        payload = json.dumps({"text": text, "clientId": client_id}).encode("utf-8")
        return self.request("PUT", "/api/text", payload, {"Content-Type": "application/json"})

    def post_file(self, raw: bytes = TINY_PNG, content_type: str = "image/png", name: str = "shot.png"):
        return self.request(
            "POST",
            "/api/files?clientId=tester",
            raw,
            {"Content-Type": content_type, "X-File-Name": urllib.parse.quote(name)},
        )

    def post_file_id(self, **kwargs) -> str:
        status, body, _ = self.post_file(**kwargs)
        self.assertEqual(200, status, body)
        return json.loads(body)["id"]

    # 文字 ───────────────────────────────────────────────────

    def test_text_roundtrip_and_rev_increments(self):
        before = self.get_json("/api/state")["rev"]

        status, _, _ = self.put_text("eyJhbGciOiJIUzI1NiJ9", "phone")
        self.assertEqual(200, status)

        state = self.get_json("/api/state")
        self.assertEqual("eyJhbGciOiJIUzI1NiJ9", state["text"])
        self.assertEqual("phone", state["updatedBy"])
        self.assertEqual(before + 1, state["rev"])
        self.assertGreater(state["updatedAt"], 0)

    def test_text_keeps_unicode(self):
        self.put_text("中文 · 全角符号 · emoji 🙂")
        self.assertEqual("中文 · 全角符号 · emoji 🙂", self.get_json("/api/state")["text"])

    def test_text_too_long_rejected(self):
        status, body, _ = self.put_text("x" * (srv.MAX_TEXT_CHARS + 1))
        self.assertEqual(413, status)
        self.assertIn("上限", json.loads(body)["error"])

    def test_text_wrong_type_rejected(self):
        payload = json.dumps({"text": 123, "clientId": "t"}).encode("utf-8")
        status, _, _ = self.request("PUT", "/api/text", payload, {"Content-Type": "application/json"})
        self.assertEqual(400, status)

    def test_broken_json_rejected(self):
        status, _, _ = self.request("PUT", "/api/text", b"{not json", {"Content-Type": "application/json"})
        self.assertEqual(400, status)

    # 文件 ───────────────────────────────────────────────────

    def test_image_upload_download_roundtrip(self):
        self.board.clear_files("t")
        file_id = self.post_file_id()

        state = self.get_json("/api/state")
        self.assertEqual(1, len(state["files"]))
        self.assertEqual("shot.png", state["files"][0]["name"])
        self.assertEqual(len(TINY_PNG), state["files"][0]["size"])
        # 驼峰，和 updatedBy / updatedAt 一个风格 —— 前端靠它判断能不能当图片预览，
        # 发成 content_type 的话页面读到 undefined，所有图片都会退化成普通文件卡片
        self.assertEqual("image/png", state["files"][0]["contentType"])
        self.assertNotIn("content_type", state["files"][0])

        status, raw, headers = self.request("GET", f"/api/files/{file_id}")
        self.assertEqual(200, status)
        self.assertEqual(TINY_PNG, raw)
        # 图片原样回去，浏览器才能点开直接看
        self.assertEqual("image/png", headers["Content-Type"])
        self.assertTrue(headers["Content-Disposition"].startswith("inline"))

    def test_any_file_type_accepted(self):
        """以前只收图片，现在什么都收 —— apk / zip / 没有 Content-Type 的都行。"""
        self.board.clear_files("t")
        blob = bytes(range(256)) * 40  # 带 0x00 和高位字节，能发现编码问题

        for ctype, name in [
            ("application/vnd.android.package-archive", "app-debug.apk"),
            ("application/zip", "logs.zip"),
            ("application/pdf", "合同 (最终版).pdf"),
            ("", "无类型文件"),
        ]:
            file_id = self.post_file_id(raw=blob, content_type=ctype, name=name)
            status, raw, headers = self.request("GET", f"/api/files/{file_id}")
            self.assertEqual(200, status)
            self.assertEqual(blob, raw, f"{name} 取回来的字节和传上去的不一样")
            # 非图片一律当附件下发，不让浏览器拿它当页面渲染
            self.assertEqual("application/octet-stream", headers["Content-Type"])
            self.assertTrue(headers["Content-Disposition"].startswith("attachment"))

        names = [f["name"] for f in self.get_json("/api/state")["files"]]
        self.assertIn("合同 (最终版).pdf", names)

    def test_html_and_svg_never_served_inline(self):
        """传个 .html / .svg 上来，点开不能变成一个跑脚本的同源页面。"""
        for ctype, name in [("text/html", "x.html"), ("image/svg+xml", "x.svg")]:
            file_id = self.post_file_id(raw=b"<svg onload=alert(1)>", content_type=ctype, name=name)
            _, _, headers = self.request("GET", f"/api/files/{file_id}")
            self.assertEqual("application/octet-stream", headers["Content-Type"])
            self.assertTrue(headers["Content-Disposition"].startswith("attachment"))

    def test_oversize_file_rejected(self):
        original = srv.MAX_FILE_BYTES
        srv.MAX_FILE_BYTES = 1024  # 不真传 200 MB，把上限调小来测
        try:
            status, body, _ = self.post_file(raw=b"x" * 2048, content_type="application/zip", name="big.zip")
            self.assertEqual(413, status)
            self.assertIn("上限", json.loads(body)["error"])
        finally:
            srv.MAX_FILE_BYTES = original

        # 拒绝之后连接必须还能正常做下一单，不能被没读完的 body 带歪
        self.assertEqual(200, self.request("GET", "/api/state")[0])

    def test_empty_upload_rejected(self):
        status, _, _ = self.post_file(raw=b"", content_type="application/zip", name="empty.zip")
        self.assertEqual(400, status)

    def test_filename_is_stripped_of_path(self):
        """文件名只当展示用，带路径的也不能影响落盘。"""
        self.board.clear_files("t")
        file_id = self.post_file_id(name="../../../etc/passwd")
        meta = self.board.file_meta(file_id)
        self.assertEqual("passwd", meta.name)
        self.assertEqual(self.data_dir / f"{file_id}.bin", self.board.file_path(meta))
        self.assertTrue(self.board.file_path(meta).exists())

    # 收件箱 ─────────────────────────────────────────────────

    def test_inbox_gets_file_under_original_name(self):
        """传上来就该直接躺在下载目录里，名字是原来那个，不用再下载一次。"""
        self.board.clear_files("t")
        self.clear_inbox()
        self.post_file_id(name="合同 (最终版).pdf", content_type="application/pdf")

        landed = self.inbox / "合同 (最终版).pdf"
        self.assertTrue(landed.exists())
        self.assertEqual(TINY_PNG, landed.read_bytes())

    def test_state_reports_inbox_without_leaking_path(self):
        """网页靠这个显示「自动存进电脑的 XXX」。
        只给目录名 —— 完整路径带着本机用户名，没必要发给局域网上每一台设备。"""
        inbox = self.get_json("/api/state")["inbox"]
        self.assertTrue(inbox["enabled"])
        self.assertEqual(self.inbox.name, inbox["name"])
        self.assertNotIn(str(self.inbox.parent), json.dumps(inbox))

    def test_inbox_never_overwrites_existing_file(self):
        """同名的往后排。下载目录里已有的文件绝不能被悄悄替掉。"""
        self.board.clear_files("t")
        self.clear_inbox()
        mine = self.inbox / "shot.png"
        mine.write_bytes(b"my own file")

        self.post_file_id(name="shot.png")
        self.post_file_id(name="shot.png")

        self.assertEqual(b"my own file", mine.read_bytes())
        self.assertEqual(TINY_PNG, (self.inbox / "shot-2.png").read_bytes())
        self.assertEqual(TINY_PNG, (self.inbox / "shot-3.png").read_bytes())

    def test_inbox_log_records_every_file(self):
        """网页列表重启就空了，这份流水账是唯一长期的收件凭据。"""
        self.board.clear_files("t")
        self.clear_inbox()
        self.post_file_id(name="图纸.pdf", content_type="application/pdf")
        self.post_file_id(name="录屏.mp4", content_type="video/mp4")

        lines = (self.inbox / srv.INBOX_LOG_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, sum(1 for ln in lines if ln.startswith("#")), "表头只该写一次")
        body = [ln for ln in lines if not ln.startswith("#")]
        self.assertEqual(2, len(body))
        self.assertIn(str(self.inbox / "图纸.pdf"), body[0])
        self.assertIn(str(self.inbox / "录屏.mp4"), body[1])
        self.assertRegex(body[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s")

    def test_inbox_log_notes_the_original_name_when_renamed(self):
        """落盘名被改过（重名排号、非法字符换掉）就得把原名记下来，否则对不上账。"""
        self.board.clear_files("t")
        self.clear_inbox()
        self.post_file_id(name="报表.xlsx")
        self.post_file_id(name="报表.xlsx")

        body = [
            ln
            for ln in (self.inbox / srv.INBOX_LOG_NAME).read_text(encoding="utf-8").splitlines()
            if not ln.startswith("#")
        ]
        self.assertNotIn("原名", body[0])
        self.assertIn("报表-2.xlsx", body[1])
        self.assertIn("(原名 报表.xlsx)", body[1])

    def test_inbox_copy_survives_eviction(self):
        """网页上的列表会被挤掉，下载目录里那份得留着 —— 不然等于白传。"""
        self.board.clear_files("t")
        self.clear_inbox()
        first = self.post_file_id(name="early.png")
        for i in range(srv.MAX_FILES + 2):
            self.post_file_id(name=f"later-{i}.png")

        self.assertIsNone(self.board.file_meta(first))
        self.assertFalse((self.data_dir / f"{first}.bin").exists())
        self.assertTrue((self.inbox / "early.png").exists())

    def test_inbox_does_not_escape_the_folder(self):
        """带路径、带设备名的文件名都不许写到收件箱外面去。"""
        self.board.clear_files("t")
        self.clear_inbox()
        self.post_file_id(name="../../../pwned.png")
        self.post_file_id(name="CON.txt", content_type="text/plain")

        landed = sorted(p.name for p in self.inbox.iterdir() if p.name != srv.INBOX_LOG_NAME)
        self.assertEqual(["_CON.txt", "pwned.png"], landed)
        self.assertFalse((self.inbox.parent / "pwned.png").exists())

    def test_newest_first_and_eviction(self):
        self.board.clear_files("t")
        ids = [self.post_file_id(name=f"{i}.png") for i in range(srv.MAX_FILES + 3)]

        state = self.get_json("/api/state")
        self.assertEqual(srv.MAX_FILES, len(state["files"]))
        # 列表按新到旧
        self.assertEqual(ids[-1], state["files"][0]["id"])
        # 被挤掉的那几个，元数据和磁盘文件都该没了
        for gone in ids[:3]:
            self.assertEqual(404, self.request("GET", f"/api/files/{gone}")[0])
        self.assertEqual(srv.MAX_FILES, len(list(self.data_dir.glob("*.bin"))))

    def test_total_size_eviction(self):
        """个数没超但磁盘总量超了，也要把最早的挤掉。"""
        self.board.clear_files("t")
        original = srv.MAX_TOTAL_BYTES
        srv.MAX_TOTAL_BYTES = 3000
        try:
            blob = b"x" * 1000
            ids = [self.post_file_id(raw=blob, content_type="application/zip", name=f"{i}.zip") for i in range(5)]
        finally:
            srv.MAX_TOTAL_BYTES = original

        state = self.get_json("/api/state")
        self.assertEqual(3, len(state["files"]))
        self.assertEqual(ids[-1], state["files"][0]["id"])
        self.assertEqual(404, self.request("GET", f"/api/files/{ids[0]}")[0])

    def test_oversize_single_file_still_kept(self):
        """单个文件就超过总量上限时别把它自己挤没了，不然看起来像传失败。"""
        self.board.clear_files("t")
        original = srv.MAX_TOTAL_BYTES
        srv.MAX_TOTAL_BYTES = 10
        try:
            file_id = self.post_file_id(raw=b"x" * 500, content_type="application/zip", name="big.zip")
        finally:
            srv.MAX_TOTAL_BYTES = original
        self.assertEqual(200, self.request("GET", f"/api/files/{file_id}")[0])

    def test_delete_file_removes_disk_copy(self):
        self.board.clear_files("t")
        file_id = self.post_file_id()
        path = self.data_dir / f"{file_id}.bin"
        self.assertTrue(path.exists())

        status, _, _ = self.request("DELETE", f"/api/files/{file_id}?clientId=t")
        self.assertEqual(200, status)
        self.assertFalse(path.exists())
        self.assertEqual(0, len(self.get_json("/api/state")["files"]))
        self.assertEqual(404, self.request("DELETE", f"/api/files/{file_id}")[0])

    def test_clear_all_files(self):
        self.board.clear_files("t")
        self.post_file_id()
        self.post_file_id(raw=b"abc", content_type="text/plain", name="a.txt")
        self.assertEqual(200, self.request("DELETE", "/api/files?clientId=t")[0])
        self.assertEqual(0, len(self.get_json("/api/state")["files"]))
        self.assertEqual(0, len(list(self.data_dir.glob("*.bin"))))

    def test_file_id_must_be_uuid_hex(self):
        """路径穿越：id 直接当文件名拼路径的话，这几个请求能读到任意文件。"""
        for evil in ["..%2f..%2fserver.py", "....//server.py", "abc", "", "%27%3BDROP"]:
            status, _, _ = self.request("GET", f"/api/files/{evil}")
            self.assertIn(status, (400, 404), f"{evil!r} 应该被挡掉，实际 {status}")

        # 格式合法但不存在的 id 也只能是 404，不能漏成 500
        self.assertEqual(404, self.request("GET", "/api/files/" + "0" * 32)[0])

    def test_state_exposes_limits(self):
        """前端靠这几个数做上传前预检，少了就会传完才被拒。"""
        limits = self.get_json("/api/state")["limits"]
        self.assertEqual(srv.MAX_FILE_BYTES, limits["maxFileBytes"])
        self.assertEqual(srv.MAX_FILES, limits["maxFiles"])
        self.assertEqual(srv.MAX_TEXT_CHARS, limits["maxTextChars"])

    # 实时通道 ───────────────────────────────────────────────

    def test_sse_sends_snapshot_then_updates(self):
        req = urllib.request.Request(self.url("/api/events"))
        stream = urllib.request.urlopen(req, timeout=5)
        try:
            self.assertTrue(stream.headers["Content-Type"].startswith("text/event-stream"))

            first = self._read_event(stream)
            self.assertIn("rev", first)  # 一连上就先给一份当前快照

            self.put_text("从另一台设备发来的", "desktop")
            pushed = self._read_event(stream)
            self.assertEqual("从另一台设备发来的", pushed["text"])
            self.assertEqual("desktop", pushed["updatedBy"])
            self.assertEqual(first["rev"] + 1, pushed["rev"])
        finally:
            stream.close()

    def test_sse_pushes_on_file_upload(self):
        stream = urllib.request.urlopen(urllib.request.Request(self.url("/api/events")), timeout=5)
        try:
            self._read_event(stream)
            self.post_file_id(raw=b"hello", content_type="text/plain", name="note.txt")
            pushed = self._read_event(stream)
            self.assertEqual("note.txt", pushed["files"][0]["name"])
        finally:
            stream.close()

    def test_sse_unsubscribes_on_disconnect(self):
        self._wait_subscribers(0)  # 别的测试可能还挂着连接，先催干净再数

        stream = urllib.request.urlopen(urllib.request.Request(self.url("/api/events")), timeout=5)
        self._read_event(stream)
        self.assertEqual(1, self.bus.count)

        stream.close()
        self._wait_subscribers(0)
        self.assertEqual(0, self.bus.count, "客户端断开后订阅没被回收，连多了会泄漏线程")

    def _wait_subscribers(self, target: int) -> None:
        """服务端要等下一次写失败才察觉断开，推几条过去催它。"""
        for _ in range(40):
            if self.bus.count == target:
                return
            self.put_text("poke")
            time.sleep(0.02)

    @staticmethod
    def _read_event(stream) -> dict:
        """从 SSE 流里读出下一条 data:，跳过心跳行。"""
        while True:
            line = stream.readline().decode("utf-8")
            if not line:
                raise AssertionError("流断了，没读到事件")
            if line.startswith("data: "):
                return json.loads(line[6:])

    # 路由 ───────────────────────────────────────────────────

    def test_page_served(self):
        status, body, headers = self.request("GET", "/")
        self.assertEqual(200, status)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"EventSource", body)

    def test_unknown_route_404(self):
        self.assertEqual(404, self.request("GET", "/api/nope")[0])
        self.assertEqual(404, self.request("PUT", "/api/nope", b"{}")[0])
        self.assertEqual(404, self.request("POST", "/api/images", b"x")[0])


class SafeNameTest(unittest.TestCase):
    def test_keeps_readable_names(self):
        self.assertEqual("合同 (最终版).pdf", srv.safe_name("%E5%90%88%E5%90%8C%20(%E6%9C%80%E7%BB%88%E7%89%88).pdf"))
        self.assertEqual("app-debug.apk", srv.safe_name("app-debug.apk"))

    def test_strips_path_and_control_chars(self):
        self.assertEqual("passwd", srv.safe_name("../../etc/passwd"))
        self.assertEqual("x.txt", srv.safe_name(r"C:\Users\me\x.txt"))
        self.assertEqual("ab.txt", srv.safe_name("a\r\nb.txt"))

    def test_falls_back_when_empty(self):
        self.assertEqual("文件", srv.safe_name(""))
        self.assertEqual("文件", srv.safe_name("   "))

    def test_caps_length(self):
        self.assertEqual(120, len(srv.safe_name("n" * 500)))


class InboxNameTest(unittest.TestCase):
    """收件箱是唯一拿原始文件名往文件系统上写的地方，这里兜不住就出事了。"""

    def test_keeps_normal_names(self):
        self.assertEqual("app-debug.apk", srv.inbox_name("app-debug.apk"))
        self.assertEqual("合同 (最终版).pdf", srv.inbox_name("合同 (最终版).pdf"))

    def test_strips_path_and_illegal_chars(self):
        self.assertEqual("passwd", srv.inbox_name("../../etc/passwd"))
        self.assertEqual("a_b.txt", srv.inbox_name("a:b.txt"))  # : 会被当成备用数据流
        self.assertEqual("q_.log", srv.inbox_name('q*.log'))

    def test_dodges_windows_device_names(self):
        self.assertEqual("_CON", srv.inbox_name("CON"))
        self.assertEqual("_com1.log", srv.inbox_name("com1.log"))
        self.assertEqual("console.txt", srv.inbox_name("console.txt"))  # 只是开头像而已

    def test_trims_trailing_dots_and_spaces(self):
        self.assertEqual("note", srv.inbox_name("note. . "))
        self.assertEqual("文件", srv.inbox_name("..."))
        self.assertEqual("文件", srv.inbox_name(""))


class HumanSizeTest(unittest.TestCase):
    def test_reads_like_a_person_wrote_it(self):
        self.assertEqual("512 B", srv.human_size(512))
        self.assertEqual("2 KB", srv.human_size(2048))
        self.assertEqual("1.0 MB", srv.human_size(1048576))
        self.assertEqual("48.0 MB", srv.human_size(48 * 1048576))


class UniquePathTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="clipboard-unique-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)

    def test_free_name_used_as_is(self):
        self.assertEqual(self.folder / "a.txt", srv.unique_path(self.folder, "a.txt"))

    def test_counts_up_until_free(self):
        (self.folder / "a.txt").touch()
        (self.folder / "a-2.txt").touch()
        self.assertEqual(self.folder / "a-3.txt", srv.unique_path(self.folder, "a.txt"))

    def test_handles_names_without_extension(self):
        (self.folder / "README").touch()
        self.assertEqual(self.folder / "README-2", srv.unique_path(self.folder, "README"))

    def test_dotfile_keeps_its_whole_name(self):
        (self.folder / ".gitignore").touch()
        self.assertEqual(self.folder / ".gitignore-2", srv.unique_path(self.folder, ".gitignore"))


class LanIpTest(unittest.TestCase):
    def test_returns_an_ipv4(self):
        parts = srv.lan_ip().split(".")
        self.assertEqual(4, len(parts))
        self.assertTrue(all(p.isdigit() and 0 <= int(p) <= 255 for p in parts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
