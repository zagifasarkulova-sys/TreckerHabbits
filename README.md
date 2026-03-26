# 🎯 Трекер Привычек — Telegram Bot

Telegram-бот для отслеживания привычек с прогрессией и напоминаниями.

## Возможности

- ➕ Добавление привычек с автоувеличением цели
- ✅ Ежедневная отметка: Сделано / Не сделано / Пропуск
- 📊 Статистика и streak
- 🔔 Спам-напоминания с 21:00 до 00:00 каждые 2 минуты

## Деплой на Render

### 1. GitHub
```bash
git init
git add .
git commit -m "habit tracker bot"
git remote add origin https://github.com/YOUR_USER/habit-tracker-bot.git
git push -u origin main
```

### 2. Render
1. Зайди на [render.com](https://render.com) → **New** → **Worker**
2. Подключи GitHub репозиторий
3. Runtime: **Docker**
4. Environment → добавь переменную:
   - **Key:** `BOT_TOKEN`
   - **Value:** токен от @BotFather
5. Disk → Add Disk:
   - **Name:** `habit-data`
   - **Mount Path:** `/data`
   - **Size:** 1 GB
6. **Create Worker**

Бот запустится автоматически!
