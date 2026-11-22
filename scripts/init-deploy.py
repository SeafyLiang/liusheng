"""从仓库根目录执行；创建私有配置，不覆盖现有配置或修改既有音乐。"""
import os
import secrets
from pathlib import Path


def initialize(root):
    target = root / '.env'
    uid = os.getuid() if hasattr(os, 'getuid') else 1000
    gid = os.getgid() if hasattr(os, 'getgid') else 1000
    if uid == 0:
        raise SystemExit('请以用于运行服务的普通用户执行，避免以 root 运行容器。')
    content = (
        f'LIUSHENG_UID={uid}\nLIUSHENG_GID={gid}\n'
        'BIND_ADDRESS=127.0.0.1\nHTTP_PORT=38661\nTZ=Asia/Shanghai\n'
        'DATA_PATH=./data\nMUSIC_PATH=./music\nINBOX_PATH=./inbox\n'
        f'LX_RUNNER_TOKEN={secrets.token_hex(32)}\nSPOTIFY_HTTPS_PROXY=\n'
    )
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit('.env 已存在，未覆盖。请按部署指南检查配置和目录权限。') from None
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    for name in ('data', 'music', 'inbox'):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    print('已创建 .env 和默认数据目录；令牌未输出。下一步：docker compose config --quiet')


if __name__ == '__main__':
    initialize(Path(__file__).resolve().parents[1])
