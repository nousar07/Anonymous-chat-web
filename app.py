from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
import random
from datetime import datetime
import os
import json
import gevent  # Gevent ki async handling use karne ke liye
from pywebpush import webpush, WebPushException

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# Tracks active users
active_users = {}

# Permanent JSON Storage Database Setup
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
        for sid, info in active_users.items()
    ]
    emit('user_count', {'count': len(active_users)}, broadcast=True)
    emit('update_users_list', users_list, broadcast=True)

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
            print(f"[JSON DB] New secure subscription locked. Total active devices: {len(current_subs)}")
    return jsonify({"status": "success", "message": "Subscription securely locked."}), 200

# Sabse safe tarika: Gevent context ke andar HTTP request ko process pool/isolated pool me bhej dena
def send_push_isolated(sub, payload, private_key, claims_email):
    try:
        # Yeh line gevent ko bolti hai ki requests block ko safely background context me execute kare
        with gevent.Timeout(10, False):
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": claims_email}
            )
            print("[WebPush] Notification payload delivered successfully to a device.")
            return True
    except WebPushException as ex:
        print(f"[WebPush] Failed sending to a device: {ex}")
        if ex.response and ex.response.status_code in [404, 410]:
            return "remove"
    except Exception as e:
        print(f"[WebPush] Isolated exception caught: {e}")
    return False

def trigger_background_push(title, body_text):
    public_key = os.environ.get("VAPID_PUBLIC_KEY")
    private_key = os.environ.get("VAPID_PRIVATE_KEY")
    claims_email = "mailto:nousarmazumder@gmail.com"
    
    if not public_key or not private_key:
        print("[VAPID] Keys not found in environment variables. Skipping background push.")
        return

    payload = json.dumps({
        "title": title,
        "body": body_text,
        "icon": "https://cdn-icons-png.flaticon.com/512/207/207219.png"
    })

    current_subs = load_subscriptions()
    if not current_subs:
        print("[WebPush] No active device subscriptions found in file database.")
        return

    # Gevent greenlet pool ka use karke non-blocking fire-and-forget call karenge
    def run_pool():
        removable_subs = []
        for sub in current_subs:
            res = send_push_isolated(sub, payload, private_key, claims_email)
            if res == "remove":
                removable_subs.append(sub)
        
        if removable_subs:
            updated_subs = [s for s in current_subs if s not in removable_subs]
            save_subscriptions_to_file(updated_subs)

    # Gevent task spawn kar do, bina thread ke recursion crash bypass ho jayega
    gevent.spawn(run_pool)

# ---------------- -------------------------------------------------------

@app.route('/')
def home():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    emit('user_count', {'count': len(active_users)})

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
        "status": "online"
    }
    
    emit('session_joined', {"username": anon_name})
    broadcast_user_list_and_count()
    emit('system_message', {'msg': f"🟢 {anon_name} entered the session"}, broadcast=True)

    # Push Trigger
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
        'gender': user_info['gender'],
        'msg': data['msg'],
        'reply_to': data.get('reply_to')
    }
    emit('new_message', payload, broadcast=True)

@socketio.on('typing')
def handle_typing(data):
    user_info = active_users.get(request.sid)
    if user_info:
        emit('user_typing', {'username': user_info['username'], 'is_typing': data['is_typing']}, broadcast=True, include_self=False)
        active_users[request.sid]['status'] = 'typing' if data['is_typing'] else 'online'
        users_list = [
            {"id": sid[:6], "alias": info["username"], "gender": info["gender"], "status": info.get("status", "online")} 
            for sid, info in active_users.items()
        ]
        emit('update_users_list', users_list, broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    user_info = active_users.pop(request.sid, None)
    if user_info:
        broadcast_user_list_and_count()
        emit('system_message', {'msg': f"🔴 {user_info['username']} left the session"}, broadcast=True)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    socketio.run(app, host='0.0.0.0', port=port)