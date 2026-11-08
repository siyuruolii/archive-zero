"""受信启动器：等待父进程绑定进程组/Job 后才读取并执行命令。"""

import json
import subprocess
import sys


def main():
    request = json.loads(sys.stdin.buffer.readline())
    result = subprocess.run(request["argv"], stdin=subprocess.DEVNULL, check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
