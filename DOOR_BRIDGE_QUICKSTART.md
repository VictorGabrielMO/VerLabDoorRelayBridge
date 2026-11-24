# Deploy the door bridge and test with the ESP32 simulator

Install only the bridge on the door Raspberry Pi. Keep the existing RFID and
relay services running. Replace `PI_USER`, `PI_IP` and `SIMULATOR_IP` with your
actual values. The bridge uses HTTP on a trusted LAN; recognition still uses
HTTPS. The bridge token is unencrypted in transit.

## 1. Clone the bridge on the Pi and check Python

```bash
ssh PI_USER@PI_IP

git clone https://github.com/VictorGabrielMO/VerLabDoorRelayBridge.git
cd ~/VerLabDoorRelayBridge

python3 --version
ps -eo pid,args | grep '[r]elay_server.py'
head -1 /var/doorsystem/src/relay_server.py
command -v python python2.7 python3
```

The bridge needs Python 3.7+. Its relay helper must use the same interpreter as
the running relay daemon. The example below assumes `/usr/bin/python2.7`; verify
and adjust it. If the repository is already cloned, update that checkout instead
of cloning over it.

## 2. Install the bridge

Run on the Pi, from `~/VerLabDoorRelayBridge`:

```bash
id door-bridge >/dev/null 2>&1 || \
  sudo useradd --system --no-create-home \
  --shell /usr/sbin/nologin door-bridge

sudo install -d -m 755 /opt/door-bridge
sudo install -m 644 bridge.py relay_send.py /opt/door-bridge/
sudo install -d -o root -g door-bridge -m 750 /etc/door-bridge
sudo install -m 644 door-bridge.service \
  /etc/systemd/system/door-bridge.service
```

Create configuration and a token only if they do not already exist:

```bash
if ! sudo test -f /etc/door-bridge/config.json; then
  sudo install -o root -g door-bridge -m 640 \
    config.example.json /etc/door-bridge/config.json
fi

sudo sh -c '
  if [ ! -f /etc/door-bridge/token ]; then
    umask 027
    openssl rand -hex 32 > /etc/door-bridge/token
  fi
'
sudo chown root:door-bridge /etc/door-bridge/token
sudo chmod 640 /etc/door-bridge/token
```

## 3. Copy only the relay connection settings

This does not modify the existing RFID configuration or copy LDAP credentials.
Run on the Pi:

```bash
sudo python3 - <<'PY'
import json
import os

path = '/etc/door-bridge/relay.json'
if not os.path.exists(path):
    with open('/var/doorsystem/config/config.json') as stream:
        config = json.load(stream)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({
            'relay_port': config['relay_port'],
            'process_passwd': config['process_passwd'],
        }, stream)
PY
sudo chown root:door-bridge /etc/door-bridge/relay.json
sudo chmod 640 /etc/door-bridge/relay.json
```

## 4. Configure the bridge

```bash
sudo nano /etc/door-bridge/config.json
```

Replace the address placeholders and verify the interpreter path:

```json
{
  "door_id": "verlab-entrance",
  "bind": "PI_IP",
  "port": 9080,
  "allowed_ips": ["SIMULATOR_IP"],
  "token_file": "/etc/door-bridge/token",
  "dry_run": true,
  "cooldown_seconds": 5,
  "relay_timeout_seconds": 3,
  "relay_python": "/usr/bin/python2.7",
  "relay_config": "/etc/door-bridge/relay.json"
}
```

`SIMULATOR_IP` is the notebook/server address as seen by the Pi. VPN/NAT may
change that address. The list accepts exact IPs, not hostnames or CIDR ranges.
Forwarded-IP headers are ignored. Add the ESP32 address later; use DHCP
reservations to keep client addresses stable.

Keep `dry_run: true` initially. Allow TCP 9080 through any firewall only from
intended clients. Keep the existing relay listener on localhost. No bridge
certificate or database is required.

## 5. Start the service

On the Pi:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now door-bridge
sudo systemctl status door-bridge --no-pager
sudo journalctl -u door-bridge -n 50 --no-pager
```

## 6. Transfer the bridge token to the notebook

On the Pi:

```bash
sudo install -o "$USER" -m 600 \
  /etc/door-bridge/token "$HOME/door-bridge.token"
```

On the notebook:

```bash
mkdir -p ~/.config/facerec-door
chmod 700 ~/.config/facerec-door
scp PI_USER@PI_IP:~/door-bridge.token \
  ~/.config/facerec-door/door-bridge.token
chmod 600 ~/.config/facerec-door/door-bridge.token
ssh PI_USER@PI_IP 'rm -f ~/door-bridge.token'
```

Never transfer the existing relay secret. Only the dedicated bridge token is
needed by the simulator.

## 7. Prepare the simulator

On the notebook, in the updated facial-recognition project (Python 3.10+):

```bash
cd ~/Projects/graduacao/facial_recognition_project
python3 -m venv .venv-door
.venv-door/bin/pip install 'httpx>=0.27,<1'

read -rs -p 'ESP32 camera credential from central UI: ' ESP_CAMERA_TOKEN
echo
export ESP_CAMERA_TOKEN
export DOOR_BRIDGE_TOKEN="$(cat ~/.config/facerec-door/door-bridge.token)"
```

The camera credential authorizes recognition; the bridge token authorizes door
commands. Register the camera in central if needed. The edge must be provisioned,
but it does not need an active browser PIN session.

The following commands assume the edge server is `150.164.212.129:8444` and its
exported Caddy root is `~/facerec-server-root.crt`. Adjust these if necessary.

## 8. Test recognition only

Use an enrolled person's photo:

```bash
.venv-door/bin/python scripts/esp32_simulator.py /absolute/path/to/face.jpg \
  --edge-url https://150.164.212.129:8444 \
  --edge-ca ~/facerec-server-root.crt \
  --door-url http://PI_IP:9080 \
  --door-id verlab-entrance
```

Without `--unlock`, the simulator never contacts the bridge.

## 9. Test simulated unlocking

```bash
.venv-door/bin/python scripts/esp32_simulator.py /absolute/path/to/face.jpg \
  --edge-url https://150.164.212.129:8444 \
  --edge-ca ~/facerec-server-root.crt \
  --door-url http://PI_IP:9080 \
  --door-id verlab-entrance \
  --unlock
```

With an accepted match and bridge `dry_run: true`, expect `bridge.status` to be
`simulated`. The relay must not operate. A rejected match produces
`unlock_requested: false`.

On the Pi, inspect:

```bash
sudo journalctl -u door-bridge -n 30 --no-pager
```

While still in dry-run mode, verify that an unlisted source IP receives 403 and
an allowed source with an invalid token receives 401. Allow at least five seconds
between accepted test requests to avoid the default cooldown.

## 10. Supervised physical test

First verify RFID still works. On the Pi:

```bash
sudo nano /etc/door-bridge/config.json
```

Change `dry_run` to `false`, then:

```bash
sudo systemctl restart door-bridge
```

Repeat the simulator command with `--unlock` while someone supervises the door.
An accepted match should produce `command_dispatched`. Verify the physical lock
response and RFID again. This response does not confirm that the door opened;
the existing relay provides no acknowledgment or door-position sensor.

A timeout can leave the physical outcome uncertain: inspect before retrying.
Duplicate-request tracking and cooldown reset when the bridge restarts.

To disable the bridge without affecting RFID:

```bash
sudo systemctl disable --now door-bridge
```

After testing, clear credentials from the notebook shell:

```bash
unset ESP_CAMERA_TOKEN DOOR_BRIDGE_TOKEN
```
