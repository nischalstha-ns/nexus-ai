/// NEXUS companion — minimal but functional.
///
/// Screens:
/// 1. Pairing: enter the desktop service URL + the 8-char pairing code.
/// 2. Home: pending approvals (approve/deny), notifications, refresh.
///
/// NOTE: the agent service binds loopback only. Reach it from the phone
/// via `adb reverse tcp:8765 tcp:8765` (then base URL http://127.0.0.1:8765)
/// or over Tailscale (then base URL http://<tailscale-ip>:8765).
library;

import 'package:flutter/material.dart';

import 'api.dart';

void main() => runApp(const NexusCompanionApp());

class NexusCompanionApp extends StatefulWidget {
  const NexusCompanionApp({super.key});

  @override
  State<NexusCompanionApp> createState() => _NexusCompanionAppState();
}

class _NexusCompanionAppState extends State<NexusCompanionApp> {
  NexusApi? _api;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'NEXUS Companion',
      theme: ThemeData(colorSchemeSeed: Colors.indigo, useMaterial3: true),
      home: _api == null
          ? PairingScreen(onPaired: (api) => setState(() => _api = api))
          : HomeScreen(api: _api!),
    );
  }
}

class PairingScreen extends StatefulWidget {
  const PairingScreen({super.key, required this.onPaired});

  final ValueChanged<NexusApi> onPaired;

  @override
  State<PairingScreen> createState() => _PairingScreenState();
}

class _PairingScreenState extends State<PairingScreen> {
  final _url = TextEditingController(text: 'http://127.0.0.1:8765');
  final _code = TextEditingController();
  final _name = TextEditingController(text: 'android');
  String? _error;
  bool _busy = false;

  Future<void> _pair() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final api = NexusApi(_url.text.trim());
      final res = await api.redeem(
          _code.text.trim().toUpperCase(), _name.text.trim());
      api.token = res['device_token'] as String?;
      widget.onPaired(api);
    } on NexusApiError catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Pair with NEXUS')),
      body: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
                'On the desktop: POST /pairing/code, then enter the 8-letter code here.'),
            const SizedBox(height: 16),
            TextField(
                controller: _url,
                decoration:
                    const InputDecoration(labelText: 'Service base URL')),
            TextField(
                controller: _code,
                decoration:
                    const InputDecoration(labelText: 'Pairing code'),
                textCapitalization: TextCapitalization.characters),
            TextField(
                controller: _name,
                decoration:
                    const InputDecoration(labelText: 'Device name')),
            const SizedBox(height: 16),
            if (_error != null)
              Text(_error!, style: const TextStyle(color: Colors.red)),
            FilledButton(
                onPressed: _busy ? null : _pair,
                child: _busy
                    ? const SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Text('Pair')),
          ],
        ),
      ),
    );
  }
}

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key, required this.api});

  final NexusApi api;

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  List<dynamic> _approvals = [];
  List<dynamic> _notes = [];
  String? _error;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final approvals = await widget.api.approvals();
      final notes = await widget.api.notifications();
      setState(() {
        _approvals = approvals;
        _notes = notes;
      });
    } on NexusApiError catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _decide(String taskId, bool approved) async {
    try {
      await widget.api.decide(taskId, approved);
      await _refresh();
    } on NexusApiError catch (e) {
      setState(() => _error = e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('NEXUS'),
        actions: [
          IconButton(
              onPressed: _busy ? null : _refresh,
              icon: const Icon(Icons.refresh)),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _refresh,
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (_error != null)
              Text(_error!, style: const TextStyle(color: Colors.red)),
            Text('Approvals (${_approvals.length})',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            if (_approvals.isEmpty)
              const Text('Nothing waiting for approval.'),
            for (final a in _approvals)
              Card(
                child: ListTile(
                  title: Text(a['title']?.toString() ?? a['id'].toString()),
                  subtitle: Text(
                      'wants: ${a['pending_approval']?['tool'] ?? '?'}'),
                  trailing: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      IconButton(
                          icon: const Icon(Icons.check, color: Colors.green),
                          onPressed: () => _decide(
                              a['id'].toString(), true)),
                      IconButton(
                          icon: const Icon(Icons.close, color: Colors.red),
                          onPressed: () => _decide(
                              a['id'].toString(), false)),
                    ],
                  ),
                ),
              ),
            const SizedBox(height: 16),
            Text('Notifications (${_notes.length})',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            for (final n in _notes)
              Card(
                child: ListTile(
                  title: Text(n['title']?.toString() ?? ''),
                  subtitle: Text(n['body']?.toString() ?? ''),
                  trailing: Text(n['kind']?.toString() ?? ''),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
