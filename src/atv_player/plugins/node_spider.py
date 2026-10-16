from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any


class NodeSpider:
    def __init__(
        self,
        plugin_path: Path,
        cache_dir: Path,
        plugin_id: int,
        *,
        node_executable: str | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        executable = node_executable or shutil.which("node")
        if not executable:
            raise ValueError("未找到 Node.js 运行环境，无法加载 JavaScript 插件")
        self.plugin_path = Path(plugin_path)
        self.cache_dir = Path(cache_dir)
        self.plugin_id = int(plugin_id)
        self.node_executable = executable
        self.timeout_seconds = timeout_seconds
        self._request_id = 0
        self._process: subprocess.Popen[str] | None = None
        self._stdout_lines: queue.Queue[str | None] = queue.Queue()
        self._stderr_lines: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()

    def init(self, extend: str = "") -> None:
        self._call("init", extend)

    def homeContent(self, filter):
        return self._call("home", filter)

    def categoryContent(self, tid, pg, filter, extend):
        return self._call("category", tid, pg, filter, extend)

    def detailContent(self, ids):
        first_id = ids[0] if ids else ""
        return self._call("detail", first_id)

    def searchContent(self, key, quick, pg=1, category=""):
        return self._call("search", key, quick, pg, category)

    def playerContent(self, flag, id, vipFlags):
        return self._call("play", flag, id, vipFlags)

    def danmakuContent(self, flag, id):
        return self._call("danmakuContent", flag, id)

    def supports_danmaku_content(self) -> bool:
        return bool(self._call("hasMethod", "danmakuContent"))

    def getName(self):
        try:
            return str(self._call("getName") or "")
        except RuntimeError:
            return ""

    def supports_search(self) -> bool:
        return bool(self._call("hasMethod", "search"))

    def destroy(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    def _bridge_path(self) -> Path:
        return Path(__file__).resolve().parent / "js_bridge" / "tvbox_spider_runner.mjs"

    def _ensure_process(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self.node_executable,
            str(self._bridge_path()),
            "--plugin",
            str(self.plugin_path),
            "--cache-dir",
            str(self.cache_dir),
            "--plugin-id",
            str(self.plugin_id),
        ]
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._stdout_lines = queue.Queue()
        self._stderr_lines = queue.Queue()
        threading.Thread(
            target=self._read_stdout, args=(self._process,), daemon=True
        ).start()
        threading.Thread(
            target=self._read_stderr, args=(self._process,), daemon=True
        ).start()
        return self._process

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in iter(process.stdout.readline, ""):
            self._stdout_lines.put(line)
        self._stdout_lines.put(None)

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in iter(process.stderr.readline, ""):
            stripped = line.rstrip()
            try:
                record = json.loads(stripped)
                if isinstance(record, dict) and "level" in record and "message" in record:
                    print(f"[JS:{record.get('level', 'info')}] {record['message']}")
                    continue
            except (json.JSONDecodeError, ValueError):
                pass
            self._stderr_lines.put(stripped)

    def _call(self, method: str, *args: Any):
        with self._lock:
            process = self._ensure_process()
            if process.stdin is None:
                raise RuntimeError("Node.js 插件进程不可写入")
            self._request_id += 1
            request_id = self._request_id
            process.stdin.write(
                json.dumps(
                    {"id": request_id, "method": method, "args": list(args)},
                    ensure_ascii=False,
                )
                + "\n"
            )
            process.stdin.flush()
            try:
                line = self._stdout_lines.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                self.destroy()
                raise TimeoutError(f"JavaScript 插件调用超时: {method}") from exc
            if line is None:
                error = self._latest_stderr() or f"JavaScript 插件进程已退出: {method}"
                raise RuntimeError(error)
            response = json.loads(line)
            if response.get("id") != request_id:
                raise RuntimeError(
                    f"JavaScript 插件响应 id 不匹配: {response.get('id')}"
                )
            if not response.get("ok"):
                raise RuntimeError(
                    str(response.get("error") or "JavaScript 插件调用失败")
                )
            return response.get("result")

    def _latest_stderr(self) -> str:
        messages: list[str] = []
        while True:
            try:
                messages.append(self._stderr_lines.get_nowait())
            except queue.Empty:
                break
        return "\n".join(message for message in messages if message).strip()
