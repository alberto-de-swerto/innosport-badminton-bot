"""Compare an authentic scheduled roster with read-only attendance marks."""

from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Optional

from sportsite import (Config, DataError, SportError, connect, parse_datetime,
                       positive_id, private_dir, write_json, atomic_write)


def training_start(config: Config, requested_date: Optional[date], now: datetime) -> datetime:
    if requested_date:
        starts = config.starts(requested_date)
        if len(starts) != 1:
            raise SportError("На эту дату нет единственной тренировки Beginners в расписании.")
        start = starts[0]
        if start > now:
            raise SportError("Тренировка ещё не началась. Отправь /check после сохранения посещаемости на сайте.")
        return start
    starts = [start for offset in range(15)
              for start in config.starts(now.date() - timedelta(days=offset)) if start <= now]
    if not starts:
        raise SportError("Не найдена недавняя тренировка. Укажи дату: /check YYYY-MM-DD.")
    return max(starts)


def load_baseline(config: Config, start: datetime) -> dict:
    paths = [path for path in (config.root / "exports").glob(
        config.job_key(start) + "_training-*/snapshot.json") if "_manual_" not in path.parent.name]
    if not paths:
        imported = config.root / "imports" / config.job_key(start) / "baseline.json"
        if imported.exists():
            return load_imported_baseline(config, start, imported)
        raise SportError(
            "Нет планового снимка за час до тренировки %s. "
            "Сравнить отмены достоверно нельзя; ручные снимки его не заменяют."
            % start.strftime("%d.%m.%Y %H:%M"))
    if len(paths) != 1:
        raise DataError("Найдено несколько плановых снимков занятия. Нужна проверка сохранённых файлов.")
    try:
        raw = json.loads(paths[0].read_text(encoding="utf-8"))
        due, captured = parse_datetime(raw["scheduled_capture_at"]), parse_datetime(raw["captured_at"])
        requested = parse_datetime(raw["request_started_at"])
        if (raw["schema_version"] != 1 or raw["mode"] != "scheduled"
                or parse_datetime(raw["training_start"]) != start
                or due != config.due_at(start) or not due <= requested <= captured < start
                or (captured - due).total_seconds() > config.max_lateness_seconds
                or not isinstance(raw["participants"], list)):
            raise ValueError()
        event = {"title": raw["title"], "extendedProps": {"group_id": positive_id(raw["group_id"])}}
        if not config.matches(event):
            raise ValueError()
        positive_id(raw["training_id"])
        ids = set()
        for person in raw["participants"]:
            ident = positive_id(person["student_id"])
            if (ident in ids or not isinstance(person["full_name"], str)
                    or not person["full_name"].strip()):
                raise ValueError()
            ids.add(ident)
        if type(raw["participant_count"]) is not int or raw["participant_count"] != len(ids):
            raise ValueError()
        return raw
    except (OSError, ValueError, TypeError, KeyError, SportError):
        raise DataError("Плановый снимок повреждён или не соответствует настройкам занятия. Сравнение остановлено.") from None


def load_imported_baseline(config: Config, start: datetime, path: Path) -> dict:
    """An explicitly supplied historical list is labelled; never backdate a scheduled job."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        imported_at = parse_datetime(raw["imported_at"])
        if (raw["schema_version"] != 1 or raw["mode"] != "user_import"
                or raw["source_kind"] != "user_screenshots"
                or raw["reference_at_basis"] != "user_statement"
                or parse_datetime(raw["training_start"]) != start
                or parse_datetime(raw["reference_at"]) != config.due_at(start)
                or imported_at < start or not isinstance(raw["participants"], list)
                or not isinstance(raw["title"], str) or not raw["title"].strip()
                or not isinstance(raw["source_files"], list) or not raw["source_files"]
                or any(not isinstance(name, str) or not name.strip() for name in raw["source_files"])
                or "captured_at" in raw or "request_started_at" in raw):
            raise ValueError()
        event = {"title": raw["title"], "extendedProps": {"group_id": positive_id(raw["group_id"])}}
        if not config.matches(event):
            raise ValueError()
        positive_id(raw["training_id"])
        ids = set()
        for person in raw["participants"]:
            ident = positive_id(person["student_id"])
            if (ident in ids or not isinstance(person["full_name"], str) or not person["full_name"].strip()):
                raise ValueError()
            ids.add(ident)
        if type(raw["participant_count"]) is not int or raw["participant_count"] != len(ids):
            raise ValueError()
        return raw
    except (OSError, ValueError, TypeError, KeyError, SportError):
        raise DataError("Внесённый вручную список повреждён или относится к другому занятию. Сравнение остановлено.") from None


def compare(baseline: dict, current: list) -> dict:
    before = {p["student_id"]: {"student_id": p["student_id"], "full_name": p["full_name"]}
              for p in baseline["participants"]}
    after = {p["student_id"]: dict(p) for p in current}
    order = lambda p: (p["full_name"].casefold(), p["student_id"])
    return {
        "registered": sorted(before.values(), key=order),
        "cancelled": sorted([p for ident, p in before.items() if ident not in after], key=order),
        "attended": sorted([dict(p, in_snapshot=ident in before)
                            for ident, p in after.items() if p["hours"] > 0], key=order),
        "no_hours": sorted([after[ident] for ident in before
                            if ident in after and after[ident]["hours"] == 0], key=order),
        "new_no_hours": sorted([p for ident, p in after.items()
                                if ident not in before and p["hours"] == 0], key=order),
    }


def build_report(config: Config, requested_date: Optional[date] = None, connector=connect, clock=None) -> dict:
    now = clock or config.now
    start = training_start(config, requested_date, now())
    baseline = load_baseline(config, start)
    with connector(config) as api:
        event = api.find(start)
        if (event["extendedProps"]["id"] != baseline["training_id"]
                or event["extendedProps"]["group_id"] != baseline["group_id"]):
            raise DataError("На сайте сейчас другое занятие. Сравнение с прежним снимком остановлено.")
        detail = api.get("/api/training/%s" % baseline["training_id"])
        try:
            training = detail["training"]
            end = parse_datetime(training["end"])
            if (training["id"] != baseline["training_id"] or training["group"]["id"] != baseline["group_id"]
                    or parse_datetime(training["start"]) != start or end <= start):
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise DataError("Не удалось проверить дату и группу занятия для сравнения.") from None
        current = api.attendance(event, start)
        checked_at = now()
    return dict(compare(baseline, current), schema_version=1,
                training_id=baseline["training_id"], group_id=baseline["group_id"],
                title=baseline["title"], training_start=start.isoformat(), training_end=end.isoformat(),
                baseline_mode=baseline["mode"], snapshot_at=baseline.get("captured_at"),
                reference_at=baseline.get("reference_at"), imported_at=baseline.get("imported_at"),
                scheduled_capture_at=config.due_at(start).isoformat(),
                checked_at=checked_at.isoformat(), preliminary=checked_at < end)


def display_name(person: dict) -> str:
    name = " ".join(person["full_name"].split())
    return name if len(name) <= 240 else name[:237] + "…"


def text_report(report: dict) -> str:
    start = parse_datetime(report["training_start"])
    checked_at = parse_datetime(report["checked_at"]).astimezone(start.tzinfo)
    lines = ["🏸 Beginners · %s МСК" % start.strftime("%d.%m.%Y %H:%M")]
    imported = report.get("baseline_mode") == "user_import"
    if imported:
        reference_at = parse_datetime(report["reference_at"]).astimezone(start.tzinfo)
        lines.extend(["🧪 Тест по списку со скриншотов пользователя.",
                      "Исходный список на %s МСК — время со слов пользователя. "
                      "Автоматического снимка на этот момент нет." % reference_at.strftime("%d.%m %H:%M"),
                      "Часы проверены: %s МСК." % checked_at.strftime("%d.%m %H:%M")])
    else:
        snapshot_at = parse_datetime(report["snapshot_at"]).astimezone(start.tzinfo)
        lines.append("Снимок записи: %s · Проверка: %s" % (snapshot_at.strftime("%H:%M:%S"), checked_at.strftime("%d.%m %H:%M")))
    if report["preliminary"]:
        lines.append("⏳ Занятие ещё идёт — это предварительное сравнение.")
    if not report["attended"]:
        lines.append("⚠️ На сайте нет ни одной положительной отметки. Проверь, что часы сохранены.")

    def section(title, people, render=display_name):
        lines.extend(["", "%s — %s" % (title, len(people))])
        lines.extend("%s. %s" % (i, render(person)) for i, person in enumerate(people, 1))
        if not people:
            lines.append("—")

    section("📋 Исходный список со скриншотов" if imported else "📋 Были записаны за час", report["registered"])
    section("🚫 Исчезли из исходного списка, без часов" if imported else
            "🚫 Исчезли из списка после снимка, без часов", report["cancelled"])
    section("✅ Пришли, часы выставлены", report["attended"],
            lambda p: "%s — %s ч%s" % (display_name(p), format(p["hours"], "g"),
                                        (" · не было в исходном списке" if imported else " · не было в снимке")
                                        if not p["in_snapshot"] else ""))
    section("❌ Не пришли / остались без часов", report["no_hours"])
    if report["new_no_hours"]:
        section("➕ Не было в исходном списке, без часов" if imported else
                "➕ Появились после снимка, без часов", report["new_no_hours"])
    lines.extend(["", "Возможная отмена = человек был в исходном списке, но исчез из текущей таблицы и не получил часы. "
                  "Кто отменил — участник или тренер — сайт в этой таблице не сообщает.",
                  "«Не пришли» определено по 0 часам при сохранившейся записи. "
                  "Если человек был, но часы не сохранены, исправь их на сайте и повтори /check.",
                  "Отмену с повторной записью и отмену у человека с выставленными часами так обнаружить нельзя."])
    return "\n".join(lines)


def save_report(config: Config, report: dict) -> Path:
    from violation_registry import ViolationRegistry

    start, checked = parse_datetime(report["training_start"]), parse_datetime(report["checked_at"])
    directory = config.root / "reports" / (config.job_key(start) + "_training-%s_check_%s" % (
        report["training_id"], checked.strftime("%Y%m%dT%H%M%S%f")))
    private_dir(config.root / "reports")
    private_dir(directory)
    write_json(directory / "report.json", report)
    atomic_write(directory / "report.txt", text_report(report) + "\n")
    ViolationRegistry(config).record(report)
    return directory
