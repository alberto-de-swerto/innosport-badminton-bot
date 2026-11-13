"""Durable post-practice reminders and export problem alerts, without website polling."""

from datetime import timedelta
import fcntl
import json

from attendance_report import load_baseline
from sportsite import SportError, parse_datetime, private_dir, write_json
from violation_registry import report_session
from i18n import language_of, tr


class NotificationError(SportError):
    pass


ERROR_TEXT = {
    "auth": "Сессия спортсайта истекла или вход недействителен. Открой login.command и заново войди через SSO.",
    "permission": "Спортсайт отказал в доступе к участникам. Проверь права тренера для Beginners.",
    "data": "Данные спортсайта не прошли проверку. Нужна проверка расписания, группы или формата ответа.",
    "export": "Не удалось выгрузить список. Проверь интернет, VPN и доступ к спортсайту.",
}


class ServiceNotifications:
    def __init__(self, config, settings, api):
        self.config, self.settings, self.api = config, settings, api
        self.state_path = config.root / ".state" / "telegram-service-notifications.json"

    def load_state(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (state["schema_version"] != 1 or state["chat_id"] != self.settings["chat_id"]
                    or not isinstance(state["delivered"], dict)):
                raise ValueError()
            parse_datetime(state["enabled_at"])
            for key, value in state["delivered"].items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    raise ValueError()
                parse_datetime(value["at"])
                if value["status"] not in ("sent", "checked", "expired"):
                    raise ValueError()
            return state
        except FileNotFoundError:
            state = {"schema_version": 1, "chat_id": self.settings["chat_id"],
                     "enabled_at": self.config.now().isoformat(), "delivered": {}}
            write_json(self.state_path, state)
            return state
        except (OSError, ValueError, KeyError, TypeError, AttributeError, SportError):
            raise NotificationError("Повреждён журнал напоминаний и предупреждений. Данные не перезаписаны.") from None

    def end_at(self, start):
        fallback = start + timedelta(minutes=self.config.training_duration_minutes)
        try:
            baseline = load_baseline(self.config, start)
            end = parse_datetime(baseline["training_end"])
            if start < end <= start + timedelta(days=1):
                return end
        except (OSError, ValueError, KeyError, TypeError, AttributeError, SportError):
            pass
        return fallback

    def already_checked(self, start, now):
        pattern = self.config.job_key(start) + "_training-*/report.json"
        for path in (self.config.root / "reports").glob(pattern):
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                session = report_session(report)
                if (session is not None and parse_datetime(session["training_start"]) == start
                        and parse_datetime(session["checked_at"]) <= now
                        and self.config.matches({"title": report["title"],
                                                 "extendedProps": {"group_id": session["group_id"]}})):
                    return True
            except (OSError, ValueError, KeyError, TypeError, AttributeError, SportError):
                continue
        return False

    def tick(self):
        _ = lambda text: tr(text, language_of(self.settings))
        private_dir(self.state_path.parent)
        with (self.state_path.parent / "telegram-service-notifications.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            state = self.load_state()
            enabled = parse_datetime(state["enabled_at"])
            now = self.config.now()
            sent = 0

            def checkpoint(key, status="sent"):
                state["delivered"][key] = {"status": status, "at": now.isoformat()}
                write_json(self.state_path, state)

            def deliver(key, text):
                nonlocal sent
                if key not in state["delivered"]:
                    self.api.send(self.settings["chat_id"], text)
                    checkpoint(key)
                    sent += 1

            jobs = {}
            jobs_path = self.config.root / ".state" / "jobs.json"
            if jobs_path.exists():
                try:
                    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
                    if not isinstance(jobs, dict) or any(not isinstance(v, dict) for v in jobs.values()):
                        raise ValueError()
                    if "journal:jobs" in state["delivered"]:
                        del state["delivered"]["journal:jobs"]
                        write_json(self.state_path, state)
                except (OSError, ValueError, TypeError):
                    deliver("journal:jobs", _("⚠️ Не удалось прочитать журнал выгрузок. Проверь работу программы на Mac."))
                    jobs = {}

            health_path = self.config.root / ".state" / "worker-health.json"
            if health_path.exists():
                try:
                    health = json.loads(health_path.read_text(encoding="utf-8"))
                    if health["schema_version"] != 1 or health["status"] not in ("ready", "error"):
                        raise ValueError()
                    parse_datetime(health["observed_at"])
                    if health["status"] == "error":
                        kind = health.get("kind")
                        kind = kind if isinstance(kind, str) and kind in ERROR_TEXT else "export"
                        deliver("worker:" + kind, _("⚠️ Фоновый сборщик не прошёл проверку\n") + _(ERROR_TEXT[kind]))
                    else:
                        previous = [key for key in state["delivered"] if key.startswith("worker:")]
                        if previous:
                            for key in previous:
                                del state["delivered"][key]
                            write_json(self.state_path, state)
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    deliver("health-journal", _("⚠️ Не удалось проверить состояние фонового сборщика. Проверь программу на Mac."))

            for offset in (-1, 0):
                for start in self.config.starts(now.date() + timedelta(days=offset)):
                    key = self.config.job_key(start)
                    due = self.config.due_at(start)
                    job = jobs.get(key, {})
                    fresh_job = due >= enabled
                    for field in ("last_attempt_at", "recorded_at"):
                        try:
                            fresh_job = fresh_job or enabled <= parse_datetime(job[field]) <= now
                        except (KeyError, ValueError, TypeError, AttributeError, SportError):
                            pass
                    if fresh_job and due <= now:
                        status = job.get("status")
                        try:
                            valid_snapshot = load_baseline(self.config, start)["mode"] == "scheduled"
                        except (OSError, ValueError, KeyError, TypeError, AttributeError, SportError):
                            valid_snapshot = False
                        if status == "retry" and not valid_snapshot:
                            kind = job.get("error_kind")
                            kind = kind if isinstance(kind, str) and kind in ERROR_TEXT else "export"
                            deliver("job:%s:%s" % (key, kind),
                                    _("⚠️ Ошибка выгрузки · %s МСК\n%s\nПрограмма повторит попытку в допустимом окне.")
                                    % (start.strftime("%d.%m.%Y %H:%M"), _(ERROR_TEXT[kind])))
                        if status == "saved" and not valid_snapshot:
                            deliver("invalid:" + key, _("⚠️ Сохранённый снимок · %s МСК не прошёл проверку. "
                                    "Не используй его для выводов об отменах; проверь локальные файлы программы.")
                                    % start.strftime("%d.%m.%Y %H:%M"))
                        deadline = due + timedelta(seconds=self.config.max_lateness_seconds + 60)
                        if status == "missed" or (status != "saved" and now > deadline):
                            if not valid_snapshot:
                                deliver("missed:" + key,
                                        _("🚨 Снимок за час пропущен · %s МСК\n"
                                        "Достоверного списка за час до начала нет. Проверь, что Mac включён, не спит, "
                                        "а программа имеет доступ к сайту. Восстановить такой снимок задним числом нельзя.")
                                        % start.strftime("%d.%m.%Y %H:%M"))

                    end = self.end_at(start)
                    reminder_due = end + timedelta(minutes=self.config.reminder_delay_minutes)
                    reminder_key = "reminder:" + key
                    if reminder_due < enabled or now < reminder_due or reminder_key in state["delivered"]:
                        continue
                    if self.already_checked(start, now):
                        checkpoint(reminder_key, "checked")
                    elif now > reminder_due + timedelta(hours=3):
                        checkpoint(reminder_key, "expired")
                    else:
                        deliver(reminder_key,
                                _("🏸 Beginners · занятие %s МСК завершилось\n\n"
                                "Сохрани посещаемость и часы на спортсайте, затем отправь:\n/check %s")
                                % (start.strftime("%d.%m.%Y %H:%M"), start.date().isoformat()))
            return sent
