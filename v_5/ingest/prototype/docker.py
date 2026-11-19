"""Run one loader step in the existing Docker image. No agent logic here."""
import json
import subprocess
import uuid
from pathlib import Path

from v_5.app.config import IMAGE


def run_code(code, files, workspace, turn_dir):
    """Execute a notebook loader script in Docker.

    When: The prototype notebook requests a code-execution turn.
    Inputs: code: Python source; files: uploads; workspace: work root; turn_dir: script/log
        destination.
    Returns: Execution dict with exit_code/output/truncated/full_log, or error/full_log on
        timeout.
    Flow: Mounts inputs read-only, limits resources and network access, saves full output
        while returning an excerpt, and always removes the container.
    """
    workspace, turn_dir = Path(workspace).resolve(), Path(turn_dir).resolve()
    turn_dir.mkdir(parents=True, exist_ok=True)
    script = turn_dir / 'code.py'
    script.write_text(code, encoding='utf-8')
    name = 'prototype-loader-' + uuid.uuid4().hex[:12]
    command = ['docker', 'run', '--name', name, '--rm', '--network', 'none',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--user', '65534:65534', '--pids-limit', '64', '--memory', '1g',
               '--memory-swap', '1g', '--cpus', '1', '--log-driver', 'none',
               '--tmpfs', '/tmp:rw,nosuid,size=192m', '--entrypoint', 'python']
    for file_id, file in files.items():
        command += ['--mount', f"type=bind,source={Path(file['path']).resolve()},target=/input/{file_id}{Path(file['path']).suffix},readonly"]
    command += ['--mount', f'type=bind,source={workspace},target=/work',
                IMAGE, '/work/' + script.relative_to(workspace).as_posix()]
    # A file keeps unexpectedly verbose code output out of notebook memory/chat.
    log = turn_dir / 'stdout.txt'
    try:
        with log.open('w', encoding='utf-8') as output:
            process = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                     text=True, timeout=240)
        with log.open(encoding='utf-8', errors='replace') as output:
            result = output.read(24000)
            clipped = bool(output.read(1))
        return {'exit_code': process.returncode, 'output': result,
                'truncated': clipped, 'full_log': str(log)}
    except subprocess.TimeoutExpired:
        return {'error': 'Code exceeded 240 seconds; use smaller batches.', 'full_log': str(log)}
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
