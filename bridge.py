#!/usr/bin/env python3
"""Small standalone HTTP door bridge. Python 3.7+, standard library only."""
import argparse
import hmac
import json
import logging
import math
from pathlib import Path
import re
import ipaddress
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger('door_bridge')


class Rejected(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


class Bridge:
    def __init__(self, config, dispatch=None, clock=time.time):
        self.config, self.clock = config, clock
        allowed = config.get('allowed_ips')
        if not isinstance(allowed, list) or not allowed or any(not isinstance(ip, str) for ip in allowed):
            raise ValueError('allowed_ips must be a nonempty list of exact IP addresses')
        self.allowed_ips = {ipaddress.ip_address(ip) for ip in allowed}
        self.token = Path(config['token_file']).read_text().strip()
        if len(self.token) < 32:
            raise ValueError('Door credential must contain at least 32 characters.')
        if not config.get('door_id'):
            raise ValueError('door_id is required')
        self.cooldown = float(config.get('cooldown_seconds', 5))
        self.timeout = float(config.get('relay_timeout_seconds', 3))
        if not math.isfinite(self.cooldown) or not 1 <= self.cooldown <= 300:
            raise ValueError('Cooldown must be between 1 and 300 seconds')
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 10:
            raise ValueError('Relay timeout must be between 0 and 10 seconds')
        self.dry_run = config.get('dry_run', True)
        if type(self.dry_run) is not bool:
            raise ValueError('dry_run must be a JSON boolean')
        self.lock = threading.Lock()
        self.dispatch = dispatch or self.send_relay
        self.requests = {}
        self.cooldown_until = 0.
        self.slots = threading.BoundedSemaphore(8)

    def check_source(self, source):
        try:
            address = ipaddress.ip_address(source)
        except ValueError:
            raise Rejected(403, 'ip_not_allowed')
        if address not in self.allowed_ips:
            raise Rejected(403, 'ip_not_allowed')

    def authenticate(self, authorization):
        if not isinstance(authorization, str) or not hmac.compare_digest(
                authorization.encode('utf-8'), ('Bearer ' + self.token).encode('utf-8')):
            raise Rejected(401, 'unauthorized')

    def unlock(self, authorization, payload):
        self.authenticate(authorization)
        if not isinstance(payload, dict) or set(payload) != {'request_id', 'door_id', 'expires_at'}:
            raise Rejected(422, 'invalid_request')
        request_id = payload['request_id']
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request_id):
            raise Rejected(422, 'invalid_request')
        if payload['door_id'] != self.config['door_id']:
            raise Rejected(403, 'wrong_door')
        expires = payload['expires_at']
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise Rejected(422, 'invalid_expiry')
        # Serialize every bridge dispatch; no unlock queue waits behind the lock.
        if not self.lock.acquire(False):
            raise Rejected(429, 'busy')
        try:
            now = self.clock()
            if not now < expires <= now + 30:
                raise Rejected(422, 'expired_or_excessive_lifetime')
            self.requests = {key: deadline for key, deadline in self.requests.items()
                             if deadline >= now}
            if request_id in self.requests:
                raise Rejected(409, 'duplicate_request')
            if now < self.cooldown_until:
                raise Rejected(429, 'cooldown')
            # Record BEFORE dispatch: an uncertain outcome remains consumed.
            # Both values intentionally reset when this process restarts.
            self.requests[request_id] = now + 300
            self.cooldown_until = now + self.cooldown
            if not self.dry_run:
                try:
                    self.dispatch()
                except Exception:
                    LOG.warning('Relay dispatch failed or outcome unknown: %s', request_id)
                    raise Rejected(503, 'relay_outcome_unknown')
            status = 'simulated' if self.dry_run else 'command_dispatched'
            LOG.info('%s %s', request_id, status)
            return {'request_id': request_id, 'status': status, 'door_open_confirmed': False}
        finally:
            self.lock.release()

    def send_relay(self):
        subprocess.run([self.config['relay_python'],
                        str(Path(__file__).with_name('relay_send.py')),
                        self.config['relay_config']],
                       timeout=self.timeout, check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(5)
        return sock, address

    def process_request(self, request, client_address):
        if not self.bridge.slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.bridge.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        except Exception:
            self.shutdown_request(request)
        finally:
            self.bridge.slots.release()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log headers, tokens, or request bodies.

    def respond(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            self.server.bridge.check_source(self.client_address[0])
            if self.path != '/api/unlock':
                raise Rejected(404, 'not_found')
            self.server.bridge.authenticate(self.headers.get('Authorization'))
            if self.headers.get('Transfer-Encoding'):
                raise Rejected(400, 'unsupported_transfer_encoding')
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1:
                raise Rejected(411, 'content_length_required')
            try:
                size = int(lengths[0])
            except ValueError:
                raise Rejected(400, 'invalid_length')
            if not 0 < size <= 2048:
                raise Rejected(413, 'body_limit')
            if self.headers.get_content_type() != 'application/json':
                raise Rejected(415, 'json_required')
            try:
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError()
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                raise Rejected(400, 'invalid_json')
            result = self.server.bridge.unlock(self.headers.get('Authorization'), payload)
            self.respond(200, result)
        except Rejected as exc:
            self.respond(exc.status, {'error': exc.code})
        except Exception:
            self.respond(503, {'error': 'bridge_unavailable'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    with open(args.config) as stream:
        config = json.load(stream)
    bridge = Bridge(config)
    server = Server((config.get('bind', '127.0.0.1'), int(config.get('port', 9080))), Handler)
    server.bridge = bridge
    logging.basicConfig(level=logging.INFO)
    LOG.info('Bridge starting; dry_run=%s', bridge.dry_run)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
