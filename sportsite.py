#!/usr/bin/env python3
"""Read-only InnoSport roster snapshots, with interactive SSO and local scheduling."""

import argparse
from contextlib import contextmanager
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
import fcntl
from io import StringIO
import json
import logging
import math
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
ORIGIN = "https://sport.innopolis.university"
HOST = urlsplit(ORIGIN).hostname
LABEL = "university.innopolis.badminton-snapshots"
INSTALL_ROOT = Path.home() / "Library" / "Application Support" / "InnoSport Badminton"
LOG = logging.getLogger("sportsite")
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


class SportError(Exception):
    """An actionable error that can safely be shown without credentials."""


class AuthError(SportError):
    pass


class PermissionError(SportError):
    pass


class DataError(SportError):
    pass


class TooLateError(SportError):
    pass


def parse_datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise DataError("Сайт вернул время без часового пояса; выгрузка остановлена.")
    return result


def positive_id(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise DataError("Сайт вернул некорректный идентификатор.")
    return value


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@dataclass(frozen=True)
class Config:
    root: Path
    timezone: ZoneInfo
    title_pattern: re.Pattern
    group_ids: frozenset
    schedule: tuple
    lead_minutes: int
    max_lateness_seconds: int
    request_timeout_seconds: int
    poll_seconds: int
    vpn_control: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            schedule = tuple(
                (row["weekday"], dt_time.fromisoformat(row["time"]))
                for row in raw["schedule"]
            )
            if not schedule or len(set(schedule)) != len(schedule):
                raise ValueError("расписание пустое или содержит повторы")
            for weekday, clock in schedule:
                if type(weekday) is not int or not 0 <= weekday <= 6:
                    raise ValueError("weekday: 0 = понедельник, …, 6 = воскресенье")
                if clock.tzinfo or clock.second or clock.microsecond:
                    raise ValueError("время должно иметь формат HH:MM")
            numbers = {}
            for key in ("lead_minutes", "max_lateness_seconds", "request_timeout_seconds", "poll_seconds"):
                value = raw[key]
                if type(value) is not int or value <= 0:
                    raise ValueError(key + " должен быть положительным целым числом")
                numbers[key] = value
            if numbers["lead_minutes"] >= 24 * 60:
                raise ValueError("lead_minutes должен быть меньше суток")
            if numbers["max_lateness_seconds"] >= numbers["lead_minutes"] * 60:
                raise ValueError("окно опоздания должно заканчиваться до тренировки")
            groups = frozenset(positive_id(item) for item in raw["group_ids"])
            pattern = re.compile(raw["title_pattern"], re.IGNORECASE)
            if not raw["title_pattern"].strip():
                raise ValueError("title_pattern не должен быть пустым")
            vpn = raw.get("vpn_control", {"enabled": False})
            if not isinstance(vpn, dict) or type(vpn.get("enabled", False)) is not bool:
                raise ValueError("vpn_control.enabled должен быть true или false")
            if any(vpn.get(key) for key in ("connect_shortcut", "disconnect_shortcut", "shortcut_service_id")):
                import uuid
                uuid.UUID(vpn["shortcut_service_id"])
                if any(not isinstance(vpn.get(key), str) or not vpn[key].strip()
                       for key in ("connect_shortcut", "disconnect_shortcut")):
                    raise ValueError("Укажите обе команды управления VPN")
            return cls(path.resolve().parent, ZoneInfo(raw["timezone"]), pattern,
                       groups, schedule, vpn_control=vpn, **numbers)
        except (OSError, ValueError, KeyError, TypeError, re.error, SportError) as exc:
            raise SportError("Ошибка в config.json: " + str(exc)) from None

    @property
    def auth_path(self) -> Path:
        return self.root / ".state" / "session.json"

    @property
    def api_token_path(self) -> Path:
        return self.root / ".state" / "api-token.json"

    def now(self) -> datetime:
        return datetime.now(self.timezone)

    def starts(self, day: date) -> list:
        return sorted(datetime.combine(day, clock, self.timezone)
                      for weekday, clock in self.schedule if weekday == day.weekday())

    def upcoming(self, now: datetime, days: int = 14) -> list:
        return [start for offset in range(days)
                for start in self.starts(now.date() + timedelta(days=offset)) if start > now]

    def due_at(self, start: datetime) -> datetime:
        return start - timedelta(minutes=self.lead_minutes)

    def job_key(self, start: datetime) -> str:
        return start.strftime("%Y-%m-%d_%H-%M")

    def matches(self, event: dict) -> bool:
        if self.group_ids:
            return event["extendedProps"]["group_id"] in self.group_ids
        return bool(self.title_pattern.search(event["title"]))


def save_session(path: Path, state: dict) -> None:
    # Keep only the Django session for this website, never the SSO provider's cookies.
    cookies = [cookie for cookie in state.get("cookies", [])
               if cookie.get("name") == "sessionid"
               and cookie.get("domain", "").lstrip(".") == HOST]
    if not cookies:
        raise AuthError("Нет сессии спортсайта. Завершите SSO-вход и откройте профиль.")
    private_dir(path.parent)
    write_json(path, {"cookies": cookies, "origins": []})


class SportAPI:
    def __init__(self, request: Any, config: Config, bearer_token: Optional[str] = None):
        self.request = request
        self.config = config
        self.bearer_token = bearer_token

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        from playwright.sync_api import Error as PlaywrightError
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        if self.bearer_token:
            headers["Authorization"] = "Bearer " + self.bearer_token
        try:
            response = self.request.get(
                ORIGIN + path, params=params,
                timeout=self.config.request_timeout_seconds * 1000,
                max_redirects=0,
                headers=headers,
            )
        except PlaywrightError:
            # Playwright exceptions can include cookies and request headers; don't log them.
            raise SportError("Сайт недоступен: проверьте интернет, VPN и доступ к спортсайту.") from None
        try:
            if response.status == 401 or 300 <= response.status < 400:
                raise AuthError("Сессия истекла. Выполните: python sportsite.py login")
            if response.status == 403:
                try:
                    detail = str(response.json().get("detail", "")).lower()
                except (ValueError, AttributeError):
                    detail = ""
                if any(word in detail for word in ("credentials", "not authenticated", "token")):
                    raise AuthError("Нужен повторный вход: python sportsite.py login")
                raise PermissionError(
                    "Сайт отказал в доступе (403). Для списка нужны права тренера этой группы."
                )
            if response.status != 200:
                raise SportError("Спортсайт вернул HTTP %s для %s." % (response.status, path))
            if "application/json" not in response.headers.get("content-type", "").lower():
                raise AuthError("Вместо данных получена страница входа. Повторите login.")
            try:
                return response.json()
            except ValueError:
                raise DataError("Сайт вернул некорректный JSON.") from None
        finally:
            response.dispose()

    def calendar(self, day: date, days: int = 1) -> list:
        start = datetime.combine(day, dt_time.min, self.config.timezone)
        raw = self.get("/api/calendar/trainings", {
            "start": start.isoformat(), "end": (start + timedelta(days=days)).isoformat(),
        })
        try:
            if not isinstance(raw, list):
                raise ValueError()
            for event in raw:
                if not isinstance(event["title"], str):
                    raise ValueError()
                parse_datetime(event["start"])
                positive_id(event["extendedProps"]["id"])
                positive_id(event["extendedProps"]["group_id"])
            return raw
        except (KeyError, TypeError, ValueError):
            raise DataError("Формат расписания изменился; требуется обновить скрипт.") from None

    def find(self, start: datetime) -> dict:
        matches = [event for event in self.calendar(start.date())
                   if parse_datetime(event["start"]) == start and self.config.matches(event)]
        if not matches:
            raise DataError("Тренировка %s не найдена. Проверьте расписание, название и group_ids."
                            % start.strftime("%d.%m %H:%M"))
        if len(matches) != 1:
            raise DataError("На это время найдено несколько групп. Укажите group_ids в config.json.")
        if matches[0]["extendedProps"].get("can_grade") is False:
            raise PermissionError("Аккаунт не является тренером найденной группы.")
        return matches[0]

    def roster(self, event: dict, start: datetime) -> list:
        training_id = event["extendedProps"]["id"]
        raw = self.get("/api/attendance/%s/grades" % training_id)
        try:
            if (raw["group_id"] != event["extendedProps"]["group_id"]
                    or parse_datetime(raw["start"]) != start
                    or not isinstance(raw["grades"], list)):
                raise ValueError()
            people = {}
            for row in raw["grades"]:
                student_id = positive_id(row["student_id"])
                name = row["full_name"]
                if not isinstance(name, str) or not name.strip() or student_id in people:
                    raise ValueError()
                # Deliberately drop medical groups, marks, emails, and other extra fields.
                people[student_id] = {"student_id": student_id, "full_name": name.strip()}
        except (KeyError, TypeError, ValueError):
            raise DataError("Формат списка участников изменился; выгрузка остановлена.") from None

        # The site's trainer table is a UNION of check-ins and existing attendance marks.
        # Before training, the row count must agree with the actual check-in count.
        detail = self.get("/api/training/%s" % training_id)
        try:
            training = detail["training"]
            load = training["load"]
            if (training["id"] != training_id or training["group"]["id"] != raw["group_id"]
                    or parse_datetime(training["start"]) != start or type(load) is not int or load < 0):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise DataError("Не удалось проверить число записавшихся на тренировку.") from None
        if len(people) != load:
            raise DataError(
                "В таблице %s человек, записей %s. Список меняется или уже есть отметки посещения; "
                "такой снимок не сохранён." % (len(people), load)
            )
        return sorted(people.values(), key=lambda person: (person["full_name"].casefold(), person["student_id"]))

    def attendance(self, event: dict, start: datetime) -> list:
        """Read marks after training; the trainer table also contains unmarked check-ins."""
        raw = self.get("/api/attendance/%s/grades" % event["extendedProps"]["id"])
        try:
            if (raw["group_id"] != event["extendedProps"]["group_id"]
                    or parse_datetime(raw["start"]) != start or not isinstance(raw["grades"], list)):
                raise ValueError()
            people = {}
            for row in raw["grades"]:
                student_id = positive_id(row["student_id"])
                name, hours = row["full_name"], row["hours"]
                if (not isinstance(name, str) or not name.strip() or student_id in people
                        or type(hours) not in (int, float) or not math.isfinite(hours) or not 0 <= hours < 1000):
                    raise ValueError()
                # Keep only the fields needed for comparison, never email or medical data.
                people[student_id] = {"student_id": student_id, "full_name": name.strip(), "hours": hours}
            return sorted(people.values(), key=lambda person: (person["full_name"].casefold(), person["student_id"]))
        except (KeyError, TypeError, ValueError):
            raise DataError("Формат посещаемости изменился; сравнение остановлено.") from None


def load_session(config: Config) -> dict:
    if not config.auth_path.exists():
        raise AuthError("Сначала войдите на сайт: python sportsite.py login")
    try:
        state = json.loads(config.auth_path.read_text(encoding="utf-8"))
        cookies = state.get("cookies", [])
        sessions = [cookie for cookie in cookies if cookie.get("name") == "sessionid"
                    and cookie.get("domain", "").lstrip(".") == HOST]
        if not sessions:
            raise ValueError()
    except (OSError, ValueError, AttributeError):
        raise AuthError("Файл сессии повреждён. Выполните login повторно.") from None
    expires = sessions[0].get("expires", -1)
    if expires > 0 and expires <= config.now().timestamp():
        raise AuthError("Сессия истекла. Выполните login повторно.")
    return state


@contextmanager
def connect(config: Config):
    from vpn_control import VPNController, VPNError
    try:
        with VPNController(config.root / ".state", config.vpn_control).suspended():
            with connect_direct(config) as api:
                yield api
    except VPNError as exc:
        raise SportError(str(exc)) from None


@contextmanager
def connect_direct(config: Config):
    from playwright.sync_api import sync_playwright
    using_api = config.api_token_path.exists()
    state = {"cookies": [], "origins": []} if using_api else load_session(config)
    with sync_playwright() as playwright:
        from playwright.sync_api import Error as PlaywrightError
        try:
            request = playwright.request.new_context(storage_state=state)
        except PlaywrightError:
            raise AuthError("Не удалось загрузить сессию. Выполните login повторно.") from None
        try:
            token = None
            if using_api:
                from api_auth import APIAuthError, APINetworkError, exchange_sport_token
                try:
                    token = exchange_sport_token(request, config.api_token_path, config.request_timeout_seconds)
                except APIAuthError as exc:
                    raise AuthError(str(exc)) from None
                except APINetworkError as exc:
                    raise SportError(str(exc)) from None
            yield SportAPI(request, config, bearer_token=token)
        finally:
            request.dispose()


def login(config: Config) -> None:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
    print("В открывшемся браузере войдите через университетский SSO и дождитесь профиля.")
    print("Пароль вводите только на сайте. Будет сохранена локальная сессия спортсайта.")
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=False)
        except PlaywrightError:
            raise SportError("Установите браузер: python -m playwright install chromium") from None
        try:
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(ORIGIN + "/profile/", timeout=45000, wait_until="domcontentloaded")
            except PlaywrightError:
                print("Страница не успела загрузиться. Проверьте VPN и обновите её в этом браузере.")
            input("После входа вернитесь сюда и нажмите Enter: ")
            # Preserve an already completed sign-in even if connectivity changes during validation.
            save_session(config.auth_path, context.storage_state())
            api = SportAPI(context.request, config)
            api.calendar(config.now().date())
            print("Вход проверен. Сессия сохранена. Следующая команда: python sportsite.py check")
        finally:
            browser.close()


def login_api(config: Config) -> None:
    from api_auth import APIAuthError, APINetworkError, generate_service_token
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
    print("Однократное подключение персонального API-ключа InNoHassle.")
    print("Войдите в свой аккаунт через университетский SSO в открывшемся браузере.")
    print("После подтверждения будет создан долгосрочный ключ с правом sport:me — только ваш спорт-аккаунт.")
    print("Программа использует его для автоматического получения новых токенов спортсайта.")
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=False)
        except PlaywrightError:
            raise SportError("Установите браузер: python -m playwright install chromium") from None
        try:
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto("https://innohassle.ru/account", timeout=45000, wait_until="domcontentloaded")
            except PlaywrightError:
                print("Страница не успела загрузиться; проверьте VPN и обновите её в браузере.")
            answer = input("После входа введите ДА для создания и сохранения API-ключа: ").strip().casefold()
            if answer not in ("да", "yes"):
                raise SportError("Подключение API отменено; прежняя настройка сохранена.")
            try:
                token, metadata = generate_service_token(context.request, config.request_timeout_seconds)
            except APIAuthError as exc:
                raise AuthError(str(exc)) from None
            except APINetworkError as exc:
                raise SportError(str(exc)) from None
            private_dir(config.api_token_path.parent)
            write_json(config.api_token_path, {"access_token": token})
            expiry = datetime.fromtimestamp(metadata["expires"], config.timezone)
            print("Ключ сохранён локально. Срок действия: %s. Проверяю доступ к тренировке…"
                  % expiry.strftime("%d.%m.%Y %H:%M %Z"))
        finally:
            browser.close()
    check(config)
    print("API подключён. Новые токены спортсайта будут получаться автоматически без SSO-входа.")


def check(config: Config) -> None:
    with connect(config) as api:
        events = [event for event in api.calendar(config.now().date(), 14)
                  if config.matches(event) and parse_datetime(event["start"]) > config.now()]
        if not events:
            raise DataError("На ближайшие 14 дней нет подходящих занятий. Проверьте title_pattern/group_ids.")
        scheduled = config.upcoming(config.now())
        events = [event for event in events if parse_datetime(event["start"]) in scheduled]
        if not events:
            raise DataError("Бадминтон найден, но время на сайте отличается от config.json. Выполните list.")
        start = min(parse_datetime(event["start"]) for event in events)
        event = api.find(start)
        people = api.roster(event, start)
        print("Вход и доступ к списку проверены. Ближайшая тренировка: %s, участников сейчас: %s."
              % (start.strftime("%d.%m %H:%M"), len(people)))


def csv_text(snapshot: dict) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["№", "Имя", "ID участника", "Начало тренировки", "Список получен", "Режим"])
    for index, person in enumerate(snapshot["participants"], 1):
        name = person["full_name"]
        if name.lstrip().startswith(("=", "+", "-", "@")):
            name = "'" + name
        writer.writerow([index, name, person["student_id"], snapshot["training_start"],
                         snapshot["captured_at"], snapshot["mode"]])
    return "\ufeff" + output.getvalue()


def collect(config: Config, start: datetime, scheduled: bool,
            clock: Optional[Callable] = None, connector: Callable = connect) -> Path:
    now = clock or config.now
    requested_at = now()
    due = config.due_at(start)
    if requested_at >= start:
        raise TooLateError("Тренировка уже началась: список до начала восстановить невозможно.")
    if scheduled and not due <= requested_at <= due + timedelta(seconds=config.max_lateness_seconds):
        raise TooLateError("Пропущено время выгрузки. Снимок за час до тренировки уже не получить.")
    with connector(config) as api:
        event = api.find(start)
        people = api.roster(event, start)
        # Capture time belongs to the data response, before restoring the VPN connection.
        captured_at = now()
    if captured_at >= start or (scheduled and (captured_at - due).total_seconds() > config.max_lateness_seconds):
        raise TooLateError("Ответ сайта получен слишком поздно. Плановый снимок не сохранён.")
    snapshot = {
        "schema_version": 1,
        "mode": "scheduled" if scheduled else "manual",
        "training_id": event["extendedProps"]["id"],
        "group_id": event["extendedProps"]["group_id"],
        "title": event["title"],
        "training_start": start.isoformat(),
        "scheduled_capture_at": due.isoformat(),
        "request_started_at": requested_at.isoformat(),
        "captured_at": captured_at.isoformat(),
        "delay_seconds": round((captured_at - due).total_seconds(), 3),
        "participant_count": len(people),
        "source": ORIGIN + "/api/attendance/%s/grades" % event["extendedProps"]["id"],
        "participants": people,
    }
    stem = config.job_key(start) + "_training-" + str(snapshot["training_id"])
    if not scheduled:
        stem += "_manual_" + captured_at.strftime("%Y%m%dT%H%M%S%f")
    directory = config.root / "exports" / stem
    private_dir(config.root / "exports")
    if directory.exists():
        raise DataError("Снимок уже существует: %s. Исходный файл сохранён." % directory)
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=str(directory.parent)))
    try:
        atomic_write(staging / "participants.csv", csv_text(snapshot))
        write_json(staging / "snapshot.json", snapshot)
        staging.rename(directory)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return directory


class Scheduler:
    def __init__(self, config: Config, collector: Callable = collect):
        self.config = config
        self.collector = collector
        self.state_path = config.root / ".state" / "jobs.json"

    def tick(self, now: datetime) -> int:
        private_dir(self.state_path.parent)
        with (self.state_path.parent / "scheduler.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            if self.state_path.exists():
                try:
                    jobs = json.loads(self.state_path.read_text(encoding="utf-8"))
                    if not isinstance(jobs, dict):
                        raise ValueError()
                except (OSError, ValueError):
                    raise DataError("Повреждён журнал .state/jobs.json; проверьте его перед перезапуском.") from None
            else:
                jobs = {}
            failures = 0
            # Include yesterday to record a missed job after a laptop wakes the following day.
            for offset in (-1, 0, 1):
                for start in self.config.starts(now.date() + timedelta(days=offset)):
                    due = self.config.due_at(start)
                    key = self.config.job_key(start)
                    if due > now or jobs.get(key, {}).get("status") in ("saved", "missed"):
                        continue
                    # Recover safely if the process exited after export but before the journal update.
                    completed = list((self.config.root / "exports").glob(key + "_training-*/snapshot.json"))
                    completed = [p for p in completed if "_manual_" not in p.parent.name]
                    if completed:
                        jobs[key] = {"status": "saved", "path": str(completed[0].parent)}
                        write_json(self.state_path, jobs)
                        continue
                    if now > due + timedelta(seconds=self.config.max_lateness_seconds):
                        jobs[key] = {"status": "missed", "recorded_at": now.isoformat()}
                        LOG.error("ПРОПУСК %s: время снимка было %s.", key, due.isoformat())
                        write_json(self.state_path, jobs)
                        continue
                    try:
                        path = self.collector(self.config, start, scheduled=True)
                        jobs[key] = {"status": "saved", "path": str(path)}
                        LOG.info("Список сохранён: %s", path)
                    except SportError as exc:
                        failures += 1
                        previous = jobs.get(key, {})
                        if previous.get("error") != str(exc):
                            LOG.error("Выгрузка %s: %s", key, exc)
                        jobs[key] = {"status": "retry", "error": str(exc), "last_attempt_at": now.isoformat()}
                    write_json(self.state_path, jobs)
            return failures


def show_schedule(config: Config) -> None:
    print("Часовой пояс: %s. Допустимое опоздание ответа сайта: %s с."
          % (config.timezone.key, config.max_lateness_seconds))
    for start in config.upcoming(config.now(), 8):
        print("%s %s: тренировка %s → выгрузка %s" % (
            WEEKDAYS[start.weekday()], start.strftime("%d.%m.%Y"), start.strftime("%H:%M"),
            config.due_at(start).strftime("%d.%m %H:%M")))


def run(config: Config, once: bool) -> int:
    from vpn_control import VPNController, VPNError
    vpn = VPNController(config.root / ".state", config.vpn_control)
    try:
        vpn.recover()
    except VPNError as exc:
        LOG.error("Восстановление VPN: %s", exc)
    scheduler = Scheduler(config)
    if once:
        return 1 if scheduler.tick(config.now()) else 0
    try:
        check(config)
    except (AuthError, PermissionError, DataError):
        raise
    except SportError as exc:
        LOG.warning("Начальная проверка: %s Ожидание продолжится, запрос повторится в момент выгрузки.", exc)
    show_schedule(config)
    LOG.info("Ожидание включено. Компьютер должен быть включён, без сна и с доступом к сайту.")
    while True:
        try:
            vpn.recover()
        except VPNError as exc:
            LOG.error("Восстановление VPN: %s", exc)
        scheduler.tick(config.now())
        now = config.now()
        future_due = [config.due_at(start) for start in config.upcoming(now, 2)
                      if config.due_at(start) > now]
        wait = min([config.poll_seconds] + [(due - now).total_seconds() for due in future_due])
        time.sleep(max(0.1, wait))


def default_config_path() -> Path:
    marker = ROOT / ".state" / "installation.json"
    if marker.exists():
        try:
            target = Path(json.loads(marker.read_text(encoding="utf-8"))["config_path"])
            if target.is_absolute() and target.is_file():
                return target
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return ROOT / "config.json"


def deploy_runtime(config: Config, config_path: Path) -> Config:
    """Install a self-contained copy outside macOS protected Downloads/Desktop folders."""
    state, api_token = None, None
    if config.api_token_path.exists():
        from api_auth import APIAuthError, load_service_token
        try:
            api_token, _ = load_service_token(config.api_token_path)
        except APIAuthError as exc:
            raise AuthError(str(exc)) from None
    else:
        state = load_session(config)
    private_dir(INSTALL_ROOT)
    for name in ("sportsite.py", "api_auth.py", "vpn_control.py", "attendance_report.py", "telegram_bot.py",
                 "violation_registry.py", "requirements.txt"):
        source, target = ROOT / name, INSTALL_ROOT / name
        if source.resolve() != target.resolve():
            atomic_write(target, source.read_text(encoding="utf-8"))
    write_json(INSTALL_ROOT / "config.json", json.loads(config_path.read_text(encoding="utf-8")))
    deployed = Config.load(INSTALL_ROOT / "config.json")
    if api_token:
        private_dir(deployed.api_token_path.parent)
        write_json(deployed.api_token_path, {"access_token": api_token})
    elif state:
        save_session(deployed.auth_path, state)
    private_dir(deployed.root / "exports")
    python = deployed.root / ".venv" / "bin" / "python"
    if not python.exists():
        result = subprocess.run([sys.executable, "-m", "venv", str(deployed.root / ".venv")],
                                capture_output=True)
        if result.returncode:
            raise SportError("Не удалось подготовить Python для фоновой работы.")
    result = subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                             "-r", str(deployed.root / "requirements.txt")], capture_output=True)
    if result.returncode:
        raise SportError("Не удалось установить зависимости фоновой копии. Проверьте интернет и повторите установку.")
    return deployed


def install_autostart(config: Config, config_path: Path) -> None:
    if sys.platform != "darwin":
        raise SportError("Эта команда для macOS. Для Linux есть пример systemd в README.md.")
    plist_path = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    if plist_path.exists():
        raise SportError("Автозапуск уже настроен. Для переустановки сначала выполните remove-autostart.")
    deployed = deploy_runtime(config, config_path)
    private_dir(deployed.root / "logs")
    for name in ("autostart.log", "autostart-errors.log"):
        target = deployed.root / "logs" / name
        target.touch(mode=0o600, exist_ok=True)
        target.chmod(0o600)
    definition = {
        "Label": LABEL,
        "ProgramArguments": [str(deployed.root / ".venv" / "bin" / "python"),
                             str(deployed.root / "sportsite.py"),
                             "--config", str(deployed.root / "config.json"), "run"],
        "WorkingDirectory": str(deployed.root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 60,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "TZ": config.timezone.key},
        "StandardOutPath": str(deployed.root / "logs" / "autostart.log"),
        "StandardErrorPath": str(deployed.root / "logs" / "autostart-errors.log"),
    }
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(plist_path, plistlib.dumps(definition).decode("utf-8"))
    domain = "gui/%s" % os.getuid()
    result = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], capture_output=True)
    if result.returncode:
        plist_path.unlink()
        raise SportError("macOS не включила автозапуск. Код launchctl: %s." % result.returncode)
    private_dir(ROOT / ".state")
    write_json(ROOT / ".state" / "installation.json", {"config_path": str(deployed.root / "config.json")})
    print("Автозапуск включён при входе в macOS. Рабочая папка: %s" % deployed.root)


def remove_autostart() -> None:
    if sys.platform != "darwin":
        raise SportError("Эта команда предназначена для macOS.")
    path = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    result = subprocess.run(["launchctl", "bootout", "gui/%s/%s" % (os.getuid(), LABEL)], capture_output=True)
    if result.returncode not in (0, 3, 113):
        raise SportError("Не удалось остановить автозапуск; код launchctl: %s." % result.returncode)
    path.unlink(missing_ok=True)
    print("Автозапуск отключён. Выгрузки и сессия сохранены.")


def configure_logging(config: Config) -> None:
    private_dir(config.root / "logs")
    path = config.root / "logs" / "sportsite.log"
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    class MoscowFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            return datetime.fromtimestamp(record.created, config.timezone).isoformat(timespec="seconds")
    formatter = MoscowFormatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(path, encoding="utf-8")):
        handler.setFormatter(formatter)
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def main() -> int:
    def stop_requested(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_requested)
    parser = argparse.ArgumentParser(description="Списки участников бадминтона за час до тренировки")
    parser.add_argument("--config", type=Path, default=default_config_path())
    sub = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("login", "войти через SSO в браузере"),
        ("login-api", "подключить долгосрочный API-ключ с автоматическим обновлением спорт-токенов"),
        ("check", "проверить вход и доступ к участникам"),
        ("list", "показать подходящие тренировки и ID групп"),
        ("schedule", "показать расписание выгрузок"),
        ("install-autostart", "включить автозапуск в macOS с сохранённой сессией"),
        ("remove-autostart", "выключить автозапуск в macOS"),
        ("telegram-setup", "подключить личного Telegram-бота и его автозапуск"),
        ("telegram-run", "принимать команды Telegram-бота"),
        ("telegram-status", "проверить локальные настройки Telegram без вывода токена"),
    ):
        sub.add_parser(command, help=help_text)
    export = sub.add_parser("export", help="сделать отдельный ручной снимок")
    export.add_argument("--date", type=date.fromisoformat, help="дата YYYY-MM-DD; по умолчанию ближайшее занятие")
    report = sub.add_parser("report", help="сравнить плановый снимок с выставленными часами")
    report.add_argument("--date", type=date.fromisoformat, help="дата YYYY-MM-DD; по умолчанию последнее начавшееся занятие")
    report.add_argument("--json", action="store_true", help="вывести результат для Telegram-бота")
    daemon = sub.add_parser("run", help="ожидать плановых выгрузок")
    daemon.add_argument("--once", action="store_true", help="проверить только текущие задания и завершиться")
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        os.umask(0o077)
        configure_logging(config)
        if args.command == "login":
            login(config)
        elif args.command == "login-api":
            login_api(config)
        elif args.command == "check":
            check(config)
        elif args.command == "schedule":
            show_schedule(config)
        elif args.command == "list":
            with connect(config) as api:
                for event in sorted(api.calendar(config.now().date(), 14), key=lambda row: row["start"]):
                    if config.matches(event):
                        print("%s | %s | группа %s | тренировка %s | доступ тренера: %s" % (
                            parse_datetime(event["start"]).astimezone(config.timezone).strftime("%d.%m %H:%M"),
                            event["title"], event["extendedProps"]["group_id"], event["extendedProps"]["id"],
                            event["extendedProps"].get("can_grade", "неизвестно")))
        elif args.command == "export":
            now = config.now()
            starts = config.starts(args.date) if args.date else config.upcoming(now)
            starts = [start for start in starts if start > now]
            if not starts:
                raise SportError("На эту дату нет будущей тренировки в расписании.")
            print("Ручной снимок сохранён: %s" % collect(config, starts[0], scheduled=False))
        elif args.command == "run":
            return run(config, args.once)
        elif args.command == "install-autostart":
            install_autostart(config, args.config)
        elif args.command == "remove-autostart":
            remove_autostart()
        elif args.command == "report":
            from attendance_report import build_report, save_report, text_report
            try:
                result = build_report(config, args.date)
                destination = save_report(config, result)
                if args.json:
                    print(json.dumps({"ok": True, "report": result}, ensure_ascii=False))
                else:
                    print(text_report(result))
                    print("\nОтчёт сохранён: %s" % destination)
            except SportError as exc:
                if not args.json:
                    raise
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
                return 1
        elif args.command.startswith("telegram-"):
            from telegram_bot import setup, serve, show_status
            if args.command == "telegram-setup":
                setup(config)
            elif args.command == "telegram-run":
                serve(config)
            else:
                show_status(config)
        return 0
    except SportError as exc:
        LOG.error("%s", exc)
        return 1
    except ModuleNotFoundError:
        print("Установите зависимости: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nОстановлено.")
        return 130
    except OSError as exc:
        LOG.error("Ошибка файла или системы: %s", exc.strerror)
        return 1


if __name__ == "__main__":
    # Companion modules must share these exception classes when this file is the CLI.
    sys.modules.setdefault("sportsite", sys.modules[__name__])
    sys.exit(main())
