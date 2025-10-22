# server.py

import os
import csv
import uuid
import random
import time
import re
import hmac
import hashlib
import json
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

with app.app_context():
    db.create_all()

# Глобальные переменные для отслеживания состояния
active_games, open_games = {}, {}
lobby_sids = set()

# --- Вспомогательные функции ---

def broadcast_lobby_stats():
    stats = {
        'players_in_lobby': len(lobby_sids),
        'players_in_game': sum(len(g['game'].players) for g in active_games.values())
    }
    socketio.emit('lobby_stats_update', stats)

def is_player_busy(sid):
    for game_session in active_games.values():
        if any(p.get('sid') == sid for p in game_session['game'].players.values()):
            return True
    for open_game in open_games.values():
        if open_game['creator']['sid'] == sid:
            return True
    return False

def add_player_to_lobby(sid):
    if is_player_busy(sid):
        # print(f"[LOBBY] Игрок {sid} уже занят, не добавлен в лобби.")
        return
    lobby_sids.add(sid)
    # print(f"[LOBBY] Игрок {sid} вошел в лобби.")
    broadcast_lobby_stats()

def remove_player_from_lobby(sid):
    if sid in lobby_sids:
        lobby_sids.discard(sid)
        # print(f"[LOBBY] Игрок {sid} вышел из лобби.")
        broadcast_lobby_stats()

def load_league_data(filename, league_name):
    clubs_data = {}
    try:
        with open(filename, mode='r', encoding='utf-8') as infile:
            reader = csv.reader(infile)
            for row in reader:
                if not row or len(row) < 2 or not row[0] or not row[1]: continue
                player_name_full, club_name = row[0].strip(), row[1].strip()
                primary_surname = player_name_full.split()[-1]
                aliases = {primary_surname}
                if len(row) > 2:
                    for alias in row[2:]:
                        if alias.strip(): aliases.add(alias.strip())
                valid_normalized_names = {a.lower().replace('ё', 'е') for a in aliases}
                player_object = {
                    'full_name': player_name_full, 'primary_name': primary_surname,
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

all_leagues_data = {}
all_leagues_data.update(load_league_data('players.csv', 'РПЛ'))

# --- ИСПРАВЛЕНИЕ: Функция больше не управляет сессией (контекстом/коммитом) ---
def update_ratings(p1_user_obj, p2_user_obj, p1_outcome):
    """
    Обновляет Glicko-2 рейтинги, МОДИФИЦИРУЕТ ОБЪЕКТЫ и ВОЗВРАЩАЕТ новые значения.
    p1_outcome: 1.0 (p1 победил), 0.0 (p1 проиграл), 0.5 (ничья)
    Возвращает: tuple (p1_new_rating, p2_new_rating) или None в случае ошибки.
    !!! ВНИМАНИЕ: Эта функция НЕ коммитит изменения. Вызывающая функция
    !!! должна сама управлять сессией и вызывать db.session.commit().
    """
    try:
        # КОНТЕКСТ УБРАН
        p1 = Player(rating=p1_user_obj.rating, rd=p1_user_obj.rd, vol=p1_user_obj.vol)
        p2 = Player(rating=p2_user_obj.rating, rd=p2_user_obj.rd, vol=p2_user_obj.vol)

        p2_outcome = 1.0 - p1_outcome

        p1_old_rating_for_calc = p1.rating
        p2_old_rating_for_calc = p2.rating
        p1_old_rd_for_calc = p1.rd
        p2_old_rd_for_calc = p2.rd

        p1.update_player([p2_old_rating_for_calc], [p2_old_rd_for_calc], [p1_outcome])
        p2.update_player([p1_old_rating_for_calc], [p1_old_rd_for_calc], [p2_outcome])

        # Обновляем данные в объектах SQLAlchemy (они из сессии)
        p1_user_obj.rating = p1.rating
        p1_user_obj.rd = p1.rd
        p1_user_obj.vol = p1.vol

        p2_user_obj.rating = p2.rating
        p2_user_obj.rd = p2.rd
        p2_user_obj.vol = p2.vol

        # КОММИТ УБРАН
        print(f"[RATING] Рейтинги обновлены (в объектах). {p1_user_obj.nickname} ({p1_outcome}) -> {int(p1.rating)} vs {p2_user_obj.nickname} ({p2_outcome}) -> {int(p2.rating)}")
        # Возвращаем новые целочисленные рейтинги
        return int(p1.rating), int(p2.rating)

    except Exception as e:
        # РОЛЛБЭК УБРАН
        print(f"[ERROR] Ошибка при расчете Glicko: {e}")
        return None
# --- КОНЕЦ ИСПРАВЛЕНИЯ ---

def get_leaderboard_data():
    """Собирает данные для таблицы лидеров."""
    try:
        with app.app_context():
            # Используем .with_entities() для оптимизации
            users_data = db.session.query(User.nickname, User.rating, User.games_played)\
                .order_by(User.rating.desc()).limit(100).all()
            leaderboard = [
                {
                    'nickname': nickname,
                    'rating': int(rating),
                    'games_played': games_played
                }
                for nickname, rating, games_played in users_data
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
        self.players = {0: player1_info}
        if player2_info: self.players[1] = player2_info
        self.scores = {0: 0.0, 1: 0.0}

        # --- ИЗМЕНЕНИЕ: Определение мин. клубов в зависимости от режима ---
        min_clubs = 1 if self.mode == 'solo' else 3
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---

        temp_settings = settings or {}
        league = temp_settings.get('league', 'РПЛ')
        self.all_clubs_data = all_leagues.get(league, {})
        if not self.all_clubs_data:
            print(f"[WARNING] Данные для лиги '{league}' не найдены!")

        max_clubs_in_league = len(self.all_clubs_data)
        default_settings = { 'num_rounds': max_clubs_in_league, 'time_bank': 90.0, 'league': league }
        self.settings = settings or default_settings

        selected_clubs = self.settings.get('selected_clubs')
        num_rounds_setting = self.settings.get('num_rounds', 0)

        if selected_clubs and len(selected_clubs) > 0:
            valid_selected_clubs = [c for c in selected_clubs if c in self.all_clubs_data]
            # --- ИЗМЕНЕНИЕ: Используем min_clubs ---
            if len(valid_selected_clubs) < min_clubs:
                 print(f"[WARNING] Недостаточно валидных клубов ({len(valid_selected_clubs)}) для режима {self.mode}. Мин: {min_clubs}. Используются все клубы.")
                 available_clubs = list(self.all_clubs_data.keys())
                 self.num_rounds = len(available_clubs)
                 self.game_clubs = random.sample(available_clubs, self.num_rounds)
            else:
                self.game_clubs = random.sample(valid_selected_clubs, len(valid_selected_clubs))
                self.num_rounds = len(self.game_clubs)
        # --- ИЗМЕНЕНИЕ: Используем min_clubs ---
        elif num_rounds_setting >= min_clubs:
            available_clubs = list(self.all_clubs_data.keys())
            self.num_rounds = min(num_rounds_setting, len(available_clubs))
            self.game_clubs = random.sample(available_clubs, self.num_rounds)
        else:
            # --- ИЗМЕНЕНИЕ: Используем min_clubs ---
            print(f"[WARNING] Некорректные настройки клубов (менее {min_clubs}), выбраны все клубы лиги.")
            available_clubs = list(self.all_clubs_data.keys())
            self.num_rounds = len(available_clubs)
            self.game_clubs = random.sample(available_clubs, self.num_rounds)

        self.current_round = -1
        self.current_player_index = 0
        self.current_club_name = None
        self.players_for_comparison = []
        self.named_players_full_names = set()
        self.named_players = []

        self.round_history = []
        self.end_reason = 'normal'
        self.last_successful_guesser_index = None
        self.previous_round_loser_index = None

        time_bank_setting = self.settings.get('time_bank', 90.0)
        self.time_banks = {0: time_bank_setting}
        if self.mode != 'solo': self.time_banks[1] = time_bank_setting
        self.turn_start_time = 0

    def start_new_round(self):
        """Начинает новый раунд или возвращает False."""
        if self.is_game_over(): return False
        self.current_round += 1
        if len(self.players) > 1:
            if self.current_round == 0: self.current_player_index = random.randint(0, 1)
            elif self.previous_round_loser_index is not None: self.current_player_index = self.previous_round_loser_index
            elif self.last_successful_guesser_index is not None: self.current_player_index = 1 - self.last_successful_guesser_index
            else: self.current_player_index = self.current_round % 2
        else: self.current_player_index = 0
        self.previous_round_loser_index = None
        time_bank_setting = self.settings.get('time_bank', 90.0)
        self.time_banks = {0: time_bank_setting}
        if self.mode != 'solo': self.time_banks[1] = time_bank_setting
        if self.current_round < len(self.game_clubs):
            self.current_club_name = self.game_clubs[self.current_round]
            player_objects = self.all_clubs_data.get(self.current_club_name, [])
            self.players_for_comparison = sorted(player_objects, key=lambda p: p['primary_name'])
        else: return False
        self.named_players_full_names = set()
        self.named_players = []
        return True

    def process_guess(self, guess):
        """Обрабатывает попытку угадать игрока."""
        guess_norm = guess.strip().lower().replace('ё', 'е')
        if not guess_norm: return {'result': 'not_found'}
        for player_data in self.players_for_comparison:
            if guess_norm in player_data['valid_normalized_names'] and player_data['full_name'] not in self.named_players_full_names:
                return {'result': 'correct', 'player_data': player_data}
        best_match_player, max_ratio = None, 0
        for player_data in self.players_for_comparison:
            if player_data['full_name'] in self.named_players_full_names: continue
            primary_norm = player_data['primary_name'].lower().replace('ё', 'е')
            ratio = fuzz.ratio(guess_norm, primary_norm)
            if ratio > max_ratio: max_ratio, best_match_player = ratio, player_data
        if max_ratio >= TYPO_THRESHOLD: return {'result': 'correct_typo', 'player_data': best_match_player}
        for player_data in self.players_for_comparison:
             if guess_norm in player_data['valid_normalized_names']: return {'result': 'already_named'}
        return {'result': 'not_found'}

    def add_named_player(self, player_data, player_index):
        """Добавляет игрока в список названных."""
        self.named_players.append({'full_name': player_data['full_name'], 'name': player_data['primary_name'], 'by': player_index})
        self.named_players_full_names.add(player_data['full_name'])
        self.last_successful_guesser_index = player_index
        if self.mode != 'solo': self.switch_player()

    def switch_player(self):
        """Переключает ход."""
        if len(self.players) > 1: self.current_player_index = 1 - self.current_player_index

    def is_round_over(self):
        """Проверяет, завершен ли раунд."""
        return len(self.players_for_comparison) > 0 and len(self.named_players) == len(self.players_for_comparison)

    def is_game_over(self):
        """Проверяет, завершена ли игра."""
        if self.current_round >= (self.num_rounds - 1): self.end_reason = 'normal'; return True
        if len(self.players) > 1:
            score_diff = abs(self.scores[0] - self.scores[1])
            rounds_left = self.num_rounds - (self.current_round + 1)
            if score_diff > rounds_left: self.end_reason = 'unreachable_score'; return True
        return False

# --- Основная логика игры ---

def get_game_state_for_client(game, room_id):
    """Собирает данные о состоянии игры для клиента."""
    return {
        'roomId': room_id, 'mode': game.mode,
        'players': {i: {'nickname': p['nickname'], 'sid': p.get('sid')} for i, p in game.players.items()},
        'scores': game.scores, 'round': game.current_round + 1, 'totalRounds': game.num_rounds,
        'clubName': game.current_club_name, 'namedPlayers': game.named_players,
        'fullPlayerList': [p['full_name'] for p in game.players_for_comparison],
        'currentPlayerIndex': game.current_player_index, 'timeBanks': game.time_banks
    }

def start_next_human_turn(room_id):
    """Начинает ход игрока."""
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']
    game.turn_start_time = time.time()
    turn_id = f"{room_id}_{game.current_round}_{len(game.named_players)}"
    game_session['turn_id'] = turn_id
    time_left = game.time_banks[game.current_player_index]
    current_player_nick = game.players[game.current_player_index]['nickname']
    print(f"[TURN] {room_id}: Ход для {current_player_nick} (Индекс: {game.current_player_index}, Время: {time_left:.1f}s)")
    if time_left > 0:
        socketio.start_background_task(turn_watcher, room_id, turn_id, time_left)
    else:
        print(f"[TURN_END] {room_id}: Время уже вышло для {current_player_nick}. Завершение хода.")
        on_timer_end(room_id)
        return
    socketio.emit('turn_updated', get_game_state_for_client(game, room_id), room=room_id)

def turn_watcher(room_id, turn_id, time_limit):
    """Фоновая задача, ждет и проверяет, актуален ли ход."""
    socketio.sleep(time_limit)
    game_session = active_games.get(room_id)
    if game_session and game_session.get('turn_id') == turn_id:
        print(f"[TIMEOUT] {room_id}: Время вышло для хода {turn_id}.")
        on_timer_end(room_id)

def on_timer_end(room_id):
    """Обрабатывает окончание хода по тайм-ауту или сдаче."""
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']
    loser_index = game.current_player_index
    game.time_banks[loser_index] = 0.0
    socketio.emit('timer_expired', {'playerIndex': loser_index, 'timeBanks': game.time_banks}, room=room_id)
    if game.mode != 'solo' and len(game.players) > 1:
        winner_index = 1 - loser_index
        game.scores[winner_index] += 1
        game.previous_round_loser_index = loser_index
        game_session['last_round_winner_index'] = winner_index
    if not game_session.get('last_round_end_reason'): game_session['last_round_end_reason'] = 'timeout'
    game_session['last_round_end_player_nickname'] = game.players[loser_index]['nickname']
    print(f"[ROUND_END] {room_id}: Раунд завершен из-за '{game_session['last_round_end_reason']}' игрока {game.players[loser_index]['nickname']}.")
    show_round_summary_and_schedule_next(room_id)

# --- ИСПРАВЛЕНИЕ: Управление сессией вынесено сюда, update_ratings больше не коммитит ---
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
            'rating_changes': None
        }
        print(f"[GAME_OVER] {room_id}: Игра окончена. Причина: {game.end_reason}, Счет: {game.scores.get(0, 0)}-{game.scores.get(1, 0)}")

        for player_index, player_info in game.players.items():
            if player_info.get('sid') and player_info['sid'] != 'BOT' and socketio.server.manager.is_connected(player_info['sid'], '/'):
                 add_player_to_lobby(player_info['sid'])

        # --- ИСПРАВЛЕНИЕ: Единый блок try/except/with app_context для коммита ---
        if game.mode == 'pvp' and len(game.players) > 1:
            print(f"[RATING_CALC] {room_id}: Начало подсчета рейтинга для PvP.")
            p1_nick = game.players[0]['nickname']
            p2_nick = game.players[1]['nickname']
            p1_new_rating, p2_new_rating = None, None
            p1_old_rating, p2_old_rating = 1500, 1500 # Значения по умолчанию

            with app.app_context(): # <-- ОДИН КОНТЕКСТ
                try:
                    p1_user = User.query.filter_by(nickname=p1_nick).first()
                    p2_user = User.query.filter_by(nickname=p2_nick).first()

                    if p1_user and p2_user:
                        p1_old_rating = int(p1_user.rating)
                        p2_old_rating = int(p2_user.rating)
                        print(f"[RATING_CALC] {room_id}: Старые рейтинги: {p1_nick}({p1_old_rating}), {p2_nick}({p2_old_rating})")

                        # 1. Обновляем счетчик игр
                        p1_user.games_played += 1
                        p2_user.games_played += 1
                        print(f"[STATS] {room_id}: Игрокам {p1_user.nickname} и {p2_user.nickname} засчитана игра.")

                        # 2. Определяем исход
                        outcome = 0.5
                        if game.scores[0] > game.scores[1]: outcome = 1.0
                        elif game.scores[1] > game.scores[0]: outcome = 0.0
                        print(f"[RATING_CALC] {room_id}: Исход для P1 ({p1_nick}): {outcome}")

                        # 3. Обновляем рейтинги (БЕЗ КОНТЕКСТА ВНУТРИ)
                        new_ratings_tuple = update_ratings(p1_user_obj=p1_user, p2_user_obj=p2_user, p1_outcome=outcome)

                        if new_ratings_tuple:
                            p1_new_rating, p2_new_rating = new_ratings_tuple
                            print(f"[RATING_CALC] {room_id}: Новые рейтинги ПОЛУЧЕНЫ: {p1_nick}({p1_new_rating}), {p2_nick}({p2_new_rating})")
                        else:
                            print(f"[ERROR][RATING_CALC] {room_id}: Функция update_ratings не вернула новые рейтинги.")
                            p1_new_rating, p2_new_rating = p1_old_rating, p2_old_rating # Fallback

                        # 4. Коммитим ВСЕ
                        db.session.commit() # <-- ОДИН КОММИТ
                        print(f"[RATING_CALC] {room_id}: Все изменения (игры и рейтинги) сохранены в БД.")

                        # 5. Формируем данные
                        game_over_data['rating_changes'] = {
                            '0': {'nickname': p1_nick, 'old': p1_old_rating, 'new': p1_new_rating},
                            '1': {'nickname': p2_nick, 'old': p2_old_rating, 'new': p2_new_rating}
                        }
                        socketio.emit('leaderboard_data', get_leaderboard_data()) # Отправляем свежие данные

                    else:
                        print(f"[ERROR][RATING_CALC] {room_id}: Не удалось найти одного из игроков ({p1_nick}, {p2_nick}) в БД.")
                        game_over_data['rating_changes'] = {
                            '0': {'nickname': p1_nick, 'old': p1_old_rating, 'new': p1_old_rating},
                            '1': {'nickname': p2_nick, 'old': p2_old_rating, 'new': p2_old_rating}
                        }

                except Exception as e:
                    db.session.rollback()
                    print(f"[ERROR][RATING_CALC] {room_id}: Ошибка в транзакции обновления рейтинга: {e}")
                    # Убедимся, что у клиента не будет ошибки
                    game_over_data['rating_changes'] = {
                        '0': {'nickname': p1_nick, 'old': p1_old_rating, 'new': p1_old_rating},
                        '1': {'nickname': p2_nick, 'old': p2_old_rating, 'new': p2_old_rating}
                    }

        else:
             print(f"[GAME_OVER] {room_id}: Рейтинги не подсчитывались (Режим: {game.mode}, Игроков: {len(game.players)}).")

        if room_id in active_games: del active_games[room_id]
        broadcast_lobby_stats()
        socketio.emit('game_over', game_over_data, room=room_id)
        return
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    # --- ИГРА ПРОДОЛЖАЕТСЯ ---
    print(f"[ROUND_START] {room_id}: Начинается раунд {game.current_round + 1}/{game.num_rounds}. Клуб: {game.current_club_name}.")
    socketio.emit('round_started', get_game_state_for_client(game, room_id), room=room_id)
    start_next_human_turn(room_id)


def show_round_summary_and_schedule_next(room_id):
    """Показывает итоги раунда и запускает таймер паузы."""
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']
    p1_named_count = len([p for p in game.named_players if p['by'] == 0])
    p2_named_count = len([p for p in game.named_players if p.get('by') == 1]) if game.mode != 'solo' else 0
    round_result = {
        'club_name': game.current_club_name, 'p1_named': p1_named_count, 'p2_named': p2_named_count,
        'result_type': game_session.get('last_round_end_reason', 'completed'),
        'player_nickname': game_session.get('last_round_end_player_nickname', None),
        'winner_index': game_session.get('last_round_winner_index')
    }
    game.round_history.append(round_result)
    print(f"[SUMMARY] {room_id}: Раунд {game.current_round + 1} завершен. Итог: {round_result['result_type']}")
    game_session['skip_votes'] = set()
    game_session['last_round_end_reason'] = None
    game_session['last_round_end_player_nickname'] = None
    game_session['last_round_winner_index'] = None
    summary_data = {
        'clubName': game.current_club_name, 'fullPlayerList': [p['full_name'] for p in game.players_for_comparison],
        'namedPlayers': game.named_players, 'players': {i: {'nickname': p['nickname']} for i, p in game.players.items()},
        'scores': game.scores, 'mode': game.mode
    }
    socketio.emit('round_summary', summary_data, room=room_id)
    pause_id = f"pause_{room_id}_{game.current_round}"
    game_session['pause_id'] = pause_id
    socketio.start_background_task(pause_watcher, room_id, pause_id)

def pause_watcher(room_id, pause_id):
    """Фоновая задача, ждет паузу."""
    socketio.sleep(PAUSE_BETWEEN_ROUNDS)
    game_session = active_games.get(room_id)
    if game_session and game_session.get('pause_id') == pause_id:
        print(f"[GAME] {room_id}: Пауза окончена, запуск следующего раунда.")
        start_game_loop(room_id)

def get_lobby_data_list():
    """Собирает список открытых игр."""
    lobby_list = []
    with app.app_context():
        for room_id, game_info in list(open_games.items()):
            creator_user = User.query.filter_by(nickname=game_info['creator']['nickname']).first()
            if creator_user:
                settings_with_clubs = game_info['settings']
                selected_clubs = settings_with_clubs.get('selected_clubs', [])
                lobby_list.append({
                    'settings': settings_with_clubs, 'creator_nickname': creator_user.nickname,
                    'creator_rating': int(creator_user.rating), 'creator_sid': game_info['creator']['sid'],
                    'selected_clubs_names': selected_clubs
                })
            else:
                print(f"[LOBBY CLEANUP] Пользователь {game_info['creator']['nickname']} не найден, удаляю его открытую игру {room_id}")
                if room_id in open_games: del open_games[room_id]
    return lobby_list

# --- Обработчики событий Socket.IO ---

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    print(f"[CONNECTION] Клиент подключился: {sid}")
    emit('auth_request')

# --- ИСПРАВЛЕНИЕ: Убрано обновление статистики при дисконнекте ---
@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    print(f"[CONNECTION] Клиент {sid} отключился.")
    remove_player_from_lobby(sid)
    room_to_delete_from_lobby = next((rid for rid, g in open_games.items() if g['creator']['sid'] == sid), None)
    if room_to_delete_from_lobby:
        del open_games[room_to_delete_from_lobby]
        print(f"[LOBBY] Создатель {sid} отключился. Комната {room_to_delete_from_lobby} удалена.")
        socketio.emit('update_lobby', get_lobby_data_list())

    game_to_terminate_id, opponent_sid = None, None
    game_session_to_terminate = None
    disconnected_player_index = -1

    for room_id, game_session in list(active_games.items()):
        game = game_session['game']
        idx = next((i for i, p in game.players.items() if p.get('sid') == sid), -1)
        if idx != -1:
            game_to_terminate_id = room_id
            game_session_to_terminate = game_session
            disconnected_player_index = idx
            if len(game.players) > 1:
                opponent_index = 1 - idx
                if game.players[opponent_index].get('sid') and game.players[opponent_index]['sid'] != 'BOT':
                    opponent_sid = game.players[opponent_index]['sid']
            break

    if game_to_terminate_id and game_session_to_terminate:
        game = game_session_to_terminate['game']
        disconnected_player_nick = game.players[disconnected_player_index].get('nickname', 'Неизвестный')
        print(f"[DISCONNECT] Игрок {sid} ({disconnected_player_nick}) отключился от игры {game_to_terminate_id}. Игра прекращена.")

        if game.mode == 'pvp' and opponent_sid:
            # --- ИСПРАВЛЕНИЕ: По новой логике, НЕ обновляем рейтинг и игры при дисконнекте ---
            print(f"[RATING_CALC_DC] {game_to_terminate_id}: Игра отменена из-за дисконнекта. Статистика и рейтинг НЕ обновляются.")

            if opponent_sid in socketio.server.manager.connected_sids.get('/', set()): # Проверяем, онлайн ли оппонент
                add_player_to_lobby(opponent_sid)
                # --- ИЗМЕНЕНИЕ: Отправляем другое сообщение ---
                emit('opponent_disconnected', {'message': f'Соперник ({disconnected_player_nick}) отключился. Игра отменена, статистика не засчитана.'}, room=opponent_sid)
                print(f"[GAME] {game_to_terminate_id}: Отправлено уведомление об отмене игры {opponent_sid}.")
            else:
                 print(f"[GAME] {game_to_terminate_id}: Оставшийся игрок {opponent_sid} тоже отключился.")
            # --- БЛОК С ОБНОВЛЕНИЕМ РЕЙТИНГА ПОЛНОСТЬЮ УДАЛЕН ---

        else:
             print(f"[DISCONNECT] {game_to_terminate_id}: Игра была не PvP или не было оппонента, рейтинг не обновлен.")

        if game_to_terminate_id in active_games: del active_games[game_to_terminate_id]
        broadcast_lobby_stats()
# --- КОНЕЦ ИСПРАВЛЕНИЯ ---

# --- Логика аутентификации ---
def validate_telegram_data(init_data_str):
    try:
        unquoted_data = unquote(init_data_str)
        params_list = unquoted_data.split('&')
        params_dict = {}
        for item in params_list:
            if '=' in item:
                key, value = item.split('=', 1)
                params_dict[key] = value
        sorted_keys = sorted(params_dict.keys())
        received_hash = params_dict.get('hash', '')
        data_to_check_list = []
        user_data_value = None
        for key in sorted_keys:
            value = params_dict[key]
            if key != 'hash': data_to_check_list.append(f"{key}={value}")
            if key == 'user': user_data_value = value
        data_check_string = "\n".join(data_to_check_list)
        secret_key = hmac.new("WebAppData".encode(), TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calculated_hash == received_hash:
            if user_data_value: return json.loads(unquote(user_data_value))
            else: print("[AUTH ERROR] Хеш верный, но параметр 'user' не найден."); return None
        else: print(f"[AUTH ERROR] Хеши не совпали! Получен: {received_hash}, Вычислен: {calculated_hash}"); return None
    except Exception as e:
        print(f"[AUTH ERROR] Исключение при валидации данных Telegram: {e}")
        import traceback; traceback.print_exc(); return None

@socketio.on('login_with_telegram')
def handle_telegram_login(data):
    init_data = data.get('initData'); sid = request.sid
    if not init_data: emit('auth_status', {'success': False, 'message': 'Отсутствуют данные для аутентификации.'}); return
    user_info = validate_telegram_data(init_data)
    if not user_info: emit('auth_status', {'success': False, 'message': 'Неверные данные аутентификации.'}); return
    telegram_id = user_info.get('id')
    if not telegram_id: emit('auth_status', {'success': False, 'message': 'Не удалось получить Telegram ID.'}); return
    with app.app_context():
        user = User.query.filter_by(telegram_id=telegram_id).first()
        if user:
            add_player_to_lobby(sid); emit('auth_status', {'success': True, 'nickname': user.nickname})
            emit('update_lobby', get_lobby_data_list()); print(f"[AUTH] Игрок {user.nickname} (TG ID: {telegram_id}, SID: {sid}) вошел.")
        else:
            print(f"[AUTH] Новый пользователь (TG ID: {telegram_id}, SID: {sid}). Запрос никнейма.")
            emit('request_nickname', {'telegram_id': telegram_id})

@socketio.on('set_initial_username')
def handle_set_username(data):
    nickname = data.get('nickname', '').strip(); telegram_id = data.get('telegram_id'); sid = request.sid
    if not telegram_id: emit('auth_status', {'success': False, 'message': 'Ошибка: отсутствует Telegram ID.'}); return
    if not nickname or not re.match(r'^[a-zA-Z0-9_-]{3,20}$', nickname): emit('auth_status', {'success': False, 'message': 'Никнейм: 3-20 символов (лат., цифры, _, -).'}); return
    with app.app_context():
        if User.query.filter_by(nickname=nickname).first(): emit('auth_status', {'success': False, 'message': 'Этот никнейм уже занят.'}); return
        if User.query.filter_by(telegram_id=telegram_id).first(): emit('auth_status', {'success': False, 'message': 'Пользователь с таким Telegram ID уже зарегистрирован.'}); return
        try:
            new_user = User(telegram_id=telegram_id, nickname=nickname); db.session.add(new_user); db.session.commit()
            add_player_to_lobby(sid); print(f"[AUTH] Зарегистрирован: {nickname} (TG ID: {telegram_id}, SID: {sid})")
            emit('auth_status', {'success': True, 'nickname': new_user.nickname}); emit('update_lobby', get_lobby_data_list())
        except Exception as e:
            db.session.rollback(); print(f"[ERROR] Ошибка при создании пользователя {nickname}: {e}")
            emit('auth_status', {'success': False, 'message': 'Ошибка при регистрации. Попробуйте позже.'})

# --- Обработчики игровых действий ---

# --- ИСПРАВЛЕНИЕ: UnboundLocalError ---
@socketio.on('request_skip_pause')
def handle_request_skip_pause(data):
    room_id = data.get('roomId'); sid = request.sid
    game_session = active_games.get(room_id)

    # ИСПРАВЛЕНИЕ: Разделили проверку и присваивание
    if not game_session:
        print(f"[ERROR][SKIP_PAUSE] {sid} отправил skip для несуществующей комнаты {room_id}")
        return
    game = game_session['game']

    if game.mode == 'solo':
        if game_session.get('pause_id'):
            print(f"[GAME] {room_id}: Пропуск паузы (соло) от {sid}."); game_session['pause_id'] = None; start_game_loop(room_id)
    elif game.mode == 'pvp':
        player_index = next((i for i, p in game.players.items() if p.get('sid') == sid), -1)
        if player_index != -1 and game_session.get('pause_id'):
            game_session['skip_votes'].add(player_index); emit('skip_vote_accepted')
            socketio.emit('skip_vote_update', {'count': len(game_session['skip_votes'])}, room=room_id)
            print(f"[GAME] {room_id}: Голос за пропуск паузы от {game.players[player_index]['nickname']} ({len(game_session['skip_votes'])}/{len(game.players)}).")
            if len(game_session['skip_votes']) >= len(game.players):
                print(f"[GAME] {room_id}: Пропуск паузы (PvP, все голоса)."); game_session['pause_id'] = None; start_game_loop(room_id)
# --- КОНЕЦ ИСПРАВЛЕНИЯ ---

@socketio.on('get_leaderboard')
def handle_get_leaderboard(): emit('leaderboard_data', get_leaderboard_data())

@socketio.on('get_league_clubs')
def handle_get_league_clubs(data):
    league_name = data.get('league', 'РПЛ'); league_data = all_leagues_data.get(league_name, {})
    club_list = sorted(list(league_data.keys())); emit('league_clubs_data', {'league': league_name, 'clubs': club_list})

@socketio.on('start_game')
def handle_start_game(data):
    sid, mode, nickname, settings = request.sid, data.get('mode'), data.get('nickname'), data.get('settings')
    if not nickname: print(f"[ERROR] Попытка начать игру без никнейма от {sid}"); return
    if is_player_busy(sid): print(f"[SECURITY] {nickname} ({sid}) уже занят, старт игры отклонен."); return
    if mode == 'solo':
        player1_info_full = {'sid': sid, 'nickname': nickname}; room_id = str(uuid.uuid4()); join_room(room_id)
        try:
            # --- ИЗМЕНЕНИЕ: Передаем 'solo' в GameState ---
            game = GameState(player1_info_full, all_leagues_data, mode='solo', settings=settings)
            # --- ИЗМЕНЕНИЕ: Проверка на 0, на случай если лига пуста ---
            if game.num_rounds == 0:
                 print(f"[ERROR] {nickname} ({sid}) соло игра создана с 0 клубов."); leave_room(room_id); add_player_to_lobby(sid)
                 emit('start_game_fail', {'message': 'Не удалось загрузить клубы.'}); return
            active_games[room_id] = {'game': game, 'turn_id': None, 'pause_id': None, 'skip_votes': set(), 'last_round_end_reason': None}
            remove_player_from_lobby(sid); broadcast_lobby_stats()
            print(f"[GAME] {nickname} начал тренировку. Комната: {room_id}. Клубов: {game.num_rounds}")
            start_game_loop(room_id)
        except Exception as e:
            print(f"[ERROR] Ошибка при создании соло-игры для {nickname}: {e}"); leave_room(room_id)
            if room_id in active_games: del active_games[room_id]; add_player_to_lobby(sid)
            emit('start_game_fail', {'message': 'Ошибка сервера.'})

@socketio.on('create_game')
def handle_create_game(data):
    sid, nickname, settings = request.sid, data.get('nickname'), data.get('settings')
    if not nickname: print(f"[ERROR] Попытка создать игру без никнейма от {sid}"); return
    if is_player_busy(sid): print(f"[SECURITY] {nickname} ({sid}) уже занят, создание отклонено."); return
    try:
        # --- ИЗМЕНЕНИЕ: Передаем 'pvp' в GameState ---
        temp_game = GameState({'nickname': nickname}, all_leagues_data, mode='pvp', settings=settings)
    except Exception as e: print(f"[ERROR] Ошибка валидации настроек для {nickname}: {e}"); emit('create_game_fail', {'message': 'Ошибка настроек.'}); return
    
    # --- ИЗМЕНЕНИЕ: Эта проверка все еще корректна, т.к. для pvp min=3 ---
    # (Она сработает, если num_rounds=0 из-за пустой лиги)
    if temp_game.num_rounds < 3: 
        print(f"[ERROR] {nickname} ({sid}) pvp игра < 3 клубов (num_rounds: {temp_game.num_rounds})."); 
        emit('create_game_fail', {'message': 'Мин. 3 клуба.'}); 
        return
    
    room_id = str(uuid.uuid4()); join_room(room_id); open_games[room_id] = {'creator': {'sid': sid, 'nickname': nickname}, 'settings': settings}
    remove_player_from_lobby(sid); print(f"[LOBBY] {nickname} ({sid}) создал {room_id}. Клубов: {temp_game.num_rounds}, ТБ: {settings.get('time_bank', 90)}")
    socketio.emit('update_lobby', get_lobby_data_list())

@socketio.on('cancel_game')
def handle_cancel_game():
    sid = request.sid; room_to_delete = next((rid for rid, g in open_games.items() if g['creator']['sid'] == sid), None)
    if room_to_delete:
        leave_room(room_to_delete, sid=sid); del open_games[room_to_delete]; add_player_to_lobby(sid)
        print(f"[LOBBY] Создатель {sid} отменил игру {room_to_delete}."); socketio.emit('update_lobby', get_lobby_data_list())

@socketio.on('join_game')
def handle_join_game(data):
    joiner_sid, joiner_nickname, creator_sid = request.sid, data.get('nickname'), data.get('creator_sid')
    if not joiner_nickname or not creator_sid: print(f"[ERROR] Некорректный join: {data} от {joiner_sid}"); return
    if is_player_busy(joiner_sid): print(f"[SECURITY] {joiner_nickname} ({joiner_sid}) занят, join отклонен."); return
    room_id_to_join = next((rid for rid, g in open_games.items() if g['creator']['sid'] == creator_sid), None)
    if not room_id_to_join: print(f"[LOBBY] {joiner_nickname} не смог join к {creator_sid}. Комната не найдена."); emit('join_game_fail', {'message': 'Игра не найдена.'}); return
    game_to_join = open_games.pop(room_id_to_join); socketio.emit('update_lobby', get_lobby_data_list())
    creator_info = game_to_join['creator']
    if creator_info['sid'] == joiner_sid: print(f"[SECURITY] {joiner_nickname} join к своей игре {room_id_to_join}."); open_games[room_id_to_join] = game_to_join; socketio.emit('update_lobby', get_lobby_data_list()); return
    p1_info_full, p2_info_full = {'sid': creator_info['sid'], 'nickname': creator_info['nickname']}, {'sid': joiner_sid, 'nickname': joiner_nickname}
    join_room(room_id_to_join, sid=p2_info_full['sid']); remove_player_from_lobby(p2_info_full['sid'])
    try:
        game = GameState(p1_info_full, all_leagues_data, player2_info=p2_info_full, mode='pvp', settings=game_to_join['settings'])
        active_games[room_id_to_join] = {'game': game, 'turn_id': None, 'pause_id': None, 'skip_votes': set(), 'last_round_end_reason': None}
        broadcast_lobby_stats(); print(f"[GAME] Старт PvP: {p1_info_full['nickname']} vs {p2_info_full['nickname']}. Комната: {room_id_to_join}. Клубов: {game.num_rounds}")
        start_game_loop(room_id_to_join)
    except Exception as e:
         print(f"[ERROR] Ошибка создания PvP {room_id_to_join}: {e}"); leave_room(room_id_to_join, sid=p1_info_full['sid']); leave_room(room_id_to_join, sid=p2_info_full['sid'])
         if room_id_to_join in active_games: del active_games[room_id_to_join]; add_player_to_lobby(p1_info_full['sid']); add_player_to_lobby(p2_info_full['sid'])
         emit('join_game_fail', {'message': 'Ошибка сервера.'}, room=p1_info_full['sid']); emit('join_game_fail', {'message': 'Ошибка сервера.'}, room=p2_info_full['sid'])

# --- ИСПРАВЛЕНИЕ: UnboundLocalError ---
@socketio.on('submit_guess')
def handle_submit_guess(data):
    room_id, guess, sid = data.get('roomId'), data.get('guess'), request.sid
    game_session = active_games.get(room_id)

    # ИСПРАВЛЕНИЕ: Разделили проверку и присваивание
    if not game_session:
        print(f"[ERROR][GUESS] {sid} отправил guess для несуществующей комнаты {room_id}")
        return
    game = game_session['game']

    # ИСПРАВЛЕНИЕ: Добавили лог
    if game.players[game.current_player_index].get('sid') != sid:
        print(f"[SECURITY][GUESS] {sid} попытался угадать в {room_id}, но сейчас не его ход.")
        return

    result = game.process_guess(guess); current_player_nick = game.players[game.current_player_index]['nickname']
    print(f"[GUESS] {room_id}: {current_player_nick} '{guess}' -> {result['result']}")

    if result['result'] in ['correct', 'correct_typo']:
        time_spent = time.time() - game.turn_start_time; game_session['turn_id'] = None
        game.time_banks[game.current_player_index] -= time_spent
        if game.time_banks[game.current_player_index] < 0:
            print(f"[TIMEOUT] {room_id}: {current_player_nick} угадал, но время вышло ({game.time_banks[game.current_player_index]:.1f}s).");
            on_timer_end(room_id);
            return

        game.add_named_player(result['player_data'], game.current_player_index)
        emit('guess_result', {'result': result['result'], 'corrected_name': result['player_data']['full_name']})

        if game.is_round_over():
            print(f"[ROUND_END] {room_id}: Раунд завершен (все названы). Ничья 0.5-0.5"); game_session['last_round_end_reason'] = 'completed'
            if game.mode == 'pvp': game.scores[0] += 0.5; game.scores[1] += 0.5; game_session['last_round_winner_index'] = 'draw'
            show_round_summary_and_schedule_next(room_id)
        else:
            start_next_human_turn(room_id)
    else:
        emit('guess_result', {'result': result['result']})
# --- КОНЕЦ ИСПРАВЛЕНИЯ ---

# --- ИСПРАВЛЕНИЕ: UnboundLocalError ---
@socketio.on('surrender_round')
def handle_surrender(data):
    room_id, sid = data.get('roomId'), request.sid
    game_session = active_games.get(room_id)

    # ИСПРАВЛЕНИЕ: Разделили проверку и присваивание
    if not game_session:
        print(f"[ERROR][SURRENDER] {sid} отправил surrender для несуществующей комнаты {room_id}")
        return
    game = game_session['game']

    # ИСПРАВЛЕНИЕ: Добавили лог
    if game.players[game.current_player_index].get('sid') != sid:
        print(f"[SECURITY][SURRENDER] {sid} попытался сдаться в {room_id}, но сейчас не его ход.")
        return

    game_session['turn_id'] = None; game_session['last_round_end_reason'] = 'surrender'
    surrendering_player_nick = game.players[game.current_player_index]['nickname']
    print(f"[ROUND_END] {room_id}: Игрок {surrendering_player_nick} сдался.");
    on_timer_end(room_id)
# --- КОНЕЦ ИСПРАВЛЕНИЯ ---

@app.route('/')
def index(): return render_template('index.html')

# Запуск через Dockerfile