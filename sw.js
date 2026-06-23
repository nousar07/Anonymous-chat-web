self.addEventListener('push', function(event) {
    let data = { title: 'Anonymous Chat', body: 'Someone joined the session!' };
    
    if (event.data) {
        try {
            data = event.data.json();
        } catch (e) {
            data = { title: 'Anonymous Chat', body: event.data.text() };
        }
    }

    const options = {
        body: data.body,
        icon: 'https://cdn-icons-png.flaticon.com/512/2593/2593468.png',
        badge: 'https://cdn-icons-png.flaticon.com/512/2593/2593468.png',
        vibrate: [200, 100, 200], // Vibration pattern for phones
        data: {
            dateOfArrival: Date.now()
        },
        actions: [
            { action: 'open', title: 'Open Chat 💬' }
        ]
    };

    event.waitUntil(
        self.registration.showNotification(data.title, options)
    );
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            for (let i = 0; i < clientList.length; i++) {
                let client = clientList[i];
                if ('focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow('/');
            }
        })
    );
});