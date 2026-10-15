# Packaging NEXUS for Windows

> Status: documented path, **not built or verified here** (this machine is
> Linux; there is no Windows VM, no Rust toolchain, no Android SDK).
> Do not claim a shippable installer until these steps run on Windows.

## Agent service (Python)

On a Windows machine with Python 3.12+:

```powershell
cd nexus-ai\services\agent
pip install pyinstaller
pyinstaller --onefile --name nexus-agent `
  --add-data "..\..\data\migrations;data\migrations" `
  nexus\service\app.py
```

- The exe must ship the `data/migrations/*.sql` files next to it.
- First run creates `%APPDATA%\nexus\nexus.db` (set `NEXUS_DATA_DIR`).
- The service still binds loopback only — the desktop shell talks to it
  over `http://127.0.0.1:8765`.

`packaging/nexus-agent.spec` (PyInstaller spec) is the canonical build
definition — extend it, don't hand-roll commands.

## Desktop shell (Tauri)

Scaffolded in `desktop/`. On Windows with Rust installed:

```powershell
cd nexus-ai\desktop
npm install
npx tauri build
```

This produces the NSIS installer (`*.exe`) / MSI under
`src-tauri\target\release\bundle\`.

## Android companion

```bash
cd nexus-ai/companion
flutter build apk --release
```

Needs the Android SDK. The APK talks to the desktop service over
`adb reverse` or Tailscale (see `companion/README.md`).

## Checklist before calling it shippable

- [ ] `nexus-agent.exe` runs on a clean Windows 10/11 VM
- [ ] Tauri installer installs, starts the service, opens the UI
- [ ] Pairing + approval flow works phone -> desktop over Tailscale
- [ ] `docs/security-review.md` re-verified against the packaged build
