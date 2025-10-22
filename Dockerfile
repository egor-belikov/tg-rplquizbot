# Используем официальный образ Python
FROM python:3.10-slim

# --- ИСПРАВЛЕНИЕ 2 ---
# Сначала устанавливаем компилятор (build-essential),
# который нужен для 'python-Levenshtein' (из fuzzywuzzy)
RUN apt-get update && apt-get install -y build-essential

# Устанавливаем рабочую директорию в контейнере
WORKDIR /app

# Копируем файл с зависимостями
COPY requirements.txt requirements.txt

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта в контейнер
COPY . .

# --- ИСПРАВЛЕНИЕ 1 ---
# Используем 'python -m gunicorn' для надежного запуска
CMD ["python", "-m", "gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:8080", "server:app"]