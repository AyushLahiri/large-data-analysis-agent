"""Run one loader step in the existing Docker image. No agent logic here."""
import json
import subprocess
import time
import uuid
from pathlib import Path

from v_5.app.config import IMAGE


def run_code(code, files, workspace, turn_dir, read_only=False, review_output=True, cancelled=None):
    """Execute one Python script in the isolated sandbox.

    When: A loader, codebook worker or analysis helper needs Python execution.
    Inputs: code: script; files: uploads to mount; workspace: work directory; turn_dir:
        logs/script; read_only/review_output: mount permissions; cancelled: optional stop
        event.
    Returns: Execution dict with exit_code/output/truncated/full_log, or error/full_log on
        timeout; cancellation raises.
    Flow: Writes the script, mounts originals read-only, runs with resource limits and no
        network, bounds returned output and always removes the container. Session.run_code
        adds agent diagnostics.
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
    # Reviewers cannot change prepared files, but may write new evidence into their own turn directory.
    if read_only and review_output:
        command += ['--mount', f'type=bind,source={turn_dir},target=/review']
    command += ['--mount', f'type=bind,source={workspace},target=/work' + (',readonly' if read_only else ''),
                IMAGE, '/work/' + script.relative_to(workspace).as_posix()]
    # A file keeps unexpectedly verbose code output out of notebook memory/chat.
    log = turn_dir / 'stdout.txt'
    try:
        with log.open('w', encoding='utf-8') as output, subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT) as process:
            deadline = time.monotonic() + 240
            while True:
                if cancelled is not None and cancelled.is_set():
                    process.kill()
                    raise InterruptedError('Stopped by the user.')
                remaining = deadline - time.monotonic()
                try:
                    process.wait(timeout=min(0.25, remaining) if cancelled is not None else remaining)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        process.kill()
                        raise
        with log.open(encoding='utf-8', errors='replace') as output:
            result = output.read(24000)
            clipped = bool(output.read(1))
        return {'exit_code': process.returncode, 'output': result,
                'truncated': clipped, 'full_log': str(log)}
    except subprocess.TimeoutExpired:
        return {'error': 'Code exceeded 240 seconds; use smaller batches.', 'full_log': str(log)}
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
