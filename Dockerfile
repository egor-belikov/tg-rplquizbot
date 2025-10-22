# Используем официальный образ Python
FROM python:3.10-slim

# Устанавливаем компилятор (для python-Levenshtein)
RUN apt-get update && apt-get install -y build-essential

WORKDIR /app

# Копируем файл с зависимостями
COPY requirements.txt requirements.txt

# Устанавливаем зависимости
# УБЕРИ aiogram из requirements.txt перед деплоем!
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта (БЕЗ bot.py и start.sh!)
COPY . .

# Запускаем веб-сервер игры
CMD ["python", "-m", "gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:8000", "server:app"]