from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import os
import json
import gevent
from pywebpush import webpush, WebPushException

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

active_users = {}
pending_private_requests = {}  

SUBS_FILE = "subscriptions_db.json"

def load_subscriptions():
    if os.path.exists(SUBS_FILE):
        try:
            with open(SUBS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_subscriptions_to_file(subs_list):
    try:
        with open(SUBS_FILE, "w") as f:
            json.dump(subs_list, f)
    except Exception as e:
        print(f"[JSON DB] Error writing subscription: {e}")

MAX_USERS = 200

def broadcast_user_list_and_count():
    users_list = [
        {
            "id": sid[:6], 
            "alias": info["username"], 
            "gender": info["gender"], 
            "status": info.get("status", "online")
        } 
        for sid, info in active_users.items() if info.get("room") == "public"
    ]
    # Broadcast globally so background users inside private rooms also get counts synchronized
    socketio.emit('user_count', {'count': len([s for s, i in active_users.items() if i.get("room") == "public"])}, room="public")
    socketio.emit('update_users_list', users_list, room="public")

# ----------------- BACKGROUND PUSH NOTIFICATION ROUTES -----------------

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('.', 'sw.js', mimetype='application/javascript')

@app.route('/api/save-subscription', methods=['POST'])
def save_subscription():
    sub_data = request.get_json()
    if sub_data:
        current_subs = load_subscriptions()
        sub_string_list = [json.dumps(s, sort_keys=True) for s in current_subs]
        new_sub_string = json.dumps(sub_data, sort_keys=True)
        
        if new_sub_string not in sub_string_list:
            current_subs.append(sub_data)
            save_subscriptions_to_file(current_subs)
    return jsonify({"status": "success", "message": "Subscription securely locked."}), 200

def send_push_isolated(sub, payload, private_key, claims_email):
    try:
        with gevent.Timeout(10, False):
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": claims_email}
            )
            return True
    except WebPushException as ex:
        if ex.response and ex.response.status_code in [404, 410]:
            return "remove"
    except Exception:
        pass
    return False

def trigger_background_push(title, body_text):
    public_key = os.environ.get("VAPID_PUBLIC_KEY")
    private_key = os.environ.get("VAPID_PRIVATE_KEY")
    claims_email = "mailto:nousarmazumder@gmail.com"
    
    if not public_key or not private_key:
        return

    payload = json.dumps({
        "title": title,
        "body": body_text,
        "icon": "https://cdn-icons-png.flaticon.com/512/207/207219.png"
    })

    current_subs = load_subscriptions()
    if not current_subs:
        return

    def run_pool():
        removable_subs = []
        for sub in current_subs:
            res = send_push_isolated(sub, payload, private_key, claims_email)
            if res == "remove":
                removable_subs.append(sub)
        if removable_subs:
            updated_subs = [s for s in current_subs if s not in removable_subs]
            save_subscriptions_to_file(updated_subs)

    gevent.spawn(run_pool)

# -----------------------------------------------------------------------

@app.route('/')
def home():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    emit('user_count', {'count': len([s for s, i in active_users.items() if i.get("room") == "public"])})

@socketio.on('join_session')
def handle_join(data):
    if len(active_users) >= MAX_USERS:
        emit('session_full', {'max_limit': MAX_USERS})
        return

    gender = data.get('gender', 'Male')
    gender_tag = 'M' if gender == 'Male' else 'F'
    random_id = random.randint(100, 999)
    anon_name = f"User_{gender_tag}_{random_id}"
    
    active_users[request.sid] = {
        "username": anon_name,
        "gender": gender,
        "status": "online",
        "room": "public"  
    }
    
    join_room("public")
    emit('session_joined', {"username": anon_name})
    broadcast_user_list_and_count()
    emit('system_message', {'msg': f"🟢 {anon_name} entered the session"}, room="public", broadcast=True)

    trigger_background_push(
        title="Anonymous Chat",
        body_text=f"🟢 {anon_name} has just entered the session. Say Hi!"
    )

@socketio.on('message')
def handle_message(data):
    user_info = active_users.get(request.sid)
    if not user_info:
        return
        
    payload = {
        'sender': user_info['username'],
        'sender_sid': request.sid,
        'gender': user_info['gender'],
        'msg': data['msg'],
        'audioData': data.get('audioData'),
        'reply_to': data.get('reply_to'),
        'target_room': "public" if user_info['room'] == "public" else "private"
    }
    
    # Broadcast pipeline distribution logic matching target setups
    if user_info['room'] == "public":
        emit('new_message', payload, room="public")
    else:
        emit('new_message', payload, room=user_info['room'])

@socketio.on('typing')
def handle_typing(data):
    user_info = active_users.get(request.sid)
    if user_info and user_info['room'] == 'public':
        emit('user_typing', {'username': user_info['username'], 'is_typing': data['is_typing']}, room="public", include_self=False)
        active_users[request.sid]['status'] = 'typing' if data['is_typing'] else 'online'
        users_list = [
            {"id": sid[:6], "alias": info["username"], "gender": info["gender"], "status": info.get("status", "online")} 
            for sid, info in active_users.items() if info['room'] == 'public'
        ]
        emit('update_users_list', users_list, room="public")

# ----------------- PRIVATE MATCHMAKING LOGIC CORES -----------------

def auto_timeout_handler(sender_sid):
    gevent.sleep(10) 
    if sender_sid in pending_private_requests:
        target_sid, _ = pending_private_requests.pop(sender_sid)
        socketio.emit('request_status_update', {'msg': '⏱️ Request timed out! No response.'}, room=sender_sid)
        socketio.emit('request_auto_cancelled', room=target_sid)

@socketio.on('send_private_request')
def handle_private_request(data):
    sender_sid = request.sid
    target_sid = data.get('target_sid')
    
    if not target_sid or target_sid not in active_users:
        emit('request_status_update', {'msg': '❌ User is no longer online.'})
        return
        
    if active_users[target_sid]['room'] != 'public':
        emit('request_status_update', {'msg': '⚠️ User is already inside a private chat.'})
        return

    timeout_job = gevent.spawn(auto_timeout_handler, sender_sid)
    pending_private_requests[sender_sid] = (target_sid, timeout_job)
    
    sender_name = active_users[sender_sid]['username']
    emit('private_request_received', {'sender_sid': sender_sid, 'sender_name': sender_name}, room=target_sid)

@socketio.on('respond_private_request')
def handle_private_response(data):
    target_sid = request.sid 
    sender_sid = data.get('sender_sid')
    accepted = data.get('accepted', False)
    
    if sender_sid not in pending_private_requests:
        return
        
    saved_target_sid, timeout_job = pending_private_requests.pop(sender_sid)
    timeout_job.kill() 
    
    if not accepted:
        socketio.emit('request_status_update', {'msg': '❌ Request was declined by the user.'}, room=sender_sid)
        return
        
    if sender_sid not in active_users or target_sid not in active_users:
        socketio.emit('request_status_update', {'msg': '❌ Partner disconnected.'}, room=sender_sid)
        return

    private_room_id = f"private_{sender_sid[:5]}_{target_sid[:5]}"
    
    # CRITICAL TWEAK: Do NOT leave public channel stream! Just append new private layer room.
    join_room(private_room_id, sid=sender_sid)
    join_room(private_room_id, sid=target_sid)
    
    active_users[sender_sid]['room'] = private_room_id
    active_users[target_sid]['room'] = private_room_id
    
    socketio.emit('switch_to_private_room', {'partner': active_users[target_sid]['username']}, room=sender_sid)
    socketio.emit('switch_to_private_room', {'partner': active_users[sender_sid]['username']}, room=target_sid)
    
    broadcast_user_list_and_count()

@socketio.on('exit_private_room')
def handle_exit_private():
    sid = request.sid
    user_info = active_users.get(sid)
    if not user_info or user_info['room'] == 'public':
        return
        
    private_room_id = user_info['room']
    
    # Locate partner info link mapping setups
    partner_sid = None
    for s_id, info in active_users.items():
        if info['room'] == private_room_id and s_id != sid:
            partner_sid = s_id
            break
            
    # Remove only current client cleanly from tracking room layer
    leave_room(private_room_id, sid=sid)
    active_users[sid]['room'] = "public"
    emit('returned_to_public_lobby')
    
    # Alert remaining isolated partner instead of pulling them forcefully
    if partner_sid:
        socketio.emit('partner_left_private_early', {'msg': f"{user_info['username']} left this private room and went back to public group."}, room=partner_sid)
    
    broadcast_user_list_and_count()

@socketio.on('refresh_lobby_counters')
def handle_counter_sync():
    broadcast_user_list_and_count()

# -------------------------------------------------------------------

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    
    if sid in pending_private_requests:
        target_sid, timeout_job = pending_private_requests.pop(sid)
        timeout_job.kill()
        socketio.emit('request_auto_cancelled', room=target_sid)
        
    user_info = active_users.pop(sid, None)
    if user_info:
        if user_info['room'] != 'public':
            private_room_id = user_info['room']
            for s_id, info in active_users.items():
                if info['room'] == private_room_id:
                    leave_room(private_room_id, sid=s_id)
                    active_users[s_id]['room'] = "public"
                    socketio.emit('returned_to_public_lobby', room=s_id)
                    socketio.emit('system_message', {'msg': f"⚠️ Private connection dropped. User went offline."}, room=s_id)
        else:
            socketio.emit('system_message', {'msg': f"🔴 {user_info['username']} left the session"}, room="public", broadcast=True)
        
        broadcast_user_list_and_count()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    socketio.run(app, host='0.0.0.0', port=port)