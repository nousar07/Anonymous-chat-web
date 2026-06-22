from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import random
from datetime import datetime
import os

app = Flask(__name__)
# Render par sahi se background sync karne ke liye cors aur gevent ensure kiya
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# Tracks active users: { request.sid: {"username": str, "gender": str, "id": str, "status": str} }
active_users = {}

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
        # timestamp yahan se hata diya
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