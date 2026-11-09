"""Temporarily pause registered macOS VPNs, then restore only previously active ones."""

from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

LOG = logging.getLogger("sportsite")
SCUTIL = "/usr/sbin/scutil"
SHORTCUTS = "/usr/bin/shortcuts"


class VPNError(Exception):
    pass


class VPNController:
    def __init__(self, state_dir: Path, settings: dict, runner=None, clock=None, sleeper=None):
        self.state_dir = state_dir
        self.settings = settings
        self.runner = runner or subprocess.run
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.journal = state_dir / "vpn-restore.json"

    def command(self, args, timeout=10):
        try:
            result = self.runner(args, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise VPNError("Команда управления VPN не завершилась. Проверьте приложение VPN.") from None
        if result.returncode:
            # Do not include arbitrary command output or profile configuration in logs.
            raise VPNError("macOS не выполнила команду управления VPN (код %s)." % result.returncode)
        return result.stdout

    def services(self):
        output = self.command([SCUTIL, "--nc", "list"])
        pattern = re.compile(r'^\s*\*?\s*\(([^)]+)\)\s+([0-9a-fA-F-]{36})\s+VPN\s+.*?"([^"]+)"')
        found = []
        for line in output.splitlines():
            match = pattern.search(line)
            if match:
                state, service_id, name = match.groups()
                try:
                    uuid.UUID(service_id)
                except ValueError:
                    raise VPNError("Не удалось распознать профиль VPN.") from None
                found.append({"id": service_id, "name": name, "state": state})
        if "(Connected)" in output and not any(service["state"] == "Connected" for service in found):
            raise VPNError("Виден активный VPN, но его профиль не распознан.")
        return found

    def status(self, service_id):
        output = self.command([SCUTIL, "--nc", "status", service_id])
        state = output.splitlines()[0].strip() if output.splitlines() else ""
        if state not in {"Connected", "Connecting", "Disconnected", "Disconnecting"}:
            raise VPNError("Не удалось определить состояние VPN.")
        return state

    def switch(self, service, connect):
        shortcut_key = "connect_shortcut" if connect else "disconnect_shortcut"
        shortcut = self.settings.get(shortcut_key)
        if shortcut and service["id"] == self.settings.get("shortcut_service_id"):
            args = [SHORTCUTS, "run", shortcut]
        else:
            args = [SCUTIL, "--nc", "start" if connect else "stop", service["id"]]
        self.command(args, timeout=25)
        deadline = self.clock() + 20
        target = "Connected" if connect else "Disconnected"
        while True:
            if self.status(service["id"]) == target:
                return
            if self.clock() >= deadline:
                raise VPNError("VPN не перешёл в состояние %s за отведённое время." % target)
            self.sleeper(0.5)

    def write_journal(self, services):
        fd, temporary = tempfile.mkstemp(prefix=".vpn-", dir=str(self.state_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"services": [{"id": s["id"], "name": s["name"]} for s in services]}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.journal)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @contextmanager
    def locked(self, wait):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state_dir / "vpn.lock").open("a") as lock:
            deadline = self.clock() + (15 if wait else 0)
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self.clock() >= deadline:
                        if wait:
                            raise VPNError("Другая выгрузка сейчас управляет VPN. Повторите попытку позже.")
                        yield False
                        return
                    self.sleeper(0.2)
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def restore_locked(self):
        if not self.journal.exists():
            return True
        try:
            pending = json.loads(self.journal.read_text(encoding="utf-8"))["services"]
            if not isinstance(pending, list):
                raise ValueError()
            for service in pending:
                uuid.UUID(service["id"])
                if not isinstance(service["name"], str):
                    raise ValueError()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise VPNError("Повреждён журнал восстановления VPN. Проверьте VPN вручную.") from None
        failed = []
        for service in pending:
            try:
                if self.status(service["id"]) != "Connected":
                    LOG.info("Восстанавливаю VPN: %s", service["name"])
                    self.switch(service, connect=True)
            except VPNError as exc:
                LOG.error("VPN %s пока не восстановлен: %s", service["name"], exc)
                failed.append(service)
        if failed:
            self.write_journal(failed)
            return False
        self.journal.unlink()
        return True

    def recover(self):
        if not self.journal.exists():
            return
        with self.locked(wait=False) as acquired:
            if acquired:
                self.restore_locked()

    @contextmanager
    def suspended(self):
        if not self.settings.get("enabled", False):
            yield
            return
        if sys.platform != "darwin":
            raise VPNError("Автоматическое управление VPN настроено только для macOS.")
        with self.locked(wait=True):
            if not self.restore_locked():
                raise VPNError("Предыдущий VPN ещё не восстановлен; новая выгрузка приостановлена.")
            services = self.services()
            active = [service for service in services if service["state"] in {"Connected", "Connecting"}]
            if active:
                # Save intent before switching; recovery also works after a process crash/restart.
                self.write_journal(active)
            try:
                for service in active:
                    LOG.info("Временно отключаю VPN перед выгрузкой: %s", service["name"])
                    self.switch(service, connect=False)
                if any(service["state"] in {"Connected", "Connecting", "Disconnecting"}
                       for service in self.services()):
                    raise VPNError("VPN остаётся активным. Запрос к сайту пока не отправлен.")
                yield
            finally:
                # Restoration errors are journaled and retried by the daemon; don't lose a valid roster.
                self.restore_locked()
