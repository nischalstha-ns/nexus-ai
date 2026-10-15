# NEXUS Companion (Android)

Minimal companion app for the NEXUS agent service: approve/deny elevated
tool calls and read notifications from your phone.

## How it connects

The agent service binds **loopback only** (`127.0.0.1`) and refuses LAN
binds — your data never listens on the open network. Reach it from the
phone through a tunnel you control:

**Option A — adb (USB):**
```bash
adb reverse tcp:8765 tcp:8765
# base URL in the app: http://127.0.0.1:8765
```

**Option B — Tailscale:** join both devices to your tailnet, then use
`http://<desktop-tailscale-ip>:8765` as the base URL.

## Pairing

1. Start the agent service on the desktop.
2. Generate a one-time code (valid 10 minutes, single use):
   ```bash
   curl -s -X POST localhost:8765/pairing/code
   ```
3. In the app: enter the base URL, the code, a device name → Pair.
4. The device token is shown once and stored by the app for API calls
   (`Authorization: Bearer <token>`).

Revoke a device any time:
```bash
curl -s localhost:8765/devices
curl -s -X DELETE localhost:8765/devices/<device-id>
```

## Build

```bash
export PATH=~/workspace/sdks/flutter/bin:$PATH
flutter pub get
flutter analyze
flutter build apk --release   # needs the Android SDK
```

## What's next (not in this scaffold)

- Secure token storage (flutter_secure_storage) instead of memory.
- Push notifications via FCM — the service already records notifications
  (`GET /companion/notifications`); a small server-side FCM sender is the
  remaining piece for true push.
- Voice input using the `/voice/*` endpoints.
