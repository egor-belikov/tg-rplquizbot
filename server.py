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
    if is_player_busy(sid): return
    lobby_sids.add(sid)
    broadcast_lobby_stats()

def remove_player_from_lobby(sid):
    if sid in lobby_sids:
        lobby_sids.discard(sid)
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

# --- Функции Рейтинга ---
def update_ratings(p1_user_obj, p2_user_obj, p1_outcome):
    try:
        with app.app_context():
            p1 = Player(rating=p1_user_obj.rating, rd=p1_user_obj.rd, vol=p1_user_obj.vol)
            p2 = Player(rating=p2_user_obj.rating, rd=p2_user_obj.rd, vol=p2_user_obj.vol)
            p2_outcome = 1.0 - p1_outcome
            p1_old_rating_for_calc, p2_old_rating_for_calc = p1.rating, p2.rating
            p1_old_rd_for_calc, p2_old_rd_for_calc = p1.rd, p2.rd
            p1.update_player([p2_old_rating_for_calc], [p2_old_rd_for_calc], [p1_outcome])
            p2.update_player([p1_old_rating_for_calc], [p1_old_rd_for_calc], [p2_outcome])
            p1_user_obj.rating, p1_user_obj.rd, p1_user_obj.vol = p1.rating, p1.rd, p1.vol
            p2_user_obj.rating, p2_user_obj.rd, p2_user_obj.vol = p2.rating, p2.rd, p2.vol
            db.session.commit()
            print(f"[RATING] Рейтинги обновлены. {p1_user_obj.nickname} ({p1_outcome}) -> {int(p1.rating)} vs {p2_user_obj.nickname} ({p2_outcome}) -> {int(p2.rating)}")
            return int(p1.rating), int(p2.rating)
    except Exception as e:
        db.session.rollback()
        print(f"[ERROR] Ошибка при обновлении/сохранении рейтингов: {e}")
        return None

def get_leaderboard_data():
    try:
        with app.app_context():
            users_data = db.session.query(User.nickname, User.rating, User.games_played)\
                .order_by(User.rating.desc()).limit(100).all()
            leaderboard = [{'nickname': n, 'rating': int(r), 'games_played': g} for n, r, g in users_data]
        return leaderboard
    except Exception as e:
        print(f"[ERROR] Ошибка при получении данных для лидерборда: {e}")
        return []

# --- Класс Состояния Игры ---
class GameState:
    def __init__(self, player1_info, all_leagues, player2_info=None, mode='solo', settings=None):
        self.mode = mode; self.players = {0: player1_info}
        if player2_info: self.players[1] = player2_info; self.scores = {0: 0.0, 1: 0.0}
        temp_settings = settings or {}; league = temp_settings.get('league', 'РПЛ')
        self.all_clubs_data = all_leagues.get(league, {});
        if not self.all_clubs_data: print(f"[WARNING] Данные для лиги '{league}' не найдены!")
        max_clubs = len(self.all_clubs_data); default_settings = { 'num_rounds': max_clubs, 'time_bank': 90.0, 'league': league }
        self.settings = settings or default_settings; selected_clubs = self.settings.get('selected_clubs'); num_rounds_setting = self.settings.get('num_rounds', 0)
        if selected_clubs and len(selected_clubs) > 0:
            valid_clubs = [c for c in selected_clubs if c in self.all_clubs_data]
            if len(valid_clubs) < 3: print(f"[WARNING] < 3 вал. клубов ({len(valid_clubs)}). Используются все."); available = list(self.all_clubs_data.keys()); self.num_rounds, self.game_clubs = len(available), random.sample(available, len(available))
            else: self.game_clubs, self.num_rounds = random.sample(valid_clubs, len(valid_clubs)), len(valid_clubs)
        elif num_rounds_setting >= 3: available = list(self.all_clubs_data.keys()); self.num_rounds = min(num_rounds_setting, len(available)); self.game_clubs = random.sample(available, self.num_rounds)
        else: print("[WARNING] < 3 клубов, выбраны все."); available = list(self.all_clubs_data.keys()); self.num_rounds, self.game_clubs = len(available), random.sample(available, len(available))
        self.current_round = -1; self.current_player_index = 0; self.current_club_name = None; self.players_for_comparison = []; self.named_players_full_names = set(); self.named_players = []
        self.round_history = []; self.end_reason = 'normal'; self.last_successful_guesser_index = None; self.previous_round_loser_index = None
        tb_setting = self.settings.get('time_bank', 90.0); self.time_banks = {0: tb_setting};
        if self.mode != 'solo': self.time_banks[1] = tb_setting; self.turn_start_time = 0

    def start_new_round(self):
        if self.is_game_over(): return False; self.current_round += 1
        if len(self.players) > 1:
            if self.current_round == 0: self.current_player_index = random.randint(0, 1)
            elif self.previous_round_loser_index is not None: self.current_player_index = self.previous_round_loser_index
            elif self.last_successful_guesser_index is not None: self.current_player_index = 1 - self.last_successful_guesser_index
            else: self.current_player_index = self.current_round % 2
        else: self.current_player_index = 0
        self.previous_round_loser_index = None; tb_setting = self.settings.get('time_bank', 90.0); self.time_banks = {0: tb_setting};
        if self.mode != 'solo': self.time_banks[1] = tb_setting
        if self.current_round < len(self.game_clubs): self.current_club_name = self.game_clubs[self.current_round]; p_objs = self.all_clubs_data.get(self.current_club_name, []); self.players_for_comparison = sorted(p_objs, key=lambda p: p['primary_name'])
        else: return False
        self.named_players_full_names = set(); self.named_players = []; return True

    def process_guess(self, guess):
        guess_norm = guess.strip().lower().replace('ё', 'е');
        if not guess_norm: return {'result': 'not_found'}
        for pd in self.players_for_comparison:
            if guess_norm in pd['valid_normalized_names'] and pd['full_name'] not in self.named_players_full_names: return {'result': 'correct', 'player_data': pd}
        best, max_r = None, 0
        for pd in self.players_for_comparison:
            if pd['full_name'] in self.named_players_full_names: continue; ratio = fuzz.ratio(guess_norm, pd['primary_name'].lower().replace('ё', 'е'));
            if ratio > max_r: max_r, best = ratio, pd
        if max_r >= TYPO_THRESHOLD: return {'result': 'correct_typo', 'player_data': best}
        for pd in self.players_for_comparison:
            if guess_norm in pd['valid_normalized_names']: return {'result': 'already_named'}
        return {'result': 'not_found'}

    def add_named_player(self, pd, p_idx):
        self.named_players.append({'full_name': pd['full_name'], 'name': pd['primary_name'], 'by': p_idx}); self.named_players_full_names.add(pd['full_name']); self.last_successful_guesser_index = p_idx
        if self.mode != 'solo': self.switch_player()

    def switch_player(self):
        if len(self.players) > 1: self.current_player_index = 1 - self.current_player_index

    def is_round_over(self): return len(self.players_for_comparison) > 0 and len(self.named_players) == len(self.players_for_comparison)

    def is_game_over(self):
        if self.current_round >= (self.num_rounds - 1): self.end_reason = 'normal'; return True
        if len(self.players) > 1: score_diff = abs(self.scores[0] - self.scores[1]); r_left = self.num_rounds - (self.current_round + 1);
        if score_diff > r_left: self.end_reason = 'unreachable_score'; return True
        return False

# --- Основная логика игры ---

def get_game_state_for_client(game, r_id):
    return { 'roomId': r_id, 'mode': game.mode, 'players': {i: {'nickname': p['nickname'], 'sid': p.get('sid')} for i, p in game.players.items()},
        'scores': game.scores, 'round': game.current_round + 1, 'totalRounds': game.num_rounds, 'clubName': game.current_club_name,
        'namedPlayers': game.named_players, 'fullPlayerList': [p['full_name'] for p in game.players_for_comparison],
        'currentPlayerIndex': game.current_player_index, 'timeBanks': game.time_banks }

def start_next_human_turn(r_id):
    gs = active_games.get(r_id);
    if not gs: return; game = gs['game']; game.turn_start_time = time.time(); turn_id = f"{r_id}_{game.current_round}_{len(game.named_players)}"; gs['turn_id'] = turn_id
    t_left = game.time_banks[game.current_player_index]; nick = game.players[game.current_player_index]['nickname']
    print(f"[TURN] {r_id}: Ход {nick} (Idx:{game.current_player_index}, T:{t_left:.1f}s)");
    if t_left > 0: socketio.start_background_task(turn_watcher, r_id, turn_id, t_left)
    else: print(f"[TURN_END] {r_id}: Время {nick} уже вышло."); on_timer_end(r_id); return
    socketio.emit('turn_updated', get_game_state_for_client(game, r_id), room=r_id)

def turn_watcher(r_id, turn_id, t_limit):
    socketio.sleep(t_limit); gs = active_games.get(r_id)
    if gs and gs.get('turn_id') == turn_id: print(f"[TIMEOUT] {r_id}: Время вышло {turn_id}."); on_timer_end(r_id)

def on_timer_end(r_id):
    gs = active_games.get(r_id);
    if not gs: return; game = gs['game']; loser_idx = game.current_player_index; game.time_banks[loser_idx] = 0.0
    socketio.emit('timer_expired', {'playerIndex': loser_idx, 'timeBanks': game.time_banks}, room=r_id)
    if game.mode != 'solo' and len(game.players) > 1: winner_idx = 1 - loser_idx; game.scores[winner_idx] += 1; game.previous_round_loser_index = loser_idx; gs['last_round_winner_index'] = winner_idx
    if not gs.get('last_round_end_reason'): gs['last_round_end_reason'] = 'timeout'
    gs['last_round_end_player_nickname'] = game.players[loser_idx]['nickname']; print(f"[ROUND_END] {r_id}: Раунд '{gs['last_round_end_reason']}' игрока {game.players[loser_idx]['nickname']}.")
    show_round_summary_and_schedule_next(r_id)

def start_game_loop(r_id):
    gs = active_games.get(r_id);
    if not gs: print(f"[ERROR] Старт цикла {r_id} не найден"); return; game = gs['game']
    if not game.start_new_round():
        # --- ИГРА ОКОНЧЕНА ---
        go_data = { 'final_scores': game.scores, 'players': {i: {'nickname': p['nickname']} for i, p in game.players.items()}, 'history': game.round_history, 'mode': game.mode, 'end_reason': game.end_reason, 'rating_changes': None }
        print(f"[GAME_OVER] {r_id}: Причина: {game.end_reason}, Счет: {game.scores.get(0,0)}-{game.scores.get(1,0)}")
        for p_idx, p_info in game.players.items():
            if p_info.get('sid') and p_info['sid'] != 'BOT' and socketio.server.manager.is_connected(p_info['sid'], '/'): add_player_to_lobby(p_info['sid'])
        if game.mode == 'pvp' and len(game.players) > 1:
            p1_id, p2_id = None, None; p1_old_r, p2_old_r = 1500, 1500; p1_new_r, p2_new_r = None, None
            with app.app_context():
                p1_q = User.query.filter_by(nickname=game.players[0]['nickname']).first(); p2_q = User.query.filter_by(nickname=game.players[1]['nickname']).first()
                if p1_q and p2_q: p1_id, p2_id = p1_q.id, p2_q.id; p1_old_r, p2_old_r = int(p1_q.rating), int(p2_q.rating); print(f"[RATING_FETCH] {r_id}: Старые: {p1_old_r}, {p2_old_r}")
                else: print(f"[ERROR] {r_id}: Не найден один из игроков ({game.players[0]['nickname']}, {game.players[1]['nickname']}) перед обновлением.")
            if p1_id and p2_id:
                with app.app_context():
                    p1_upd = db.session.get(User, p1_id); p2_upd = db.session.get(User, p2_id)
                    if p1_upd and p2_upd: p1_upd.games_played += 1; p2_upd.games_played += 1; db.session.commit(); print(f"[STATS] {r_id}: Игрокам {p1_upd.nickname}, {p2_upd.nickname} засчитана игра.")
                    else: print(f"[ERROR] {r_id}: Не удалось обновить счетчик игр.")
                
                outcome = 0.5
                if game.scores[0] > game.scores[1]: 
                    outcome = 1.0
                elif game.scores[1] > game.scores[0]: 
                    outcome = 0.0

                with app.app_context():
                    # --- НАЧАЛО ИСПРАВЛЕНИЯ (ОШИБКА 1: IF/ELSE ЛОГИКА) ---
                    p1_r = db.session.get(User, p1_id)
                    p2_r = db.session.get(User, p2_id)
                    if p1_r and p2_r: 
                        ratings_tuple = update_ratings(p1_user_obj=p1_r, p2_user_obj=p2_r, p1_outcome=outcome)
                        if ratings_tuple: 
                            p1_new_r, p2_new_r = ratings_tuple
                            print(f"[RATING_FETCH] {r_id}: Новые ПОЛУЧЕНЫ: {p1_new_r}, {p2_new_r}")
                        else: 
                            print(f"[ERROR] {r_id}: update_ratings не вернула рейтинги.")
                            p1_new_r, p2_new_r = p1_old_r, p2_old_r
                    else: 
                        print(f"[ERROR] {r_id}: Не удалось перезапросить для обновления рейтинга.")
                        p1_new_r, p2_new_r = p1_old_r, p2_old_r
                    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

                go_data['rating_changes'] = { '0': {'nickname': game.players[0]['nickname'], 'old': p1_old_r, 'new': p1_new_r if p1_new_r is not None else p1_old_r}, '1': {'nickname': game.players[1]['nickname'], 'old': p2_old_r, 'new': p2_new_r if p2_new_r is not None else p2_old_r} }; socketio.emit('leaderboard_data', get_leaderboard_data())
            else: print(f"[ERROR] {r_id}: Рейтинги НЕ обновлены.")
        if r_id in active_games: del active_games[r_id]; broadcast_lobby_stats(); socketio.emit('game_over', go_data, room=r_id); return
    # --- ИГРА ПРОДОЛЖАЕТСЯ ---
    print(f"[ROUND_START] {r_id}: Раунд {game.current_round + 1}/{game.num_rounds}. Клуб: {game.current_club_name}.")
    socketio.emit('round_started', get_game_state_for_client(game, r_id), room=r_id); start_next_human_turn(r_id)

def show_round_summary_and_schedule_next(r_id):
    gs = active_games.get(r_id);
    if not gs: return; game = gs['game']; p1_n = len([p for p in game.named_players if p['by'] == 0]); p2_n = len([p for p in game.named_players if p.get('by') == 1]) if game.mode != 'solo' else 0
    round_res = { 'club_name': game.current_club_name, 'p1_named': p1_n, 'p2_named': p2_n, 'result_type': gs.get('last_round_end_reason', 'completed'), 'player_nickname': gs.get('last_round_end_player_nickname', None), 'winner_index': gs.get('last_round_winner_index') }
    game.round_history.append(round_res); print(f"[SUMMARY] {r_id}: Раунд {game.current_round + 1} завершен. Итог: {round_res['result_type']}")
    gs['skip_votes'] = set(); gs['last_round_end_reason'] = None; gs['last_round_end_player_nickname'] = None; gs['last_round_winner_index'] = None
    summary_data = { 'clubName': game.current_club_name, 'fullPlayerList': [p['full_name'] for p in game.players_for_comparison], 'namedPlayers': game.named_players, 'players': {i: {'nickname': p['nickname']} for i, p in game.players.items()}, 'scores': game.scores, 'mode': game.mode }
    socketio.emit('round_summary', summary_data, room=r_id); pause_id = f"pause_{r_id}_{game.current_round}"; gs['pause_id'] = pause_id; socketio.start_background_task(pause_watcher, r_id, pause_id)

def pause_watcher(r_id, pause_id):
    socketio.sleep(PAUSE_BETWEEN_ROUNDS); gs = active_games.get(r_id)
    if gs and gs.get('pause_id') == pause_id: print(f"[GAME] {r_id}: Пауза окончена."); start_game_loop(r_id)

def get_lobby_data_list():
    lobby = []
    with app.app_context():
        for r_id, g_info in list(open_games.items()):
            creator = User.query.filter_by(nickname=g_info['creator']['nickname']).first()
            if creator: settings = g_info['settings']; s_clubs = settings.get('selected_clubs', []); lobby.append({ 'settings': settings, 'creator_nickname': creator.nickname, 'creator_rating': int(creator.rating), 'creator_sid': g_info['creator']['sid'], 'selected_clubs_names': s_clubs })
            else: print(f"[LOBBY CLEANUP] User {g_info['creator']['nickname']} not found, removing {r_id}");
            if r_id in open_games: del open_games[r_id]
    return lobby

# --- Обработчики событий Socket.IO ---

@socketio.on('connect')
def handle_connect(): sid = request.sid; print(f"[CONNECTION] Connect: {sid}"); emit('auth_request')

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid; print(f"[CONNECTION] Disconnect: {sid}"); remove_player_from_lobby(sid)
    room_del = next((rid for rid, g in open_games.items() if g['creator']['sid'] == sid), None)
    if room_del: del open_games[room_del]; print(f"[LOBBY] Creator {sid} left. Room {room_del} deleted."); socketio.emit('update_lobby', get_lobby_data_list())
    term_id, opp_sid, term_gs, disc_idx = None, None, None, -1
    for r_id, gs in list(active_games.items()):
        game = gs['game']; idx = next((i for i, p in game.players.items() if p.get('sid') == sid), -1)
        if idx != -1: 
            term_id, term_gs, disc_idx = r_id, gs, idx
            if len(game.players) > 1: 
                opp_idx = 1 - idx; 
                if game.players.get(opp_idx) and game.players[opp_idx].get('sid') and game.players[opp_idx]['sid'] != 'BOT': 
                    opp_sid = game.players[opp_idx]['sid']; 
                    break 
    if term_id and term_gs:
        game = term_gs['game']; disc_nick = game.players[disc_idx].get('nickname', '?'); print(f"[DISCONNECT] Player {sid} ({disc_nick}) left game {term_id}. Ending game.")
        if game.mode == 'pvp' and opp_sid:
            winner_idx, loser_idx = 1 - disc_idx, disc_idx; p1_r, p2_r = None, None; w_id, l_id = None, None
            with app.app_context():
                w_obj = User.query.filter_by(nickname=game.players[winner_idx]['nickname']).first(); l_obj = User.query.filter_by(nickname=game.players[loser_idx]['nickname']).first()
                if w_obj and l_obj: 
                    w_id, l_id = w_obj.id, l_obj.id; w_obj.games_played += 1; l_obj.games_played += 1; db.session.commit(); print(f"[STATS] {term_id}: Game counted due to disconnect.")
                    p1_r = w_obj if winner_idx == 0 else l_obj; p2_r = l_obj if winner_idx == 0 else w_obj
                    if p1_r and p2_r:
                        update_ratings(p1_user_obj=p1_r, p2_user_obj=p2_r, p1_outcome=1.0 if winner_idx == 0 else 0.0)
                        socketio.emit('leaderboard_data', get_leaderboard_data())
                    else:
                         print(f"[ERROR] {term_id}: Не удалось получить объекты User для обновления рейтинга при дисконнекте.")
                else:
                    print(f"[ERROR] {term_id}: Cannot find players ({game.players[winner_idx].get('nickname','?')}, {game.players[loser_idx].get('nickname','?')}) for forfeit.")
        if socketio.server.manager.is_connected(opp_sid, '/'): add_player_to_lobby(opp_sid); emit('opponent_disconnected', {'message': f'Соперник ({disc_nick}) отключился. Победа.'}, room=opp_sid); print(f"[GAME] {term_id}: Notified {opp_sid} of win.")
        else: print(f"[GAME] {term_id}: Opponent {opp_sid} also disconnected.")
        if term_id in active_games: del active_games[term_id]; broadcast_lobby_stats()

# --- Логика аутентификации ---
def validate_telegram_data(init_data_str):
    try:
        unquoted = unquote(init_data_str); params_list = unquoted.split('&'); params_dict = {k: v for k, v in [item.split('=', 1) for item in params_list if '=' in item]}; sorted_keys = sorted(params_dict.keys())
        rcv_hash = params_dict.get('hash', ''); check_list = []; user_val = None
        for key in sorted_keys: v = params_dict[key];
        if key != 'hash': check_list.append(f"{key}={v}");
        if key == 'user': user_val = v
        check_str = "\n".join(check_list); secret = hmac.new("WebAppData".encode(), TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
        if calc_hash == rcv_hash:
            if user_val: return json.loads(unquote(user_val))
            else: print("[AUTH ERROR] Hash OK, no 'user'."); return None
        else: print(f"[AUTH ERROR] Hash mismatch! R:{rcv_hash}, C:{calc_hash}"); return None
    except Exception as e: print(f"[AUTH ERROR] Exception: {e}"); import traceback; traceback.print_exc(); return None

@socketio.on('login_with_telegram')
def handle_telegram_login(data):
    init_data = data.get('initData'); sid = request.sid;
    if not init_data: emit('auth_status', {'success': False, 'message': 'Нет InitData.'}); return
    user_info = validate_telegram_data(init_data)
    if not user_info: emit('auth_status', {'success': False, 'message': 'Неверные данные.'}); return
    tg_id = user_info.get('id');
    if not tg_id: emit('auth_status', {'success': False, 'message': 'Нет TG ID.'}); return
    with app.app_context(): user = User.query.filter_by(telegram_id=tg_id).first()
    if user: add_player_to_lobby(sid); emit('auth_status', {'success': True, 'nickname': user.nickname}); emit('update_lobby', get_lobby_data_list()); print(f"[AUTH] Login OK: {user.nickname} (TG:{tg_id}, SID:{sid}).")
    else: print(f"[AUTH] New user (TG:{tg_id}, SID:{sid}). Request nickname."); emit('request_nickname', {'telegram_id': tg_id})

@socketio.on('set_initial_username')
def handle_set_username(data):
    nick = data.get('nickname', '').strip(); tg_id = data.get('telegram_id'); sid = request.sid
    if not tg_id: emit('auth_status', {'success': False, 'message': 'Нет TG ID.'}); return
    if not nick or not re.match(r'^[a-zA-Z0-9_-]{3,20}$', nick): emit('auth_status', {'success': False, 'message': 'Ник: 3-20 симв. (лат., цифры, _, -).'}); return
    with app.app_context():
        if User.query.filter_by(nickname=nick).first(): emit('auth_status', {'success': False, 'message': 'Ник занят.'}); return
        if User.query.filter_by(telegram_id=tg_id).first(): emit('auth_status', {'success': False, 'message': 'TG ID уже есть.'}); return
        try: new_user = User(telegram_id=tg_id, nickname=nick); db.session.add(new_user); db.session.commit(); add_player_to_lobby(sid); print(f"[AUTH] Registered: {nick} (TG:{tg_id}, SID:{sid})"); emit('auth_status', {'success': True, 'nickname': new_user.nickname}); emit('update_lobby', get_lobby_data_list())
        except Exception as e: db.session.rollback(); print(f"[ERROR] User creation failed {nick}: {e}"); emit('auth_status', {'success': False, 'message': 'Ошибка регистрации.'})

# --- Обработчики игровых действий ---
@socketio.on('request_skip_pause')
def handle_request_skip_pause(data):
    r_id = data.get('roomId'); sid = request.sid; gs = active_games.get(r_id);
    if not gs: return; game = gs['game']
    if game.mode == 'solo':
        if gs.get('pause_id'): print(f"[GAME] {r_id}: Skip pause (solo) {sid}."); gs['pause_id'] = None; start_game_loop(r_id)
    elif game.mode == 'pvp':
        p_idx = next((i for i, p in game.players.items() if p.get('sid') == sid), -1)
        if p_idx != -1 and gs.get('pause_id'):
            gs['skip_votes'].add(p_idx); emit('skip_vote_accepted'); count = len(gs['skip_votes']); socketio.emit('skip_vote_update', {'count': count}, room=r_id); print(f"[GAME] {r_id}: Skip vote {game.players[p_idx]['nickname']} ({count}/{len(game.players)}).")
            if count >= len(game.players): print(f"[GAME] {r_id}: Skip pause (PvP)."); gs['pause_id'] = None; start_game_loop(r_id)

@socketio.on('get_leaderboard')
def handle_get_leaderboard(): emit('leaderboard_data', get_leaderboard_data())

@socketio.on('get_league_clubs')
def handle_get_league_clubs(data): l_name = data.get('league', 'РПЛ'); l_data = all_leagues_data.get(l_name, {}); clubs = sorted(list(l_data.keys())); emit('league_clubs_data', {'league': l_name, 'clubs': clubs})

@socketio.on('start_game')
def handle_start_game(data):
    sid, mode, nick, settings = request.sid, data.get('mode'), data.get('nickname'), data.get('settings')
    if not nick: print(f"[ERROR] Start game no nickname {sid}"); return;
    if is_player_busy(sid): print(f"[SECURITY] {nick} ({sid}) busy, start rejected."); return
    
    r_id = None # Определяем r_id до блока try
    p1_info = None
    
    if mode == 'solo': 
        p1_info = {'sid': sid, 'nickname': nick}
        r_id = str(uuid.uuid4())
        join_room(r_id)
    else:
        emit('start_game_fail', {'message': 'Неизвестный режим.'})
        return

    # --- НАЧАЛО ИСПРАВЛЕНИЯ (ОШИБКА 2: TRY/EXCEPT) ---
    try: 
        game = GameState(p1_info, all_leagues_data, mode='solo', settings=settings)
        
        if game.num_rounds == 0: 
            print(f"[ERROR] {nick} ({sid}) solo no clubs.")
            leave_room(r_id)
            add_player_to_lobby(sid)
            emit('start_game_fail', {'message': 'Нет клубов.'})
            return
        
        active_games[r_id] = {'game': game, 'turn_id': None, 'pause_id': None, 'skip_votes': set(), 'last_round_end_reason': None}
        remove_player_from_lobby(sid)
        broadcast_lobby_stats()
        print(f"[GAME] {nick} started solo. Room: {r_id}. Rounds: {game.num_rounds}")
        start_game_loop(r_id)
    
    except Exception as e: 
        print(f"[ERROR] Solo creation failed {nick}: {e}")
        if r_id: # Проверяем, что r_id был создан
            leave_room(r_id, sid=sid)
            if r_id in active_games: 
                del active_games[r_id]
        add_player_to_lobby(sid) # Возвращаем игрока в лобби при ошибке
        emit('start_game_fail', {'message': 'Ошибка сервера.'})
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

@socketio.on('create_game')
def handle_create_game(data):
    sid, nick, settings = request.sid, data.get('nickname'), data.get('settings')
    if not nick: print(f"[ERROR] Create game no nickname {sid}"); return;
    if is_player_busy(sid): print(f"[SECURITY] {nick} ({sid}) busy, create rejected."); return
    
    # --- НАЧАЛО ИСПРАВЛЕНИЯ (ОШИБКА 2: TRY/EXCEPT) ---
    try: 
        temp_game = GameState({'nickname': nick}, all_leagues_data, mode='pvp', settings=settings)
        if temp_game.num_rounds < 3: 
            print(f"[ERROR] {nick} ({sid}) game < 3 clubs.")
            emit('create_game_fail', {'message': 'Мин. 3 клуба.'})
            return
            
        r_id = str(uuid.uuid4())
        join_room(r_id)
        open_games[r_id] = {'creator': {'sid': sid, 'nickname': nick}, 'settings': settings}
        remove_player_from_lobby(sid)
        print(f"[LOBBY] {nick} ({sid}) created {r_id}. R:{temp_game.num_rounds}, TB:{settings.get('time_bank', 90)}")
        socketio.emit('update_lobby', get_lobby_data_list())

    except Exception as e: 
        print(f"[ERROR] Settings validation failed {nick}: {e}")
        emit('create_game_fail', {'message': 'Ошибка настроек.'})
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

@socketio.on('cancel_game')
def handle_cancel_game():
    sid = request.sid; r_del = next((rid for rid, g in open_games.items() if g['creator']['sid'] == sid), None)
    if r_del: leave_room(r_del, sid=sid); del open_games[r_del]; add_player_to_lobby(sid); print(f"[LOBBY] Creator {sid} canceled {r_del}."); socketio.emit('update_lobby', get_lobby_data_list())

@socketio.on('join_game')
def handle_join_game(data):
    j_sid, j_nick, c_sid = request.sid, data.get('nickname'), data.get('creator_sid')
    if not j_nick or not c_sid: print(f"[ERROR] Invalid join: {data} from {j_sid}"); return
    if is_player_busy(j_sid): print(f"[SECURITY] {j_nick} ({j_sid}) busy, join rejected."); return
    
    r_id = next((rid for rid, g in open_games.items() if g['creator']['sid'] == c_sid), None)
    if not r_id: print(f"[LOBBY] {j_nick} failed join {c_sid}. Not found."); emit('join_game_fail', {'message': 'Игра не найдена.'}); return
    
    g_join = open_games.pop(r_id)
    socketio.emit('update_lobby', get_lobby_data_list())
    c_info = g_join['creator']
    
    if c_info['sid'] == j_sid: 
        print(f"[SECURITY] {j_nick} joined own game {r_id}.")
        open_games[r_id] = g_join # Вернуть игру обратно, т.к. вышли
        socketio.emit('update_lobby', get_lobby_data_list())
        return
        
    p1, p2 = {'sid': c_info['sid'], 'nickname': c_info['nickname']}, {'sid': j_sid, 'nickname': j_nick}
    join_room(r_id, sid=p2['sid'])
    remove_player_from_lobby(p2['sid'])
    
    # --- НАЧАЛО ИСПРАВЛЕНИЯ (ОШИБКА 2: TRY/EXCEPT) ---
    try: 
        game = GameState(p1, all_leagues_data, p2, 'pvp', g_join['settings'])
        active_games[r_id] = {'game': game, 'turn_id': None, 'pause_id': None, 'skip_votes': set(), 'last_round_end_reason': None}
        broadcast_lobby_stats()
        print(f"[GAME] Start PvP: {p1['nickname']} vs {p2['nickname']}. Room: {r_id}. R:{game.num_rounds}")
        start_game_loop(r_id)
    
    except Exception as e: 
        print(f"[ERROR] PvP creation failed {r_id}: {e}")
        leave_room(r_id, sid=p1['sid'])
        leave_room(r_id, sid=p2['sid'])
        if r_id in active_games: 
            del active_games[r_id]
        
        # Возвращаем игроков в лобби при ошибке
        add_player_to_lobby(p1['sid'])
        add_player_to_lobby(p2['sid'])
        emit('join_game_fail', {'message': 'Ошибка сервера.'}, room=p1['sid'])
        emit('join_game_fail', {'message': 'Ошибка сервера.'}, room=p2['sid'])
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

@socketio.on('submit_guess')
def handle_submit_guess(data):
    room_id, guess, sid = data.get('roomId'), data.get('guess'), request.sid
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game'] 
    if game.players[game.current_player_index].get('sid') != sid: return
    result = game.process_guess(guess); current_player_nick = game.players[game.current_player_index]['nickname']
    print(f"[GUESS] {room_id}: {current_player_nick} '{guess}' -> {result['result']}")
    if result['result'] in ['correct', 'correct_typo']:
        time_spent = time.time() - game.turn_start_time; game_session['turn_id'] = None
        game.time_banks[game.current_player_index] -= time_spent
        if game.time_banks[game.current_player_index] < 0: print(f"[TIMEOUT] {room_id}: {current_player_nick} correct but time ran out ({game.time_banks[game.current_player_index]:.1f}s)."); on_timer_end(room_id); return
        game.add_named_player(result['player_data'], game.current_player_index)
        emit('guess_result', {'result': result['result'], 'corrected_name': result['player_data']['full_name']})
        if game.is_round_over():
            print(f"[ROUND_END] {room_id}: Round finished (all named). Draw 0.5-0.5"); game_session['last_round_end_reason'] = 'completed'
            if game.mode == 'pvp': game.scores[0] += 0.5; game.scores[1] += 0.5; game_session['last_round_winner_index'] = 'draw'
            show_round_summary_and_schedule_next(room_id)
        else: start_next_human_turn(room_id)
    else: emit('guess_result', {'result': result['result']})

@socketio.on('surrender_round')
def handle_surrender(data):
    room_id, sid = data.get('roomId'), request.sid
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game'] 
    if game.players[game.current_player_index].get('sid') != sid: return
    game_session['turn_id'] = None; game_session['last_round_end_reason'] = 'surrender'
    surrendering_player_nick = game.players[game.current_player_index]['nickname']
    print(f"[ROUND_END] {room_id}: Player {surrendering_player_nick} surrendered."); on_timer_end(room_id)

@app.route('/')
def index(): return render_template('index.html')

# Запуск через Dockerfile и Gunicorn