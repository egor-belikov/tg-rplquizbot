# Используем официальный образ Python
FROM python:3.10-slim

# Устанавливаем компилятор (для python-Levenshtein)
RUN apt-get update && apt-get install -y build-essential

WORKDIR /app

# Копируем файл с зависимостями
COPY requirements.txt requirements.txt

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта в контейнер
COPY . .

# --- ИСПРАВЛЕНИЕ ---
# Даем права на выполнение нашему скрипту
# (Это дублирует команду git, но так надежнее)
RUN chmod +x /app/start.sh

# Запускаем наш главный скрипт
CMD ["/app/start.sh"]