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

# --- ИЗМЕНЕНИЕ: Добавлены счетчики зрителей и тренирующихся ---
def broadcast_lobby_stats():
    players_spectating = sum(len(g.get('spectators', [])) for g in active_games.values())
    players_training = sum(1 for g in active_games.values() if g['game'].mode == 'solo')
    players_in_pvp = sum(len(g['game'].players) for g in active_games.values() if g['game'].mode == 'pvp')

    stats = {
        'players_in_lobby': len(lobby_sids),
        'players_in_pvp': players_in_pvp,
        'players_training': players_training,
        'players_spectating': players_spectating
    }
    socketio.emit('lobby_stats_update', stats)
# --- КОНЕЦ ИЗМЕНЕНИЯ ---

# --- ИЗМЕНЕНИЕ: Учитываем зрителей ---
def is_player_busy(sid):
    # Проверяем, не играет ли уже
    for game_session in active_games.values():
        if any(p.get('sid') == sid for p in game_session['game'].players.values()):
            return True
        # Проверяем, не зритель ли
        if any(spec['sid'] == sid for spec in game_session.get('spectators', [])):
            return True
    # Проверяем, не создал ли игру
    for open_game in open_games.values():
        if open_game['creator']['sid'] == sid:
            return True
    return False
# --- КОНЕЦ ИЗМЕНЕНИЯ ---

def add_player_to_lobby(sid):
    if is_player_busy(sid):
        return
    lobby_sids.add(sid)
    broadcast_lobby_stats()

def remove_player_from_lobby(sid):
    was_in_lobby = sid in lobby_sids
    lobby_sids.discard(sid)
    if was_in_lobby:
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
                player_object = {'full_name': player_name_full, 'primary_name': primary_surname, 'valid_normalized_names': valid_normalized_names}
                if club_name not in clubs_data: clubs_data[club_name] = []
                clubs_data[club_name].append(player_object)
        print(f"[DATA] Данные для лиги '{league_name}' успешно загружены из {filename}.")
        return {league_name: clubs_data}
    except FileNotFoundError: print(f"[CRITICAL ERROR] Файл {filename} не найден! Не удалось загрузить данные лиги '{league_name}'."); return {}
    except Exception as e: print(f"[CRITICAL ERROR] Ошибка при загрузке {filename} для лиги '{league_name}': {e}"); return {}

all_leagues_data = {}
all_leagues_data.update(load_league_data('players.csv', 'РПЛ'))

def update_ratings(p1_user_obj, p2_user_obj, p1_outcome):
    try:
        p1 = Player(rating=p1_user_obj.rating, rd=p1_user_obj.rd, vol=p1_user_obj.vol)
        p2 = Player(rating=p2_user_obj.rating, rd=p2_user_obj.rd, vol=p2_user_obj.vol)
        p2_outcome = 1.0 - p1_outcome
        p1_old_rating_for_calc, p2_old_rating_for_calc = p1.rating, p2.rating
        p1_old_rd_for_calc, p2_old_rd_for_calc = p1.rd, p2.rd
        p1.update_player([p2_old_rating_for_calc], [p2_old_rd_for_calc], [p1_outcome])
        p2.update_player([p1_old_rating_for_calc], [p1_old_rd_for_calc], [p2_outcome])
        p1_user_obj.rating, p1_user_obj.rd, p1_user_obj.vol = p1.rating, p1.rd, p1.vol
        p2_user_obj.rating, p2_user_obj.rd, p2_user_obj.vol = p2.rating, p2.rd, p2.vol
        print(f"[RATING] Рейтинги обновлены (в объектах). {p1_user_obj.nickname} ({p1_outcome}) -> {int(p1.rating)} vs {p2_user_obj.nickname} ({p2_outcome}) -> {int(p2.rating)}")
        return int(p1.rating), int(p2.rating)
    except Exception as e: print(f"[ERROR] Ошибка при расчете Glicko: {e}"); return None

def get_leaderboard_data():
    try:
        with app.app_context():
            users_data = db.session.query(User.nickname, User.rating, User.games_played).order_by(User.rating.desc()).limit(100).all()
            leaderboard = [{'nickname': n, 'rating': int(r), 'games_played': g} for n, r, g in users_data]
        return leaderboard
    except Exception as e: print(f"[ERROR] Ошибка при получении данных для лидерборда: {e}"); return []

# --- ИЗМЕНЕНИЕ: Функция для форматирования инфо о зрителях ---
def format_spectator_info(spectators):
    """Форматирует информацию о зрителях для отображения."""
    count = len(spectators)
    if count == 0:
        return None
    elif count <= 3:
        # Показываем ники, обрезанные до 10 символов
        names = [spec['nickname'][:10] + ('...' if len(spec['nickname']) > 10 else '') for spec in spectators]
        return f"👀 Смотрят: {', '.join(names)}"
    else:
        # Показываем только количество
        return f"👀 Зрителей: {count}"
# --- КОНЕЦ ИЗМЕНЕНИЯ ---

# --- ИЗМЕНЕНИЕ: Функция для отправки обновления о зрителях ---
def broadcast_spectator_update(room_id):
    """Отправляет обновленную информацию о зрителях всем в комнате."""
    game_session = active_games.get(room_id)
    if not game_session:
        return
    spectators = game_session.get('spectators', [])
    spectator_text = format_spectator_info(spectators)
    socketio.emit('spectator_update', {'text': spectator_text}, room=room_id)
# --- КОНЕЦ ИЗМЕНЕНИЯ ---

class GameState:
    def __init__(self, player1_info, all_leagues, player2_info=None, mode='solo', settings=None):
        self.mode = mode
        self.players = {0: player1_info}
        if player2_info: self.players[1] = player2_info
        self.scores = {0: 0.0, 1: 0.0}
        min_clubs = 1 if self.mode == 'solo' else 3
        temp_settings = settings or {}
        league = temp_settings.get('league', 'РПЛ')
        self.all_clubs_data = all_leagues.get(league, {})
        if not self.all_clubs_data: print(f"[WARNING] Данные для лиги '{league}' не найдены!")
        max_clubs_in_league = len(self.all_clubs_data)
        default_settings = {'num_rounds': max_clubs_in_league, 'time_bank': 90.0, 'league': league}
        self.settings = settings or default_settings
        selected_clubs = self.settings.get('selected_clubs')
        num_rounds_setting = self.settings.get('num_rounds', 0)
        if selected_clubs and len(selected_clubs) > 0:
            valid_selected_clubs = [c for c in selected_clubs if c in self.all_clubs_data]
            if len(valid_selected_clubs) < min_clubs:
                 print(f"[WARNING] Недостаточно валидных клубов ({len(valid_selected_clubs)}) для режима {self.mode}. Мин: {min_clubs}. Используются все клубы.")
                 available_clubs = list(self.all_clubs_data.keys())
                 self.num_rounds = len(available_clubs)
                 self.game_clubs = random.sample(available_clubs, self.num_rounds) if available_clubs else []
            else:
                self.game_clubs = random.sample(valid_selected_clubs, len(valid_selected_clubs))
                self.num_rounds = len(self.game_clubs)
        elif num_rounds_setting >= min_clubs:
            available_clubs = list(self.all_clubs_data.keys())
            self.num_rounds = min(num_rounds_setting, len(available_clubs))
            self.game_clubs = random.sample(available_clubs, self.num_rounds) if available_clubs else []
        else:
            print(f"[WARNING] Некорректные настройки клубов (менее {min_clubs}), выбраны все клубы лиги.")
            available_clubs = list(self.all_clubs_data.keys())
            self.num_rounds = len(available_clubs)
            self.game_clubs = random.sample(available_clubs, self.num_rounds) if available_clubs else []
        self.current_round, self.current_player_index = -1, 0
        self.current_club_name, self.players_for_comparison = None, []
        self.named_players_full_names, self.named_players = set(), []
        self.round_history, self.end_reason = [], 'normal'
        self.last_successful_guesser_index, self.previous_round_loser_index = None, None
        time_bank_setting = self.settings.get('time_bank', 90.0)
        self.time_banks = {0: time_bank_setting}
        if self.mode != 'solo': self.time_banks[1] = time_bank_setting
        self.turn_start_time = 0

    def start_new_round(self):
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
        self.named_players_full_names, self.named_players = set(), []
        return True

    def process_guess(self, guess):
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
        self.named_players.append({'full_name': player_data['full_name'], 'name': player_data['primary_name'], 'by': player_index})
        self.named_players_full_names.add(player_data['full_name'])
        self.last_successful_guesser_index = player_index
        if self.mode != 'solo': self.switch_player()

    def switch_player(self):
        if len(self.players) > 1: self.current_player_index = 1 - self.current_player_index

    def is_round_over(self):
        return len(self.players_for_comparison) > 0 and len(self.named_players) == len(self.players_for_comparison)

    def is_game_over(self):
        if self.current_round >= (self.num_rounds - 1): self.end_reason = 'normal'; return True
        if len(self.players) > 1:
            score_diff = abs(self.scores[0] - self.scores[1])
            rounds_left = self.num_rounds - (self.current_round + 1)
            if score_diff > rounds_left: self.end_reason = 'unreachable_score'; return True
        return False

# --- ИЗМЕНЕНИЕ: Включаем инфо о зрителях ---
def get_game_state_for_client(game_session, room_id):
    """Собирает данные о состоянии игры для клиента."""
    game = game_session['game']
    spectators = game_session.get('spectators', [])
    spectator_text = format_spectator_info(spectators)
    return {
        'roomId': room_id, 'mode': game.mode,
        'players': {i: {'nickname': p['nickname'], 'sid': p.get('sid')} for i, p in game.players.items()},
        'scores': game.scores, 'round': game.current_round + 1, 'totalRounds': game.num_rounds,
        'clubName': game.current_club_name, 'namedPlayers': game.named_players,
        'fullPlayerList': [p['full_name'] for p in game.players_for_comparison],
        'currentPlayerIndex': game.current_player_index, 'timeBanks': game.time_banks,
        'spectatorInfoText': spectator_text # <-- Добавлено
    }
# --- КОНЕЦ ИЗМЕНЕНИЯ ---

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
    # Отправляем всем в комнате, включая зрителей
    socketio.emit('turn_updated', get_game_state_for_client(game_session, room_id), room=room_id)

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
    # Отправляем всем в комнате
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

def start_game_loop(room_id):
    """Основной цикл игры: запускает новый раунд или завершает игру."""
    game_session = active_games.get(room_id)
    if not game_session: print(f"[ERROR] Попытка запустить цикл для несуществующей игры {room_id}"); return
    game = game_session['game']

    if not game.start_new_round():
        # --- ИГРА ОКОНЧЕНА ---
        game_over_data = {
            'final_scores': game.scores, 'players': {i: {'nickname': p['nickname']} for i, p in game.players.items()},
            'history': game.round_history, 'mode': game.mode, 'end_reason': game.end_reason, 'rating_changes': None
        }
        print(f"[GAME_OVER] {room_id}: Игра окончена. Причина: {game.end_reason}, Счет: {game.scores.get(0, 0)}-{game.scores.get(1, 0)}")

        # Возвращаем игроков (не зрителей) в лобби
        for player_index, player_info in game.players.items():
            if player_info.get('sid') and player_info['sid'] != 'BOT' and socketio.server.manager.is_connected(player_info['sid'], '/'):
                 add_player_to_lobby(player_info['sid'])
        # --- ИЗМЕНЕНИЕ: Возвращаем зрителей в лобби ---
        spectators = game_session.get('spectators', [])
        for spec in spectators:
             if socketio.server.manager.is_connected(spec['sid'], '/'):
                 add_player_to_lobby(spec['sid'])
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---

        if game.mode == 'pvp' and len(game.players) > 1:
            print(f"[RATING_CALC] {room_id}: Начало подсчета рейтинга для PvP.")
            p1_nick, p2_nick = game.players[0]['nickname'], game.players[1]['nickname']
            p1_new_rating, p2_new_rating = None, None
            p1_old_rating, p2_old_rating = 1500, 1500
            with app.app_context():
                try:
                    p1_user, p2_user = User.query.filter_by(nickname=p1_nick).first(), User.query.filter_by(nickname=p2_nick).first()
                    if p1_user and p2_user:
                        p1_old_rating, p2_old_rating = int(p1_user.rating), int(p2_user.rating)
                        print(f"[RATING_CALC] {room_id}: Старые рейтинги: {p1_nick}({p1_old_rating}), {p2_nick}({p2_old_rating})")
                        p1_user.games_played += 1; p2_user.games_played += 1
                        print(f"[STATS] {room_id}: Игрокам {p1_user.nickname} и {p2_user.nickname} засчитана игра.")
                        outcome = 0.5;
                        if game.scores[0] > game.scores[1]: outcome = 1.0
                        elif game.scores[1] > game.scores[0]: outcome = 0.0
                        print(f"[RATING_CALC] {room_id}: Исход для P1 ({p1_nick}): {outcome}")
                        new_ratings_tuple = update_ratings(p1_user_obj=p1_user, p2_user_obj=p2_user, p1_outcome=outcome)
                        if new_ratings_tuple:
                            p1_new_rating, p2_new_rating = new_ratings_tuple
                            print(f"[RATING_CALC] {room_id}: Новые рейтинги ПОЛУЧЕНЫ: {p1_nick}({p1_new_rating}), {p2_nick}({p2_new_rating})")
                        else: print(f"[ERROR][RATING_CALC] {room_id}: Функция update_ratings не вернула новые рейтинги."); p1_new_rating, p2_new_rating = p1_old_rating, p2_old_rating
                        db.session.commit(); print(f"[RATING_CALC] {room_id}: Все изменения (игры и рейтинги) сохранены в БД.")
                        game_over_data['rating_changes'] = {'0': {'nickname': p1_nick, 'old': p1_old_rating, 'new': p1_new_rating}, '1': {'nickname': p2_nick, 'old': p2_old_rating, 'new': p2_new_rating}}
                        socketio.emit('leaderboard_data', get_leaderboard_data())
                    else:
                        print(f"[ERROR][RATING_CALC] {room_id}: Не удалось найти одного из игроков ({p1_nick}, {p2_nick}) в БД.")
                        game_over_data['rating_changes'] = {'0': {'nickname': p1_nick, 'old': p1_old_rating, 'new': p1_old_rating}, '1': {'nickname': p2_nick, 'old': p2_old_rating, 'new': p2_old_rating}}
                except Exception as e:
                    db.session.rollback(); print(f"[ERROR][RATING_CALC] {room_id}: Ошибка в транзакции обновления рейтинга: {e}")
                    game_over_data['rating_changes'] = {'0': {'nickname': p1_nick, 'old': p1_old_rating, 'new': p1_old_rating}, '1': {'nickname': p2_nick, 'old': p2_old_rating, 'new': p2_old_rating}}
        else: print(f"[GAME_OVER] {room_id}: Рейтинги не подсчитывались (Режим: {game.mode}, Игроков: {len(game.players)}).")

        if room_id in active_games: del active_games[room_id]
        broadcast_lobby_stats() # Статистика обновится после удаления игры
        socketio.emit('game_over', game_over_data, room=room_id)
        # --- ИЗМЕНЕНИЕ: Закрываем комнату после отправки game_over ---
        socketio.close_room(room_id)
        print(f"[GAME_OVER] {room_id}: Комната закрыта.")
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---
        return

    # --- ИГРА ПРОДОЛЖАЕТСЯ ---
    print(f"[ROUND_START] {room_id}: Начинается раунд {game.current_round + 1}/{game.num_rounds}. Клуб: {game.current_club_name}.")
    # Отправляем всем в комнате
    socketio.emit('round_started', get_game_state_for_client(game_session, room_id), room=room_id)
    start_next_human_turn(room_id)

# --- ИЗМЕНЕНИЕ: Отправка времени окончания паузы ---
def show_round_summary_and_schedule_next(room_id):
    """Показывает итоги раунда и запускает таймер паузы."""
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']
    p1_named_count = len([p for p in game.named_players if p['by'] == 0])
    p2_named_count = len([p for p in game.named_players if p.get('by') == 1]) if game.mode != 'solo' else 0
    round_result = { 'club_name': game.current_club_name, 'p1_named': p1_named_count, 'p2_named': p2_named_count, 'result_type': game_session.get('last_round_end_reason', 'completed'), 'player_nickname': game_session.get('last_round_end_player_nickname', None), 'winner_index': game_session.get('last_round_winner_index') }
    game.round_history.append(round_result)
    print(f"[SUMMARY] {room_id}: Раунд {game.current_round + 1} завершен. Итог: {round_result['result_type']}")
    game_session['skip_votes'] = set(); game_session['last_round_end_reason'] = None; game_session['last_round_end_player_nickname'] = None; game_session['last_round_winner_index'] = None
    
    pause_end_time = time.time() + PAUSE_BETWEEN_ROUNDS # <-- Время окончания паузы

    summary_data = {
        'clubName': game.current_club_name, 'fullPlayerList': [p['full_name'] for p in game.players_for_comparison],
        'namedPlayers': game.named_players, 'players': {i: {'nickname': p['nickname']} for i, p in game.players.items()},
        'scores': game.scores, 'mode': game.mode,
        'pauseEndTime': pause_end_time # <-- Добавлено
    }
    # Отправляем всем в комнате
    socketio.emit('round_summary', summary_data, room=room_id)
    pause_id = f"pause_{room_id}_{game.current_round}"
    game_session['pause_id'] = pause_id
    socketio.start_background_task(pause_watcher, room_id, pause_id)
# --- КОНЕЦ ИЗМЕНЕНИЯ ---

def pause_watcher(room_id, pause_id):
    """Фоновая задача, ждет паузу."""
    socketio.sleep(PAUSE_BETWEEN_ROUNDS)
    game_session = active_games.get(room_id)
    if game_session and game_session.get('pause_id') == pause_id:
        print(f"[GAME] {room_id}: Пауза окончена, запуск следующего раунда.")
        start_game_loop(room_id)

# --- ИЗМЕНЕНИЕ: Функции для получения данных лобби ---
def get_open_games_for_lobby():
    """Собирает список открытых игр для лобби."""
    open_list = []
    with app.app_context():
        for room_id, game_info in list(open_games.items()):
            creator_user = User.query.filter_by(nickname=game_info['creator']['nickname']).first()
            if creator_user:
                open_list.append({
                    'settings': game_info['settings'],
                    'creator_nickname': creator_user.nickname,
                    'creator_rating': int(creator_user.rating),
                    'creator_sid': game_info['creator']['sid']
                })
            else:
                print(f"[LOBBY CLEANUP] Пользователь {game_info['creator']['nickname']} не найден, удаляю его открытую игру {room_id}")
                if room_id in open_games: del open_games[room_id]
    return open_list

def get_active_games_for_lobby():
    """Собирает список активных игр для лобби (зрители)."""
    active_list = []
    for room_id, game_session in active_games.items():
        game = game_session.get('game')
        # Показываем только PvP игры
        if game and game.mode == 'pvp' and len(game.players) == 2:
            active_list.append({
                'roomId': room_id,
                'player1_nickname': game.players[0]['nickname'],
                'player2_nickname': game.players[1]['nickname'],
                'spectator_count': len(game_session.get('spectators', []))
            })
    return active_list

def emit_lobby_update():
    """Отправляет обновленные списки игр всем в лобби."""
    open_games_list = get_open_games_for_lobby()
    active_games_list = get_active_games_for_lobby()
    # Отправляем в общем событии
    socketio.emit('update_lobby', {'open_games': open_games_list, 'active_games': active_games_list})
# --- КОНЕЦ ИЗМЕНЕНИЯ ---

# --- Обработчики событий Socket.IO ---

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    print(f"[CONNECTION] Клиент подключился: {sid}")
    emit('auth_request')

# --- ИЗМЕНЕНИЕ: Учет зрителей при дисконнекте ---
@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    print(f"[CONNECTION] Клиент {sid} отключился.")
    remove_player_from_lobby(sid) # Попытается удалить из лобби, если он там был

    # Проверяем, не создатель ли он открытой игры
    room_to_delete = next((rid for rid, g in open_games.items() if g['creator']['sid'] == sid), None)
    if room_to_delete:
        del open_games[room_to_delete]
        print(f"[LOBBY] Создатель {sid} отключился. Комната {room_to_delete} удалена.")
        emit_lobby_update() # Обновляем лобби

    # Проверяем, не игрок ли он в активной игре
    player_game_id, opponent_sid = None, None
    game_session_player = None
    disconnected_player_index = -1
    for room_id, game_session in list(active_games.items()):
        game = game_session['game']
        idx = next((i for i, p in game.players.items() if p.get('sid') == sid), -1)
        if idx != -1:
            player_game_id = room_id
            game_session_player = game_session
            disconnected_player_index = idx
            if len(game.players) > 1:
                opponent_index = 1 - idx
                if game.players[opponent_index].get('sid') and game.players[opponent_index]['sid'] != 'BOT':
                    opponent_sid = game.players[opponent_index]['sid']
            break

    if player_game_id and game_session_player:
        game = game_session_player['game']
        disconnected_player_nick = game.players[disconnected_player_index].get('nickname', 'Неизвестный')
        print(f"[DISCONNECT] Игрок {sid} ({disconnected_player_nick}) отключился от игры {player_game_id}. Игра прекращена.")
        if game.mode == 'pvp' and opponent_sid:
            print(f"[RATING_CALC_DC] {player_game_id}: Игра отменена из-за дисконнекта игрока. Статистика и рейтинг НЕ обновляются.")
            if socketio.server.manager.is_connected(opponent_sid, '/'):
                add_player_to_lobby(opponent_sid)
                emit('opponent_disconnected', {'message': f'Соперник ({disconnected_player_nick}) отключился. Игра отменена, статистика не засчитана.'}, room=opponent_sid)
                print(f"[GAME] {player_game_id}: Отправлено уведомление об отмене игры {opponent_sid}.")
            else: print(f"[GAME] {player_game_id}: Оставшийся игрок {opponent_sid} тоже отключился.")
        elif game.mode == 'solo':
             print(f"[DISCONNECT] {player_game_id}: Игрок вышел из тренировки.")
        
        # --- ИЗМЕНЕНИЕ: Сообщаем зрителям и возвращаем их в лобби ---
        spectators = game_session_player.get('spectators', [])
        for spec in spectators:
            if socketio.server.manager.is_connected(spec['sid'], '/'):
                emit('opponent_disconnected', {'message': f'Игрок ({disconnected_player_nick}) отключился. Игра завершена.'}, room=spec['sid'])
                add_player_to_lobby(spec['sid']) # Возвращаем зрителя в лобби
                print(f"[GAME] {player_game_id}: Зрителю {spec['nickname']} отправлено уведомление о завершении.")
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---

        if player_game_id in active_games: del active_games[player_game_id]
        socketio.close_room(player_game_id) # Закрываем комнату
        broadcast_lobby_stats()
        emit_lobby_update() # Обновляем список активных игр
        return # Выходим, т.к. игрок не может быть одновременно зрителем

    # --- ИЗМЕНЕНИЕ: Проверяем, не зритель ли ---
    spectator_game_id = None
    for room_id, game_session in list(active_games.items()):
        spectators = game_session.get('spectators', [])
        if any(spec['sid'] == sid for spec in spectators):
            spectator_game_id = room_id
            # Удаляем зрителя из списка
            game_session['spectators'] = [spec for spec in spectators if spec['sid'] != sid]
            print(f"[SPECTATOR] Зритель {sid} отключился от игры {spectator_game_id}.")
            broadcast_spectator_update(spectator_game_id) # Обновляем инфо в игре
            broadcast_lobby_stats() # Обновляем счетчик
            emit_lobby_update() # Обновляем счетчик в карточке игры
            break
    # --- КОНЕЦ ИЗМЕНЕНИЯ ---

# --- КОНЕЦ ИСПРАВЛЕНИЯ ---


def validate_telegram_data(init_data_str):
    try:
        unquoted_data = unquote(init_data_str); params_list = unquoted_data.split('&'); params_dict = {}
        for item in params_list:
            if '=' in item: key, value = item.split('=', 1); params_dict[key] = value
        sorted_keys = sorted(params_dict.keys()); received_hash = params_dict.get('hash', '')
        data_to_check_list = []; user_data_value = None
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
    except Exception as e: print(f"[AUTH ERROR] Исключение при валидации данных Telegram: {e}"); import traceback; traceback.print_exc(); return None

@socketio.on('login_with_telegram')
def handle_telegram_login(data):
    init_data = data.get('initData'); sid = request.sid
    if not init_data: emit('auth_status', {'success': False, 'message': 'Отсутствуют данные.'}); return
    user_info = validate_telegram_data(init_data)
    if not user_info: emit('auth_status', {'success': False, 'message': 'Неверные данные.'}); return
    telegram_id = user_info.get('id')
    if not telegram_id: emit('auth_status', {'success': False, 'message': 'Нет Telegram ID.'}); return
    with app.app_context():
        user = User.query.filter_by(telegram_id=telegram_id).first()
        if user:
            add_player_to_lobby(sid); emit('auth_status', {'success': True, 'nickname': user.nickname})
            emit_lobby_update(); print(f"[AUTH] Игрок {user.nickname} (TG ID: {telegram_id}, SID: {sid}) вошел.")
        else:
            print(f"[AUTH] Новый пользователь (TG ID: {telegram_id}, SID: {sid}). Запрос никнейма.")
            emit('request_nickname', {'telegram_id': telegram_id})

@socketio.on('set_initial_username')
def handle_set_username(data):
    nickname = data.get('nickname', '').strip(); telegram_id = data.get('telegram_id'); sid = request.sid
    if not telegram_id: emit('auth_status', {'success': False, 'message': 'Ошибка: нет TG ID.'}); return
    if not nickname or not re.match(r'^[a-zA-Z0-9_-]{3,20}$', nickname): emit('auth_status', {'success': False, 'message': 'Ник: 3-20 симв (лат., цифры, _, -).'}); return
    with app.app_context():
        if User.query.filter_by(nickname=nickname).first(): emit('auth_status', {'success': False, 'message': 'Никнейм занят.'}); return
        if User.query.filter_by(telegram_id=telegram_id).first(): emit('auth_status', {'success': False, 'message': 'TG ID уже зарег.'}); return
        try:
            new_user = User(telegram_id=telegram_id, nickname=nickname); db.session.add(new_user); db.session.commit()
            add_player_to_lobby(sid); print(f"[AUTH] Зарегистрирован: {nickname} (TG ID: {telegram_id}, SID: {sid})")
            emit('auth_status', {'success': True, 'nickname': new_user.nickname}); emit_lobby_update()
        except Exception as e: db.session.rollback(); print(f"[ERROR] Ошибка создания {nickname}: {e}"); emit('auth_status', {'success': False, 'message': 'Ошибка регистрации.'})

# --- Обработчики игровых действий ---

@socketio.on('request_skip_pause')
def handle_request_skip_pause(data):
    room_id = data.get('roomId'); sid = request.sid
    game_session = active_games.get(room_id)
    if not game_session: print(f"[ERROR][SKIP_PAUSE] {sid} skip для не сущ. {room_id}"); return
    game = game_session['game']
    if game.mode == 'solo':
        if game_session.get('pause_id'): print(f"[GAME] {room_id}: Пропуск паузы (соло) от {sid}."); game_session['pause_id'] = None; start_game_loop(room_id)
    elif game.mode == 'pvp':
        player_index = next((i for i, p in game.players.items() if p.get('sid') == sid), -1)
        if player_index != -1 and game_session.get('pause_id'):
            game_session['skip_votes'].add(player_index); emit('skip_vote_accepted') # Подтверждение голоса
            socketio.emit('skip_vote_update', {'count': len(game_session['skip_votes'])}, room=room_id)
            print(f"[GAME] {room_id}: Голос за пропуск от {game.players[player_index]['nickname']} ({len(game_session['skip_votes'])}/{len(game.players)}).")
            if len(game_session['skip_votes']) >= len(game.players):
                print(f"[GAME] {room_id}: Пропуск паузы (PvP, все голоса)."); game_session['pause_id'] = None; start_game_loop(room_id)

@socketio.on('get_leaderboard')
def handle_get_leaderboard(): emit('leaderboard_data', get_leaderboard_data())

@socketio.on('get_league_clubs')
def handle_get_league_clubs(data):
    league_name = data.get('league', 'РПЛ'); league_data = all_leagues_data.get(league_name, {})
    club_list = sorted(list(league_data.keys())); emit('league_clubs_data', {'league': league_name, 'clubs': club_list})

@socketio.on('start_game')
def handle_start_game(data):
    sid, mode, nickname, settings = request.sid, data.get('mode'), data.get('nickname'), data.get('settings')
    if not nickname: print(f"[ERROR] Старт без никнейма от {sid}"); return
    if is_player_busy(sid): print(f"[SECURITY] {nickname} ({sid}) занят, старт отклонен."); return
    if mode == 'solo':
        player1_info_full = {'sid': sid, 'nickname': nickname}; room_id = str(uuid.uuid4()); join_room(room_id)
        try:
            game = GameState(player1_info_full, all_leagues_data, mode='solo', settings=settings)
            if game.num_rounds == 0: print(f"[ERROR] {nickname} ({sid}) соло 0 клубов."); leave_room(room_id); add_player_to_lobby(sid); emit('start_game_fail', {'message': 'Нет клубов.'}); return
            # --- ИЗМЕНЕНИЕ: Добавляем пустой список зрителей ---
            active_games[room_id] = {'game': game, 'turn_id': None, 'pause_id': None, 'skip_votes': set(), 'last_round_end_reason': None, 'spectators': []}
            remove_player_from_lobby(sid); broadcast_lobby_stats()
            print(f"[GAME] {nickname} начал тренировку. Комната: {room_id}. Клубов: {game.num_rounds}")
            start_game_loop(room_id)
        except Exception as e:
            print(f"[ERROR] Ошибка создания соло {nickname}: {e}"); leave_room(room_id)
            if room_id in active_games: del active_games[room_id]; add_player_to_lobby(sid)
            emit('start_game_fail', {'message': 'Ошибка сервера.'})
            broadcast_lobby_stats() # Обновляем статы, если игра не создалась

@socketio.on('create_game')
def handle_create_game(data):
    sid, nickname, settings = request.sid, data.get('nickname'), data.get('settings')
    if not nickname: print(f"[ERROR] Создание без никнейма от {sid}"); return
    if is_player_busy(sid): print(f"[SECURITY] {nickname} ({sid}) занят, создание отклонено."); return
    try: temp_game = GameState({'nickname': nickname}, all_leagues_data, mode='pvp', settings=settings)
    except Exception as e: print(f"[ERROR] Ошибка валидации {nickname}: {e}"); emit('create_game_fail', {'message': 'Ошибка настроек.'}); return
    if temp_game.num_rounds < 3: print(f"[ERROR] {nickname} ({sid}) pvp < 3 клубов ({temp_game.num_rounds})."); emit('create_game_fail', {'message': 'Мин. 3 клуба.'}); return
    room_id = str(uuid.uuid4()); join_room(room_id); open_games[room_id] = {'creator': {'sid': sid, 'nickname': nickname}, 'settings': settings}
    remove_player_from_lobby(sid); print(f"[LOBBY] {nickname} ({sid}) создал {room_id}. Клубов: {temp_game.num_rounds}, ТБ: {settings.get('time_bank', 90)}")
    emit_lobby_update() # Обновляем лобби для всех

@socketio.on('cancel_game')
def handle_cancel_game(data=None): # data=None чтобы можно было вызывать из кода
    sid = data.get('sid') if data else request.sid # Получаем sid из data или request
    room_to_delete = next((rid for rid, g in open_games.items() if g['creator']['sid'] == sid), None)
    if room_to_delete:
        leave_room(room_to_delete, sid=sid); del open_games[room_to_delete]; add_player_to_lobby(sid)
        print(f"[LOBBY] Создатель {sid} отменил игру {room_to_delete}.")
        emit_lobby_update() # Обновляем лобби для всех

@socketio.on('join_game')
def handle_join_game(data):
    joiner_sid, joiner_nickname, creator_sid = request.sid, data.get('nickname'), data.get('creator_sid')
    if not joiner_nickname or not creator_sid: print(f"[ERROR] Некорр. join: {data} от {joiner_sid}"); return
    if is_player_busy(joiner_sid): print(f"[SECURITY] {joiner_nickname} ({joiner_sid}) занят, join отклонен."); return
    room_id_to_join = next((rid for rid, g in open_games.items() if g['creator']['sid'] == creator_sid), None)
    if not room_id_to_join: print(f"[LOBBY] {joiner_nickname} join к {creator_sid} не найден."); emit('join_game_fail', {'message': 'Игра не найдена.'}); return
    game_to_join = open_games.pop(room_id_to_join); emit_lobby_update()
    creator_info = game_to_join['creator']
    if creator_info['sid'] == joiner_sid: print(f"[SECURITY] {joiner_nickname} join к своей игре {room_id_to_join}."); open_games[room_id_to_join] = game_to_join; emit_lobby_update(); return
    p1_info_full, p2_info_full = {'sid': creator_info['sid'], 'nickname': creator_info['nickname']}, {'sid': joiner_sid, 'nickname': joiner_nickname}
    join_room(room_id_to_join, sid=p2_info_full['sid']); remove_player_from_lobby(p2_info_full['sid'])
    try:
        game = GameState(p1_info_full, all_leagues_data, player2_info=p2_info_full, mode='pvp', settings=game_to_join['settings'])
        # --- ИЗМЕНЕНИЕ: Добавляем пустой список зрителей ---
        active_games[room_id_to_join] = {'game': game, 'turn_id': None, 'pause_id': None, 'skip_votes': set(), 'last_round_end_reason': None, 'spectators': []}
        broadcast_lobby_stats(); emit_lobby_update() # Обновляем и статы, и список активных игр
        print(f"[GAME] Старт PvP: {p1_info_full['nickname']} vs {p2_info_full['nickname']}. Комната: {room_id_to_join}. Клубов: {game.num_rounds}")
        start_game_loop(room_id_to_join)
    except Exception as e:
         print(f"[ERROR] Ошибка создания PvP {room_id_to_join}: {e}"); leave_room(room_id_to_join, sid=p1_info_full['sid']); leave_room(room_id_to_join, sid=p2_info_full['sid'])
         if room_id_to_join in active_games: del active_games[room_id_to_join]; add_player_to_lobby(p1_info_full['sid']); add_player_to_lobby(p2_info_full['sid'])
         emit('join_game_fail', {'message': 'Ошибка сервера.'}, room=p1_info_full['sid']); emit('join_game_fail', {'message': 'Ошибка сервера.'}, room=p2_info_full['sid'])
         broadcast_lobby_stats(); emit_lobby_update()

# --- ИЗМЕНЕНИЕ: Обработчики для зрителей ---
@socketio.on('join_as_spectator')
def handle_join_as_spectator(data):
    sid, nickname, room_id = request.sid, data.get('nickname'), data.get('roomId')
    if not nickname or not room_id: print(f"[ERROR] Некорр. spectate: {data} от {sid}"); return
    if is_player_busy(sid): print(f"[SECURITY] {nickname} ({sid}) занят, spectate отклонен."); emit('spectate_fail', {'message': 'Вы уже заняты.'}); return
    game_session = active_games.get(room_id)
    if not game_session: print(f"[SPECTATOR] Игра {room_id} не найдена для {nickname}."); emit('spectate_fail', {'message': 'Игра не найдена.'}); return

    # --- Автоотмена созданной игры ---
    my_open_game_id = next((rid for rid, g in open_games.items() if g['creator']['sid'] == sid), None)
    if my_open_game_id:
        print(f"[SPECTATOR] {nickname} ({sid}) заходит зрителем, отменяя свою игру {my_open_game_id}.")
        handle_cancel_game({'sid': sid}) # Вызываем отмену с sid зрителя
    # --- ---

    join_room(room_id, sid=sid)
    if 'spectators' not in game_session: game_session['spectators'] = []
    game_session['spectators'].append({'sid': sid, 'nickname': nickname})
    remove_player_from_lobby(sid) # Удаляем из лобби
    print(f"[SPECTATOR] {nickname} ({sid}) присоединился к игре {room_id}.")

    # Отправляем текущее состояние игры новому зрителю
    emit('round_started', get_game_state_for_client(game_session, room_id)) # Отправляем round_started, т.к. он содержит все необх.
    emit('spectate_success', {'roomId': room_id}) # Отправляем подтверждение зрителю

    broadcast_spectator_update(room_id) # Обновляем инфо о зрителях для всех в комнате
    broadcast_lobby_stats() # Обновляем счетчики в лобби
    emit_lobby_update() # Обновляем счетчик в карточке игры в лобби

@socketio.on('leave_as_spectator')
def handle_leave_as_spectator(data):
    sid, room_id = request.sid, data.get('roomId')
    game_session = active_games.get(room_id)
    if not game_session: print(f"[ERROR] Попытка уйти из несущ. игры {room_id} зрителем {sid}"); return

    initial_spectators = game_session.get('spectators', [])
    game_session['spectators'] = [spec for spec in initial_spectators if spec['sid'] != sid]

    if len(initial_spectators) > len(game_session['spectators']):
        leave_room(room_id, sid=sid)
        add_player_to_lobby(sid) # Возвращаем в лобби
        print(f"[SPECTATOR] Зритель {sid} покинул игру {room_id}.")
        broadcast_spectator_update(room_id)
        broadcast_lobby_stats()
        emit_lobby_update()
    else:
        print(f"[ERROR] Зритель {sid} не найден в игре {room_id} при попытке выхода.")
# --- КОНЕЦ ИЗМЕНЕНИЯ ---


@socketio.on('submit_guess')
def handle_submit_guess(data):
    room_id, guess, sid = data.get('roomId'), data.get('guess'), request.sid
    game_session = active_games.get(room_id)
    if not game_session: print(f"[ERROR][GUESS] {sid} guess для не сущ. {room_id}"); return
    game = game_session['game']
    if game.players[game.current_player_index].get('sid') != sid: print(f"[SECURITY][GUESS] {sid} не его ход в {room_id}."); return

    result = game.process_guess(guess); current_player_nick = game.players[game.current_player_index]['nickname']
    print(f"[GUESS] {room_id}: {current_player_nick} '{guess}' -> {result['result']}")

    if result['result'] in ['correct', 'correct_typo']:
        time_spent = time.time() - game.turn_start_time; game_session['turn_id'] = None
        game.time_banks[game.current_player_index] -= time_spent
        if game.time_banks[game.current_player_index] < 0: print(f"[TIMEOUT] {room_id}: {current_player_nick} угадал, но время вышло."); on_timer_end(room_id); return

        game.add_named_player(result['player_data'], game.current_player_index)
        # Отправляем всем в комнате
        emit('guess_result', {'result': result['result'], 'corrected_name': result['player_data']['full_name']}, room=room_id) # Отправляем всем

        if game.is_round_over():
            print(f"[ROUND_END] {room_id}: Раунд завершен (все названы). Ничья 0.5-0.5"); game_session['last_round_end_reason'] = 'completed'
            if game.mode == 'pvp': game.scores[0] += 0.5; game.scores[1] += 0.5; game_session['last_round_winner_index'] = 'draw'
            show_round_summary_and_schedule_next(room_id)
        else:
            start_next_human_turn(room_id) # Это отправит turn_updated всем
    else:
        # Отправляем только угадывающему
        emit('guess_result', {'result': result['result']})

@socketio.on('surrender_round')
def handle_surrender(data):
    room_id, sid = data.get('roomId'), request.sid
    game_session = active_games.get(room_id)
    if not game_session: print(f"[ERROR][SURRENDER] {sid} surrender для не сущ. {room_id}"); return
    game = game_session['game']
    if game.players[game.current_player_index].get('sid') != sid: print(f"[SECURITY][SURRENDER] {sid} не его ход в {room_id}."); return
    game_session['turn_id'] = None; game_session['last_round_end_reason'] = 'surrender'
    surrendering_player_nick = game.players[game.current_player_index]['nickname']
    print(f"[ROUND_END] {room_id}: Игрок {surrendering_player_nick} сдался.");
    on_timer_end(room_id) # Это отправит timer_expired всем

@app.route('/')
def index(): return render_template('index.html')

# Запуск через Dockerfile