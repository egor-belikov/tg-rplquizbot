# server.py

import os, csv, uuid, random, time, re, hmac, hashlib, json
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
from fuzzywuzzy import fuzz
from glicko2 import Player
from sqlalchemy.pool import NullPool
from urllib.parse import unquote

# --- Конфигурация для Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Необходимо установить переменную окружения TELEGRAM_BOT_TOKEN")

# Константы
PAUSE_BETWEEN_ROUNDS = 10
TYPO_THRESHOLD = 85

# Настройка Flask, SQLAlchemy
basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__)
app.config['SECRET_KEY'] = 'a_very_secret_key_that_is_long_and_secure'
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL.replace("postgres://", "postgresql://", 1)
else:
    # Fallback to SQLite for local development (will be ephemeral on Koyeb/Render)
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL or 'sqlite:///' + os.path.join(basedir, 'game.db')
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = { 'poolclass': NullPool }
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# --- Модель Базы Данных ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    telegram_id = db.Column(db.BigInteger, unique=True, nullable=False)
    nickname = db.Column(db.String(80), unique=True, nullable=False)
    rating = db.Column(db.Float, default=1500)
    rd = db.Column(db.Float, default=350)
    vol = db.Column(db.Float, default=0.06)
    games_played = db.Column(db.Integer, default=0, nullable=False)

# Создаем таблицы при старте приложения, если их нет
with app.app_context():
    db.create_all()

# Глобальные переменные для отслеживания состояния
active_games, open_games = {}, {}
lobby_sids = set()

# --- Вспомогательные функции ---

def broadcast_lobby_stats():
    """Отправляет всем статистику лобби (игроки в лобби/в игре)."""
    stats = {
        'players_in_lobby': len(lobby_sids),
        'players_in_game': sum(len(g['game'].players) for g in active_games.values())
    }
    socketio.emit('lobby_stats_update', stats)

def is_player_busy(sid):
    """Проверяет, занят ли игрок (в активной игре или создал открытую игру)."""
    # Проверяем активные игры
    for game_session in active_games.values():
        if any(p.get('sid') == sid for p in game_session['game'].players.values()):
            return True
    # Проверяем открытые игры (создатель)
    for open_game in open_games.values():
        if open_game['creator']['sid'] == sid:
            return True
    return False

def add_player_to_lobby(sid):
    """Добавляет игрока в лобби, если он не занят."""
    if is_player_busy(sid):
        print(f"[LOBBY] Игрок {sid} уже занят, не добавлен в лобби.")
        return
    lobby_sids.add(sid)
    print(f"[LOBBY] Игрок {sid} вошел в лобби.")
    broadcast_lobby_stats()

def remove_player_from_lobby(sid):
    """Удаляет игрока из лобби."""
    if sid in lobby_sids:
        lobby_sids.discard(sid)
        print(f"[LOBBY] Игрок {sid} вышел из лобби.")
        broadcast_lobby_stats()

def load_league_data(filename, league_name):
    """Загружает данные игроков и клубов из CSV для одной лиги."""
    clubs_data = {}
    try:
        with open(filename, mode='r', encoding='utf-8') as infile:
            reader = csv.reader(infile)
            for row in reader:
                if not row or len(row) < 2 or not row[0] or not row[1]: continue # Проверка на пустые строки/ячейки
                player_name_full, club_name = row[0].strip(), row[1].strip()
                # Извлекаем фамилию (последнее слово) как основной идентификатор
                primary_surname = player_name_full.split()[-1]
                aliases = {primary_surname}
                # Добавляем псевдонимы, если они есть
                if len(row) > 2:
                    for alias in row[2:]:
                        if alias.strip(): aliases.add(alias.strip())

                # Создаем нормализованные (lowercase, ё->е) версии для поиска
                valid_normalized_names = {a.lower().replace('ё', 'е') for a in aliases}

                player_object = {
                    'full_name': player_name_full,
                    'primary_name': primary_surname,
                    'valid_normalized_names': valid_normalized_names
                }
                if club_name not in clubs_data: clubs_data[club_name] = []
                clubs_data[club_name].append(player_object)
        print(f"[DATA] Данные для лиги '{league_name}' успешно загружены из {filename}.")
        return {league_name: clubs_data}
    except FileNotFoundError:
        print(f"[CRITICAL ERROR] Файл {filename} не найден! Не удалось загрузить данные лиги '{league_name}'.")
        return {}
    except Exception as e:
        print(f"[CRITICAL ERROR] Ошибка при загрузке {filename} для лиги '{league_name}': {e}")
        return {}


# Загрузка данных при старте
all_leagues_data = {}
all_leagues_data.update(load_league_data('players.csv', 'РПЛ'))
# Сюда можно добавить загрузку других лиг из других файлов

# --- Функции Рейтинга ---

def update_ratings(p1_user_obj, p2_user_obj, p1_outcome):
    """
    Обновляет Glicko-2 рейтинги двух игроков и сохраняет в БД.
    p1_outcome: 1.0 (p1 победил), 0.0 (p1 проиграл), 0.5 (ничья)
    ВАЖНО: Эта функция должна вызываться внутри app_context.
    """
    p1 = Player(rating=p1_user_obj.rating, rd=p1_user_obj.rd, vol=p1_user_obj.vol)
    p2 = Player(rating=p2_user_obj.rating, rd=p2_user_obj.rd, vol=p2_user_obj.vol)

    p2_outcome = 1.0 - p1_outcome

    p1_old_rating_for_calc = p1.rating
    p2_old_rating_for_calc = p2.rating
    p1_old_rd_for_calc = p1.rd
    p2_old_rd_for_calc = p2.rd

    p1.update_player([p2_old_rating_for_calc], [p2_old_rd_for_calc], [p1_outcome])
    p2.update_player([p1_old_rating_for_calc], [p1_old_rd_for_calc], [p2_outcome])

    # Обновляем данные в объектах SQLAlchemy
    p1_user_obj.rating = p1.rating
    p1_user_obj.rd = p1.rd
    p1_user_obj.vol = p1.vol

    p2_user_obj.rating = p2.rating
    p2_user_obj.rd = p2.rd
    p2_user_obj.vol = p2.vol

    # Коммитим изменения в базу
    try:
        db.session.commit()
        print(f"[RATING] Рейтинги обновлены. {p1_user_obj.nickname} ({p1_outcome}) vs {p2_user_obj.nickname} ({p2_outcome})")
    except Exception as e:
        db.session.rollback()
        print(f"[ERROR] Ошибка при сохранении обновленных рейтингов: {e}")


def get_leaderboard_data():
    """Собирает данные для таблицы лидеров."""
    try:
        with app.app_context():
            users = User.query.order_by(User.rating.desc()).limit(100).all()
            leaderboard = [
                {
                    'nickname': user.nickname,
                    'rating': int(user.rating),
                    'games_played': user.games_played
                }
                for user in users
            ]
        return leaderboard
    except Exception as e:
        print(f"[ERROR] Ошибка при получении данных для лидерборда: {e}")
        return []

# --- Класс Состояния Игры ---
class GameState:
    """Хранит и управляет состоянием одной игровой сессии."""
    def __init__(self, player1_info, all_leagues, player2_info=None, mode='solo', settings=None):
        self.mode = mode
        self.players = {0: player1_info} # Игрок 0 всегда есть
        if player2_info: self.players[1] = player2_info # Игрок 1 - опционально
        self.scores = {0: 0.0, 1: 0.0} # Инициализируем очки

        # Настройки игры
        temp_settings = settings or {}
        league = temp_settings.get('league', 'РПЛ') # Лига по умолчанию
        self.all_clubs_data = all_leagues.get(league, {}) # Данные клубов для выбранной лиги
        if not self.all_clubs_data:
            print(f"[WARNING] Данные для лиги '{league}' не найдены!")

        max_clubs_in_league = len(self.all_clubs_data)
        default_settings = { 'num_rounds': max_clubs_in_league, 'time_bank': 90.0, 'league': league }
        self.settings = settings or default_settings

        selected_clubs = self.settings.get('selected_clubs')
        num_rounds_setting = self.settings.get('num_rounds', 0)

        # Выбираем клубы для игры
        if selected_clubs and len(selected_clubs) > 0:
            # Режим "Выбрать вручную"
            valid_selected_clubs = [c for c in selected_clubs if c in self.all_clubs_data]
            self.game_clubs = random.sample(valid_selected_clubs, len(valid_selected_clubs))
            self.num_rounds = len(self.game_clubs)
        elif num_rounds_setting > 0:
            # Режим "Случайные клубы"
            available_clubs = list(self.all_clubs_data.keys())
            self.num_rounds = min(num_rounds_setting, len(available_clubs))
            self.game_clubs = random.sample(available_clubs, self.num_rounds)
        else:
            # Фолбэк (если настройки некорректны) - все клубы лиги
            print("[WARNING] Некорректные настройки клубов, выбраны все клубы лиги.")
            available_clubs = list(self.all_clubs_data.keys())
            self.num_rounds = len(available_clubs)
            self.game_clubs = random.sample(available_clubs, self.num_rounds)

        # Состояние текущего раунда
        self.current_round = -1 # Индекс текущего раунда (начнется с 0)
        self.current_player_index = 0 # Индекс игрока, чей ход (0 или 1)
        self.current_club_name = None # Название текущего клуба
        self.players_for_comparison = [] # Список объектов игроков {full_name, primary_name, valid_normalized_names} для текущего клуба
        self.named_players_full_names = set() # Множество full_name уже названных игроков (для быстрой проверки)
        self.named_players = [] # Список {full_name, name, by} названных игроков (для отправки клиенту)

        # История и таймеры
        self.round_history = [] # Список результатов каждого раунда
        self.end_reason = 'normal' # Причина завершения игры ('normal', 'unreachable_score', 'disconnect')
        self.last_successful_guesser_index = None # Кто последним угадал (для смены хода)
        self.previous_round_loser_index = None # Кто проиграл прошлый раунд (для смены хода)

        # Тайм-банки
        time_bank_setting = self.settings.get('time_bank', 90.0)
        self.time_banks = {0: time_bank_setting}
        if self.mode != 'solo': self.time_banks[1] = time_bank_setting
        self.turn_start_time = 0 # Время начала текущего хода (для вычета из тайм-банка)

    def start_new_round(self):
        """Начинает новый раунд или возвращает False, если игра окончена."""
        if self.is_game_over():
            return False

        self.current_round += 1

        # Определение, кто ходит первым в раунде
        if len(self.players) > 1: # Только для PvP
            if self.current_round == 0: # Первый раунд - случайно
                self.current_player_index = random.randint(0, 1)
            elif self.previous_round_loser_index is not None: # Ходит проигравший
                self.current_player_index = self.previous_round_loser_index
            elif self.last_successful_guesser_index is not None: # Ходит не тот, кто угадал последним
                self.current_player_index = 1 - self.last_successful_guesser_index
            else: # Фолбэк (если прошлый раунд - ничья, а последнего угадавшего нет - ???) - ходит по очереди
                self.current_player_index = self.current_round % 2
        else: # В соло-режиме всегда ходит игрок 0
            self.current_player_index = 0

        self.previous_round_loser_index = None # Сбрасываем флаг проигравшего

        # Сброс тайм-банков в начале каждого раунда
        time_bank_setting = self.settings.get('time_bank', 90.0)
        self.time_banks = {0: time_bank_setting}
        if self.mode != 'solo': self.time_banks[1] = time_bank_setting

        # Загрузка данных для нового клуба
        if self.current_round < len(self.game_clubs):
            self.current_club_name = self.game_clubs[self.current_round]
            player_objects = self.all_clubs_data.get(self.current_club_name, [])
            self.players_for_comparison = sorted(player_objects, key=lambda p: p['primary_name'])
        else:
            # Этого не должно происходить из-за is_game_over(), но на всякий случай
            print(f"[ERROR] Попытка начать раунд {self.current_round+1}, но клубы закончились ({len(self.game_clubs)}).")
            return False

        self.named_players_full_names = set()
        self.named_players = []
        return True

    def process_guess(self, guess):
        """Обрабатывает попытку угадать игрока."""
        guess_norm = guess.strip().lower().replace('ё', 'е')
        if not guess_norm: # Пустой ввод
            return {'result': 'not_found'}

        # 1. Точное совпадение с одним из валидных имен (и еще не назван)
        for player_data in self.players_for_comparison:
            if guess_norm in player_data['valid_normalized_names'] and player_data['full_name'] not in self.named_players_full_names:
                return {'result': 'correct', 'player_data': player_data}

        # 2. Проверка на опечатку (только если не было точного совпадения)
        best_match_player = None
        max_ratio = 0
        for player_data in self.players_for_comparison:
            # Пропускаем уже названных
            if player_data['full_name'] in self.named_players_full_names:
                continue
            # Сравниваем с основной фамилией
            primary_norm = player_data['primary_name'].lower().replace('ё', 'е')
            ratio = fuzz.ratio(guess_norm, primary_norm)
            if ratio > max_ratio:
                max_ratio = ratio
                best_match_player = player_data

        if max_ratio >= TYPO_THRESHOLD:
            return {'result': 'correct_typo', 'player_data': best_match_player}

        # 3. Проверка, был ли этот игрок уже назван (если совпадение было, но он в named_players_full_names)
        for player_data in self.players_for_comparison:
             if guess_norm in player_data['valid_normalized_names']:
                 # Мы сюда попадем, только если он уже был назван (первая проверка не прошла)
                 return {'result': 'already_named'}

        # 4. Не найдено
        return {'result': 'not_found'}

    def add_named_player(self, player_data, player_index):
        """Добавляет игрока в список названных и переключает ход (в PvP)."""
        self.named_players.append({'full_name': player_data['full_name'], 'name': player_data['primary_name'], 'by': player_index})
        self.named_players_full_names.add(player_data['full_name'])
        self.last_successful_guesser_index = player_index
        if self.mode != 'solo':
            self.switch_player()

    def switch_player(self):
        """Переключает индекс текущего игрока (только в PvP)."""
        if len(self.players) > 1:
            self.current_player_index = 1 - self.current_player_index

    def is_round_over(self):
        """Проверяет, названы ли все игроки в текущем раунде."""
        return len(self.named_players) == len(self.players_for_comparison)

    def is_game_over(self):
        """Проверяет, окончена ли игра (все раунды сыграны или счет недосягаемый)."""
        # Проверка на конец раундов
        if self.current_round >= (self.num_rounds - 1):
            self.end_reason = 'normal'
            return True

        # Проверка на недосягаемый счет (только PvP)
        if len(self.players) > 1:
            score_diff = abs(self.scores[0] - self.scores[1])
            rounds_left = self.num_rounds - (self.current_round + 1) # Раунды, которые ЕЩЕ НЕ начались
            if score_diff > rounds_left:
                self.end_reason = 'unreachable_score'
                return True
        return False

# --- Основная логика игры (циклы, ходы, раунды) ---

def get_game_state_for_client(game, room_id):
    """Собирает данные о состоянии игры для отправки клиенту."""
    return {
        'roomId': room_id,
        'mode': game.mode,
        'players': {i: {'nickname': p['nickname'], 'sid': p.get('sid')} for i, p in game.players.items()}, # Используем .get для sid
        'scores': game.scores,
        'round': game.current_round + 1, # Для пользователя нумерация с 1
        'totalRounds': game.num_rounds,
        'clubName': game.current_club_name,
        'namedPlayers': game.named_players,
        'fullPlayerList': [p['full_name'] for p in game.players_for_comparison], # Полный список для экрана Summary
        'currentPlayerIndex': game.current_player_index,
        'timeBanks': game.time_banks
    }

def start_next_human_turn(room_id):
    """Начинает ход текущего игрока, запускает таймер и отправляет обновление клиентам."""
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']

    game.turn_start_time = time.time() # Засекаем время начала хода
    turn_id = f"{room_id}_{game.current_round}_{len(game.named_players)}" # Уникальный ID хода
    game_session['turn_id'] = turn_id
    time_left = game.time_banks[game.current_player_index]

    current_player_nick = game.players[game.current_player_index]['nickname']
    print(f"[TURN] {room_id}: Ход для {current_player_nick} (Индекс: {game.current_player_index}, Время: {time_left:.1f}s)")

    if time_left > 0:
        # Запускаем фоновую задачу, которая сработает, если время выйдет
        socketio.start_background_task(turn_watcher, room_id, turn_id, time_left)
    else:
        # Если времени уже нет (маловероятно, но возможно), сразу завершаем ход
        print(f"[TURN_END] {room_id}: Время уже вышло для {current_player_nick}. Завершение хода.")
        on_timer_end(room_id)
        return # Важно выйти, чтобы не отправить 'turn_updated'

    # Отправляем обновленное состояние всем в комнате
    socketio.emit('turn_updated', get_game_state_for_client(game, room_id), room=room_id)

def turn_watcher(room_id, turn_id, time_limit):
    """Фоновая задача, ждет time_limit секунд и проверяет, актуален ли еще этот ход."""
    socketio.sleep(time_limit)
    game_session = active_games.get(room_id)
    # Если игра еще идет И ID хода совпадает (т.е. игрок не успел ответить), завершаем ход
    if game_session and game_session.get('turn_id') == turn_id:
        print(f"[TIMEOUT] {room_id}: Время вышло для хода {turn_id}.")
        on_timer_end(room_id)

def on_timer_end(room_id):
    """Обрабатывает окончание хода по тайм-ауту или сдаче."""
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']

    loser_index = game.current_player_index
    game.time_banks[loser_index] = 0.0 # Обнуляем тайм-банк проигравшего

    # Уведомляем клиентов, что таймер истек
    socketio.emit('timer_expired', {'playerIndex': loser_index, 'timeBanks': game.time_banks}, room=room_id)

    # Начисляем очки в PvP
    if game.mode != 'solo' and len(game.players) > 1:
        winner_index = 1 - loser_index
        game.scores[winner_index] += 1
        game.previous_round_loser_index = loser_index # Проигравший будет ходить первым в след. раунде
        game_session['last_round_winner_index'] = winner_index # Запоминаем победителя раунда для истории

    # Устанавливаем причину завершения раунда, если она еще не установлена (например, 'surrender')
    if not game_session.get('last_round_end_reason'):
        game_session['last_round_end_reason'] = 'timeout'

    # Запоминаем ник проигравшего для истории
    game_session['last_round_end_player_nickname'] = game.players[loser_index]['nickname']

    print(f"[ROUND_END] {room_id}: Раунд завершен из-за '{game_session['last_round_end_reason']}' игрока {game.players[loser_index]['nickname']}.")

    # Показываем итоги раунда и планируем следующий
    show_round_summary_and_schedule_next(room_id)

def start_game_loop(room_id):
    """Основной цикл игры: запускает новый раунд или завершает игру."""
    game_session = active_games.get(room_id)
    if not game_session:
        print(f"[ERROR] Попытка запустить цикл для несуществующей игры {room_id}")
        return
    game = game_session['game']

    if not game.start_new_round():
        # --- ИГРА ОКОНЧЕНА ---
        game_over_data = {
            'final_scores': game.scores,
            'players': {i: {'nickname': p['nickname']} for i, p in game.players.items()},
            'history': game.round_history,
            'mode': game.mode,
            'end_reason': game.end_reason,
            'rating_changes': None # Инициализируем
        }
        print(f"[GAME_OVER] {room_id}: Игра окончена. Причина: {game.end_reason}, Счет: {game.scores.get(0, 0)}-{game.scores.get(1, 0)}")

        # Возвращаем игроков в лобби (если они еще подключены)
        for player_index, player_info in game.players.items():
             # Проверяем, что это не бот и сокет еще активен
            if player_info.get('sid') and player_info['sid'] != 'BOT' and player_info['sid'] in socketio.server.sockets:
                 add_player_to_lobby(player_info['sid'])

        # Обновление рейтинга только для PvP
        if game.mode == 'pvp' and len(game.players) > 1:
            p1_id = None
            p2_id = None
            p1_old_rating = 1500 # Значения по умолчанию
            p2_old_rating = 1500

            # Получаем ID и старые рейтинги
            with app.app_context():
                p1_obj_query = User.query.filter_by(nickname=game.players[0]['nickname']).first()
                p2_obj_query = User.query.filter_by(nickname=game.players[1]['nickname']).first()

                if p1_obj_query and p2_obj_query:
                    p1_id = p1_obj_query.id
                    p2_id = p2_obj_query.id
                    p1_old_rating = int(p1_obj_query.rating)
                    p2_old_rating = int(p2_obj_query.rating)
                    print(f"[RATING_FETCH] {room_id}: Старые рейтинги: {p1_old_rating}, {p2_old_rating}")
                else:
                    print(f"[ERROR] {room_id}: Не удалось найти одного из игроков ({game.players[0]['nickname']}, {game.players[1]['nickname']}) в БД перед обновлением рейтинга.")

            # Если нашли обоих игроков, обновляем счетчики и рейтинги
            if p1_id and p2_id:
                # Обновляем счетчик игр
                with app.app_context():
                    # Перезапрашиваем объекты в новой сессии для обновления
                    p1_to_update = db.session.get(User, p1_id) # Используем get для большей надежности
                    p2_to_update = db.session.get(User, p2_id)
                    if p1_to_update and p2_to_update:
                        p1_to_update.games_played += 1
                        p2_to_update.games_played += 1
                        db.session.commit()
                        print(f"[STATS] {room_id}: Игрокам {p1_to_update.nickname} и {p2_to_update.nickname} засчитана игра.")
                    else:
                         print(f"[ERROR] {room_id}: Не удалось перезапросить игроков для обновления счетчика игр.")

                # Определяем исход для P1
                outcome = 0.5 # Ничья по умолчанию
                if game.scores[0] > game.scores[1]:
                    outcome = 1.0 # P1 победил
                elif game.scores[1] > game.scores[0]:
                    outcome = 0.0 # P1 проиграл

                # Обновляем рейтинги (функция сама делает commit)
                with app.app_context():
                    # Перезапрашиваем объекты снова, чтобы update_ratings работала с актуальными данными
                    p1_for_rating = db.session.get(User, p1_id)
                    p2_for_rating = db.session.get(User, p2_id)
                    if p1_for_rating and p2_for_rating:
                        update_ratings(p1_user_obj=p1_for_rating, p2_user_obj=p2_for_rating, p1_outcome=outcome)
                    else:
                        print(f"[ERROR] {room_id}: Не удалось перезапросить игроков для обновления рейтинга.")

                # Получаем НОВЫЕ рейтинги ПОСЛЕ обновления
                p1_new_rating = p1_old_rating # Значения по умолчанию
                p2_new_rating = p2_old_rating
                with app.app_context():
                    # Перезапрашиваем ИЗ БАЗЫ ДАННЫХ, чтобы получить 100% актуальные значения ПОСЛЕ commit'а
                    updated_p1 = db.session.get(User, p1_id)
                    updated_p2 = db.session.get(User, p2_id)
                    if updated_p1 and updated_p2:
                        p1_new_rating = int(updated_p1.rating)
                        p2_new_rating = int(updated_p2.rating)
                        print(f"[RATING_FETCH] {room_id}: Новые рейтинги ПОСЛЕ обновления: {p1_new_rating}, {p2_new_rating}")
                    else:
                        print(f"[ERROR] {room_id}: Не удалось получить обновленные рейтинги из БД.")

                # Формируем данные для клиента
                game_over_data['rating_changes'] = {
                    '0': {'nickname': game.players[0]['nickname'], 'old': p1_old_rating, 'new': p1_new_rating},
                    '1': {'nickname': game.players[1]['nickname'], 'old': p2_old_rating, 'new': p2_new_rating}
                }
                socketio.emit('leaderboard_data', get_leaderboard_data()) # Обновляем лидерборд для всех
            else:
                 print(f"[ERROR] {room_id}: Рейтинги НЕ обновлены из-за отсутствия одного из игроков в БД.")

        # Удаляем игру из активных
        if room_id in active_games:
            del active_games[room_id]
        broadcast_lobby_stats() # Обновляем статистику лобби

        # Отправляем итоги игры клиентам в комнате
        socketio.emit('game_over', game_over_data, room=room_id)
        return # Важно завершить функцию здесь

    # --- ИГРА ПРОДОЛЖАЕТСЯ, НОВЫЙ РАУНД ---
    print(f"[ROUND_START] {room_id}: Начинается раунд {game.current_round + 1}/{game.num_rounds}. Клуб: {game.current_club_name}.")
    socketio.emit('round_started', get_game_state_for_client(game, room_id), room=room_id)
    start_next_human_turn(room_id)

def show_round_summary_and_schedule_next(room_id):
    """Показывает итоги раунда и запускает таймер паузы перед следующим."""
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']

    p1_named_count = len([p for p in game.named_players if p['by'] == 0])
    p2_named_count = len([p for p in game.named_players if p.get('by') == 1]) if game.mode != 'solo' else 0

    round_result = {
        'club_name': game.current_club_name,
        'p1_named': p1_named_count,
        'p2_named': p2_named_count,
        'result_type': game_session.get('last_round_end_reason', 'completed'),
        'player_nickname': game_session.get('last_round_end_player_nickname', None),
        'winner_index': game_session.get('last_round_winner_index') # draw, 0 или 1
    }
    game.round_history.append(round_result)

    print(f"[SUMMARY] {room_id}: Раунд {game.current_round + 1} завершен. Итог: {round_result['result_type']}")

    # Сброс временных флагов раунда
    game_session['skip_votes'] = set()
    game_session['last_round_end_reason'] = None
    game_session['last_round_end_player_nickname'] = None
    game_session['last_round_winner_index'] = None

    summary_data = {
        'clubName': game.current_club_name,
        'fullPlayerList': [p['full_name'] for p in game.players_for_comparison],
        'namedPlayers': game.named_players,
        'players': {i: {'nickname': p['nickname']} for i, p in game.players.items()},
        'scores': game.scores,
        'mode': game.mode
    }
    socketio.emit('round_summary', summary_data, room=room_id)

    # Запускаем таймер паузы
    pause_id = f"pause_{room_id}_{game.current_round}"
    game_session['pause_id'] = pause_id
    socketio.start_background_task(pause_watcher, room_id, pause_id)

def pause_watcher(room_id, pause_id):
    """Фоновая задача, ждет паузу и запускает следующий раунд."""
    socketio.sleep(PAUSE_BETWEEN_ROUNDS)
    game_session = active_games.get(room_id)
    # Если игра еще идет и ID паузы совпадает (т.е. ее не прервали кнопкой "Skip")
    if game_session and game_session.get('pause_id') == pause_id:
        print(f"[GAME] {room_id}: Пауза окончена, запуск следующего раунда.")
        start_game_loop(room_id)

def get_lobby_data_list():
    """Собирает список открытых игр для лобби."""
    lobby_list = []
    with app.app_context():
        # Используем items() для итерации и копируем, чтобы избежать ошибок при удалении
        for room_id, game_info in list(open_games.items()):
            creator_user = User.query.filter_by(nickname=game_info['creator']['nickname']).first()
            if creator_user:
                settings_with_clubs = game_info['settings']
                selected_clubs = settings_with_clubs.get('selected_clubs', [])
                lobby_list.append({
                    'settings': settings_with_clubs,
                    'creator_nickname': creator_user.nickname,
                    'creator_rating': int(creator_user.rating),
                    'creator_sid': game_info['creator']['sid'],
                    'selected_clubs_names': selected_clubs # Передаем ID выбранных клубов
                })
            else:
                # Если создатель игры удалился из базы (маловероятно), удаляем игру
                print(f"[LOBBY CLEANUP] Пользователь {game_info['creator']['nickname']} не найден, удаляю его открытую игру {room_id}")
                if room_id in open_games: del open_games[room_id]

    return lobby_list

# --- Обработчики событий Socket.IO ---

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    print(f"[CONNECTION] Клиент подключился: {sid}")
    emit('auth_request') # Запрашиваем аутентификацию

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    print(f"[CONNECTION] Клиент отключился: {sid}")
    
    # Удаляем из лобби
    remove_player_from_lobby(sid)
    
    # Если он был создателем открытой игры, удаляем ее
    room_to_delete_from_lobby = next((rid for rid, g in open_games.items() if g['creator']['sid'] == sid), None)
    if room_to_delete_from_lobby:
        del open_games[room_to_delete_from_lobby]
        print(f"[LOBBY] Создатель {sid} отключился. Комната {room_to_delete_from_lobby} удалена.")
        socketio.emit('update_lobby', get_lobby_data_list()) # Обновляем список для всех
    
    # Если он был в активной игре, завершаем ее и засчитываем поражение
    game_to_terminate_id = None
    opponent_sid = None
    game_session_to_terminate = None
    disconnected_player_index = -1

    # Ищем игру, где был этот игрок
    for room_id, game_session in list(active_games.items()):
        game = game_session['game']
        idx = next((i for i, p in game.players.items() if p.get('sid') == sid), -1)
        if idx != -1:
            game_to_terminate_id = room_id
            game_session_to_terminate = game_session
            disconnected_player_index = idx
            # Ищем SID оппонента (если он есть и не бот)
            if len(game.players) > 1:
                opponent_index = 1 - idx
                if game.players[opponent_index].get('sid') and game.players[opponent_index]['sid'] != 'BOT':
                    opponent_sid = game.players[opponent_index]['sid']
            break # Нашли игру, выходим из цикла

    # Если нашли активную игру с отключившимся игроком
    if game_to_terminate_id and game_session_to_terminate:
        game = game_session_to_terminate['game']
        disconnected_player_nick = game.players[disconnected_player_index].get('nickname', 'Неизвестный')
        print(f"[DISCONNECT] Игрок {sid} ({disconnected_player_nick}) отключился от активной игры {game_to_terminate_id}. Игра прекращена.")

        # --- НАЧИСЛЕНИЕ ТЕХНИЧЕСКОГО ПОРАЖЕНИЯ (PvP) ---
        if game.mode == 'pvp' and opponent_sid:
            winner_index = 1 - disconnected_player_index
            loser_index = disconnected_player_index
            
            p1_for_rating = None
            p2_for_rating = None
            winner_id = None
            loser_id = None
            
            # Используем отдельный app_context для работы с БД
            with app.app_context():
                winner_obj = User.query.filter_by(nickname=game.players[winner_index]['nickname']).first()
                loser_obj = User.query.filter_by(nickname=game.players[loser_index]['nickname']).first()

                if winner_obj and loser_obj:
                    winner_id = winner_obj.id
                    loser_id = loser_obj.id

                    # Обновляем счетчик игр
                    winner_obj.games_played += 1
                    loser_obj.games_played += 1
                    db.session.commit() # Коммитим счетчик игр
                    print(f"[STATS] {game_to_terminate_id}: Засчитана игра из-за дисконнекта.")

                    # Обновляем рейтинг (победа для оставшегося)
                    # update_ratings сама делает commit
                    update_ratings(p1_user_obj=winner_obj if winner_index == 0 else loser_obj,
                                   p2_user_obj=loser_obj if winner_index == 0 else winner_obj,
                                   p1_outcome=1.0 if winner_index == 0 else 0.0)

                    # Отправляем обновленный лидерборд всем
                    socketio.emit('leaderboard_data', get_leaderboard_data())
                else:
                    print(f"[ERROR] {game_to_terminate_id}: Не удалось найти игроков ({game.players[winner_index]['nickname']}, {game.players[loser_index]['nickname']}) для тех. поражения.")

            # Уведомляем оставшегося игрока и возвращаем в лобби (если он еще онлайн)
            if opponent_sid in socketio.server.sockets:
                add_player_to_lobby(opponent_sid)
                emit('opponent_disconnected', {'message': f'Соперник ({disconnected_player_nick}) отключился. Вам засчитана победа.'}, room=opponent_sid)
                print(f"[GAME] {game_to_terminate_id}: Отправлено уведомление о победе {opponent_sid}.")
            else:
                 print(f"[GAME] {game_to_terminate_id}: Оставшийся игрок {opponent_sid} тоже отключился.")

        # Удаляем игру из активных
        if game_to_terminate_id in active_games:
            del active_games[game_to_terminate_id]
        broadcast_lobby_stats() # Обновляем статистику

# --- Логика аутентификации через Telegram ---

def validate_telegram_data(init_data_str):
    """Проверяет подлинность данных Telegram Web App InitData."""
    try:
        # Шаг 1: Декодируем URL-кодированные данные
        unquoted_data = unquote(init_data_str)
        # Шаг 2: Разбираем параметры &key=value
        params = sorted([p.split('=', 1) for p in unquoted_data.split('&')], key=lambda x: x[0])
        
        received_hash = ''
        data_to_check_list = []
        # Шаг 3: Формируем строку для проверки хеша (все параметры кроме 'hash', отсортированные)
        for key, value in params:
            if key == 'hash':
                received_hash = value
            else:
                data_to_check_list.append(f"{key}={value}")
        data_check_string = "\n".join(data_to_check_list)

        # Шаг 4: Генерируем секретный ключ из токена бота
        secret_key = hmac.new("WebAppData".encode(), TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
        # Шаг 5: Вычисляем хеш нашей строки с секретным ключом
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        # Шаг 6: Сравниваем хеши
        if calculated_hash == received_hash:
            # Если хеши совпали, извлекаем и возвращаем данные пользователя
            user_data_str = [p.split('=', 1)[1] for p in params if p[0] == 'user'][0]
            return json.loads(unquote(user_data_str)) # Пользовательские данные тоже могут быть URL-кодированы

        print(f"[AUTH ERROR] Хеши не совпали! Получен: {received_hash}, Вычислен: {calculated_hash}")
        return None
    except Exception as e:
        print(f"[AUTH ERROR] Ошибка валидации данных Telegram: {e}")
        return None

@socketio.on('login_with_telegram')
def handle_telegram_login(data):
    """Обрабатывает попытку входа через Telegram."""
    init_data = data.get('initData')
    if not init_data:
        emit('auth_status', {'success': False, 'message': 'Отсутствуют данные для аутентификации.'})
        return

    user_info = validate_telegram_data(init_data)
    if not user_info:
        emit('auth_status', {'success': False, 'message': 'Неверные данные аутентификации.'})
        return

    telegram_id = user_info.get('id')
    if not telegram_id:
        emit('auth_status', {'success': False, 'message': 'Не удалось получить Telegram ID.'})
        return

    with app.app_context():
        user = User.query.filter_by(telegram_id=telegram_id).first()
        if user:
            # Пользователь найден, логиним
            add_player_to_lobby(request.sid)
            emit('auth_status', {'success': True, 'nickname': user.nickname})
            emit('update_lobby', get_lobby_data_list()) # Отправляем новому игроку список игр
            print(f"[AUTH] Игрок {user.nickname} (TG ID: {telegram_id}) вошел через Telegram.")
        else:
            # Пользователь не найден, запрашиваем никнейм
            print(f"[AUTH] Новый пользователь (TG ID: {telegram_id}). Запрос никнейма.")
            emit('request_nickname', {'telegram_id': telegram_id})

@socketio.on('set_initial_username')
def handle_set_username(data):
    """Обрабатывает установку никнейма для нового пользователя."""
    nickname = data.get('nickname', '').strip()
    telegram_id = data.get('telegram_id')

    # Валидация
    if not telegram_id:
        emit('auth_status', {'success': False, 'message': 'Ошибка: отсутствует Telegram ID.'})
        return
    if not nickname or not re.match(r'^[a-zA-Z0-9_-]{3,20}$', nickname):
        emit('auth_status', {'success': False, 'message': 'Никнейм должен быть от 3 до 20 символов и содержать только латиницу, цифры, _ или -.'})
        return

    with app.app_context():
        # Проверяем, не занят ли никнейм
        if User.query.filter_by(nickname=nickname).first():
            emit('auth_status', {'success': False, 'message': 'Этот никнейм уже занят.'})
            return
        # Проверяем, нет ли уже пользователя с таким telegram_id (маловероятно, но все же)
        if User.query.filter_by(telegram_id=telegram_id).first():
             emit('auth_status', {'success': False, 'message': 'Пользователь с таким Telegram ID уже зарегистрирован.'})
             return

        # Создаем нового пользователя
        try:
            new_user = User(telegram_id=telegram_id, nickname=nickname)
            db.session.add(new_user)
            db.session.commit()
            add_player_to_lobby(request.sid)
            print(f"[AUTH] Зарегистрирован новый игрок: {nickname} (TG ID: {telegram_id})")
            emit('auth_status', {'success': True, 'nickname': new_user.nickname})
            emit('update_lobby', get_lobby_data_list()) # Отправляем ему список игр
        except Exception as e:
            db.session.rollback()
            print(f"[ERROR] Ошибка при создании пользователя {nickname}: {e}")
            emit('auth_status', {'success': False, 'message': 'Ошибка при регистрации. Попробуйте позже.'})


# --- Обработчики игровых действий ---

@socketio.on('request_skip_pause')
def handle_request_skip_pause(data):
    """Обрабатывает запрос на пропуск паузы между раундами."""
    room_id = data.get('roomId')
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']

    if game.mode == 'solo':
        # В соло-режиме пропускаем сразу
        if game_session.get('pause_id'): # Проверяем, что пауза еще идет
            print(f"[GAME] {room_id}: Пропуск паузы (соло).")
            game_session['pause_id'] = None # Отменяем таймер паузы
            start_game_loop(room_id) # Запускаем следующий раунд немедленно
    elif game.mode == 'pvp':
        # В PvP нужен голос второго игрока
        player_index = next((i for i, p in game.players.items() if p.get('sid') == request.sid), -1)
        if player_index != -1 and game_session.get('pause_id'): # Голосуем, только если пауза активна
            game_session['skip_votes'].add(player_index)
            emit('skip_vote_accepted') # Подтверждаем голос этому игроку
            # Сообщаем всем в комнате, сколько голосов
            socketio.emit('skip_vote_update', {'count': len(game_session['skip_votes'])}, room=room_id)
            # Если все проголосовали
            if len(game_session['skip_votes']) >= len(game.players):
                print(f"[GAME] {room_id}: Пропуск паузы (PvP, все голоса).")
                game_session['pause_id'] = None # Отменяем таймер паузы
                start_game_loop(room_id) # Запускаем следующий раунд

@socketio.on('get_leaderboard')
def handle_get_leaderboard():
    """Отправляет данные для таблицы лидеров запросившему клиенту."""
    emit('leaderboard_data', get_leaderboard_data())

@socketio.on('get_league_clubs')
def handle_get_league_clubs(data):
    """Отправляет список клубов для выбранной лиги."""
    league_name = data.get('league', 'РПЛ')
    league_data = all_leagues_data.get(league_name, {})
    club_list = sorted(list(league_data.keys()))
    emit('league_clubs_data', {'league': league_name, 'clubs': club_list})

@socketio.on('start_game')
def handle_start_game(data):
    """Начинает игру (тренировку или PvP после join)."""
    sid = request.sid
    mode = data.get('mode')
    nickname = data.get('nickname')
    settings = data.get('settings')

    if not nickname:
         print(f"[ERROR] Попытка начать игру без никнейма от {sid}")
         # Можно отправить ошибку клиенту
         return
    if is_player_busy(sid):
        print(f"[SECURITY] {nickname} ({sid}) уже занят, старт игры отклонен.")
        # Можно отправить уведомление клиенту
        return

    if mode == 'solo':
        player1_info_full = {'sid': sid, 'nickname': nickname}
        room_id = str(uuid.uuid4())
        join_room(room_id) # Добавляем игрока в комнату Socket.IO
        try:
            game = GameState(player1_info_full, all_leagues_data, mode='solo', settings=settings)
            active_games[room_id] = {'game': game, 'turn_id': None, 'pause_id': None, 'skip_votes': set(), 'last_round_end_reason': None}
            remove_player_from_lobby(sid) # Убираем из лобби
            broadcast_lobby_stats() # Обновляем счетчики
            print(f"[GAME] {nickname} начал тренировку. Комната: {room_id}. Клубов: {game.num_rounds}")
            start_game_loop(room_id) # Запускаем игру
        except Exception as e:
            print(f"[ERROR] Ошибка при создании соло-игры для {nickname}: {e}")
            leave_room(room_id)
            if room_id in active_games: del active_games[room_id]
            add_player_to_lobby(sid) # Возвращаем в лобби в случае ошибки

@socketio.on('create_game')
def handle_create_game(data):
    """Создает открытую PvP игру и добавляет ее в лобби."""
    sid = request.sid
    nickname = data.get('nickname')
    settings = data.get('settings')

    if not nickname:
         print(f"[ERROR] Попытка создать игру без никнейма от {sid}")
         return
    if is_player_busy(sid):
        print(f"[SECURITY] {nickname} ({sid}) уже занят, создание игры отклонено.")
        return

    room_id = str(uuid.uuid4())
    join_room(room_id) # Создатель сразу входит в комнату
    open_games[room_id] = {'creator': {'sid': sid, 'nickname': nickname}, 'settings': settings}
    remove_player_from_lobby(sid) # Создатель выходит из лобби
    print(f"[LOBBY] {nickname} ({sid}) создал комнату {room_id}. Настройки: {settings}")
    socketio.emit('update_lobby', get_lobby_data_list()) # Обновляем список для всех

@socketio.on('cancel_game')
def handle_cancel_game():
    """Отменяет созданную игру (только создатель)."""
    sid = request.sid
    room_to_delete = next((rid for rid, g in open_games.items() if g['creator']['sid'] == sid), None)
    if room_to_delete:
        leave_room(room_to_delete, sid=sid) # Выходим из комнаты Socket.IO
        del open_games[room_to_delete]
        add_player_to_lobby(sid) # Возвращаемся в лобби
        print(f"[LOBBY] Создатель {sid} отменил игру. Комната {room_to_delete} удалена.")
        socketio.emit('update_lobby', get_lobby_data_list()) # Обновляем список для всех

@socketio.on('join_game')
def handle_join_game(data):
    """Присоединяет второго игрока к открытой игре."""
    joiner_sid = request.sid
    joiner_nickname = data.get('nickname')
    creator_sid = data.get('creator_sid')

    if not joiner_nickname or not creator_sid:
        print(f"[ERROR] Некорректный запрос на присоединение: {data} от {joiner_sid}")
        return
    if is_player_busy(joiner_sid):
         print(f"[SECURITY] {joiner_nickname} ({joiner_sid}) уже занят, присоединение отклонено.")
         return

    # Ищем игру по SID создателя
    room_id_to_join = next((rid for rid, g in open_games.items() if g['creator']['sid'] == creator_sid), None)

    if not room_id_to_join:
        print(f"[LOBBY] {joiner_nickname} не смог присоединиться к {creator_sid}. Комната не найдена (возможно, уже началась или отменена).")
        # Можно отправить уведомление клиенту
        emit('join_game_fail', {'message': 'Игра не найдена или уже началась.'})
        return

    # Забираем игру из открытых
    game_to_join = open_games.pop(room_id_to_join)
    socketio.emit('update_lobby', get_lobby_data_list()) # Обновляем список у всех

    creator_info = game_to_join['creator']

    # Не даем создателю присоединиться к своей же игре
    if creator_info['sid'] == joiner_sid:
        print(f"[SECURITY] {joiner_nickname} попытался присоединиться к своей игре {room_id_to_join}.")
        open_games[room_id_to_join] = game_to_join # Возвращаем игру обратно
        socketio.emit('update_lobby', get_lobby_data_list())
        return

    # Формируем информацию об игроках
    # Индекс 0 - создатель, Индекс 1 - присоединившийся
    p1_info_full = {'sid': creator_info['sid'], 'nickname': creator_info['nickname']}
    p2_info_full = {'sid': joiner_sid, 'nickname': joiner_nickname}

    # Добавляем второго игрока в комнату Socket.IO
    join_room(room_id_to_join, sid=p2_info_full['sid'])

    # Убираем второго игрока из лобби (создатель уже убран)
    remove_player_from_lobby(p2_info_full['sid'])

    # Создаем состояние игры
    try:
        game = GameState(p1_info_full, all_leagues_data, player2_info=p2_info_full, mode='pvp', settings=game_to_join['settings'])
        active_games[room_id_to_join] = {'game': game, 'turn_id': None, 'pause_id': None, 'skip_votes': set(), 'last_round_end_reason': None}
        broadcast_lobby_stats() # Обновляем счетчики
        print(f"[GAME] Начинается PvP игра: {p1_info_full['nickname']} vs {p2_info_full['nickname']}. Комната: {room_id_to_join}. Клубов: {game.num_rounds}")
        start_game_loop(room_id_to_join) # Запускаем игру
    except Exception as e:
         print(f"[ERROR] Ошибка при создании PvP-игры {room_id_to_join}: {e}")
         # Возвращаем игроков в лобби в случае ошибки
         leave_room(room_id_to_join, sid=p1_info_full['sid'])
         leave_room(room_id_to_join, sid=p2_info_full['sid'])
         if room_id_to_join in active_games: del active_games[room_id_to_join]
         add_player_to_lobby(p1_info_full['sid'])
         add_player_to_lobby(p2_info_full['sid'])


@socketio.on('submit_guess')
def handle_submit_guess(data):
    """Обрабатывает отправку ответа игроком."""
    room_id = data.get('roomId')
    guess = data.get('guess')
    sid = request.sid

    game_session = active_games.get(room_id)
    if not game_session: return # Игра не найдена
    game = game_session['game']

    # Проверяем, ход ли этого игрока
    if game.players[game.current_player_index].get('sid') != sid:
        print(f"[SECURITY] {sid} попытался сделать ход вне своей очереди в {room_id}.")
        return

    result = game.process_guess(guess)
    current_player_nick = game.players[game.current_player_index]['nickname']
    print(f"[GUESS] {room_id}: {current_player_nick} угадывает '{guess}'. Результат: {result['result']}")

    if result['result'] in ['correct', 'correct_typo']:
        time_spent = time.time() - game.turn_start_time
        game_session['turn_id'] = None # Сбрасываем ID текущего хода, таймер больше не нужен
        
        # Вычитаем время
        game.time_banks[game.current_player_index] -= time_spent
        if game.time_banks[game.current_player_index] < 0:
            print(f"[TIMEOUT] {room_id}: {current_player_nick} угадал, но время вышло ({game.time_banks[game.current_player_index]:.1f}s).")
            on_timer_end(room_id) # Засчитываем таймаут, если время ушло в минус
            return

        # Добавляем игрока в список и передаем ход (если PvP)
        game.add_named_player(result['player_data'], game.current_player_index)
        
        # Отправляем результат угадывания ТОЛЬКО угадавшему игроку
        emit('guess_result', {'result': result['result'], 'corrected_name': result['player_data']['full_name']})

        # Проверяем, закончился ли раунд
        if game.is_round_over():
            print(f"[ROUND_END] {room_id}: Раунд завершен (все названы). Ничья 0.5-0.5")
            game_session['last_round_end_reason'] = 'completed'
            if game.mode == 'pvp':
                game.scores[0] += 0.5
                game.scores[1] += 0.5
                game_session['last_round_winner_index'] = 'draw' # Ничья
            show_round_summary_and_schedule_next(room_id)
        else:
            # Если раунд не закончен, начинаем ход следующего игрока
            start_next_human_turn(room_id)
    else:
        # Если ответ неверный или уже был, сообщаем ТОЛЬКО этому игроку
        emit('guess_result', {'result': result['result']})

@socketio.on('surrender_round')
def handle_surrender(data):
    """Обрабатывает сдачу раунда текущим игроком."""
    room_id = data.get('roomId')
    sid = request.sid

    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']

    # Проверяем, ход ли этого игрока
    if game.players[game.current_player_index].get('sid') != sid:
        print(f"[SECURITY] {sid} попытался сдаться вне своей очереди в {room_id}.")
        return

    game_session['turn_id'] = None # Сбрасываем ID хода, таймер больше не нужен
    game_session['last_round_end_reason'] = 'surrender' # Устанавливаем причину
    # Ник проигравшего установится в on_timer_end
    
    surrendering_player_nick = game.players[game.current_player_index]['nickname']
    print(f"[ROUND_END] {room_id}: Игрок {surrendering_player_nick} сдался.")
    
    # Вызываем ту же логику, что и при тайм-ауте
    on_timer_end(room_id)

@app.route('/')
def index():
    """Отдает главную HTML страницу."""
    return render_template('index.html')

# Запуск через start.sh и Dockerfile, этот блок больше не нужен
# if __name__ == '__main__':
#     if not all_leagues_data:
#         print("КРИТИЧЕСКАЯ ОШИБКА: Не удалось загрузить данные игроков!")
#     else:
#         print("Сервер Flask-SocketIO запускается...")
#         # Используем Gunicorn или другой ASGI сервер в продакшене
#         socketio.run(app, debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))