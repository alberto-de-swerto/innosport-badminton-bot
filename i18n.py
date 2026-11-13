"""Local Russian/English UI strings; participant data never passes through translation."""

import re

LANGUAGES = ("ru", "en")

HELP_RU = ("🏸 Бадминтон · Beginners\n\n"
           "/check — сравнить последнее начавшееся занятие\n"
           "/check YYYY-MM-DD — сравнить занятие за указанную дату\n"
           "/violations — счётчики поздних отмен, −1 и удаление\n"
           "/undo — отменить последнее списание или удаление\n"
           "/status — состояние снимков и ближайшая выгрузка\n"
           "/language — русский / English\n"
           "/help — эта справка\n\n"
           "После успешной плановой выгрузки автоматически приходит список записавшихся.\n\n"
           "Перед /check сохрани посещаемость и часы на спортсайте. "
           "Команда только читает данные; часы и записи на сайте не меняет.")
HELP_EN = ("🏸 Badminton · Beginners\n\n"
           "/check — compare the latest session that has started\n"
           "/check YYYY-MM-DD — compare a session on a specific date\n"
           "/violations — late cancellation counters, −1 and removal\n"
           "/undo — undo the latest counter decrease or removal\n"
           "/status — snapshots and the next scheduled export\n"
           "/language — English / русский\n"
           "/help — this help message\n\n"
           "After each successful scheduled export, the bot sends the saved roster automatically.\n\n"
           "Save attendance hours on the sports website before /check. "
           "The command only reads data; it does not change hours or registrations on the website.")

EN = {
    "Сравнить снимок записи и посещаемость": "Compare the saved roster and attendance",
    "Поздние отмены и удаление из журнала": "Late cancellations and register controls",
    "Отменить последнее списание или удаление": "Undo the latest counter decrease or removal",
    "Снимки и ближайшая выгрузка": "Snapshots and the next scheduled export",
    "Как пользоваться ботом": "How to use the bot",
    "Язык сообщений": "Message language",
    "Язык переключён на русский.": "Language switched to English.",
    "Формат: /language, /language ru или /language en.": "Usage: /language, /language ru or /language en.",
    "Не удалось сохранить язык. Попробуй ещё раз.": "Could not save the language. Please try again.",
    "✅ Список за час до тренировки успешно сохранён": "✅ The roster one hour before practice was saved successfully",
    "🏸 Beginners · %s МСК": "🏸 Beginners · %s MSK",
    "Выгрузка: %s МСК": "Exported: %s MSK",
    "Планируют прийти: %s": "Planning to attend: %s",
    "Пока никто не записан.": "Nobody has registered yet.",
    "После занятия сохрани часы на сайте и отправь /check.": "After practice, save attendance hours on the website and send /check.",
    "📒 Журнал поздних отмен и удаление записей: /violations": "📒 Late cancellation register and removal: /violations",
    "🏸 Beginners · состояние": "🏸 Beginners · status",
    "Бот принимает команды.": "The bot is accepting commands.",
    "Ближайшая выгрузка: %s МСК.": "Next scheduled export: %s MSK.",
    "неизвестно": "unknown",
    "✅ снимок сохранён": "✅ snapshot saved",
    "❌ снимок пропущен": "❌ snapshot missed",
    "⏳ повторяем выгрузку": "⏳ retrying export",
    "⚠️ Не удалось прочитать журнал снимков.": "⚠️ Could not read the snapshot log.",
    "Mac должен быть включён и не спать. Сессию сайта эта команда не проверяет.": "The Mac must be on and awake. This command does not check the website session.",
    "📒 Beginners · поздние отмены": "📒 Beginners · late cancellations",
    "Участников: %s · случаев: %s": "Participants: %s · cases: %s",
    "Страница %s/%s": "Page %s/%s",
    " · ещё %s": " · %s more",
    "Счётчик: %s · %s%s": "Counter: %s · %s%s",
    "🗑 Удалить все": "🗑 Remove all",
    "Список пуст. Записи добавляются после /check по завершённым занятиям.": "The list is empty. Cases are added after /check for completed sessions.",
    "← Назад": "← Back", "Далее →": "Next →",
    "↩️ Отменить последнее действие": "↩️ Undo the latest action",
    "Это возможные поздние отмены: были в снимке, затем исчезли без часов. Кто отменил запись, сайт не сообщает.": "These are possible late cancellations: present in the snapshot, then missing without hours. The website does not say who removed the registration.",
    "«−1» списывает самый ранний случай. При 0 участник исчезает из списка. «🗑 Удалить все» обнуляет счётчик. Повторный /check не вернёт списанное; новые отмены учитываются.": "−1 forgives the oldest case. At 0 the participant disappears. Remove all resets the counter. Repeating /check will not restore forgiven cases; new cancellations still count.",
    "* — случай со скриншотов, подтверждённый вручную. Остальные тесты и предварительные проверки не учитываются.": "* marks a screenshot case confirmed manually. Other tests and preliminary checks do not count.",
    "Отправь /check для сравнения или /status для состояния выгрузок.": "Send /check to compare attendance or /status to check scheduled exports.",
    "Команда пришла более 15 минут назад. Отправь её заново, чтобы проверить нужное занятие.": "This command is over 15 minutes old. Send it again to check the intended session.",
    "Отправь /violations без параметров. Страницы переключаются кнопками.": "Send /violations without arguments. Use the buttons to change pages.",
    "Отправь /undo без параметров — отменить последнее списание или удаление.": "Send /undo without arguments to undo the latest counter decrease or removal.",
    "Формат: /check или /check YYYY-MM-DD, например /check 2026-11-07.": "Usage: /check or /check YYYY-MM-DD, for example /check 2026-11-07.",
    "🔎 Сравниваю исходный список с сохранёнными на сайте часами…": "🔎 Comparing the original roster with attendance hours saved on the website…",
    "🧪 Тест по списку со скриншотов пользователя.": "🧪 Test based on a roster supplied in screenshots.",
    "Исходный список на %s МСК — время со слов пользователя. Автоматического снимка на этот момент нет.": "Original roster for %s MSK — time provided by the user. No automatic snapshot exists for that time.",
    "Часы проверены: %s МСК.": "Attendance hours checked: %s MSK.",
    "Снимок записи: %s · Проверка: %s": "Roster snapshot: %s · Checked: %s",
    "⏳ Занятие ещё идёт — это предварительное сравнение.": "⏳ Practice is still in progress — this is a preliminary comparison.",
    "⚠️ На сайте нет ни одной положительной отметки. Проверь, что часы сохранены.": "⚠️ No positive attendance hours are saved on the website. Check that the marks were saved.",
    "📋 Исходный список со скриншотов": "📋 Original roster from screenshots",
    "📋 Были записаны за час": "📋 Registered one hour before practice",
    "🚫 Исчезли из исходного списка, без часов": "🚫 Missing from the original roster, without hours",
    "🚫 Исчезли из списка после снимка, без часов": "🚫 Missing after the snapshot, without hours",
    "✅ Пришли, часы выставлены": "✅ Attendance hours credited",
    "%s — %s ч%s": "%s — %s h%s",
    " · не было в исходном списке": " · not in the original roster",
    " · не было в снимке": " · not in the snapshot",
    "❌ Не пришли / остались без часов": "❌ Absent / no hours credited",
    "➕ Не было в исходном списке, без часов": "➕ Not in the original roster, without hours",
    "➕ Появились после снимка, без часов": "➕ Added after the snapshot, without hours",
    "Возможная отмена = человек был в исходном списке, но исчез из текущей таблицы и не получил часы. Кто отменил — участник или тренер — сайт в этой таблице не сообщает.": "Possible cancellation means the person was in the original roster but is now missing and has no hours. This table does not say whether the participant or a trainer removed the registration.",
    "«Не пришли» определено по 0 часам при сохранившейся записи. Если человек был, но часы не сохранены, исправь их на сайте и повтори /check.": "Absent means zero hours with the registration still present. If the person attended but their hours were not saved, correct them on the website and repeat /check.",
    "Отмену с повторной записью и отмену у человека с выставленными часами так обнаружить нельзя.": "This cannot detect cancellation followed by re-registration, or cancellation by someone who has credited hours.",
    "Сессия спортсайта истекла или вход недействителен. Открой login.command и заново войди через SSO.": "The sports-site session expired or is invalid. Open login.command and sign in again through SSO.",
    "Спортсайт отказал в доступе к участникам. Проверь права тренера для Beginners.": "The sports website denied access to participants. Check trainer permissions for Beginners.",
    "Данные спортсайта не прошли проверку. Нужна проверка расписания, группы или формата ответа.": "Sports-site data failed validation. Check the timetable, group, or response format.",
    "Не удалось выгрузить список. Проверь интернет, VPN и доступ к спортсайту.": "Could not export the roster. Check the internet connection, VPN, and access to the sports website.",
    "⚠️ Не удалось прочитать журнал выгрузок. Проверь работу программы на Mac.": "⚠️ Could not read the export log. Check the program on your Mac.",
    "⚠️ Фоновый сборщик не прошёл проверку\n": "⚠️ The background exporter failed its startup check\n",
    "⚠️ Не удалось проверить состояние фонового сборщика. Проверь программу на Mac.": "⚠️ Could not check the background exporter. Check the program on your Mac.",
    "⚠️ Ошибка выгрузки · %s МСК\n%s\nПрограмма повторит попытку в допустимом окне.": "⚠️ Export failed · %s MSK\n%s\nThe program will retry within the allowed capture window.",
    "⚠️ Сохранённый снимок · %s МСК не прошёл проверку. Не используй его для выводов об отменах; проверь локальные файлы программы.": "⚠️ The saved snapshot for %s MSK failed validation. Do not use it to judge cancellations; check the local program files.",
    "🚨 Снимок за час пропущен · %s МСК\nДостоверного списка за час до начала нет. Проверь, что Mac включён, не спит, а программа имеет доступ к сайту. Восстановить такой снимок задним числом нельзя.": "🚨 One-hour snapshot missed · %s MSK\nNo reliable roster from one hour before practice exists. Check that the Mac is on and awake and the program can reach the website. A historical snapshot cannot be recreated.",
    "🏸 Beginners · занятие %s МСК завершилось\n\nСохрани посещаемость и часы на спортсайте, затем отправь:\n/check %s": "🏸 Beginners · practice at %s MSK has ended\n\nSave attendance hours on the sports website, then send:\n/check %s",
    "Запись уже удалена.": "The entry has already been removed.",
    "Список изменился. Проверь новые записи и нажми кнопку ещё раз.": "The list changed. Review the current entries and use the updated button.",
    "Счётчик: 0. Участник убран из списка.": "Counter: 0. The participant has been removed from the list.",
    "Нет действий для отмены. Можно отменить последние 20 списаний и удалений.": "There are no actions to undo. The latest 20 counter decreases and removals can be undone.",
    "Эта кнопка устарела. Используй /undo или новую кнопку под списком.": "This button is outdated. Use /undo or the latest button below the list.",
    "Не удалось сохранить журнал поздних отмен.": "Could not save the late cancellation register.",
    "Не удалось открыть журнал поздних отмен.": "Could not open the late cancellation register.",
    "Не удалось сохранить отмену действия.": "Could not save the undo operation.",
    "Не удалось прочитать журнал поздних отмен. Данные не перезаписаны.": "Could not read the late cancellation register. No data was overwritten.",
    "Проверка заняла слишком много времени. Повтори /check через минуту.": "The check timed out. Try /check again in a minute.",
    "Не удалось запустить сравнение. Проверь установку программы.": "Could not start the comparison. Check the program installation.",
    "Сравнение не завершилось. Проверь сессию спортсайта через login.command и повтори /check.": "The comparison did not finish. Refresh the sports-site session through login.command and repeat /check.",
    "На эту дату нет единственной тренировки Beginners в расписании.": "There is no single Beginners session scheduled for that date.",
    "Тренировка ещё не началась. Отправь /check после сохранения посещаемости на сайте.": "Practice has not started yet. Send /check after saving attendance on the website.",
    "Не найдена недавняя тренировка. Укажи дату: /check YYYY-MM-DD.": "No recent session was found. Specify a date: /check YYYY-MM-DD.",
    "Сессия истекла": "The website session expired",
}


def language_of(settings):
    value = settings.get("language", "ru")
    return value if value in LANGUAGES else "ru"


def tr(text, language="ru"):
    return EN.get(text, text) if language == "en" else text


def help_text(language="ru"):
    return HELP_EN if language == "en" else HELP_RU


NOTICE_PATTERNS = [
    (r"Удалено из журнала: (\d+)\. Новые отмены будут учитываться\.",
     lambda m: "Cases removed: %s. New cancellations will still count." % m[1]),
    (r"Счётчик: (\d+) → (\d+)\. Списан самый ранний случай\.",
     lambda m: "Counter: %s → %s. The oldest case was forgiven." % (m[1], m[2])),
    (r"↩️ Последнее действие отменено\. Восстановлено случаев: (\d+)\.(?: Уже исправлены на сайте и не восстановлены: (\d+)\.)?",
     lambda m: "↩️ Latest action undone. Cases restored: %s." % m[1]
     + (" Already corrected on the website and not restored: %s." % m[2] if m[2] else "")),
]


def localize_notice(text, language="ru"):
    if language != "en":
        return text
    if text in EN:
        return EN[text]
    for pattern, render in NOTICE_PATTERNS:
        match = re.fullmatch(pattern, text)
        if match:
            return render(match)
    return "The register was updated."


def error_text(error, language="ru"):
    message = str(error)
    if language != "en":
        return message
    if message in EN:
        return EN[message]
    if message.startswith("Нет планового снимка за час до тренировки "):
        date = re.search(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}", message)
        return ("No scheduled one-hour snapshot exists%s. Cancellations cannot be compared reliably; "
                "manual exports do not replace a scheduled snapshot." % (" for " + date[0] if date else ""))
    name = type(error).__name__
    if name in ("AuthError", "APIAuthError") or "login" in message or "Сессия" in message:
        return "The sports-site session is unavailable or expired. Open login.command, sign in through SSO, and try again."
    if name == "PermissionError":
        return "The sports website denied access. Check trainer permissions for Beginners."
    if name == "DataError":
        return "Saved or sports-site data failed validation. Check the selected session and local files; no absence conclusions were made."
    return "The request could not be completed. Check the website connection, VPN, and local program logs, then try again."
