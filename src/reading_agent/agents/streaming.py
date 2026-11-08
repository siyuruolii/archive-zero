"""有界异步事件迭代，以及只供预览的JSON answer增量解码。"""

import asyncio
import json
from dataclasses import asdict


class StreamBufferError(RuntimeError):
    pass


async def event_stream(operation, *, cancel, observer=None, max_buffer=256):
    """消费方退出即关闭生产任务；队列满明确失败，不静默丢事件。"""
    from .loop import Event

    if not 1 <= max_buffer <= 4096:
        raise ValueError("invalid stream buffer")
    queue = asyncio.Queue(maxsize=max_buffer)

    def emit(event):
        if queue.full():
            raise StreamBufferError("agent stream consumer is too slow")
        if observer is not None:
            observer(event)
        queue.put_nowait(event)

    producer = asyncio.create_task(operation(emit))
    pending = None
    try:
        while True:
            if not queue.empty():
                yield queue.get_nowait()
            elif producer.done():
                report = producer.result()
                yield Event("result", {"report": asdict(report)})
                return
            else:
                pending = asyncio.create_task(queue.get())
                await asyncio.wait({pending, producer}, return_when=asyncio.FIRST_COMPLETED)
                if pending.done():
                    yield pending.result()
                else:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                pending = None
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if not producer.done():
            cancel.set()
            producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)


class AnswerDecoder:
    """识别根对象指定的正文字符串，不把部分 JSON 作为合法最终结果。"""

    def __init__(self, field="answer"):
        self.field = field
        self.depth = 0
        self.in_string = False
        self.escaped = False
        self.expect_key = False
        self.is_key = False
        self.key = None
        self.raw = ""
        self.target_raw = ""
        self.target = False
        self.finished = False
        self.emitted = ""

    def feed(self, fragment):
        for char in fragment:
            if self.in_string:
                if char == '"' and not self.escaped:
                    if self.is_key:
                        try:
                            self.key = json.loads('"' + self.raw + '"')
                        except ValueError:
                            self.key = None
                    if self.target:
                        self.finished = True
                    self.in_string = False
                    self.target = False
                else:
                    self.raw += char
                    if self.target:
                        self.target_raw = self.raw
                    if self.escaped:
                        self.escaped = False
                    elif char == "\\":
                        self.escaped = True
            elif char in "{[":
                self.depth += 1
                if self.depth == 1:
                    self.expect_key = char == "{"
            elif char in "}]":
                self.depth -= 1
            elif char == "," and self.depth == 1:
                self.expect_key = True
            elif char == '"':
                self.in_string = True
                self.is_key = self.depth == 1 and self.expect_key
                self.target = (
                    self.depth == 1
                    and not self.is_key
                    and self.key == self.field
                    and not self.finished
                )
                self.expect_key = False
                self.raw = ""
                self.escaped = False
        raw = self.target_raw
        safe = []
        i = 0
        while i < len(raw):
            size = 1
            if raw[i] == "\\":
                if i + 1 == len(raw):
                    break
                size = 6 if raw[i + 1] == "u" else 2
                if i + size > len(raw):
                    break
            safe.append(raw[i : i + size])
            i += size
        try:
            decoded = json.loads('"' + "".join(safe) + '"')
            decoded.encode("utf-8")  # 等待完整代理对，不把半个emoji传给下游。
        except (ValueError, UnicodeError):
            return ""
        if not decoded.startswith(self.emitted):
            return ""
        delta = decoded[len(self.emitted) :]
        self.emitted = decoded
        return delta
