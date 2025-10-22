# Используем официальный образ Python
FROM python:3.10-slim

# Устанавливаем компилятор (для python-Levenshtein)
RUN apt-get update && apt-get install -y build-essential

# --- ИСПРАВЛЕНИЕ 1: Отключаем буферизацию Python ---
# Говорим Python выводить print() сразу, без накопления
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt requirements.txt
# Убедись, что aiogram УДАЛЕН из requirements.txt для этого сервиса!
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# --- НЕ НУЖНО, УБРАЛИ BOT.PY ---
# RUN chmod +x /app/start.sh

# --- ИСПРАВЛЕНИЕ 2: Настраиваем логи Gunicorn ---
# Добавляем флаги --access-logfile - и --error-logfile -
# Дефис (-) означает вывод в stdout/stderr
CMD ["python", "-m", "gunicorn", \
     "--worker-class", "eventlet", \
     "-w", "1", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "server:app"]

# --- СТАРЫЙ CMD (для справки) ---
# CMD ["/app/start.sh"] # Мы больше не запускаем start.sh
# CMD ["python", "-m", "gunicorn", "--worker-class", "eventlet", "-w", "1", "--bind", "0.0.0.0:8000", "server:app"] # Старая версия без логов