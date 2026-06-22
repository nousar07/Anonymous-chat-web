from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import random
from datetime import datetime
import os

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Tracks active users: { request.sid: {"username": str, "gender": str} }
active_users = {}

# Strict Capacity Cap
MAX_USERS = 200

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
        "gender": gender
    }
    
    emit('session_joined', {"username": anon_name})
    emit('user_count', {'count': len(active_users)}, broadcast=True)
    emit('system_message', {'msg': f"🟢 {anon_name} entered the session"}, broadcast=True)

@socketio.on('message')
def handle_message(data):
    user_info = active_users.get(request.sid)
    if not user_info:
        return
        
    current_time = datetime.now().strftime("%I:%M %p")
        
    payload = {
        'sender': user_info['username'],
        'gender': user_info['gender'],
        'msg': data['msg'],
        'reply_to': data.get('reply_to'),
        'timestamp': current_time
    }
    emit('new_message', payload, broadcast=True)

@socketio.on('typing')
def handle_typing(data):
    user_info = active_users.get(request.sid)
    if user_info:
        emit('user_typing', {'username': user_info['username'], 'is_typing': data['is_typing']}, broadcast=True, include_self=False)

@socketio.on('disconnect')
def handle_disconnect():
    user_info = active_users.pop(request.sid, None)
    if user_info:
        emit('user_count', {'count': len(active_users)}, broadcast=True)
        emit('system_message', {'msg': f"🔴 {user_info['username']} left the session"}, broadcast=True)

if __name__ == '__main__':
    # Bypasses local port 5000 conflicts dynamically
    port = int(os.environ.get("PORT", 5001))
    socketio.run(app, host='0.0.0.0', port=port, debug=True)