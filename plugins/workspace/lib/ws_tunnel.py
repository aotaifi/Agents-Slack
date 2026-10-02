"""SSH local-forward tunnels owned by this integration.

A tunnel is recorded under <home>/tunnels/<local-port>.json with its pid, exact argv and
the sessions using it. Only records we wrote are ever signalled, and only after the live
process command line still equals the recorded argv, so unrelated ssh sessions and
forwards (and recycled pids) are never touched. Host-key verification stays on: nothing
here sets StrictHostKeyChecking, UserKnownHostsFile or similar.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@:\[\]%-]*$")
OPTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*=[^\s]+$")


class TunnelError(ValueError):
    """Operator-facing explanation without credentials."""


def validate_target(target):
    if not isinstance(target, str) or not TARGET_RE.match(target) or len(target) > 255:
        raise TunnelError("SSH target must be an alias or [user@]host (no leading dash or spaces).")
    return target


def build_argv(target, local_port, remote_host, remote_port, *, ssh_port=None, options=()):
    validate_target(target)
    for value in (local_port, remote_port, ssh_port):
        if value is not None and not (isinstance(value, int) and 1 <= value <= 65535):
            raise TunnelError("Ports must be integers in 1..65535.")
    if not re.match(r"^[A-Za-z0-9.:-]+$", remote_host):
        raise TunnelError("Remote host must be a hostname or address.")
    argv = [
        "ssh", "-N", "-T",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "BatchMode=yes",
        "-o", "ControlPath=none",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-L", f"127.0.0.1:{local_port}:{remote_host}:{remote_port}",
    ]  # fmt: skip
    for option in options:
        if not OPTION_RE.match(option):
            raise TunnelError("--ssh-option must look like Key=value.")
        argv += ["-o", option]
    if ssh_port is not None:
        argv += ["-p", str(ssh_port)]
    return argv + ["--", target]


def port_is_free(port):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def port_accepts(port, timeout=0.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def proc_argv(pid):
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.decode(errors="replace").split("\0")[:-1] if raw else None
    except OSError:
        pass
    try:  # macOS and other systems without /proc
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=3
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.split() if out else None


class Tunnels:
    def __init__(self, home, *, spawn=subprocess.Popen, ready_timeout=15.0):
        self.dir = Path(home) / "tunnels"
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.spawn, self.ready_timeout = spawn, ready_timeout

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.dir / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def record_path(self, port):
        return self.dir / f"{int(port)}.json"

    def read(self, port):
        try:
            return json.loads(self.record_path(port).read_text())
        except (OSError, ValueError):
            return None

    def write(self, record):
        path = self.record_path(record["local_port"])
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
        os.replace(temporary, path)

    def alive(self, record):
        """True only while the recorded pid still runs exactly our recorded command."""
        return bool(record) and proc_argv(record["pid"]) == record["argv"]

    def status(self, port):
        record = self.read(port)
        if record is None:
            return None
        return {**record, "alive": self.alive(record), "listening": port_accepts(port)}

    def up(self, owner, target, remote_host, remote_port, *, local_port=None, ssh_port=None,
           options=()):  # fmt: skip
        """Start (or share) a tunnel for `owner`; returns its local port."""
        candidates = [local_port] if local_port else range(8002, 8022)
        with self.lock():
            for port in candidates:
                argv = build_argv(
                    target, port, remote_host, remote_port, ssh_port=ssh_port, options=options
                )
                record = self.read(port)
                if record and self.alive(record):
                    if record["argv"] == argv:
                        if owner not in record["owners"]:
                            record["owners"].append(owner)
                            self.write(record)
                        return port
                    if local_port:
                        raise TunnelError(
                            f"Local port {port} is used by another integration tunnel "
                            "(different target); choose another --local-port."
                        )
                    continue
                if not port_is_free(port):
                    if local_port:
                        raise TunnelError(
                            f"Local port {port} is already in use by another program; "
                            "choose an unused --local-port."
                        )
                    continue
                return self._start(port, argv, owner, target, remote_host, remote_port)
            raise TunnelError("No free local port in 8002-8021; pass --local-port.")

    def _start(self, port, argv, owner, target, remote_host, remote_port):
        errors = Path(self.dir) / f"{port}.err"
        fd = os.open(errors, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            process = self.spawn(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=fd,
                start_new_session=True,
            )  # fmt: skip
        except OSError:
            raise TunnelError("Could not run ssh; is OpenSSH installed?") from None
        finally:
            os.close(fd)
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise TunnelError(self._explain(errors, process.returncode))
            if port_accepts(port):
                self.write(
                    {
                        "local_port": port, "pid": process.pid, "argv": argv, "target": target,
                        "remote_host": remote_host, "remote_port": remote_port,
                        "owners": [owner], "started": time.time(),
                    }
                )  # fmt: skip
                errors.unlink(missing_ok=True)
                return port
            time.sleep(0.1)
        self._terminate(process.pid)
        raise TunnelError(
            "SSH did not open the forward in time. Run the printed ssh command yourself "
            "(for example with `! ssh ...`) to finish any prompt, then retry."
        )

    @staticmethod
    def _explain(errors, code):
        try:
            text = Path(errors).read_text(errors="replace")[-600:]
        except OSError:
            text = ""
        low = text.lower()
        if "host key verification failed" in low or "no matching host key" in low:
            hint = (
                "Host key not trusted. Verify the host, then run `ssh TARGET` once yourself "
                "to accept it (verification is never disabled here)."
            )
        elif "permission denied" in low:
            hint = (
                "SSH authentication failed without a prompt. Use your key/agent or an alias "
                "that logs in non-interactively (BatchMode is on)."
            )
        elif "address already in use" in low or "cannot listen" in low:
            hint = "The local port was taken; choose another --local-port."
        else:
            hint = "Check the SSH account, alias and network."
        return f"SSH exited with status {code}. {hint}"

    @staticmethod
    def _terminate(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)

    def release(self, owner, port):
        """Drop `owner`; stop the tunnel only if it is ours, still ours, and now unused."""
        with self.lock():
            record = self.read(port)
            if record is None:
                return "none"
            record["owners"] = [o for o in record["owners"] if o != owner]
            if record["owners"]:
                self.write(record)
                return "kept-shared"
            result = "stopped" if self.alive(record) else "already-gone"
            if result == "stopped":
                self._terminate(record["pid"])
            self.record_path(port).unlink(missing_ok=True)
            return result

    def list(self):
        found = []
        for path in sorted(self.dir.glob("*.json")):
            with contextlib.suppress(ValueError):
                found.append(self.status(int(path.stem)))
        return [s for s in found if s]
