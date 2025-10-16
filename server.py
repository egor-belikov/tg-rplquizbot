# server.py

import os, csv, uuid, random, time, re
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
from fuzzywuzzy import fuzz
from glicko2 import Player
from sqlalchemy.pool import NullPool
from werkzeug.security import generate_password_hash, check_password_hash

# Константы
PAUSE_BETWEEN_ROUNDS = 10
TYPO_THRESHOLD = 85

# Настройка Flask, SQLAlchemy
basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__)
app.config['SECRET_KEY'] = 'a_very_secret_key'
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL.replace("postgres://", "postgresql://", 1)
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'game.db')
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = { 'poolclass': NullPool }
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# Модель Базы Данных
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nickname = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=True)
    rating = db.Column(db.Float, default=1500)
    rd = db.Column(db.Float, default=350)
    vol = db.Column(db.Float, default=0.06)

with app.app_context():
    db.create_all()

# Глобальные переменные для отслеживания состояния
active_games, open_games = {}, {}
lobby_sids = set()

# --- НАЧАЛО БЛОКА ДЛЯ ВСТАВКИ ---

def broadcast_lobby_stats():
    """Отправляет всем клиентам актуальное количество игроков."""
    stats = {
        'players_in_lobby': len(lobby_sids),
        'players_in_game': sum(len(g['game'].players) for g in active_games.values())
    }
    socketio.emit('lobby_stats_update', stats)

def add_player_to_lobby(sid):
    """Добавляет игрока в лобби и оповещает всех."""
    if is_player_busy(sid): return
    lobby_sids.add(sid)
    print(f"[LOBBY] Игрок {sid} вошел в лобби. Всего в лобби: {len(lobby_sids)}")
    broadcast_lobby_stats()

def remove_player_from_lobby(sid):
    """Удаляет игрока из лобби и оповещает всех."""
    lobby_sids.discard(sid) # Используем discard вместо remove, чтобы не было ошибки, если sid уже удален
    print(f"[LOBBY] Игрок {sid} покинул лобби. Всего в лобби: {len(lobby_sids)}")
    broadcast_lobby_stats()

def is_player_busy(sid):
    """Проверяет, находится ли игрок уже в игре или в открытом лобби."""
    # Проверка на участие в активной игре
    for game_session in active_games.values():
        for player_info in game_session['game'].players.values():
            if player_info['sid'] == sid:
                return True
    # Проверка на создание открытой игры
    for open_game in open_games.values():
        if open_game['creator']['sid'] == sid:
            return True
    return False

# --- КОНЕЦ БЛОКА ДЛЯ ВСТАВКИ ---

def load_league_data(filename, league_name):
    """Загружает данные для одной лиги."""
    clubs_data = {}
    with open(filename, mode='r', encoding='utf-8') as infile:
        reader = csv.reader(infile)
        for row in reader:
            if not row or not row[0]: continue
            player_name_full, club_name = row[0], row[1]
            primary_surname = player_name_full.split()[-1]
            aliases = {primary_surname}
            if len(row) > 2:
                for alias in row[2:]:
                    if alias: aliases.add(alias)
            
            player_object = { 
                'full_name': player_name_full, 'primary_name': primary_surname, 
                'valid_normalized_names': {a.strip().lower().replace('ё', 'е') for a in aliases} 
            }
            if club_name not in clubs_data: clubs_data[club_name] = []
            clubs_data[club_name].append(player_object)
    return {league_name: clubs_data}

# Загружаем все лиги. В будущем можно будет добавить новые файлы.
all_leagues_data = {}
all_leagues_data.update(load_league_data('players.csv', 'РПЛ'))


class GameState:
    def __init__(self, player1_info, all_leagues, player2_info=None, mode='solo', settings=None):
        self.mode = mode
        self.players = {0: player1_info}
        if player2_info: self.players[1] = player2_info
        self.scores = {0: 0.0, 1: 0.0}
        
        # --- НАЧАЛО ИЗМЕНЕНИЙ ---
        # Сначала определяем лигу, чтобы знать, сколько в ней клубов
        temp_settings = settings or {}
        league = temp_settings.get('league', 'РПЛ')
        self.all_clubs_data = all_leagues.get(league, {})
        
        # Динамически задаем количество раундов по умолчанию
        max_clubs_in_league = len(self.all_clubs_data)
        default_settings = {
            'num_rounds': max_clubs_in_league, 
            'time_bank': 90.0, 
            'league': league
        }
        self.settings = settings or default_settings
        # --- КОНЕЦ ИЗМЕНЕНИЙ ---
        
        # Логика выбора клубов
        selected_clubs = self.settings.get('selected_clubs')
        if selected_clubs:
            self.game_clubs = random.sample(selected_clubs, len(selected_clubs))
            self.num_rounds = len(self.game_clubs)
        else:
            # Используем num_rounds из настроек, которое по умолчанию теперь равно max_clubs_in_league
            self.num_rounds = self.settings.get('num_rounds', max_clubs_in_league)
            available_clubs = list(self.all_clubs_data.keys())
            self.game_clubs = random.sample(available_clubs, min(self.num_rounds, len(available_clubs)))

        # ... остальная часть метода без изменений
        self.current_round = -1
        self.current_player_index, self.current_club_name = 0, None
        self.players_for_comparison, self.named_players_full_names, self.named_players = [], set(), []
        self.round_history, self.end_reason = [], 'normal'
        self.last_successful_guesser_index, self.previous_round_loser_index = None, None
        
        time_bank_setting = self.settings.get('time_bank', 90.0)
        self.time_banks = {0: time_bank_setting}
        if self.mode != 'solo':
            self.time_banks[1] = time_bank_setting
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
        if self.mode != 'solo':
            self.time_banks[1] = time_bank_setting

        self.current_club_name = self.game_clubs[self.current_round]
        player_objects = self.all_clubs_data.get(self.current_club_name, [])
        self.players_for_comparison = sorted(player_objects, key=lambda p: p['primary_name'])
        self.named_players_full_names, self.named_players = set(), []
        return True

    def process_guess(self, guess):
        guess_norm = guess.strip().lower().replace('ё', 'е')
        
        for player_data in self.players_for_comparison:
            if guess_norm in player_data['valid_normalized_names'] and player_data['full_name'] not in self.named_players_full_names:
                return {'result': 'correct', 'player_data': player_data}

        best_match_player, max_ratio = None, 0
        for player_data in self.players_for_comparison:
            if player_data['full_name'] in self.named_players_full_names: continue
            primary_norm = player_data['primary_name'].lower().replace('ё', 'е')
            ratio = fuzz.ratio(guess_norm, primary_norm)
            if ratio > max_ratio:
                max_ratio, best_match_player = ratio, player_data
        
        if max_ratio >= TYPO_THRESHOLD:
            return {'result': 'correct_typo', 'player_data': best_match_player}
        
        for player_data in self.players_for_comparison:
             if guess_norm in player_data['valid_normalized_names']:
                 return {'result': 'already_named'}

        return {'result': 'not_found'}

    def add_named_player(self, player_data, player_index):
        self.named_players.append({'full_name': player_data['full_name'], 'name': player_data['primary_name'], 'by': player_index})
        self.named_players_full_names.add(player_data['full_name'])
        self.last_successful_guesser_index = player_index
        if self.mode != 'solo':
            self.switch_player()

    def switch_player(self):
        if len(self.players) > 1:
            self.current_player_index = 1 - self.current_player_index

    def is_round_over(self): return len(self.named_players) == len(self.players_for_comparison)
    def is_game_over(self):
        if self.current_round >= self.num_rounds - 1:
            self.end_reason = 'normal'
            return True
        if len(self.players) > 1:
            score_diff = abs(self.scores[0] - self.scores[1])
            rounds_left = self.num_rounds - (self.current_round + 1)
            if score_diff > rounds_left:
                self.end_reason = 'unreachable_score'
                return True
        return False

def get_game_state_for_client(game, room_id):
    return { 
        'roomId': room_id, 'mode': game.mode, 
        'players': {i: {'nickname': p['nickname'], 'sid': p['sid']} for i, p in game.players.items()}, 
        'scores': game.scores, 'round': game.current_round + 1, 'totalRounds': game.num_rounds, 
        'clubName': game.current_club_name, 'namedPlayers': game.named_players, 
        'fullPlayerList': [p['full_name'] for p in game.players_for_comparison], 
        'currentPlayerIndex': game.current_player_index, 'timeBanks': game.time_banks 
    }

def start_next_human_turn(room_id):
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']
    game.turn_start_time = time.time()
    turn_id = f"{room_id}_{game.current_round}_{len(game.named_players)}"
    game_session['turn_id'] = turn_id
    time_left = game.time_banks[game.current_player_index]
    if time_left > 0:
        socketio.start_background_task(turn_watcher, room_id, turn_id, time_left)
    else: on_timer_end(room_id)
    socketio.emit('turn_updated', get_game_state_for_client(game, room_id), room=room_id)

def turn_watcher(room_id, turn_id, time_limit):
    socketio.sleep(time_limit)
    game_session = active_games.get(room_id)
    if game_session and game_session.get('turn_id') == turn_id: on_timer_end(room_id)

def on_timer_end(room_id):
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']
    loser_index = game.current_player_index
    game.time_banks[loser_index] = 0.0
    socketio.emit('timer_expired', {'playerIndex': loser_index, 'timeBanks': game.time_banks}, room=room_id)
    if game.mode != 'solo':
        winner_index = 1 - loser_index
        game.scores[winner_index] += 1
        game.previous_round_loser_index = loser_index
    
    game_session['last_round_end_reason'] = 'timeout'
    game_session['last_round_end_player_nickname'] = game.players[loser_index]['nickname']
    show_round_summary_and_schedule_next(room_id)

def start_game_loop(room_id):
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']
    if not game.start_new_round():
        game_over_data = { 'final_scores': game.scores, 'players': {i: {'nickname': p['nickname']} for i, p in game.players.items()}, 'history': game.round_history, 'mode': game.mode, 'end_reason': game.end_reason }
        print(f"[GAME] Игра в комнате {room_id} окончена. Причина: {game.end_reason}, Счет: {game.scores[0]}-{game.scores[1]}")
        
        for player_info in game.players.values():
            if player_info['sid'] != 'BOT' and game.mode == 'pvp':
                add_player_to_lobby(player_info['sid'])

        if game.mode == 'pvp':
            with app.app_context():
                p1_obj = User.query.filter_by(nickname=game.players[0]['nickname']).first()
                p2_obj = User.query.filter_by(nickname=game.players[1]['nickname']).first()

            if not p1_obj or not p2_obj:
                print(f"[ERROR] Не удалось найти одного из игроков в БД в конце игры {room_id}. Рейтинги не будут обновлены.")
            else:
                p1_old_rating = int(p1_obj.rating)
                p2_old_rating = int(p2_obj.rating)

                if game.scores[0] > game.scores[1]: 
                    update_ratings(winner_user_obj=p1_obj, loser_user_obj=p2_obj)
                elif game.scores[1] > game.scores[0]: 
                    update_ratings(winner_user_obj=p2_obj, loser_user_obj=p1_obj)

                with app.app_context():
                    updated_p1 = User.query.get(p1_obj.id)
                    updated_p2 = User.query.get(p2_obj.id)
                    p1_new_rating = int(updated_p1.rating)
                    p2_new_rating = int(updated_p2.rating)

                game_over_data['rating_changes'] = {
                    'p1': {'old': p1_old_rating, 'new': p1_new_rating},
                    'p2': {'old': p2_old_rating, 'new': p2_new_rating}
                }
                socketio.emit('leaderboard_data', get_leaderboard_data())
            
        del active_games[room_id]
        broadcast_lobby_stats()
        socketio.emit('game_over', game_over_data, room=room_id)
        return
        
    print(f"[GAME] Комната {room_id}: начинается раунд {game.current_round + 1}/{game.num_rounds}. Клуб: {game.current_club_name}.")
    socketio.emit('round_started', get_game_state_for_client(game, room_id), room=room_id)
    start_next_human_turn(room_id)

def show_round_summary_and_schedule_next(room_id):
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']
    p1_named_count = len([p for p in game.named_players if p['by'] == 0])
    p2_named_count = len([p for p in game.named_players if p.get('by') == 1]) if game.mode != 'solo' else 0
    round_result = { 
        'club_name': game.current_club_name, 'p1_named': p1_named_count, 'p2_named': p2_named_count, 
        'result_type': game_session.get('last_round_end_reason', 'completed'),
        'player_nickname': game_session.get('last_round_end_player_nickname', None)
    }
    game.round_history.append(round_result)
    print(f"[GAME] Комната {room_id}: раунд {game.current_round + 1} завершен. Итог: {round_result['result_type']}")
    game_session['skip_votes'] = set()
    game_session['last_round_end_reason'] = 'completed'
    game_session['last_round_end_player_nickname'] = None

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
    socketio.sleep(PAUSE_BETWEEN_ROUNDS)
    game_session = active_games.get(room_id)
    if game_session and game_session.get('pause_id') == pause_id:
        print(f"[GAME] Комната {room_id}: пауза окончена, запуск следующего раунда.")
        start_game_loop(room_id)

def get_lobby_data_list():
    lobby_list = []
    with app.app_context():
        for room_id, game_info in open_games.items():
            creator_user = User.query.filter_by(nickname=game_info['creator']['nickname']).first()
            if creator_user:
                lobby_list.append({
                    'settings': game_info['settings'], 'creator_nickname': creator_user.nickname,
                    'creator_rating': int(creator_user.rating), 'creator_sid': game_info['creator']['sid']
                })
    return lobby_list

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    print(f"[CONNECTION] Клиент подключился: {sid}")
    add_player_to_lobby(sid)
    emit('update_lobby', get_lobby_data_list())

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    print(f"[CONNECTION] Клиент отключился: {sid}")
    remove_player_from_lobby(sid)
    
    room_to_delete_from_lobby = next((rid for rid, g in open_games.items() if g['creator']['sid'] == sid), None)
    if room_to_delete_from_lobby:
        del open_games[room_to_delete_from_lobby]
        print(f"[LOBBY] Создатель отключился. Комната {room_to_delete_from_lobby} удалена.")
        socketio.emit('update_lobby', get_lobby_data_list())

    game_to_terminate_id = None
    opponent_sid = None
    for room_id, game_session in list(active_games.items()):
        game = game_session['game']
        disconnected_player_index = next((i for i, p in game.players.items() if p['sid'] == sid), -1)
        
        if disconnected_player_index != -1:
            game_to_terminate_id = room_id
            if len(game.players) > 1:
                opponent_index = 1 - disconnected_player_index
                if game.players[opponent_index]['sid'] != 'BOT':
                   opponent_sid = game.players[opponent_index]['sid']
            break

    if game_to_terminate_id:
        print(f"[GAME] Игрок {sid} отключился от активной игры {game_to_terminate_id}. Игра прекращена.")
        if opponent_sid:
            add_player_to_lobby(opponent_sid)
            emit('opponent_disconnected', {'message': 'Соперник отключился. Игра отменена.'}, room=opponent_sid)
            print(f"[GAME] Отправлено уведомление об отключении сопернику {opponent_sid}.")
        del active_games[game_to_terminate_id]
        broadcast_lobby_stats()

@socketio.on('request_skip_pause')
def handle_request_skip_pause(data):
    room_id = data.get('roomId')
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']
    if game.mode == 'solo':
        game_session['pause_id'] = None
        start_game_loop(room_id)
    elif game.mode == 'pvp':
        player_index = next((i for i, p in game.players.items() if p['sid'] == request.sid), -1)
        if player_index != -1:
            game_session['skip_votes'].add(player_index)
            emit('skip_vote_accepted')
            socketio.emit('skip_vote_update', {'count': len(game_session['skip_votes'])}, room=room_id)
            if len(game_session['skip_votes']) >= len(game.players):
                game_session['pause_id'] = None
                start_game_loop(room_id)

@socketio.on('get_leaderboard')
def handle_get_leaderboard():
    emit('leaderboard_data', get_leaderboard_data())

@socketio.on('get_league_clubs')
def handle_get_league_clubs(data):
    league_name = data.get('league', 'РПЛ')
    league_data = all_leagues_data.get(league_name, {})
    club_list = sorted(list(league_data.keys()))
    emit('league_clubs_data', {'league': league_name, 'clubs': club_list})

@socketio.on('register_user')
def handle_register_user(data):
    nickname, password = data.get('nickname'), data.get('password')
    if not nickname or not password or len(nickname) < 3 or len(nickname) > 15 or not re.match(r'^[a-zA-Z0-9а-яА-Я_-]+$', nickname) or len(password) < 3:
        emit('auth_status', {'success': False, 'message': 'Неверные данные для регистрации.', 'form': 'register'})
        return
    with app.app_context():
        if User.query.filter_by(nickname=nickname).first():
            emit('auth_status', {'success': False, 'message': 'Этот никнейм уже занят.', 'form': 'register'})
        else:
            get_or_create_user(nickname, password)
            print(f"[AUTH] Зарегистрирован новый игрок: {nickname}")
            emit('auth_status', {'success': True, 'nickname': nickname, 'form': 'register'})

@socketio.on('login_user')
def handle_login_user(data):
    nickname, password = data.get('nickname'), data.get('password')
    if not nickname or not password:
        emit('auth_status', {'success': False, 'message': 'Введите никнейм и пароль.', 'form': 'login'})
        return
    
    with app.app_context():
        user = User.query.filter_by(nickname=nickname).first()
        if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
            emit('auth_status', {'success': False, 'message': 'Неверный никнейм или пароль.', 'form': 'login'})
        else:
            print(f"[AUTH] Игрок {nickname} успешно вошел в систему.")
            emit('auth_status', {'success': True, 'nickname': nickname, 'form': 'login'})

@socketio.on('start_game')
def handle_start_game(data):
    sid, mode, nickname, settings = request.sid, data.get('mode'), data.get('nickname'), data.get('settings')
    
    if is_player_busy(sid):
        print(f"[SECURITY] Игрок {sid} уже занят, попытка начать тренировку отклонена.")
        return

    if mode == 'solo':
        with app.app_context():
            player_user = get_or_create_user(nickname)
        player1_info_full = {'sid': sid, 'nickname': nickname, 'user_obj': player_user}
        room_id = str(uuid.uuid4())
        join_room(room_id)
        
        game = GameState(player1_info_full, all_leagues_data, mode='solo', settings=settings)
        active_games[room_id] = {'game': game, 'turn_id': None, 'pause_id': None, 'skip_votes': set()}
        
        broadcast_lobby_stats()
        print(f"[GAME] Игрок {nickname} начал тренировку. Комната: {room_id}")
        start_game_loop(room_id)

@socketio.on('create_game')
def handle_create_game(data):
    sid, nickname, settings = request.sid, data.get('nickname'), data.get('settings')

    if is_player_busy(sid):
        print(f"[SECURITY] Игрок {sid} уже занят, попытка создать игру отклонена.")
        return
            
    room_id = str(uuid.uuid4())
    join_room(room_id)
    open_games[room_id] = {'creator': {'sid': sid, 'nickname': nickname}, 'settings': settings}
    print(f"[LOBBY] Игрок {nickname} создал комнату {room_id}. Настройки: {settings}")
    socketio.emit('update_lobby', get_lobby_data_list())

@socketio.on('cancel_game')
def handle_cancel_game():
    sid = request.sid
    room_to_delete = next((rid for rid, g in open_games.items() if g['creator']['sid'] == sid), None)
    
    if room_to_delete:
        leave_room(room_to_delete)
        del open_games[room_to_delete]
        print(f"[LOBBY] Создатель {sid} отменил игру. Комната {room_to_delete} удалена.")
        socketio.emit('update_lobby', get_lobby_data_list())

@socketio.on('join_game')
def handle_join_game(data):
    creator_sid, joiner_nickname = data.get('creator_sid'), data.get('nickname')
    
    room_id_to_join = next((rid for rid, g in open_games.items() if g['creator']['sid'] == creator_sid), None)

    if not room_id_to_join:
        print(f"[LOBBY] Попытка присоединиться к несуществующей игре. Отклонено.")
        return
    
    game_to_join = open_games.pop(room_id_to_join)
    socketio.emit('update_lobby', get_lobby_data_list())

    creator_info = game_to_join['creator']
    
    if creator_info['sid'] == request.sid:
        open_games[room_id_to_join] = game_to_join
        socketio.emit('update_lobby', get_lobby_data_list())
        return

    with app.app_context():
        p1_user = get_or_create_user(creator_info['nickname'])
        p2_user = get_or_create_user(joiner_nickname)

    p1_info_full = {'sid': creator_info['sid'], 'nickname': creator_info['nickname'], 'user_obj': p1_user}
    p2_info_full = {'sid': request.sid, 'nickname': joiner_nickname, 'user_obj': p2_user}
    
    join_room(room_id_to_join, sid=p2_info_full['sid'])

    remove_player_from_lobby(p1_info_full['sid'])
    remove_player_from_lobby(p2_info_full['sid'])

    game = GameState(p1_info_full, all_leagues_data, player2_info=p2_info_full, mode='pvp', settings=game_to_join['settings'])
    active_games[room_id_to_join] = {'game': game, 'turn_id': None, 'pause_id': None, 'skip_votes': set()}
    
    broadcast_lobby_stats()
    print(f"[GAME] Начинается PvP игра: {p1_info_full['nickname']} vs {p2_info_full['nickname']}. Комната: {room_id_to_join}")
    start_game_loop(room_id_to_join)

@socketio.on('submit_guess')
def handle_submit_guess(data):
    room_id, guess = data.get('roomId'), data.get('guess')
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']
    current_player_sid = game.players[game.current_player_index].get('sid')
    if current_player_sid != request.sid: 
        return
    
    result = game.process_guess(guess)
    if result['result'] in ['correct', 'correct_typo']:
        time_spent = time.time() - game.turn_start_time
        game_session['turn_id'] = None
        game.time_banks[game.current_player_index] -= time_spent
        if game.time_banks[game.current_player_index] < 0:
            on_timer_end(room_id); return
        
        game.add_named_player(result['player_data'], game.current_player_index)
        emit('guess_result', {'result': result['result'], 'corrected_name': result['player_data']['full_name']})
        
        if game.is_round_over():
            game_session['last_round_end_reason'] = 'completed'
            if game.mode == 'pvp': game.scores[0] += 0.5; game.scores[1] += 0.5
            show_round_summary_and_schedule_next(room_id)
        else:
            start_next_human_turn(room_id)
    else:
        emit('guess_result', {'result': result['result']})

@socketio.on('surrender_round')
def handle_surrender(data):
    room_id = data.get('roomId')
    game_session = active_games.get(room_id)
    if not game_session: return
    game = game_session['game']
    surrendering_player_index = game.current_player_index
    if game.players[surrendering_player_index].get('sid') != request.sid:
        return
    game_session['turn_id'] = None 
    game_session['last_round_end_reason'] = 'surrender'
    game_session['last_round_end_player_nickname'] = game.players[surrendering_player_index]['nickname']
    print(f"[GAME] Игрок {game.players[surrendering_player_index]['nickname']} сдался в комнате {room_id}.")
    on_timer_end(room_id)

@app.route('/')
def index(): return render_template('index.html')

if __name__ == '__main__':
    if not all_leagues_data: print("КРИТИЧЕСКАЯ ОШИБКА: Не удалось загрузить players.csv")
    else:
        print("Сервер запускается...")
        socketio.run(app, debug=True)