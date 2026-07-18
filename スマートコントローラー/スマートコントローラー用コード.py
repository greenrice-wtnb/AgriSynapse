'''
Copyright (C) 2026 AgriSynapse Project

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
'''

import machine, time, gc, json, os, sys, network, socket, rp2

# ==========================================
# システム設定
# ==========================================
DEBUG_MODE = True
CONFIG_FILE = "remote_conf.json"
AP_SSID = "Pico-Remote"
AP_PASS = "picow1234" #APモード時のPicoWへのアクセス用パスワードは、適宜変更してください（システムで共通化すると便利です）

TARGET_ID = "DEFAULT_FIELD"
AUTH_KEY  = "XXXXXXXXXXXXXXXX" #ここにシステム共通パスコードを半角英数字で入力してください
MY_CH     = 10

# ==========================================
# ハードウェア初期化
# ==========================================
machine.freq(150_000_000)

uart_tx = machine.Pin(0)
uart_rx = machine.Pin(1, machine.Pin.IN, machine.Pin.PULL_UP)
uart = machine.UART(0, baudrate=9600, tx=uart_tx, rx=uart_rx, rxbuf=1024)

aux = machine.Pin(2, machine.Pin.IN, machine.Pin.PULL_UP)
m1  = machine.Pin(3, machine.Pin.OUT)
m0  = machine.Pin(4, machine.Pin.OUT)
led = machine.Pin("LED", machine.Pin.OUT)

m0.value(0); m1.value(0)

# ==========================================
# LoRa ユーティリティ
# ==========================================
def wait_aux():
    st = time.ticks_ms()
    while aux.value() == 0:
        if time.ticks_diff(time.ticks_ms(), st) > 5000: break
        time.sleep_ms(10)
    time.sleep_ms(20)

def _wait_aux_mode_change():
    st = time.ticks_ms()
    while aux.value() == 1:
        if time.ticks_diff(time.ticks_ms(), st) > 50: break
        time.sleep_ms(2)
    wait_aux()

def set_mode_wor_rx(): m0.value(0); m1.value(1); time.sleep_ms(20); _wait_aux_mode_change()
def set_mode_wor_tx(): m0.value(1); m1.value(0); time.sleep_ms(20); _wait_aux_mode_change()
def set_mode_normal(): m0.value(0); m1.value(0); time.sleep_ms(20); _wait_aux_mode_change()

def send_lora(msg):
    uart.write(msg)
    time.sleep_ms(len(msg) + 50)
    wait_aux()

def safe_ascii_convert(b_data):
    try:
        res = ""
        for b in b_data:
            if (0x20 <= b <= 0x7E) or b == 0x0A or b == 0x0D: res += chr(b)
        return res
    except: return ""

def raw_to_html(raw_bytes):
    if not raw_bytes:
        return "<pre style='background:#1a1a2e;color:#aaa;padding:8px;border-radius:5px;'>（受信データなし）</pre>"
    
    if len(raw_bytes) > 2048:
        raw_bytes = raw_bytes[:2048]
        
    ascii_parts = []
    for b in raw_bytes:
        if 0x20 <= b <= 0x7E:
            c = chr(b)
            if c == '&':   ascii_parts.append('&amp;')
            elif c == '<': ascii_parts.append('&lt;')
            elif c == '>': ascii_parts.append('&gt;')
            else:          ascii_parts.append(c)
        elif b == 0x0A: ascii_parts.append('&#x21B5;\n')
        elif b == 0x0D: pass
        else: ascii_parts.append(f"<span style='color:#f48771;'>[{b:02X}]</span>")
    ascii_str = "".join(ascii_parts)
    
    hex_lines = []
    for i in range(0, len(raw_bytes), 16):
        chunk = raw_bytes[i:i+16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        # ★ ここを修正: ljustを使わずに空白を足す
        hex_part = hex_part + " " * (47 - len(hex_part))
        char_part = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk)
        hex_lines.append(f"{i:08X}  {hex_part}  |{char_part}|")
    
    hex_str = "\n".join(hex_lines)
    total = len(raw_bytes)
    
    return (
        "<pre style='background:#1a1a2e;color:#e0e0e0;font-size:0.78rem;"
        "padding:10px 12px;border-radius:6px;overflow-x:auto;white-space:pre;"
        "border:1px solid #555;margin-top:10px;line-height:1.5;'>"
        f"<b style='color:#4ec9b0;'>📡 ASCII ({total} bytes) :</b>\n{ascii_str}\n\n"
        f"<b style='color:#4ec9b0;'>🔢 HEX DUMP :</b>\n{hex_str}"
        "</pre>"
    )

def set_lora_ch(c):
    m0.value(1); m1.value(1); time.sleep_ms(20); wait_aux()
    while uart.any(): uart.read()
    cmd = bytearray([0xC2, 0x00, 0x06, 0x00, 0x00, 0x62, 0x00, int(c), 0x80])
    uart.write(cmd); time.sleep(0.6)
    while uart.any(): uart.read()
    set_mode_wor_rx()

def wait_lora_response(expected_prefix, timeout_ms=30000):
    st = time.ticks_ms()
    _buf = b""
    _all_raw = b""
    
    while time.ticks_diff(time.ticks_ms(), st) < timeout_ms:
        if aux.value() == 0 or uart.any():
            st_a = time.ticks_ms()
            while aux.value() == 0:
                if time.ticks_diff(time.ticks_ms(), st_a) > 5000: break
                time.sleep_ms(10)
            time.sleep_ms(20)
        
        if uart.any():
            r = uart.read()
            if r is not None:
                _buf += r
            
            while b'\n' in _buf:
                idx = _buf.find(b'\n')
                if len(_buf) > idx + 1:
                    rssi_byte = _buf[idx + 1]
                    rssi_val = -(256 - rssi_byte)
                    
                    raw = _buf[:idx+2]
                    _buf = _buf[idx+2:]
                    
                    if len(_all_raw) < 2048:
                        _all_raw += raw
                    
                    line = safe_ascii_convert(raw).strip()
                    
                    if expected_prefix in line:
                        p_idx = line.find(expected_prefix)
                        parsed_line = line[p_idx:]
                        l = parsed_line.split(',')
                        if len(l) > 2 and l[1] == AUTH_KEY and l[2] == TARGET_ID:
                            return parsed_line, _all_raw, rssi_val
                else:
                    break
        time.sleep_ms(50)
        
    if _buf and len(_all_raw) < 2048:
        _all_raw += _buf
    return None, _all_raw if _all_raw else None, None

# ==========================================
# Config 管理
# ==========================================
def load_config():
    global TARGET_ID, AUTH_KEY, MY_CH
    try:
        with open(CONFIG_FILE, "r") as f:
            d = json.load(f)
            TARGET_ID = d.get("id", TARGET_ID)
            AUTH_KEY  = d.get("auth", AUTH_KEY)
            MY_CH     = int(d.get("ch", MY_CH))
    except: pass

def save_config(t_id, auth, ch):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({"id": t_id, "auth": auth, "ch": ch}, f)
    except: pass

# ==========================================
# サーバー処理ヘルパー
# ==========================================
def _send_all(sock, data):
    view = memoryview(data)
    pos = 0
    while pos < len(data):
        try:
            sent = sock.send(view[pos:pos + 1024])
            if sent is not None and sent > 0:
                pos += sent
            else:
                time.sleep_ms(10)
        except OSError:
            break

def send_http_response(conn, res_str):
    try:
        gc.collect() 
        body_bytes = res_str.encode('utf-8')
        header = (
            "HTTP/1.0 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            "Connection: close\r\n\r\n"
        )
        _send_all(conn, header.encode('utf-8'))
        _send_all(conn, body_bytes)
    except Exception as e:
        if DEBUG_MODE: print("HTTP Send Error:", e)

def parse_post(body):
    params = {}
    for pair in body.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            params[k] = v.replace("+", " ").replace("%3A", ":")
    return params

# ==========================================
# Main AP Server Loop
# ==========================================
load_config()
set_lora_ch(MY_CH)

print(f"=== AP CONFIG MODE ===")
rp2.country('JP')
ap = network.WLAN(network.AP_IF)
ap.active(True)
ap.config(essid=AP_SSID, password=AP_PASS)
ap.ifconfig(("192.168.4.1", "255.255.255.0", "192.168.4.1", "192.168.4.2"))
while not ap.active(): time.sleep_ms(100)

print(f"Ready. Connect to {AP_SSID} and open http://192.168.4.1/")

try:
    with open("setup_remote.html", "r") as f: html_template = f.read()
except:
    html_template = "<html><body>Error: setup_remote.html missing</body></html>"

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", 80))
server.listen(3)
server.settimeout(0.5)

while True:
    gc.collect()
    
    try:
        conn, addr = server.accept()
    except OSError:
        time.sleep_ms(10) 
        continue
        
    try:
        led.value(1)
        conn.settimeout(65.0)
        request = conn.recv(4096).decode("utf-8", "ignore")
        
        parts = request.split("\r\n\r\n", 1)
        headers = parts[0]
        body = parts[1] if len(parts) > 1 else ""
        
        content_length = 0
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-length:"):
                try: content_length = int(line.split(":")[1].strip())
                except: pass
        
        while len(body) < content_length:
            try:
                chunk = conn.recv(1024).decode("utf-8", "ignore")
                if not chunk: break
                body += chunk
            except: break

        # ----------------------------------------------------
        # Routing
        # ----------------------------------------------------
        if "POST /save" in headers:
            p = parse_post(body)
            TARGET_ID = p.get("id", TARGET_ID)
            AUTH_KEY  = p.get("auth", AUTH_KEY)
            MY_CH     = int(p.get("ch", MY_CH))
            save_config(TARGET_ID, AUTH_KEY, MY_CH)
            set_lora_ch(MY_CH)
            send_http_response(conn, "OK")

        elif "POST /send_time" in headers:
            p = parse_post(body)
            set_mode_wor_rx()
            while uart.any(): uart.read()
            
            data_line, raw_data, rssi = wait_lora_response("DATA,", timeout_ms=60000)
            if data_line:
                time.sleep_ms(1200)
                set_mode_normal()
                pkt = f"          TIME,{AUTH_KEY},{TARGET_ID},{p.get('h')},{p.get('m')},{p.get('dry')},{p.get('full')},{p.get('mode')},{p.get('y')},{p.get('mo')},{p.get('d')}\n"
                send_lora(pkt)
                set_mode_wor_rx()
                res = (
                    f"✅ TIME送信完了！ <span style='background:#bbdefb; color:#0d47a1; padding:2px 6px; border-radius:4px; font-weight:bold; font-size:0.85em;'>RSSI: {rssi} dBm</span><br>"
                    f"<div style='margin-top:6px;'><b>トリガーパケット:</b> {data_line}</div>"
                    + raw_to_html(raw_data)
                )
            else:
                res = "❌ タイムアウト: センサーノードからのDATAパケットが検出できませんでした。" + raw_to_html(raw_data)
                
            send_http_response(conn, res)

        elif "POST /send_manual" in headers:
            p = parse_post(body)
            cmd = p.get("cmd", "OPEN")
            set_mode_wor_tx(); send_lora("WAKEUP\n")
            time.sleep_ms(1500); set_mode_normal(); time.sleep_ms(1500)
            send_lora(f"MANUAL,{AUTH_KEY},{TARGET_ID},{cmd}\n")
            
            res_line, raw_data, rssi = wait_lora_response("MANUAL_RES,", timeout_ms=28000)
            set_mode_wor_rx()
            if res_line:
                res = f"✅ 応答あり <span style='background:#bbdefb; color:#0d47a1; padding:2px 6px; border-radius:4px; font-weight:bold; font-size:0.85em;'>RSSI: {rssi} dBm</span><br><div style='margin-top:6px;'>{res_line}</div>" + raw_to_html(raw_data)
            else:
                res = "❌ 応答なし (Timeout)" + raw_to_html(raw_data)
            send_http_response(conn, res)

        elif "POST /send_command" in headers:
            p = parse_post(body)
            cmd = p.get("cmd", "OPEN")
            set_mode_wor_tx(); send_lora("WAKEUP\n")
            time.sleep_ms(2000); set_mode_normal(); time.sleep_ms(2000)
            send_lora(f"COMMAND,{AUTH_KEY},{TARGET_ID},{cmd}\n")
            
            res_line, raw_data, rssi = wait_lora_response("RESULT,", timeout_ms=28000)
            set_mode_wor_rx()
            if res_line:
                res = f"✅ 応答あり <span style='background:#bbdefb; color:#0d47a1; padding:2px 6px; border-radius:4px; font-weight:bold; font-size:0.85em;'>RSSI: {rssi} dBm</span><br><div style='margin-top:6px;'>{res_line}</div>" + raw_to_html(raw_data)
            else:
                res = "❌ 応答なし (Timeout)" + raw_to_html(raw_data)
            send_http_response(conn, res)

        elif "POST /send_dummy_data" in headers:
            p = parse_post(body)
            prefix = p.get("prefix", "ACK,") # デフォルトは "ACK," を待機
            
            # WAKEUP等のシーケンスなしで直接送信（GWは常時受信のため）
            set_mode_normal()
            time.sleep_ms(100)
            
            # GAS側のLogシートフォーマットに合わせた14要素のダミーパケット
            dummy_pkt = f'DATA,{AUTH_KEY},"TEST_DATA",5.0,50,5.00,KEEP,KEEP,TIMEOUT,12.50,NORMAL,NORMAL,5.00\n'
            send_lora(dummy_pkt)
            
            # GWからの返答を待機
            set_mode_wor_rx()
            res_line, raw_data = wait_lora_response("TIME,", timeout_ms=15000)
            
            if res_line:
                res = f"✅ GW応答あり: {res_line}" + raw_to_html(raw_data)
            else:
                res = f"❌ GW応答なし (Timeout: {prefix} を待機)" + raw_to_html(raw_data)
                
            _send_all(conn, f"HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n{res}".encode())

        else:
            page = html_template.replace("__TARGET_ID__", TARGET_ID)
            page = page.replace("__AUTH_KEY__", AUTH_KEY)
            page = page.replace("__MY_CH__", str(MY_CH))
            send_http_response(conn, page)

        conn.close()
        led.value(0)
        
    except Exception as e:
        if DEBUG_MODE: print("HTTP Server Error:", e)
        try: conn.close()
        except: pass
        try: led.value(0)
        except: pass
