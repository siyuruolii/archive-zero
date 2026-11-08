"""冻结发布入口；独立命令工作进程不能重新启动 HTTP 服务。"""

import sys

from reading_agent.infrastructure import shell_worker
from reading_agent.server import main

if __name__ == "__main__":
    if sys.argv[1:] == ["--shell-worker"]:
        shell_worker.main()
    elif sys.argv[1:]:
        raise SystemExit("Unsupported backend arguments")
    else:
        main()
