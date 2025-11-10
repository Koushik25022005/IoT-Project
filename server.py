import network
import socket
import time
import machine
from machine import Pin

# -----------------------
# CONFIG
# -----------------------
WIFI_SSID = "Wokwi-GUEST"
WIFI_PASS = ""

# CoAP server to send occupancy notifications to (change to your target)
SERVER_IP = "10.0.2.2"   # default Wokwi host IP -> change if needed
COAP_PORT = 5683

# Occupancy threshold (cm)
OCCUPIED_THRESHOLD = 30.0

# Poll interval (s)
POLL_INTERVAL = 1.0

# Minimal message id generator
_msg_id = 0
def next_msg_id():
    global _msg_id
    _msg_id = (_msg_id + 1) & 0xFFFF
    if _msg_id == 0:
        _msg_id = 1
    return _msg_id

# -----------------------
# Pin mapping (from your JSON)
# Ultrasonic (TRIG/ECHO):
# ultrasonic1: TRIG=4  ECHO=32
# ultrasonic2: TRIG=0  ECHO=26
# ultrasonic3: TRIG=19 ECHO=13
# ultrasonic4: TRIG=17 ECHO=33
# -----------------------
SENSORS = [
    {"name": "slot1", "trig": 4, "echo": 32},
    {"name": "slot2", "trig": 0, "echo": 26},
    {"name": "slot3", "trig": 19, "echo": 13},
    {"name": "slot4", "trig": 17, "echo": 33},
]

# LED pin mapping (REMAPPED in software to avoid collisions — change if you rewire)
LED_PINS = {
    "slot1": 25,
    "slot2": 27,
    "slot3": 14,
    "slot4": 12,
}

# -----------------------
# WiFi connect
# -----------------------
def wifi_connect(ssid, password, timeout=15):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to WiFi...", ssid)
        wlan.connect(ssid, password)
        t0 = time.time()
        while not wlan.isconnected():
            if time.time() - t0 > timeout:
                raise RuntimeError("WiFi connect timeout")
            time.sleep(0.5)
    print("Connected, IP:", wlan.ifconfig()[0])

# -----------------------
# HC-SR04 helper (MicroPython)
# returns distance in cm or None on timeout
# -----------------------
def measure_distance(trig_pin_num, echo_pin_num, timeout_us=30000):
    trig = Pin(trig_pin_num, Pin.OUT)
    echo = Pin(echo_pin_num, Pin.IN)
    trig.off()
    time.sleep_us(2)
    trig.on()
    time.sleep_us(10)
    trig.off()
    try:
        dur = machine.time_pulse_us(echo, 1, timeout_us)
        dist_cm = (dur / 2.0) / 29.1
        return dist_cm
    except OSError:
        return None

# -----------------------
# Minimal CoAP encoder
# -----------------------
def build_coap_request(msg_id, method_code, path, payload=b"", confirmable=True):
    ver = 1
    t = 0 if confirmable else 1
    token = b""
    tkl = len(token) & 0x0F
    first = (ver << 6) | ((t & 0x03) << 4) | (tkl & 0x0F)
    code_byte = method_code & 0xFF
    header = bytes([first, code_byte, (msg_id >> 8) & 0xFF, msg_id & 0xFF])
    buf = bytearray(header)

    last_option_number = 0
    if path:
        segs = [s for s in path.strip("/").split("/") if s]
        for seg in segs:
            opt_num = 11
            delta = opt_num - last_option_number
            last_option_number = opt_num
            val = seg.encode()
            length = len(val)
            if delta >= 13 or length >= 13:
                raise ValueError("path segments too long")
            opt_header = ((delta & 0x0F) << 4) | (length & 0x0F)
            buf.append(opt_header)
            if length:
                buf.extend(val)
    if payload:
        buf.append(0xFF)
        if isinstance(payload, str):
            payload = payload.encode()
        buf.extend(payload)
    return bytes(buf)

# -----------------------
# Minimal CoAP parser
# -----------------------
def parse_coap_message(data):
    if len(data) < 4:
        return None
    first = data[0]
    ver = (first >> 6) & 0x03
    t = (first >> 4) & 0x03
    tkl = first & 0x0F
    code = data[1]
    msg_id = (data[2] << 8) | data[3]
    idx = 4
    if tkl:
        idx += tkl
    last_option = 0
    path_segments = []
    while idx < len(data):
        b = data[idx]
        if b == 0xFF:
            idx += 1
            break
        delta = (b >> 4) & 0x0F
        length = b & 0x0F
        idx += 1
        opt_num = last_option + delta
        last_option = opt_num
        opt_val = data[idx:idx+length] if length else b""
        idx += length
        if opt_num == 11:
            path_segments.append(opt_val.decode())
    payload = data[idx:] if idx <= len(data) else b''
    path = "/" + "/".join(path_segments) if path_segments else "/"
    return {"ver": ver, "type": t, "code": code, "msg_id": msg_id, "path": path, "payload": payload}

# -----------------------
# CoAP socket helper
# -----------------------
sock = None
def udp_create_socket(bind_port=None):
    global sock
    if sock:
        try:
            sock.close()
        except:
            pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    if bind_port:
        sock.bind(('', bind_port))
    return sock

# -----------------------
# High-level send_coap
# -----------------------
def send_coap(method_code, path, payload=b"", server_ip=SERVER_IP, server_port=COAP_PORT, confirmable=False):
    msg_id = next_msg_id()
    pkt = build_coap_request(msg_id, method_code, path, payload, confirmable=confirmable)
    try:
        sock.sendto(pkt, (server_ip, server_port))
    except Exception as e:
        print("CoAP send error:", e)

# -----------------------
# Initialize sensors and LEDs
# -----------------------
sensor_objs = []
for s in SENSORS:
    trig = Pin(s["trig"], Pin.OUT)
    trig.off()
    echo_pin = s["echo"]
    sensor_objs.append({"name": s["name"], "trig": s["trig"], "echo": echo_pin, "state": None})

led_objs = {}
for name, pin_num in LED_PINS.items():
    p = Pin(pin_num, Pin.OUT)
    p.off()
    led_objs[name] = p

# -----------------------
# Start UDP socket bound to 5683 to act as server
# -----------------------
udp_create_socket(bind_port=COAP_PORT)
print("CoAP UDP socket ready on port", COAP_PORT)

# -----------------------
# Handle inbound CoAP requests
# -----------------------
def handle_incoming():
    try:
        data, addr = sock.recvfrom(1024)
    except OSError:
        return
    except Exception as e:
        print("recv error:", e)
        return

    if not data:
        return

    parsed = parse_coap_message(data)
    if not parsed:
        print("Malformed CoAP packet from", addr)
        return

    print("Received CoAP:", parsed["path"], "from", addr)
    method = parsed["code"] & 0xFF
    if method == 3 and parsed["path"].startswith("/led"):
        slot = parsed["path"].lstrip("/")
        payload = parsed["payload"].decode().strip().lower()
        if slot in led_objs:
            if payload == "on":
                led_objs[slot].on()
                print(slot, "ON")
            elif payload == "off":
                led_objs[slot].off()
                print(slot, "OFF")
        resp_code = 0x44
        first = (1 << 6) | ((2 & 0x03) << 4) | 0
        header = bytes([first, resp_code, (parsed["msg_id"] >> 8) & 0xFF, parsed["msg_id"] & 0xFF])
        try:
            sock.sendto(header, addr)
        except Exception as e:
            print("reply error", e)
    else:
        resp_code = 0x84
        first = (1 << 6) | ((2 & 0x03) << 4) | 0
        header = bytes([first, resp_code, (parsed["msg_id"] >> 8) & 0xFF, parsed["msg_id"] & 0xFF])
        try:
            sock.sendto(header, addr)
        except Exception:
            pass

# -----------------------
# Main loop
# -----------------------
def main():
    wifi_connect(WIFI_SSID, WIFI_PASS)
    print("Starting monitoring loop. Notifying server:", SERVER_IP)
    last_states = {s["name"]: None for s in sensor_objs}

    while True:
        for s in sensor_objs:
            d = measure_distance(s["trig"], s["echo"])
            name = s["name"]

            if d is None:
                state = last_states[name]
            else:
                state = "occupied" if d < OCCUPIED_THRESHOLD else "free"

            if state != last_states[name]:
                print("State change", name, "->", state, "(dist:", d, "cm)")

                # Update LED locally
                try:
                    if state == "occupied":
                        led_objs[name].on()
                    else:
                        led_objs[name].off()
                except KeyError:
                    pass

                # Send CoAP update
                path = "/" + name
                try:
                    send_coap(2, path, payload=state)
                except Exception as e:
                    print("Failed to send CoAP:", e)

                last_states[name] = state

            time.sleep(0.05)

        t0 = time.time()
        while time.time() - t0 < POLL_INTERVAL:
            handle_incoming()
            time.sleep(0.05)

# -----------------------
# Run
# -----------------------
if __name__ == "_main_":
    try:
        main()
    except Exception as e:
        print("Fatal error:", e)
        raise
