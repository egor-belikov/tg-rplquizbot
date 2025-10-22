#!/bin/bash

# 1. Запускаем бота (bot.py) в фоновом режиме
echo "Starting bot worker..."
python bot.py &

# 2. Запускаем веб-сервер (server.py) на главном потоке
echo "Starting web service (gunicorn)..."
python -m gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:8000 server:app