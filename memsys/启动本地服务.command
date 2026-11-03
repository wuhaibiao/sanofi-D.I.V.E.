#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print '缺少项目虚拟环境，请先安装本地依赖。'
  read 'reply?按 Enter 退出。'
  exit 1
fi
.venv/bin/python - <<'PY'
import getpass
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.request

root = Path.cwd()
path = root / 'deploy/local.py'
spec = importlib.util.spec_from_file_location('local_deploy', path)
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
env_path = root / '.env'
try:
    configured = deploy.dotenv(env_path)
    if not (configured.get('OPENCODE_API_KEY') or os.environ.get('OPENCODE_API_KEY')):
        key = getpass.getpass('请输入 OpenCode API key（输入不显示）：').strip()
        if not key:
            raise ValueError('密钥为空，服务未启动。')
        lines = env_path.read_text().splitlines() if env_path.exists() else []
        lines = [line for line in lines if not line.strip().removeprefix('export ').startswith('OPENCODE_API_KEY=')]
        lines.append('OPENCODE_API_KEY=' + shlex.quote(key))
        temporary = env_path.with_name('.env.setup-tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write('\n'.join(lines) + '\n')
        temporary.replace(env_path)
    binary = deploy.STATE / 'bin/qdrant'
    background_python = deploy.STATE / '.venv/bin/python'
    if background_python.exists():
        uv = __import__('shutil').which('uv') or str(Path.home() / '.local/bin/uv')
        subprocess.run([uv, 'pip', 'install', '--python', str(background_python),
                        '--no-deps', '--reinstall-package', 'memsys', str(root)], check=True)
    subprocess.run([sys.executable, str(path), 'prepare', '--qdrant-binary', str(binary)], check=True)
    subprocess.run([sys.executable, str(path), 'start'], check=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(60):
        try:
            with opener.open('http://127.0.0.1:8848/healthz', timeout=2):
                break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError('服务未就绪，请查看 ~/Library/Application Support/memsys/logs/。')
    subprocess.run([sys.executable, str(path), 'status'], check=True)
    subprocess.run(['open', 'http://127.0.0.1:8848'], check=True)
    print('本地服务已启动，关闭此窗口不会停止后台服务。')
except (Exception, KeyboardInterrupt) as exc:
    print('启动未完成：', exc)
    raise SystemExit(1)
PY
read 'reply?按 Enter 关闭窗口。'
