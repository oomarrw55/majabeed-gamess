from flask import Flask, request, jsonify, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
import random, string, os, uuid
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.secret_key = 'majabeed_2024_secret'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

users_db   = {}   # uid → {name, avatar, wins:[]}
servers_db = {}   # sid → server
games_db   = {}   # sid → game

SUITS = ['♠','♥','♦','♣']
RANKS = ['2','3','4','5','6','7','8','9','10','J','Q','K','A']
POINTS = {'10':10,'J':10,'Q':10,'K':10,'A':10,'JOKER':50}

def card_pts(r): return POINTS.get(r,0)

def make_deck(n):
    """
    2 لاعبين = 108 ورقة + 6 جواكر
    كل لاعب إضافي = +18 ورقة + 2 جوكر
    """
    base_cards = 108 + (n-2)*18
    base_jokers = 6 + (n-2)*2

    # بناء أوراق متعددة (108 = ~2 deck كامل)
    full_deck = []
    deck_count = 0
    while len(full_deck) < base_cards + base_jokers:
        for suit in SUITS:
            for rank in RANKS:
                full_deck.append({'rank':rank,'suit':suit,'id':f"{rank}{suit}_{deck_count}"})
        deck_count += 1

    # اختر بالضبط base_cards ورقة
    cards = full_deck[:base_cards]

    # أضف الجواكر
    for i in range(base_jokers):
        cards.append({'rank':'JOKER','suit':'★','id':f'JOK{i}'})

    # Fisher-Yates shuffle
    for i in range(len(cards)-1, 0, -1):
        j = random.randint(0,i)
        cards[i], cards[j] = cards[j], cards[i]

    return cards

def deal(deck, n):
    idx = 0
    hands = {}
    for i in range(n):
        hands[i] = deck[idx:idx+7]; idx += 7
    field = deck[idx:idx+7]; idx += 7
    draw  = deck[idx:]
    return hands, field, draw

def make_teams(order, mode='auto'):
    n = len(order)
    shuffled = order[:]
    random.shuffle(shuffled)
    if mode == 'solo' or n < 4:
        teams = {p:f"solo_{i}" for i,p in enumerate(shuffled)}
        tlist = [[p] for p in shuffled]
    elif n == 4:
        teams = {shuffled[0]:'A',shuffled[1]:'B',shuffled[2]:'A',shuffled[3]:'B'}
        tlist = [[shuffled[0],shuffled[2]],[shuffled[1],shuffled[3]]]
    elif n == 5:
        teams = {shuffled[0]:'A',shuffled[1]:'A',shuffled[2]:'B',shuffled[3]:'B',shuffled[4]:'B'}
        tlist = [shuffled[:2],shuffled[2:]]
    elif n == 6:
        teams = {p:('A' if i<3 else 'B') for i,p in enumerate(shuffled)}
        tlist = [shuffled[:3],shuffled[3:]]
    elif n == 7:
        teams = {p:('A' if i<3 else 'B') for i,p in enumerate(shuffled)}
        tlist = [shuffled[:3],shuffled[3:]]
    else:
        teams = {p:('A' if i<4 else 'B') for i,p in enumerate(shuffled)}
        tlist = [shuffled[:4],shuffled[4:]]
    return teams, tlist

def uname(u): return users_db.get(u,{}).get('name',u)
def uav(u):   return users_db.get(u,{}).get('avatar','')

def broadcast_players(sid):
    if sid not in servers_db: return
    srv = servers_db[sid]
    def info(p): return {'id':p,'name':uname(p),'avatar':uav(p)}
    emit('players_update',{
        'players':    [info(p) for p in srv['players']],
        'spectators': [info(p) for p in srv.get('spectators',[])],
        'host_id':    srv['host_id']
    }, room=sid)

def cleanup_server(sid):
    """احذف الخادم إذا فرغ من اللاعبين"""
    if sid not in servers_db: return
    srv = servers_db[sid]
    if len(srv['players']) == 0:
        servers_db.pop(sid, None)
        games_db.pop(sid, None)

# ── HTTP ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index(): return send_file(os.path.join(BASE_DIR,'index.html'))

@app.route('/api/register', methods=['POST'])
def register():
    d = request.json or {}
    name = d.get('name','').strip()
    avatar = d.get('avatar','')
    if not name: return jsonify({'error':'الاسم مطلوب'}),400
    uid = str(uuid.uuid4())
    users_db[uid] = {'name':name,'avatar':avatar,'wins':[]}
    return jsonify({'user_id':uid,'name':name})

@app.route('/api/user/<uid>')
def get_user(uid):
    if uid in users_db: return jsonify({'ok':True,'user':users_db[uid]})
    return jsonify({'ok':False}),404

@app.route('/api/user/<uid>', methods=['PATCH'])
def update_user(uid):
    if uid not in users_db: return jsonify({'error':'not found'}),404
    d = request.json or {}
    if 'name' in d and d['name'].strip(): users_db[uid]['name']=d['name'].strip()
    if 'avatar' in d: users_db[uid]['avatar']=d['avatar']
    return jsonify({'ok':True,'user':users_db[uid]})

@app.route('/api/user/<uid>/wins')
def get_wins(uid):
    if uid not in users_db: return jsonify({'wins':[]})
    return jsonify({'wins': list(reversed(users_db[uid].get('wins',[])))})

@app.route('/api/servers')
def list_servers():
    return jsonify([{
        'id':sid,'name':s['name'],
        'players':len(s['players']),'max_players':s['max_players'],
        'status':s['status'],'has_password':bool(s['password']),
        'mode':s.get('mode','auto'),
        'spectators':len(s.get('spectators',[]))
    } for sid,s in servers_db.items()])

@app.route('/api/servers/create', methods=['POST'])
def create_server():
    d = request.json or {}
    name=d.get('name','').strip(); password=d.get('password','')
    host_id=d.get('user_id',''); max_pl=int(d.get('max_players',4))
    mode=d.get('mode','auto')
    if not name: return jsonify({'error':'اسم الخادم مطلوب'}),400
    if host_id not in users_db: return jsonify({'error':'يجب تسجيل الدخول أولاً'}),401
    sid=''.join(random.choices(string.ascii_uppercase+string.digits,k=6))
    servers_db[sid]={'name':name,'password':password,'host_id':host_id,
        'players':[host_id],'spectators':[],'status':'waiting',
        'max_players':max(2,min(8,max_pl)),'mode':mode,
        'created_at':datetime.now().isoformat()}
    return jsonify({'server_id':sid,'name':name})

@app.route('/api/servers/join', methods=['POST'])
def join_server():
    d = request.json or {}
    sid=d.get('server_id',''); password=d.get('password','')
    uid=d.get('user_id',''); as_spec=d.get('spectator',False)
    if sid not in servers_db: return jsonify({'error':'الخادم غير موجود'}),404
    if uid not in users_db:   return jsonify({'error':'يجب تسجيل الدخول'}),401
    srv=servers_db[sid]
    if srv['password'] and srv['password']!=password: return jsonify({'error':'كلمة المرور خاطئة'}),403
    if as_spec:
        if uid not in srv['spectators']: srv['spectators'].append(uid)
    else:
        if srv['status']=='playing': return jsonify({'error':'اللعبة جارية'}),400
        if len(srv['players'])>=srv['max_players']: return jsonify({'error':'الخادم ممتلئ'}),400
        if uid not in srv['players']: srv['players'].append(uid)
    info=lambda p:{'id':p,'name':uname(p),'avatar':uav(p)}
    return jsonify({'success':True,'players':[info(p) for p in srv['players']],'spectator':as_spec})

# ── Socket ────────────────────────────────────────────────────────────────────
@socketio.on('join_personal_room')
def on_join_personal(data):
    uid=data.get('user_id')
    if uid: join_room(uid)

@socketio.on('join_server_room')
def on_join_server(data):
    sid=data.get('server_id'); uid=data.get('user_id')
    join_room(sid); broadcast_players(sid)

@socketio.on('leave_server')
def on_leave_server(data):
    sid=data.get('server_id'); uid=data.get('user_id')
    if sid in servers_db:
        srv=servers_db[sid]
        if uid in srv['players']: srv['players'].remove(uid)
        if uid in srv.get('spectators',[]): srv['spectators'].remove(uid)
        # إذا كان المضيف هو من غادر، انقل المضيفية
        if srv['host_id']==uid and srv['players']:
            srv['host_id']=srv['players'][0]
        leave_room(sid)
        broadcast_players(sid)
        cleanup_server(sid)

@socketio.on('kick_player')
def on_kick(data):
    sid=data.get('server_id'); hid=data.get('host_id'); tid=data.get('target_id')
    if sid in servers_db:
        srv=servers_db[sid]
        if srv['host_id']==hid and tid in srv['players']:
            srv['players'].remove(tid)
            emit('kicked',{'user_id':tid},room=sid)
            broadcast_players(sid)
            cleanup_server(sid)

@socketio.on('start_game')
def on_start(data):
    sid=data.get('server_id'); hid=data.get('host_id')
    if sid not in servers_db: return
    srv=servers_db[sid]
    if srv['host_id']!=hid: return
    players=srv['players']; n=len(players)
    if n<2:
        emit('error',{'msg':'يجب أن يكون هناك لاعبان على الأقل'},room=sid); return

    deck=make_deck(n)
    hands,field,draw=deal(deck,n)
    order=players[:]
    random.shuffle(order)

    teams,tlist=make_teams(order,srv.get('mode','auto'))
    tnames={}
    for i,grp in enumerate(tlist):
        tid=chr(65+i)
        for p in grp: tnames[p]=f"الفريق {tid}"

    game={
        'sid':sid,'players':order,
        'hands':{order[i]:hands[i] for i in range(n)},
        'field':field,'draw':draw,
        'banks':{p:[] for p in order},
        'turn_idx':0,'status':'playing',
        'claim':None,
        'teams':teams,'tlist':tlist,'tnames':tnames,
        'tcolors':{'A':'#4488ff','B':'#ff6644'},
        'start_time':datetime.now().isoformat(),
        'eaten_this_turn':False,
        'grab_bank_mode':False,  # هل اللاعب في وضع هات بنك
        'grab_target':None,      # البنك المختار
    }
    games_db[sid]=game
    srv['status']='playing'

    pnames={p:uname(p) for p in order}
    pavatars={p:uav(p) for p in order}
    cur=order[0]

    for p in order:
        emit('game_started',{
            'your_hand':game['hands'][p],
            'field':field,'draw_count':len(draw),
            'players':order,'player_names':pnames,'player_avatars':pavatars,
            'current_turn':cur,'your_id':p,
            'teams':teams,'tnames':tnames,'tlist':tlist,
            'tcolors':game['tcolors'],
            'total_deck':len(deck),
            'num_players':n,
        },room=p)

    emit('game_started_broadcast',{
        'field':field,'draw_count':len(draw),
        'players':order,'player_names':pnames,'current_turn':cur
    },room=sid)

# ── auto draw ─────────────────────────────────────────────────────────────────
def auto_draw(game,pid):
    drawn=[]
    while len(game['hands'][pid])<7 and game['draw']:
        c=game['draw'].pop(0)
        game['hands'][pid].append(c)
        drawn.append(c)
    return drawn

def advance_turn(game):
    n=len(game['players'])
    game['turn_idx']=(game['turn_idx']+1)%n
    game['eaten_this_turn']=False
    game['grab_bank_mode']=False
    game['grab_target']=None
    nxt=game['players'][game['turn_idx']]
    drawn=auto_draw(game,nxt)
    return nxt,drawn

def pub_hands_count(game):
    return {p:len(game['hands'][p]) for p in game['players']}

def pub_banks_count(game):
    return {p:len(game['banks'][p]) for p in game['players']}

def top_card(bank):
    """أعلى ورقة فقط (لا تكشف الباقي)"""
    if not bank: return None
    c=bank[-1]
    return {'rank':c['rank'],'suit':c['suit'],'id':c['id']}

# ── CLAIM ─────────────────────────────────────────────────────────────────────
@socketio.on('claim_card')
def on_claim(data):
    sid=data.get('server_id'); uid=data.get('user_id')
    hand_id=data.get('hand_card'); field_id=data.get('field_card')
    if sid not in games_db: return
    g=games_db[sid]
    cur=g['players'][g['turn_idx']]
    if cur!=uid:
        emit('error',{'msg':'ليس دورك'},to=uid); return
    hcard=next((c for c in g['hands'][uid] if c['id']==hand_id),None)
    fcard=next((c for c in g['field']       if c['id']==field_id),None)
    if not hcard: emit('error',{'msg':'الورقة غير في يدك'},to=uid); return
    if not fcard: emit('error',{'msg':'الورقة غير في الميدان'},to=uid); return
    if hcard['rank']!=fcard['rank'] and hcard['rank']!='JOKER' and fcard['rank']!='JOKER':
        emit('error',{'msg':'الأرقام لا تتطابق'},to=uid); return

    g['claim']={
        'pid':uid,'hcard':hcard,'fcard':fcard,
        'challengers':[],'chal_cards':{},'chal_times':{},'resolved':False
    }
    g['hands'][uid]=[c for c in g['hands'][uid] if c['id']!=hand_id]

    emit('claim_announced',{
        'player_id':uid,'player_name':uname(uid),
        'hand_card':hcard,'field_card':fcard
    },room=sid)

# ── CHALLENGE ── أول معارضة تُعتمد ─────────────────────────────────────────
@socketio.on('challenge_claim')
def on_challenge(data):
    sid=data.get('server_id'); uid=data.get('user_id'); cid=data.get('card')
    if sid not in games_db: return
    g=games_db[sid]
    cl=g.get('claim')
    if not cl or cl['resolved']: return
    if uid==cl['pid']: return
    # ✅ بدون قيود فريق — أي لاعب يعارض أي لاعب
    card=next((c for c in g['hands'][uid] if c['id']==cid),None)
    if not card: return
    tr=cl['hcard']['rank']
    if card['rank']!=tr and card['rank']!='JOKER':
        emit('error',{'msg':'الورقة لا تطابق'},to=uid); return

    import time
    now=time.time()

    # أول معارضة فقط تُعتمد
    if len(cl['challengers'])==0:
        cl['challengers'].append(uid)
        cl['chal_cards'][uid]=card
        cl['chal_times'][uid]=now
        g['hands'][uid]=[c for c in g['hands'][uid] if c['id']!=cid]
        emit('challenge_announced',{
            'player_id':uid,'player_name':uname(uid),'card':card
        },room=sid)
    else:
        # ثانية+ خلال 3 ثوانٍ → مرفوضة
        first_time=list(cl['chal_times'].values())[0]
        if now-first_time<=3.0:
            emit('error',{'msg':'تم قبول معارضة أخرى بالفعل'},to=uid)
        else:
            # بعد 3 ثوانٍ → معارضة جديدة مقبولة (على الفائز الأول)
            cl['challengers'].append(uid)
            cl['chal_cards'][uid]=card
            cl['chal_times'][uid]=now
            g['hands'][uid]=[c for c in g['hands'][uid] if c['id']!=cid]
            emit('challenge_announced',{
                'player_id':uid,'player_name':uname(uid),'card':card
            },room=sid)

# ── RESOLVE ───────────────────────────────────────────────────────────────────
@socketio.on('resolve_claim')
def on_resolve(data):
    sid=data.get('server_id'); uid=data.get('user_id')
    if sid not in games_db: return
    g=games_db[sid]
    cl=g.get('claim')
    if not cl or cl['pid']!=uid or cl['resolved']: return
    cl['resolved']=True

    challengers=cl['challengers']
    winner=uid
    if len(challengers)==0:
        won=[cl['hcard'],cl['fcard']]
    else:
        # أول معارض فقط يأخذ
        first=challengers[0]
        won=[cl['hcard'],cl['fcard'],cl['chal_cards'][first]]
        winner=first

    g['banks'][winner].extend(won)
    g['field']=[c for c in g['field'] if c['id']!=cl['fcard']['id']]
    g['claim']=None
    g['eaten_this_turn']=True

    # السحب للفائز
    drawn=auto_draw(g,winner)

    all_done=all(len(g['hands'][p])==0 for p in g['players']) and not g['draw']
    if all_done:
        _end_game(sid,g); return

    hc=pub_hands_count(g); bc=pub_banks_count(g)

    emit('claim_resolved',{
        'winner':winner,'winner_name':uname(winner),
        'won_cards':won,
        'banks_count':bc,'field':g['field'],
        'draw_count':len(g['draw']),
        'same_turn':True,'current_turn':g['players'][g['turn_idx']],
        'hands_count':hc,'drawn':drawn
    },room=sid)

    for p in g['players']:
        u={'hand':g['hands'][p]}
        if p==winner and drawn: u['drawn']=drawn
        emit('your_hand',u,to=p)

    # أرسل أعلى ورقة فقط للبنوك
    emit('banks_top_update',{
        'tops':{p:top_card(g['banks'][p]) for p in g['players']},
        'counts':bc
    },room=sid)

# ── MEYDANA ───────────────────────────────────────────────────────────────────
@socketio.on('meydana')
def on_meydana(data):
    sid=data.get('server_id'); uid=data.get('user_id'); cid=data.get('card_id')
    if sid not in games_db: return
    g=games_db[sid]
    if g['players'][g['turn_idx']]!=uid:
        emit('error',{'msg':'ليس دورك'},to=uid); return
    card=next((c for c in g['hands'][uid] if c['id']==cid),None)
    if not card:
        emit('error',{'msg':'الورقة غير في يدك'},to=uid); return

    g['hands'][uid]=[c for c in g['hands'][uid] if c['id']!=cid]
    g['field'].append(card)
    nxt,drawn=advance_turn(g)

    all_done=all(len(g['hands'][p])==0 for p in g['players']) and not g['draw']
    if all_done:
        _end_game(sid,g); return

    emit('meydana_done',{
        'player_id':uid,'player_name':uname(uid),'card':card,
        'field':g['field'],'next_turn':nxt,
        'draw_count':len(g['draw']),'hands_count':pub_hands_count(g)
    },room=sid)
    for p in g['players']:
        u={'hand':g['hands'][p]}
        if p==nxt and drawn: u['drawn']=drawn
        emit('your_hand',u,to=p)

# ── END TURN ─────────────────────────────────────────────────────────────────
@socketio.on('end_turn')
def on_end_turn(data):
    sid=data.get('server_id'); uid=data.get('user_id')
    if sid not in games_db: return
    g=games_db[sid]
    if g['players'][g['turn_idx']]!=uid:
        emit('error',{'msg':'ليس دورك'},to=uid); return
    nxt,drawn=advance_turn(g)
    all_done=all(len(g['hands'][p])==0 for p in g['players']) and not g['draw']
    if all_done:
        _end_game(sid,g); return
    emit('turn_changed',{
        'next_turn':nxt,'draw_count':len(g['draw']),'hands_count':pub_hands_count(g)
    },room=sid)
    for p in g['players']:
        u={'hand':g['hands'][p]}
        if p==nxt and drawn: u['drawn']=drawn
        emit('your_hand',u,to=p)

# ── GRAB BANK ─────────────────────────────────────────────────────────────────
@socketio.on('request_grab_bank')
def on_request_grab(data):
    """يطلب قائمة البنوك المتاحة (أعلى ورقة فقط)"""
    sid=data.get('server_id'); uid=data.get('user_id')
    if sid not in games_db: return
    g=games_db[sid]
    if g['players'][g['turn_idx']]!=uid:
        emit('error',{'msg':'ليس دورك'},to=uid); return

    banks_info=[]
    for p in g['players']:
        if p!=uid and g['banks'][p]:
            banks_info.append({
                'player_id':p,'player_name':uname(p),
                'count':len(g['banks'][p]),
                'top_card':top_card(g['banks'][p])
            })

    emit('grab_bank_list',{'banks':banks_info},to=uid)

@socketio.on('grab_bank')
def on_grab_bank(data):
    """يسحب أعلى ورقة من بنك خصم ويضعها في يده"""
    sid=data.get('server_id'); uid=data.get('user_id')
    target_id=data.get('target_id')
    if sid not in games_db: return
    g=games_db[sid]
    if g['players'][g['turn_idx']]!=uid:
        emit('error',{'msg':'ليس دورك'},to=uid); return
    if not g['banks'].get(target_id):
        emit('error',{'msg':'البنك فارغ'},to=uid); return

    top=g['banks'][target_id].pop()
    g['hands'][uid].append(top)

    bc=pub_banks_count(g)
    emit('bank_grabbed',{
        'grabber_id':uid,'grabber_name':uname(uid),
        'target_id':target_id,'target_name':uname(target_id),
        'card':top,'banks_count':bc
    },room=sid)
    emit('your_hand',{'hand':g['hands'][uid]},to=uid)
    emit('banks_top_update',{
        'tops':{p:top_card(g['banks'][p]) for p in g['players']},
        'counts':bc
    },room=sid)

# ── BURY ─────────────────────────────────────────────────────────────────────
@socketio.on('bury_cards')
def on_bury(data):
    sid=data.get('server_id'); uid=data.get('user_id'); ids=data.get('card_ids',[])
    if sid not in games_db: return
    g=games_db[sid]
    to_bury=[c for c in g['field'] if c['id'] in ids]
    if not to_bury: return
    g['banks'][uid].extend(to_bury)
    g['field']=[c for c in g['field'] if c['id'] not in ids]
    emit('cards_buried',{
        'player_id':uid,'cards':to_bury,'field':g['field'],
        'banks_count':pub_banks_count(g)
    },room=sid)
    emit('banks_top_update',{
        'tops':{p:top_card(g['banks'][p]) for p in g['players']},
        'counts':pub_banks_count(g)
    },room=sid)

# ── END GAME ─────────────────────────────────────────────────────────────────
def _end_game(sid,g):
    scores={}; details={}
    for p in g['players']:
        bank=g['banks'][p]
        pts=sum(card_pts(c['rank']) for c in bank)
        scores[p]=pts
        details[p]={'no_pts':[c for c in bank if card_pts(c['rank'])==0],
                    'with_pts':[c for c in bank if card_pts(c['rank'])>0],'total':pts}

    ranked=sorted(scores.keys(),key=lambda p:scores[p],reverse=True)
    team_scores={}
    for p,t in g['teams'].items():
        team_scores[t]=team_scores.get(t,0)+scores.get(p,0)

    g['status']='ended'
    if sid in servers_db: servers_db[sid]['status']='waiting'

    now=datetime.now()

    # احفظ في أرشيف الفوز
    for i,p in enumerate(ranked[:3]):
        if p in users_db:
            win_entry={
                'date':now.strftime('%Y-%m-%d'),
                'time':now.strftime('%H:%M'),
                'points':scores[p],
                'rank':i+1,
                'match_id':sid,
                'mode':servers_db.get(sid,{}).get('mode','auto'),
                'players':len(g['players']),
                'timestamp':now.isoformat()
            }
            if 'wins' not in users_db[p]: users_db[p]['wins']=[]
            users_db[p]['wins'].append(win_entry)

    emit('game_ended',{
        'scores':scores,'details':details,'ranking':ranked,
        'pnames':{p:uname(p) for p in g['players']},
        'pavatars':{p:uav(p) for p in g['players']},
        'top3':ranked[:3],'teams':g['teams'],
        'tnames':g['tnames'],'tlist':g['tlist'],
        'team_scores':team_scores,'start_time':g.get('start_time','')
    },room=sid)

if __name__ == '__main__':
    import os
    # المنصة تعطي السيرفر بورت تلقائي، وإذا ما لقيته بنشغل على 10000 وهو البورت الافتراضي لـ Render
    port = int(os.environ.get('PORT', 10000))
    print(f"🚀 تشغيل لعبة مجابيد على البورت: {port}")
    # تشغيل السيرفر مباشرة عبر socketio بالاعتماد على eventlet القوي
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)