"""桌面子进程入口；随机本机端口和进程令牌，不启动多 worker。"""

import asyncio
import json
import os
import socket

import uvicorn

from reading_agent.interfaces.api import create_app


async def serve():
    token = os.environ["AGENT_APP_TOKEN"]
    app = create_app(token=token)
    sock = socket.socket()
    sock.bind(("127.0.0.1", int(os.environ.get("AGENT_APP_PORT", "0"))))
    sock.listen(128)
    sock.setblocking(False)
    print(json.dumps({"port": sock.getsockname()[1]}), flush=True)
    config = uvicorn.Config(app, access_log=False, log_level="warning", lifespan="on")
    try:
        server = uvicorn.Server(config)
        app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
        await server.serve(sockets=[sock])
    finally:
        sock.close()


def main():
    asyncio.run(serve())


if __name__ == "__main__":
    main()
