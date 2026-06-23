from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
import random
from datetime import datetime
import os
import json
import threading  # SSL/Gevent recursion depth crash ko fix karne ke liye
from pywebpush import webpush, WebPushException

app = Flask(__name__)
# Render par sahi se background sync karne ke liye cors aur gevent ensure kiya
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# Tracks active users: { request.sid: {"username": str, "gender": str, "id": str, "status": str} }
active_users = {}

# Permanent JSON Storage Database Setup (Server restart par bhi active tokens delete nahi honge)
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

# Strict Capacity Cap
MAX_USERS = 200

# Helper function sabhi ko updated users list broadcast karne ke liye
def broadcast_user_list_and_count():
    # Frontend ko bhejte waqt dict se list format bana rahe hain
    users_list = [
        {
            "id": sid[:6], 
            "alias": info["username"], 
            "gender": info["gender"], 
            "status": info.get("status", "online")
        } 
        for sid, info in active_users.items()
    ]
    # Sabhi ko updated users count aur puri list broadcast karein
    emit('user_count', {'count': len(active_users)}, broadcast=True)
    emit('update_users_list', users_list, broadcast=True)

# ----------------- BACKGROUND PUSH NOTIFICATION ROUTES -----------------

# 1. Router jo Browser ko 'sw.js' file direct serve karega (Yeh 404 error fix karega)
@app.route('/sw.js')
def serve_sw():
    return send_from_directory('.', 'sw.js', mimetype='application/javascript')

# 2. Endpoint jahan browser apna Push Token (Subscription) securely register karega
@app.route('/api/save-subscription', methods=['POST'])
def save_subscription():
    sub_data = request.get_json()
    if sub_data:
        current_subs = load_subscriptions()
        # Duplicate entry check karne ke liye string comparison (same device dobara add na ho)
        sub_string_list = [json.dumps(s, sort_keys=True) for s in current_subs]
        new_sub_string = json.dumps(sub_data, sort_keys=True)
        
        if new_sub_string not in sub_string_list:
            current_subs.append(sub_data)
            save_subscriptions_to_file(current_subs)
            print(f"[JSON DB] New secure subscription locked. Total active devices: {len(current_subs)}")
    return jsonify({"status": "success", "message": "Subscription securely locked."}), 200

# 3. Helper function background mein push notification send karne ke liye
def trigger_background_push(title, body_text):
    def send_push_async():
        # Render ke Environment variables se keys read karna
        public_key = os.environ.get("VAPID_PUBLIC_KEY")
        private_key = os.environ.get("VAPID_PRIVATE_KEY")
        claims_email = "mailto:nousarmazumder@gmail.com"
        
        if not public_key or not private_key:
            print("[VAPID] Keys not found in environment variables. Skipping background push.")
            return

        payload = json.dumps({
            "title": title,
            "body": body_text,
            "icon": "https://cdn-icons-png.flaticon.com/512/207/207219.png" # Standard chat bubble icon
        })

        current_subs = load_subscriptions()
        if not current_subs:
            print("[WebPush] No active device subscriptions found in file database.")
            return

        removable_subs = []
        for sub in current_subs:
            try:
                # Threading block se gevent ka recursion depth exception 100% bypass ho jayega
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=private_key,
                    vapid_claims={"sub": claims_email}
                )
                print("[WebPush] Notification payload delivered successfully to a device.")
            except WebPushException as ex:
                print(f"[WebPush] Failed sending to a device: {ex}")
                # Agar device expire ya unsubscribe ho gaya ho (404/410), toh use mita do
                if ex.response and ex.response.status_code in [404, 410]:
                    removable_subs.append(sub)
            except Exception as e:
                print(f"[WebPush] Unexpected notification exception bypassed: {e}")

        # Expired subscriptions ko db file se clean karein
        if removable_subs:
            updated_subs = [s for s in current_subs if s not in removable_subs]
            save_subscriptions_to_file(updated_subs)

    # Main thread aur gevent loop block na ho, isliye alag background thread me run karein
    threading.Thread(target=send_push_async).start()

# -----------------------------------------------------------------------

@app.route('/')
def home():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    # Pehli baar connect hone par sirf temporary count (join karne se pehle)
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
    
    # Aapke user_info mein 'id' aur 'status' ko bhi track par lagaya
    active_users[request.sid] = {
        "username": anon_name,
        "gender": gender,
        "status": "online"
    }
    
    emit('session_joined', {"username": anon_name})
    
    # Broadcast fresh stats to everyone
    broadcast_user_list_and_count()
    emit('system_message', {'msg': f"🟢 {anon_name} entered the session"}, broadcast=True)

    # Trigger Background System Push Notification (Jab koi naya join kare)
    trigger_background_push(
        title="Anonymous Chat",
        body_text=f"🟢 {anon_name} has just entered the session. Say Hi!"
    )

@socketio.on('message')
def handle_message(data):
    user_info = active_users.get(request.sid)
    if not user_info:
        return
        
    # Ab hum timestamp backend se nahi bhejenge, frontend khud create karega
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
        # 1. Purana chat window wala typing text handle karne ke liye (include_self=False)
        emit('user_typing', {'username': user_info['username'], 'is_typing': data['is_typing']}, broadcast=True, include_self=False)
        
        # 2. Sidebar dashboard mein user ki dot state live change karne ke liye logic
        active_users[request.sid]['status'] = 'typing' if data['is_typing'] else 'online'
        
        # Sirf list status broadcast karein bina count ched-chaad kiye
        users_list = [
            {"id": sid[:6], "alias": info["username"], "gender": info["gender"], "status": info.get("status", "online")} 
            for sid, info in active_users.items()
        ]
        emit('update_users_list', users_list, broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    user_info = active_users.pop(request.sid, None)
    if user_info:
        # Sabhi ko batao ki koi gaya aur list refresh karo
        broadcast_user_list_and_count()
        emit('system_message', {'msg': f"🔴 {user_info['username']} left the session"}, broadcast=True)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    socketio.run(app, host='0.0.0.0', port=port)