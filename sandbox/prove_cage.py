"""Run destructive-limit proofs against the local sandbox image only."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from chitti.worker import DockerSandboxDispatcher, FixedOperation, WorkerLimits


async def run_command(
    dispatcher: DockerSandboxDispatcher,
    workspace: Path,
    run_id: int,
    operation: FixedOperation,
    limits: WorkerLimits,
) -> tuple[int, str, str]:
    result, stdout, stderr = await dispatcher._run_container(
        run_id,
        dispatcher._docker_command(operation, workspace, run_id, limits),
        limits,
    )
    return result.returncode, stdout, stderr


async def main() -> None:
    dispatcher = DockerSandboxDispatcher(None)  # type: ignore[arg-type]
    workspace = Path(tempfile.mkdtemp(prefix="chitti-proof-"))
    try:
        os.chown(workspace, 65532, 65532)
    except PermissionError:
        pass
    workspace.chmod(0o770)
    try:
        bounded = WorkerLimits(
            memory="2g",
            cpus=1.0,
            pids=512,
            artifact_bytes=100 * 1024 * 1024,
            output_bytes=1024 * 1024,
            workspace_bytes=4 * 1024 * 1024 * 1024,
            timeout_seconds=2,
        )
        cases = {
            "memory-oom": FixedOperation("proof", "memory", (
                "python3", "-c",
                "x=[]; [x.append(bytearray(1024*1024)) for _ in range(256)]",
            )),
            "pid-limit": FixedOperation("proof", "pids", (
                "node", "-e",
                "const {spawn}=require('child_process'); "
                "for(let i=0;i<1024;i++) spawn(process.execPath,['-e','setInterval(()=>{},100000)']);",
            )),
            "output-quota": FixedOperation("proof", "output", (
                "sh", "-c", "yes x",
            )),
            "wall-clock": FixedOperation("proof", "timeout", (
                "sh", "-c", "sleep 30",
            )),
            "service-unreachability": FixedOperation("proof", "network", (
                "node", "-e",
                "const net=require('net'); "
                "const hosts=[['postgres','172.31.250.3',5432],"
                "['redis','172.31.250.6',6379],"
                "['litellm','172.31.250.4',4000],"
                "['chitti','172.31.250.5',8000]]; "
                "let left=hosts.length, bad=false; "
                "for(const [name,host,port] of hosts){const s=net.createConnection({host,port});"
                "s.setTimeout(1000); s.on('connect',()=>{console.error(name+' reachable');bad=true;s.destroy()});"
                "s.on('timeout',()=>s.destroy()); s.on('close',()=>{if(--left===0)process.exit(bad?1:0)});"
                "s.on('error',()=>{});}",
            ), network="bridge"),
        }
        for index, (name, operation) in enumerate(cases.items(), 1):
            limits = bounded if name != "memory-oom" else WorkerLimits(
                memory="64m", pids=32, output_bytes=1024 * 1024, timeout_seconds=10
            )
            try:
                result = await run_command(dispatcher, workspace, index, operation, limits)
                print(name, result[0], result[2].strip()[-120:])
            except TimeoutError:
                print(name, "timeout")

        cancellation = asyncio.create_task(
            run_command(
                dispatcher,
                workspace,
                99,
                FixedOperation("proof", "cancel", ("sh", "-c", "sleep 30")),
                WorkerLimits(timeout_seconds=60),
            )
        )
        while 99 not in dispatcher._containers:
            await asyncio.sleep(0.05)
        await dispatcher.cancel(99)
        await cancellation
        remaining = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=chitti-worker-99"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        print("cancellation-container", "gone" if not remaining else remaining)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
