"""麦当劳 MCP Server 客户端（零第三方依赖，仅用标准库）。

支持 Streamable HTTP 传输，自动处理 application/json 与 text/event-stream 两种响应。

Token 解析优先级：
1. 环境变量 MCD_MCP_TOKEN
2. --token 参数
3. ~/.workbuddy/mcp.json 中 mcd-mcp 的 Authorization 头
4. 项目根目录 mcd_token.txt
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

MCP_URL = "https://mcp.mcd.cn"


class McdError(RuntimeError):
    """MCP 调用失败。"""


def _from_workbuddy_config():
    path = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    auth = (cfg.get("mcpServers", {}).get("mcd-mcp", {}).get("headers", {}) or {}).get("Authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def _from_token_file():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcd_token.txt")
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
        return token or None
    except OSError:
        return None


def resolve_token(explicit=None):
    token = explicit or os.environ.get("MCD_MCP_TOKEN")
    if token:
        return token.strip()
    for loader in (_from_workbuddy_config, _from_token_file):
        token = loader()
        if token:
            return token
    raise McdError(
        "未找到 MCP Token。请设置环境变量 MCD_MCP_TOKEN，或把 token 写入项目根目录 mcd_token.txt。"
    )


def extract_json(text):
    """从 MCP 返回的说明性文本中抽取原始 JSON。

    McDonald's MCP 的工具结果通常是一段带字段说明的 Markdown，末尾附
    「## Original Response」+ 原始 JSON。这里优先按标记切分，失败则回退到
    扫描第一个 JSON 对象并用 raw_decode 宽松解析。
    """
    if not isinstance(text, str):
        return text
    marker = "## Original Response"
    if marker in text:
        tail = text.split(marker, 1)[1].strip()
        try:
            return json.loads(tail)
        except ValueError:
            pass
    for m in re.finditer(r'\{"success"', text):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[m.start():])
            return obj
        except ValueError:
            continue
    return None


class McdMCP:
    def __init__(self, token=None, url=MCP_URL, timeout=40):
        self.token = resolve_token(token)
        self.url = url
        self.timeout = timeout
        self._seq = 0

    def _rpc(self, method, params):
        self._seq += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._seq, "method": method, "params": params},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer " + self.token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            if e.code == 401:
                raise McdError("MCP Token 无效或已过期（401）。") from e
            if e.code == 429:
                raise McdError("触发限流（429），每分钟上限 600 次，请降低调用频率。") from e
            raise McdError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise McdError(f"网络不可达：{e.reason}") from e

        if "text/event-stream" in ctype or raw.lstrip().startswith("event:"):
            payload = None
            for line in raw.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk and chunk != "[DONE]":
                        payload = chunk
            raw = payload or ""
        data = json.loads(raw)
        if "error" in data:
            raise McdError(f"服务端错误：{json.dumps(data['error'], ensure_ascii=False)}")
        return data.get("result", {})

    def list_tools(self):
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, name, **arguments):
        """调用工具，返回 (解析后的 JSON, 原始文本)。"""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        text = "".join(c.get("text", "") for c in result.get("content", []))
        return extract_json(text), text


if __name__ == "__main__":
    client = McdMCP()
    tools = client.list_tools()
    print(f"已连接 mcd-mcp，共 {len(tools)} 个 Tool")
    for t in tools[:5]:
        print(" -", t["name"])
    if len(sys.argv) > 1:
        data, _ = client.call("now-time-info")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:400])
