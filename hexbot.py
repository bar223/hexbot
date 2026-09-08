# -*- coding: utf-8 -*-
"""
Telegram-бот: по координатам начальной соты (широта,долгота) и числу "кругов"
рассчитывает координаты всех сот вокруг и присылает их в формате:

    001- 59.711182,27.123757

Геометрия:
  Соты — шестиугольники с остриём вверх (pointy-top), плотно заполняющие плоскость.
  У каждой соты 6 соседей. Сдвиги координат на один шаг:
    E  (вправо):  dlon = +0.000464, dlat = 0
    W  (влево):   dlon = -0.000464, dlat = 0
    NE:           dlon = +0.000232, dlat = +0.000227
    NW:           dlon = -0.000232, dlat = +0.000227
    SE:           dlon = +0.000232, dlat = -0.000227
    SW:           dlon = -0.000232, dlat = -0.000227

  Любую соту адресуем парой целых (q, r) в осевых координатах:
    lon = lon0 + q*0.000464 + r*0.000232
    lat = lat0 +              r*0.000227
  Hex-расстояние от центра = (|q| + |r| + |q+r|) / 2 — это и есть номер "круга".

  Ограничение по кругу:
  Круг (кольцо) сот — не окружность, у него есть "углы" (дальше от центра)
  и "рёбра" (ближе). Чтобы итоговая область была ближе к кругу, а не к
  звезде, самый внешний запрошенный уровень обрезается по радиусу: за
  радиус берётся расстояние до самой удалённой соты предпоследнего уровня,
  и соты последнего уровня, которые дальше этого радиуса, отбрасываются.
  Максимум доступно 10 уровней.
"""

import logging
import math
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --- Параметры сетки (фиксированные сдвиги) ---------------------------------

DLON_E = 0.000464   # шаг по долготе при движении вправо/влево
DLON_D = 0.000232   # сдвиг по долготе при диагональном шаге (вверх/вниз)
DLAT_D = 0.000227   # сдвиг по широте при диагональном шаге (вверх/вниз)

DEFAULT_RINGS = 2   # число кругов по умолчанию
MAX_RINGS = 10       # максимально допустимое число кругов

# Клавиатура под сообщением с сотой
CONTROL_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("Продолжить ▶️", callback_data="next"),
    InlineKeyboardButton("Прервать ⏹", callback_data="stop"),
]])


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --- Расчёт сот --------------------------------------------------------------

def axial_offset(q: int, r: int):
    """Смещение соты (q, r) от центра в градусах: (dlon, dlat)."""
    dlon = q * DLON_E + r * DLON_D
    dlat = r * DLAT_D
    return dlon, dlat


def axial_to_coords(lat0: float, lon0: float, q: int, r: int):
    """Перевод осевых координат (q, r) в (широта, долгота)."""
    dlon, dlat = axial_offset(q, r)
    return lat0 + dlat, lon0 + dlon


def axial_distance_from_center(q: int, r: int) -> float:
    """
    "Правильное" расстояние от центра до соты (q, r) — по геометрии
    правильного шестиугольника (pointy-top, единый масштаб x/y), а не по
    сырым градусам lon/lat.

    Сырые смещения DLON_E/DLON_D/DLAT_D откалиброваны только для того,
    чтобы находить координаты центров сот на карте — они растягивают
    фигуру по долготе (DLON_E примерно вдвое больше DLAT_D), и сравнивать
    по ним расстояния между разными сотами нельзя: круг обрезки получится
    перекошенным. Поэтому здесь используется условная система координат
    правильного шестиугольника, где сота отстоит от соседей на равные
    расстояния по всем 6 направлениям.
    """
    x = math.sqrt(3) * (q + r / 2)
    y = 1.5 * r
    return math.hypot(x, y)


def hex_distance(q: int, r: int) -> int:
    """Hex-расстояние от центра (номер круга) для осевых координат (q, r)."""
    return (abs(q) + abs(r) + abs(q + r)) // 2


def clockwise_angle(q: int, r: int) -> float:
    """
    Азимут соты от центра по часовой стрелке, отсчёт от направления "вверх"
    (рост широты). Нужен для нумерации внутри круга:
      0 — точно сверху, дальше по часовой: вправо-вверх, вправо, вправо-вниз, ...
    """
    dlat = r * DLAT_D
    dlon = q * DLON_E + r * DLON_D
    return math.atan2(dlon, dlat) % (2 * math.pi)


def generate_hexes(lat0: float, lon0: float, rings: int):
    """
    Возвращает список (номер, уровень, широта, долгота) для сот в пределах
    `rings` кругов.

    Нумерация:
      1     — центральная сота;
      далее — круг за кругом (ближний первым);
      внутри круга — по часовой стрелке, начиная с верхней соты
      (если ровно сверху соты нет — с верхней-правой).
    Номер соты — это её порядковое место в полном наборе (как если бы
    самый внешний круг не обрезался по радиусу), поэтому обрезка не
    сдвигает номера у оставшихся сот.

    Самый внешний круг обрезается по окружности: радиус берётся как
    расстояние до самой удалённой соты предпоследнего круга, и соты
    последнего круга дальше этого радиуса отбрасываются (но учитываются
    при нумерации).
    """
    # собираем все соты, группируя по номеру круга
    by_ring = {}
    for q in range(-rings, rings + 1):
        r_min = max(-rings, -q - rings)
        r_max = min(rings, -q + rings)
        for r in range(r_min, r_max + 1):
            k = hex_distance(q, r)
            by_ring.setdefault(k, []).append((q, r))

    # радиус ограничения — по самой удалённой соте предпоследнего круга
    radius = None
    if rings >= 2:
        prev_ring = by_ring.get(rings - 1, [])
        if prev_ring:
            radius = max(axial_distance_from_center(q, r) for q, r in prev_ring)

    eps = 1e-9
    result = []
    number = 0
    for k in range(0, rings + 1):
        ring = by_ring.get(k, [])
        # внутри круга — по часовой стрелке от направления "вверх"
        ring.sort(key=lambda qr: clockwise_angle(*qr))
        for q, r in ring:
            # номер считаем по полному кругу, обрезка на него не влияет
            number += 1
            if k == rings and radius is not None:
                if axial_distance_from_center(q, r) > radius + eps:
                    continue
            lat, lon = axial_to_coords(lat0, lon0, q, r)
            result.append((number, k, lat, lon))
    return result


def format_output(hexes):
    """Форматирует соты в строки вида '(2) 015- 59.711182,27.123757'."""
    lines = []
    for number, level, lat, lon in hexes:
        lines.append(f"({level}) {number:03d}- {lat:.6f},{lon:.6f}")
    return lines


def parse_message(text: str):
    """
    Разбирает сообщение пользователя.
    Допустимые форматы:
        59.711182,27.123757
        59.711182,27.123757 3
        59.711182, 27.123757 3
    Возвращает (lat, lon, rings) или бросает ValueError.
    """
    text = text.strip().replace(";", " ")

    # отделяем возможное число кругов в конце (после пробела)
    rings = DEFAULT_RINGS
    parts = text.split()
    coord_part = text

    if len(parts) >= 2 and parts[-1].lstrip("+-").isdigit():
        rings = int(parts[-1])
        coord_part = " ".join(parts[:-1])

    coord_part = coord_part.replace(" ", "")
    if "," not in coord_part:
        raise ValueError("Координаты нужно вводить через запятую: широта,долгота")

    lat_str, lon_str = coord_part.split(",", 1)
    lat = float(lat_str)
    lon = float(lon_str)

    if rings < 0 or rings > MAX_RINGS:
        raise ValueError(f"Число кругов должно быть от 0 до {MAX_RINGS}")

    return lat, lon, rings


# --- Хендлеры Telegram -------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Пришли координаты начальной соты в формате:\n\n"
        "  широта,долгота\n\n"
        "Например: 59.711182,27.123757\n\n"
        "Можно добавить число кругов в конце через пробел:\n"
        "  59.711182,27.123757 3\n\n"
        f"Если число не указано — будет {DEFAULT_RINGS} круга. "
        f"Максимум — {MAX_RINGS}."
    )


async def send_next_cell(chat, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет следующую соту из сохранённого списка с кнопками управления."""
    cells = context.user_data.get("cells", [])
    pos = context.user_data.get("pos", 0)

    if pos >= len(cells):
        context.user_data.pop("cells", None)
        context.user_data.pop("pos", None)
        await chat.send_message("Готово ✅ Все соты отправлены.")
        return

    line = cells[pos]
    context.user_data["pos"] = pos + 1
    is_last = context.user_data["pos"] >= len(cells)

    # на последней соте кнопки уже не нужны
    await chat.send_message(line, reply_markup=None if is_last else CONTROL_KB)
    if is_last:
        context.user_data.pop("cells", None)
        context.user_data.pop("pos", None)
        await chat.send_message("Готово ✅ Все соты отправлены.")


async def handle_coords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        lat, lon, rings = parse_message(update.message.text)
    except ValueError as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return
    except Exception:
        await update.message.reply_text(
            "Не понял формат. Пример: 59.711182,27.123757 2"
        )
        return

    lines = format_output(generate_hexes(lat, lon, rings))

    context.user_data["cells"] = lines
    context.user_data["pos"] = 0

    await update.message.reply_text(
        f"Кругов: {rings}, всего сот: {len(lines)}.\n"
        "Соты приходят по одной — жми «Продолжить» для следующей."
    )
    await send_next_cell(update.effective_chat, context)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок «Продолжить» / «Прервать»."""
    query = update.callback_query
    await query.answer()

    # убираем кнопки с предыдущего сообщения, чтобы их нельзя было нажать дважды
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    if query.data == "stop":
        context.user_data.pop("cells", None)
        context.user_data.pop("pos", None)
        await query.message.reply_text("⏹ Вывод прерван.")
        return

    # query.data == "next"
    if "cells" not in context.user_data:
        await query.message.reply_text(
            "Сессия завершена. Пришли координаты заново."
        )
        return

    await send_next_cell(query.message.chat, context)


def main():
    # Токен читается из переменной окружения TELEGRAM_BOT_TOKEN
    # (задаётся в .env файле рядом с docker-compose.yml)
    TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not TOKEN:
        raise SystemExit(
            "Не задан токен бота. Укажите его в переменной окружения "
            "TELEGRAM_BOT_TOKEN (например, в файле .env)."
        )

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_coords))

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
