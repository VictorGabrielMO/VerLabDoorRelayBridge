# RFID door bridge

A small HTTP service that forwards authenticated unlock requests to an existing
VeRLab Raspberry Pi relay service. It does not operate GPIO or replace RFID.

## Requirements

- Bridge: Python 3.7+; standard library only.
- Existing relay service listening on localhost.
- relay_send.py must run with the same Python interpreter as the relay daemon.
- A trusted LAN. HTTP does not encrypt the authentication token.

## Configuration and deployment

Install bridge.py and relay_send.py into /opt/door-bridge.
Create a dedicated door-bridge system user and /etc/door-bridge directory.
Copy config.example.json to /etc/door-bridge/config.json and edit:

- bind: the Pi LAN address; port defaults to 9080.
- allowed_ips: exact client IP addresses; no wildcard, CIDR or forwarded-header trust.
- token_file: a protected file containing a random token of at least 32 characters.
- relay_python: the installed relay daemon's interpreter.
- relay_config: a protected JSON file containing relay_port and process_passwd.
- dry_run: keep true for initial tests; false enables real relay commands.

The service user needs read access to configuration and secrets. No writable
state directory or database is needed. Install door-bridge.service under
/etc/systemd/system, then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now door-bridge
sudo journalctl -u door-bridge -n 50 --no-pager
```

Keep the legacy relay listener on localhost. Restrict bridge port 9080 to intended
clients through the firewall. Do not replace the existing RFID services or wiring.

## API

POST /api/unlock with Authorization: Bearer <token>, Content-Type: application/json:

```json
{"request_id":"unique-event-id","door_id":"verlab-entrance","expires_at":1795500000}
```

Use a fresh Unix expiry no more than 30 seconds ahead. Both the actual source IP
and token must be allowed. The bridge trusts the caller's decision; it does not
verify facial recognition or consult LDAP.

Success reports simulated or command_dispatched; it cannot confirm physical
opening. Errors include 401 authentication, 403 source IP/door, 409 duplicate,
422 expiry/input, 429 cooldown/busy and 503 unavailable/uncertain outcome.

Recent IDs remain in memory for five minutes, and cooldown defaults to five
seconds. Both reset on restart. An uncertain dispatch remains consumed during the
process lifetime. Never automatically retry an unlock after a timeout.

## Tests

From this repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests
```

Tests use simulated hardware and local HTTP. Python 2 compatibility and physical
relay operation require a supervised deployment check.
