/// HTTP client for the NEXUS agent companion protocol.
///
/// Pairing: POST /pairing/redeem {code, device_name} -> {device_token}
/// Everything else: Authorization: Bearer <device_token>
library;

import 'dart:convert';

import 'package:http/http.dart' as http;

class NexusApi {
  NexusApi(this.baseUrl, {this.token});

  final String baseUrl;
  String? token;

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        if (token != null) 'Authorization': 'Bearer $token',
      };

  Future<Map<String, dynamic>> redeem(String code, String deviceName) async {
    final r = await http.post(Uri.parse('$baseUrl/pairing/redeem'),
        headers: _headers,
        body: jsonEncode({'code': code, 'device_name': deviceName}));
    return _decode(r);
  }

  Future<Map<String, dynamic>> status() async {
    final r = await http.get(Uri.parse('$baseUrl/companion/status'),
        headers: _headers);
    return _decode(r);
  }

  Future<List<dynamic>> approvals() async {
    final m = await status();
    return (m['pending_approvals'] as List?) ?? [];
  }

  Future<void> decide(String taskId, bool approved) async {
    final r = await http.post(
        Uri.parse('$baseUrl/companion/approvals/$taskId'),
        headers: _headers,
        body: jsonEncode({'approved': approved}));
    _decode(r);
  }

  Future<List<dynamic>> notifications({int since = 0}) async {
    final r = await http.get(
        Uri.parse('$baseUrl/companion/notifications?since=$since'),
        headers: _headers);
    final m = _decode(r);
    return (m['notifications'] as List?) ?? [];
  }

  Future<void> markRead(int id) async {
    final r = await http.post(
        Uri.parse('$baseUrl/companion/notifications/$id/read'),
        headers: _headers, body: '{}');
    _decode(r);
  }

  Map<String, dynamic> _decode(http.Response r) {
    final body = r.body.isEmpty ? '{}' : jsonDecode(r.body);
    if (r.statusCode < 200 || r.statusCode >= 300) {
      throw NexusApiError(
          (body is Map && body['error'] is String)
              ? body['error'] as String
              : 'HTTP ${r.statusCode}');
    }
    return body as Map<String, dynamic>;
  }
}

class NexusApiError implements Exception {
  NexusApiError(this.message);
  final String message;

  @override
  String toString() => 'NexusApiError: $message';
}
