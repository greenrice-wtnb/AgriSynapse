'''
Copyright (C) 2026 AgriSynapse Project

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
'''

import machine, time, gc, os, json, sys, uselect, network, socket

# ==========================================
# システム設定
# ==========================================
DEBUG_MODE   = False  
AUTH_KEY     = "ここにシステム共通パスコードを半角英数字で入力してください"
CONFIG_FILE  = "repeater_conf.json"

AP_SSID_PREFIX   = "PicoW-Rep-"  
AP_PASSWORD      = "picow1234" #APモード時のPicoWへのアクセス用パスワードは、適宜変更してください（システムで共通化すると便利です）    
AP_TIMEOUT_MS    = 3 * 60 * 1000  
USB_VSYS_THRESHOLD = 4.7　#USB接続判定のためDCDCコンバーターMP1584ENモジュールの出力電圧は4.0V程度に設定し、この値（4.7V）を超えないようにしてください           

#メンテナンスのための自動再起動時刻設定
MAINT_HOUR = 0
MAINT_MIN  = 0

#一応チャージコントローラー機能がありますが、DenryoのSolarAMPーBに任せるのでこのチャージコントローラー機能は使いません。
CHARGE_STOP_VOLT  = 13.8
CHARGE_START_VOLT = 12.8

config = { "id": "REPEATER_01", "listen": 10, "target": 10, "offset": 0, "mode": "RUN", "auth": "KOKUFU_2026" }

# ==========================================
# ピン定義
# ==========================================
uart_tx = machine.Pin(0)
uart_rx = machine.Pin(1, machine.Pin.IN, machine.Pin.PULL_UP)
uart    = machine.UART(0, baudrate=9600, tx=uart_tx, rx=uart_rx, rxbuf=2048)

aux = machine.Pin(2, machine.Pin.IN, machine.Pin.PULL_UP)
m1  = machine.Pin(3, machine.Pin.OUT)
m0  = machine.Pin(4, machine.Pin.OUT)

# 12V電圧管理ピン
adc_12v     = machine.ADC(26)
charge_ctrl = machine.Pin(27, machine.Pin.OUT)

m0.value(0); m1.value(0)
charge_ctrl.value(0)

# ==========================================
# ★ VSYS計測 & Wi-Fi安全遮断
# ==========================================
gc.collect()
machine.Pin(23, machine.Pin.OUT).high()
import network
_wlan = network.WLAN(network.STA_IF)
_wlan.active(False) 

time.sleep_ms(100)                        
VSYS_CACHE = round(
    machine.ADC(29).read_u16() * 3.3 / 65535 * 3, 2
)

# ==========================================
# 12V・充電管理
# ==========================================
def read_12v():    return (adc_12v.read_u16() * 3.3 / 65535) * 4.9
def get_12v_avg(): return sum([read_12v() for _ in range(10)]) / 10

#manage_charging関数は使用しません。過去のバージョンの名残です。
def manage_charging():
    v = get_12v_avg()
    if v > CHARGE_STOP_VOLT:    charge_ctrl.value(0)
    elif v < CHARGE_START_VOLT: charge_ctrl.value(1)
    return v

# ==========================================
# LoRa ユーティリティ
# ==========================================
def wait_aux():
    st = time.ticks_ms()
    while aux.value() == 0:
        if time.ticks_diff(time.ticks_ms(), st) > 3000: break
        time.sleep_ms(10)
    time.sleep_ms(5)

def send_lora(msg):
    uart.write(msg)
    time.sleep_ms(len(msg) + 50)
    wait_aux()

def safe_ascii_convert(b_data):
    try:
        res = ""
        for b in b_data:
            if (0x20 <= b <= 0x7E) or b == 0x0A or b == 0x0D: res += chr(b)
        for h in ["DATA,", "TIME,"]:
            idx = res.find(h)
            if idx != -1: return res[idx:].strip()
    except: pass
    return ""

def set_lora_ch(c):
    m0.value(1); m1.value(1); time.sleep_ms(20); wait_aux()
    cmd = bytearray([0xC2, 0x04, 0x01, int(c)])
    while uart.any(): uart.read()
    uart.write(cmd); time.sleep(0.6)
    while uart.any(): uart.read()
    uart.write(bytearray([0xC1, 0x04, 0x01])); time.sleep(0.3)
    uart.read()
    m0.value(0); m1.value(0); time.sleep_ms(20); wait_aux()

def set_mode_normal():  m0.value(0); m1.value(0); time.sleep_ms(20); wait_aux()

def initialize_e220_startup():
    set_lora_ch(config["listen"])

def load_config():
    global config, AUTH_KEY
    try:
        with open(CONFIG_FILE, "r") as f:
            d = json.load(f)
            config.update(d)
            if "auth" in d: AUTH_KEY = d["auth"]
    except: pass

def save_config():
    global config, AUTH_KEY
    try:
        config["auth"] = AUTH_KEY
        with open(CONFIG_FILE, "w") as f: json.dump(config, f)
    except: pass

def send_self_report():
    if config["mode"] == "STOP": return
    curr = config["listen"]; tgt = config["target"]
    if curr != tgt: set_lora_ch(tgt)
    
    v = get_12v_avg()
    packet = f"DATA,{AUTH_KEY},{config['id']},0,0,{VSYS_CACHE:.2f},UNKNOWN,KEEP,NO_ACTION,{v:.2f},UNKNOWN,NORMAL,0.00\n"
    
    set_mode_normal()
    send_lora(packet)
    
    wait_st = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), wait_st) < 40000: 
        if aux.value() == 0 or uart.any():
            st_aux = time.ticks_ms()
            while aux.value() == 0:
                if time.ticks_diff(time.ticks_ms(), st_aux) > 2000: break
                time.sleep_ms(10)
            time.sleep_ms(20)
            
            if uart.any():
                r_raw = uart.read()
                if b'TIME,' not in r_raw: continue 
                msg = safe_ascii_convert(r_raw).strip()
                if msg:
                    r_d = msg.split(',')
                    if len(r_d) >= 5 and r_d[0] == "TIME" and r_d[2] == config['id']:
                        try: 
                            y, m, d_day = time.localtime()[0:3]
                            if len(r_d) >= 11:
                                y, m, d_day = int(r_d[8]), int(r_d[9]), int(r_d[10])
                            machine.RTC().datetime((y, m, d_day, 0, int(r_d[3]), int(r_d[4]), 0, 0))
                        except: pass
                        break
        time.sleep_ms(100)
    
    if curr != tgt: set_lora_ch(curr)

# ==========================================
# ★ USBシリアル設定モード
# ==========================================
def process_usb_serial():
    global AUTH_KEY
    import network
    _wlan = network.WLAN(network.STA_IF)
    _wlan.active(False) 
    time.sleep_ms(50)
    try:
        led = machine.Pin("LED", machine.Pin.OUT)
        led.value(0)
    except:
        led = None

    poller = uselect.poll()
    poller.register(sys.stdin, uselect.POLLIN)
    buffer = ""
    last_led_toggle = time.ticks_ms()

    while True:
        res = poller.poll(10) 
        if res:
            char = sys.stdin.read(1)
            if char:
                if char in ('\n', '\r'):
                    line = buffer.strip()
                    buffer = ""
                    if "SET_CONFIG" in line:
                        l = line.split(',')
                        if len(l) >= 10:
                            AUTH_KEY = l[1]
                            config["auth"] = AUTH_KEY
                            config["id"] = l[2]
                            config["offset"] = int(l[5])
                            config["mode"] = l[8]
                            config["target"] = int(l[9])
                            config["listen"] = int(l[10])
                            try:
                                y, m, d_day = int(l[11]), int(l[12]), int(l[13])
                                machine.RTC().datetime((y, m, d_day, 0, int(l[6]), int(l[7]), 0, 0))
                            except: pass
                            save_config()
                            
                            try:
                                with open("skip_ap.flag", "w") as f: f.write("1")
                            except: pass

                            poller.unregister(sys.stdin)
                            try: network.WLAN(network.STA_IF).active(False)
                            except: pass
                            machine.Pin(23, machine.Pin.OUT).low()
                            time.sleep_ms(1000)
                            machine.reset()
                            
                    elif "START" in line:
                        poller.unregister(sys.stdin)
                        if led:
                            try: led.value(0)
                            except: pass
                        return
                else:
                    buffer += char
                    
        if time.ticks_diff(time.ticks_ms(), last_led_toggle) > 500:
            if led:
                try: led.value(not led.value())
                except: pass
            last_led_toggle = time.ticks_ms()
            
# ==========================================
# ★ APモード設定サーバー
# ==========================================
def _send_all(sock, data, chunk=1024):
    view = memoryview(data)
    pos = 0
    while pos < len(data):
        sent = sock.send(view[pos:pos + chunk])
        pos += sent

def run_ap_config_mode():
    import rp2
    rp2.country('JP')

    ap = network.WLAN(network.AP_IF)
    ap.active(False)            
    time.sleep_ms(200)
    ap.config(essid=AP_SSID_PREFIX + config["id"],
              password=AP_PASSWORD)
    ap.ifconfig(("192.168.4.1", "255.255.255.0", "192.168.4.1", "192.168.4.2"))
    ap.active(True)
    while not ap.active():
        time.sleep_ms(100)

    try:
        with open("setup_repeater.html", "r") as f:
            html_content = f.read()
    except:
        html_content = "<html><body><h2>setup_repeater.html が見つかりません</h2></body></html>"

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 80))
    server.listen(3)
    server.settimeout(1.0)

    last_activity = time.ticks_ms()

    while True:
        gc.collect()

        if time.ticks_diff(time.ticks_ms(), last_activity) > AP_TIMEOUT_MS:
            break

        try:
            conn, addr = server.accept()
        except OSError:
            continue

        try:
            conn.settimeout(60.0)
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

            if "POST /save" in request:
                params = {}
                for pair in body.split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        params[k] = v.replace("+", " ")

                config["id"]     = params.get("id", config["id"]).strip()
                config["auth"]   = params.get("auth", AUTH_KEY).strip()
                AUTH_KEY         = config["auth"]
                config["listen"] = int(params.get("rx_ch", config["listen"]))
                config["target"] = int(params.get("tx_ch", config["target"]))
                config["offset"] = int(params.get("offset", config["offset"]))
                config["mode"]   = params.get("mode", "RUN").strip()

                save_config()
                
                try:
                    with open("skip_ap.flag", "w") as f: f.write("1")
                except: pass

                resp = (
                    "HTTP/1.0 200 OK\r\n"
                    "Content-Type: text/html; charset=utf-8\r\n"
                    "Connection: close\r\n\r\n"
                    "<html><meta charset='utf-8'><body><h2>✅ 保存しました。再起動します...</h2></body></html>"
                )
                _send_all(conn, resp.encode())
                
                time.sleep_ms(1000) 
                try: conn.close()
                except: pass
                try: server.close()
                except: pass
                time.sleep_ms(500)
                
                try: network.WLAN(network.AP_IF).active(False)
                except: pass
                machine.Pin(23, machine.Pin.OUT).low()
                time.sleep_ms(1000)
                machine.reset()

            else:
                last_activity = time.ticks_ms()  
                v12 = get_12v_avg()
                page = html_content.replace("__FIELD_ID__", config["id"])
                page = page.replace("__AUTH_KEY__", AUTH_KEY)
                page = page.replace("__RX_CH__", str(config["listen"]))
                page = page.replace("__TX_CH__", str(config["target"]))
                page = page.replace("__OFFSET_MIN__", str(config["offset"]))
                page = page.replace("__MODE__", "STOP" if config["mode"]=="STOP" else "RUN")
                page = page.replace("__VSYS__", f"{VSYS_CACHE:.2f}")
                page = page.replace("__V12__", f"{v12:.2f}")

                body_bytes = page.encode()
                header = f"HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body_bytes)}\r\nConnection: close\r\n\r\n"
                _send_all(conn, header.encode() + body_bytes)
                conn.close()

        except:
            try: conn.close()
            except: pass

    server.close(); ap.active(False)
    try: network.WLAN(network.STA_IF).active(False)
    except: pass
    machine.Pin(23, machine.Pin.OUT).low()
    gc.collect()
    time.sleep_ms(200)
    
# ==========================================
# ★ Main
# ==========================================
skip_ap_flag = False
try:
    os.stat("skip_ap.flag")
    skip_ap_flag = True
    os.remove("skip_ap.flag") 
except:
    pass

load_config()

if VSYS_CACHE >= USB_VSYS_THRESHOLD:
    process_usb_serial()
    load_config()
    try: network.WLAN(network.AP_IF).active(False)
    except: pass
    machine.Pin(23, machine.Pin.OUT).low()

else:
    _tm = time.localtime(time.time() + 9 * 3600)
    _is_scheduled_restart = (_tm[3] == MAINT_HOUR and _tm[4] == MAINT_MIN and _tm[5] < 60)
    
    if not _is_scheduled_restart and not skip_ap_flag:
        run_ap_config_mode()
        load_config()
        try: network.WLAN(network.AP_IF).active(False)
        except: pass
    machine.Pin(23, machine.Pin.OUT).low()

gc.collect()
initialize_e220_startup()

print(f"\n--- REPEATER READY ---")
print(f"ID: {config['id']} | RX_CH: {config['listen']} | TX_CH: {config['target']} | VSYS: {VSYS_CACHE:.2f}V")

last_chk_time = time.ticks_ms()
last_report_min = -1

while True:
    tm_now = time.localtime(time.time() + 9 * 3600)
    if tm_now[3] == MAINT_HOUR and tm_now[4] == MAINT_MIN and tm_now[5] < 5:
        try: network.WLAN(network.AP_IF).active(False)
        except: pass
        machine.Pin(23, machine.Pin.OUT).low()
        machine.reset()

    if time.ticks_diff(time.ticks_ms(), last_chk_time) > 5000:
        set_mode_normal() 
        v = manage_charging()
        if config["mode"] == "RUN":
            try:
                t = time.localtime(); curr_min = t[4]
                if curr_min == config["offset"] and curr_min != last_report_min:
                    send_self_report(); last_report_min = curr_min
                elif curr_min != config["offset"]:
                    last_report_min = -1
            except: pass
        last_chk_time = time.ticks_ms()

    raw = None
    if aux.value() == 0 or uart.any():
        st_aux = time.ticks_ms()
        while aux.value() == 0:
            time.sleep_ms(10)
            if time.ticks_diff(time.ticks_ms(), st_aux) > 2000: break
        time.sleep_ms(20) 
        
        if uart.any():
            raw = uart.read()
            if not (b'DATA,' in raw or b'TIME,' in raw): raw = None
            
    wait_aux(); time.sleep_ms(20)
    while uart.any(): uart.read()

    if raw:
        try:
            msg = safe_ascii_convert(raw).strip()
            if msg:
                d = msg.split(',')
                if len(d) > 0 and d[0] == "DATA":
                    if config["listen"] != config["target"]: set_lora_ch(config["target"])
                    
                    set_mode_normal()
                    send_lora(msg + "\n") 
                    
                    wait_st = time.ticks_ms()
                    while time.ticks_diff(time.ticks_ms(), wait_st) < 50000:
                        if aux.value() == 0 or uart.any():
                            st_aux = time.ticks_ms()
                            while aux.value() == 0:
                                if time.ticks_diff(time.ticks_ms(), st_aux) > 2000: break
                                time.sleep_ms(10)
                            time.sleep_ms(20)
                            
                            if uart.any():
                                r_raw = uart.read()
                                if b'TIME,' not in r_raw: continue 
                                r_msg = safe_ascii_convert(r_raw).strip()
                                if r_msg:
                                    r_d = r_msg.split(',')
                                    if len(r_d) >= 5 and r_d[0] == "TIME" and r_d[1] == d[1]:
                                        try: 
                                            y, m, d_day = time.localtime()[0:3]
                                            if len(r_d) >= 11:
                                                y, m, d_day = int(r_d[8]), int(r_d[9]), int(r_d[10])
                                            machine.RTC().datetime((y, m, d_day, 0, int(r_d[3]), int(r_d[4]), 0, 0))
                                        except: pass
                                        
                                        if config["listen"] != config["target"]: set_lora_ch(config["listen"])
                                        
                                        set_mode_normal()
                                        send_lora(r_msg + "\n")
                                        break
                        time.sleep_ms(10)
                    
                    if config["listen"] != config["target"]: set_lora_ch(config["listen"])
        except: pass
    
    gc.collect()
    set_mode_normal() 
    
    if not uart.any() and aux.value() == 1:
        time.sleep_ms(5000)