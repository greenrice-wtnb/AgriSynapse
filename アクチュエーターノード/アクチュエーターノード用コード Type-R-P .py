'''
Copyright (C) 2026 AgriSynapse Project

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
'''

import machine, time, gc, json, os, sys, uselect, network, socket, rp2

# ==========================================
# 起動直後に明示的に125MHzへ初期化・UART復元
# ==========================================
machine.freq(125_000_000)

# ==========================================
# システム設定
# ==========================================
DEBUG_MODE   = False #本番運用時はFalseにしてください
AUTH_KEY     = "ここにシステム共通パスコードを半角英数字で入力してください"
CONFIG_FILE  = "config.json"
STATE_FILE   = "state.txt"
RELAY_CMD_FILE = "relay_cmd.json"　#OPEN時に動作させるリレーの設定を、セットアップ画面から変更・保存できます。アクチュエーターの極性接続等を間違えた場合に便利です。

AP_SSID_PREFIX   = "PicoW-Gate-"  
AP_PASSWORD      = "picow1234" #APモード時のPicoWへのアクセス用パスワードは、適宜変更してください（システムで共通化すると便利です）   
AP_TIMEOUT_MS    = 3 * 60 * 1000 
USB_VSYS_THRESHOLD = 4.7 #USB接続判定のため、DCDCコンバーターMP1584ENモジュールの出力電圧は4.0V程度に設定し、この値（4.7V）を超えないようにしてください         

#メンテナンスのための自動再起動時刻設定
MAINT_HOUR = 0
MAINT_MIN  = 0


# ==========================================
# ピン定義と初期化
# ==========================================
uart_tx = machine.Pin(0)
uart_rx = machine.Pin(1, machine.Pin.IN, machine.Pin.PULL_UP)
uart    = machine.UART(0, baudrate=9600, tx=uart_tx, rx=uart_rx, rxbuf=1024)

aux = machine.Pin(2, machine.Pin.IN, machine.Pin.PULL_UP)
m1  = machine.Pin(3, machine.Pin.OUT)
m0  = machine.Pin(4, machine.Pin.OUT)

# 漏れ電流対策: 擬似オープンドレイン制御
relay_in1 = machine.Pin(12)
relay_in2 = machine.Pin(11)

def set_relay(pin, state):
    if state == "ON":
        pin.init(mode=machine.Pin.OUT, value=1)
    else:
        pin.init(mode=machine.Pin.IN, pull=None)

set_relay(relay_in1, "OFF")
set_relay(relay_in2, "OFF")
relay_in1.init(mode=machine.Pin.OUT, value=1)
relay_in2.init(mode=machine.Pin.OUT, value=1)
time.sleep_us(10)
set_relay(relay_in1, "OFF")
set_relay(relay_in2, "OFF")

adc_12v     = machine.ADC(26)
charge_ctrl = machine.Pin(27, machine.Pin.OUT)

# ==========================================
# ★ Wi-Fiチップ安全シャットダウン関数
# ==========================================
def safe_reboot():
    try:
        import network
        network.WLAN(network.AP_IF).active(False)
        network.WLAN(network.STA_IF).active(False)
    except: pass
    try: machine.Pin(23, machine.Pin.OUT).low()
    except: pass
    time.sleep(1.5) 
    machine.reset()

def safe_shutdown_wifi():
    try:
        import network
        network.WLAN(network.AP_IF).active(False)
        network.WLAN(network.STA_IF).active(False)
    except: pass
    try: machine.Pin(23, machine.Pin.OUT).low()
    except: pass
    time.sleep_ms(500)

# ==========================================
# VSYS計測
# ==========================================
gc.collect()
machine.Pin(23, machine.Pin.OUT).high()
try:
    _wlan = network.WLAN(network.STA_IF)
    _wlan.active(False)
except OSError:
    machine.Pin(23, machine.Pin.OUT).low()
    time.sleep_ms(500)
    pass

time.sleep_ms(100)                        
VSYS_CACHE = round(machine.ADC(29).read_u16() * 3.3 / 65535 * 3, 2)

# ==========================================
# 設定変数
# ==========================================
MY_FIELD_ID    = "DEFAULT_GATE"
MY_CH          = 0
RELAY_OPEN_CH  = 1
is_halt        = False
is_act_error   = False
current_pump_status = "CLOSE"

CHARGE_STOP_VOLT  = 13.8 
CHARGE_START_VOLT = 12.8 
LOW_BATT_LIMIT    = 11.5 
SLEEP_INTERVAL_MS = 300000

m0.value(0); m1.value(0)
charge_ctrl.value(0)

# ==========================================
# LoRa ユーティリティ
# ==========================================
def wait_aux():
    st = time.ticks_ms()
    while aux.value() == 0:
        if time.ticks_diff(time.ticks_ms(), st) > 3000: break
        time.sleep_ms(10)
    # ★ AUX HIGH確認後、E220内部処理の完了に余裕を持たせるため5ms→20msに延長
    time.sleep_ms(20)

def send_lora(msg):
    uart.write(msg)
    time.sleep_ms(len(msg) + 50)
    wait_aux()

def safe_ascii_convert(b_data):
    try:
        res = ""
        for b in b_data:
            if (0x20 <= b <= 0x7E) or b == 0x0A or b == 0x0D: res += chr(b)
        for h in ["COMMAND,", "MANUAL,"]:
            idx = res.find(h)
            if idx != -1: return res[idx:].strip()
    except: pass
    return ""

def set_lora_ch(c):
    m0.value(1); m1.value(1); time.sleep_ms(20); wait_aux()
    cmd = bytearray([0xC2, 0x00, 0x06, 0x00, 0x00, 0x62, 0x00, int(c), 0x80])
    while uart.any(): uart.read()
    uart.write(cmd); time.sleep(0.6)
    while uart.any(): uart.read()
    set_mode_wor_rx()

def _wait_aux_mode_change():
    # ★ M0/M1変更後にAUXが一旦LOWへ落ちるのを最大50ms待ち、
    #   その後HIGHになるまで待つ2段階確認。
    st = time.ticks_ms()
    while aux.value() == 1:
        if time.ticks_diff(time.ticks_ms(), st) > 50: break
        time.sleep_ms(2)
    wait_aux()

def set_mode_wor_rx():
    m0.value(0); m1.value(1)
    time.sleep_ms(20)
    _wait_aux_mode_change()

def set_mode_wor_tx():
    m0.value(1); m1.value(0)
    time.sleep_ms(20)
    _wait_aux_mode_change()

def set_mode_normal():
    m0.value(0); m1.value(0)
    time.sleep_ms(20)
    _wait_aux_mode_change()

def initialize_e220_startup():
    set_lora_ch(MY_CH)

# ==========================================
# 12V・充電管理
# ==========================================
def read_12v():    return (adc_12v.read_u16() * 3.3 / 65535) * 5.019
def get_12v_avg(): return sum([read_12v() for _ in range(10)]) / 10
def get_vsys():    return VSYS_CACHE  

def manage_charging():
    v = get_12v_avg()
    if v > CHARGE_STOP_VOLT:    charge_ctrl.value(0)
    elif v < CHARGE_START_VOLT: charge_ctrl.value(1)
    return v

def load_config():
    global MY_FIELD_ID, is_halt, AUTH_KEY, MY_CH
    try:
        with open(CONFIG_FILE, "r") as f:
            d = json.load(f)
            if 'auth' in d: AUTH_KEY    = d['auth']
            if 'id'   in d: MY_FIELD_ID = d['id']
            if 'mode' in d: is_halt     = (d['mode'] == "STOP")
            if 'ch'   in d: MY_CH       = int(d['ch'])
    except: pass

def load_relay_config():
    global RELAY_OPEN_CH
    try:
        with open(RELAY_CMD_FILE, "r") as f:
            d = json.load(f)
            if "open_ch" in d: RELAY_OPEN_CH = int(d["open_ch"])
    except: pass

def load_state():
    global current_pump_status, is_act_error
    try:
        with open(STATE_FILE, "r") as f:
            l = f.read().strip().split(',')
            if len(l) >= 3:
                current_pump_status = l[0]
                is_act_error = (l[2] == "ACT_ERR")
                return 
    except: pass
    save_state(current_pump_status, 0.0, "NORMAL")

def save_state(status, volt, act_st):
    try:
        with open(STATE_FILE, "w") as f: f.write(f"{status},{volt:.2f},{act_st}")
    except: pass

# ==========================================
# モーター・水門制御
# ==========================================
def drive_motor(direction):
    if direction == "OPEN":
        if RELAY_OPEN_CH == 1: 
            set_relay(relay_in1, "ON"); set_relay(relay_in2, "OFF")
        else:                   
            set_relay(relay_in1, "OFF"); set_relay(relay_in2, "ON")
    elif direction == "CLOSE":
        if RELAY_OPEN_CH == 1: 
            set_relay(relay_in1, "OFF"); set_relay(relay_in2, "ON")
        else:                   
            set_relay(relay_in1, "ON"); set_relay(relay_in2, "OFF")
    else: 
        set_relay(relay_in1, "OFF"); set_relay(relay_in2, "OFF")

def execute_op(cmd, maintenance=False):
    global is_act_error, current_pump_status
    v_now = get_12v_avg()

    if v_now > LOW_BATT_LIMIT and is_act_error:
        is_act_error = False
        save_state(current_pump_status, v_now, "NORMAL")

    if is_halt and not maintenance:      
        return "HALT", v_now, "NORMAL"
        
    if is_act_error and not maintenance: 
        return "ACT_ERR", v_now, "ACT_ERR"
        
    if v_now < LOW_BATT_LIMIT:
        is_act_error = True
        save_state(current_pump_status, v_now, "ACT_ERR")
        return "LOW_BATT", v_now, "ACT_ERR"
        
    target_status = current_pump_status
    
    if cmd in ("OPEN", "CLOSE"):
        if current_pump_status == cmd and not maintenance: 
            return "SUCCESS", v_now, "NORMAL"
            
        if maintenance and cmd == "OPEN": 
            is_act_error = False
            
        try:
            drive_motor(cmd)
            st = time.ticks_ms()
            while time.ticks_diff(time.ticks_ms(), st) < 20000:
                v = read_12v()
                if v < 9.0: 
                    is_act_error = True
                    save_state(current_pump_status, v, "ACT_ERR") 
                    return "ACT_ERR", v, "ACT_ERR"
                time.sleep_ms(100)
            target_status = cmd
        finally:
            drive_motor("STOP")
    else: 
        return "SUCCESS", v_now, "NORMAL"
        
    current_pump_status = target_status
    save_state(target_status, v_now, "NORMAL")
    return "SUCCESS", v_now, "NORMAL"

# ==========================================
# ★ USBシリアル設定モード
# ==========================================
def process_usb_serial():
    global AUTH_KEY, MY_CH
    print(f"=== USB CONFIG MODE (VSYS: {VSYS_CACHE:.2f}V) ===")
    print("Waiting for SET_CONFIG from management HTML (WebUSB)...")
    print("Press BOOTSEL button to skip and start.") 

    import network
    _wlan = network.WLAN(network.STA_IF)
    _wlan.active(False) 
    time.sleep_ms(50)
    try: led = machine.Pin("LED", machine.Pin.OUT); led.value(0)
    except: led = None

    poller = uselect.poll()
    poller.register(sys.stdin, uselect.POLLIN)
    buffer = ""
    last_led_toggle = time.ticks_ms()

    while True:
        if rp2.bootsel_button() == 1:
            print("BOOTSEL pressed: Exiting USB config mode.")
            poller.unregister(sys.stdin)
            if led:
                try: led.value(0)
                except: pass
            time.sleep_ms(500) 
            return

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
                            AUTH_KEY = l[1].strip()
                            new_id   = l[2].strip()
                            new_mode = l[8].strip()
                            new_ch   = int(l[9]) if l[9].strip().isdigit() else MY_CH
                            with open(CONFIG_FILE, "w") as f:
                                json.dump({"id": new_id, "mode": new_mode, "auth": AUTH_KEY, "ch": new_ch}, f)
                            # 設定変更された時のみ、E220のEEPROM(不揮発メモリ)に永続保存
                            try:
                                # E220をConfigモード(M0=1, M1=1)へ移行
                                m0.value(1); m1.value(1); time.sleep_ms(20)
                        
                                # 0xC2(RAM)ではなく、0xC0(EEPROM)を使用して永続書き込みコマンドを生成
                                _eeprom_cmd = bytearray([0xC0, 0x00, 0x06, 0x00, 0x00, 0x62, 0x00, new_ch, 0x80])
                        
                                # UARTバッファをクリアして送信
                                while uart.any(): uart.read()
                                uart.write(_eeprom_cmd)
                                time.sleep(0.6) # EEPROMへの物理書き込み完了を長めに待つ
                                while uart.any(): uart.read()
                                
                                if DEBUG_MODE: print(f"✅ Channel {new_ch} saved to E220 EEPROM.")
                            except Exception as e:
                                if DEBUG_MODE: print("EEPROM Save Error:", e)                            
                            try:
                                with open("skip_ap.flag", "w") as f: f.write("1")
                            except: pass
                            print("USB SET_CONFIG: SAVED. REBOOTING...")
                            poller.unregister(sys.stdin)
                            if led: 
                                for _ in range(10):
                                    try: led.value(1); time.sleep(0.05); led.value(0); time.sleep(0.05)
                                    except: pass
                            try: network.WLAN(network.STA_IF).active(False)
                            except: pass
                            machine.Pin(23, machine.Pin.OUT).low()
                            time.sleep_ms(200)
                            machine.reset()
                            
                    elif "MANUAL_OPEN" in line or "MANUAL_CLOSE" in line:
                        cmd_str = "OPEN" if "OPEN" in line else "CLOSE"
                        st, vl, act_st = execute_op(cmd_str, maintenance=True)
                        print(f"USB_RESULT,{cmd_str},{st},{vl:.2f},{act_st},{get_vsys():.2f}")
                        
                    elif "START" in line:
                        print("USB START: Exiting USB config mode.")
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
# ★ APモード設定サーバー (時刻同期JS追加版)
# ==========================================
def _send_all(sock, data, chunk=1024):
    view = memoryview(data)
    pos = 0
    while pos < len(data):
        sent = sock.send(view[pos:pos + chunk])
        pos += sent

def run_ap_config_mode():
    print(f"=== AP CONFIG MODE ===")
    print(f"SSID    : {AP_SSID_PREFIX}{MY_FIELD_ID}")
    print(f"PASSWORD: {AP_PASSWORD}")
    print(f"URL     : http://192.168.4.1/")
    print("Press BOOTSEL button to skip AP mode.")

    import network
    rp2.country('JP')

    ap = network.WLAN(network.AP_IF)
    ap.active(False)            
    time.sleep_ms(200)
    ap.config(essid=AP_SSID_PREFIX + MY_FIELD_ID,
              password=AP_PASSWORD)
    ap.ifconfig(("192.168.4.1", "255.255.255.0", "192.168.4.1", "192.168.4.2"))
    ap.active(True)
    while not ap.active():
        time.sleep_ms(100)

    try:
        with open("setup_actuator.html", "r") as f:
            html_content = f.read()
    except:
        html_content = "<html><body><h2>setup_actuator.html が見つかりません</h2></body></html>"

    server = socket.socket()
    try: server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except AttributeError: pass
    
    server.bind(("0.0.0.0", 80))
    server.listen(3)
    server.settimeout(1.0)

    last_activity = time.ticks_ms()

    while True:
        gc.collect()

        if rp2.bootsel_button() == 1:
            print("BOOTSEL pressed: Exiting AP config mode.")
            time.sleep_ms(500) 
            break

        if time.ticks_diff(time.ticks_ms(), last_activity) > AP_TIMEOUT_MS:
            print("AP CONFIG: Timeout. Proceeding to normal operation.")
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
                
            if "GET /time?dt=" in request:
                try:
                    dt_str = request.split("GET /time?dt=")[1].split(" ")[0]
                    y, m, d, h, mn, s = [int(x) for x in dt_str.split(",")]
                    machine.RTC().datetime((y, m, d, 0, h, mn, s, 0))
                    print(f"AP SYNC TIME OK: {y}/{m}/{d} {h}:{mn}:{s}")
                    _send_all(conn, b"HTTP/1.0 200 OK\r\nConnection: close\r\n\r\nOK")
                except Exception as e:
                    print("AP Time Sync Error:", e)
                conn.close()
                continue 

            if "POST /save" in request:
                params = {}
                for pair in body.split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        params[k] = v.replace("+", " ").replace("%3A", ":")

                new_id       = params.get("id", MY_FIELD_ID).strip()
                new_auth     = params.get("auth", AUTH_KEY).strip()
                new_ch       = int(params.get("ch", str(MY_CH)))
                new_mode     = params.get("mode", "RUN").strip()
                new_relay_ch = int(params.get("relay_ch", str(RELAY_OPEN_CH)))

                with open(CONFIG_FILE, "w") as f:
                    json.dump({"id": new_id, "mode": new_mode,
                               "auth": new_auth, "ch": new_ch}, f)
                with open(RELAY_CMD_FILE, "w") as f:
                    json.dump({"open_ch": new_relay_ch}, f)
                # 設定変更された時のみ、E220のEEPROM(不揮発メモリ)に永続保存
                try:
                    # E220をConfigモード(M0=1, M1=1)へ移行
                    m0.value(1); m1.value(1); time.sleep_ms(20)
                    
                    # 0xC2(RAM)ではなく、0xC0(EEPROM)を使用して永続書き込みコマンドを生成
                    _eeprom_cmd = bytearray([0xC0, 0x00, 0x06, 0x00, 0x00, 0x62, 0x00, new_ch, 0x80])
                    
                    # UARTバッファをクリアして送信
                    while uart.any(): uart.read()
                    uart.write(_eeprom_cmd)
                    time.sleep(0.6) # EEPROMへの物理書き込み完了を長めに待つ
                    while uart.any(): uart.read()
                    
                    if DEBUG_MODE: print(f"✅ Channel {new_ch} saved to E220 EEPROM.")
                except Exception as e:
                    if DEBUG_MODE: print("EEPROM Save Error:", e)
                try:
                    with open("skip_ap.flag", "w") as f: f.write("1")
                except: pass

                resp = (
                    "HTTP/1.0 200 OK\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "Access-Control-Allow-Origin: *\r\n"
                    "Connection: close\r\n\r\nOK"
                )
                _send_all(conn, resp.encode())
                
                time.sleep_ms(500) 
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

            elif "POST /open" in request:
                last_activity = time.ticks_ms() 
                st, vl, act_st = execute_op("OPEN", maintenance=True)
                v12 = get_12v_avg()
                save_state(current_pump_status, v12, "ACT_ERR" if is_act_error else "NORMAL") 
                resp_body = f"OPEN: {st} / 12V:{v12:.2f}V / Vsys:{VSYS_CACHE:.2f}V"
                _send_all(conn, ("HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n" + resp_body).encode())
                conn.close()

            elif "POST /close" in request:
                last_activity = time.ticks_ms()  
                st, vl, act_st = execute_op("CLOSE", maintenance=True)
                v12 = get_12v_avg()
                save_state(current_pump_status, v12, "ACT_ERR" if is_act_error else "NORMAL")
                resp_body = f"CLOSE: {st} / 12V:{v12:.2f}V / Vsys:{VSYS_CACHE:.2f}V"
                _send_all(conn, ("HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n" + resp_body).encode())
                conn.close()

            elif "GET /status" in request:
                v12 = get_12v_avg()
                data = json.dumps({"gate": current_pump_status, "v12": f"{v12:.2f}",
                                   "vsys": f"{VSYS_CACHE:.2f}", "act_error": is_act_error, "halt": is_halt})
                _send_all(conn, ("HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n" + data).encode())
                conn.close()

            else:
                last_activity = time.ticks_ms()  
                v12 = get_12v_avg()
                page = html_content.replace("__FIELD_ID__", MY_FIELD_ID)
                page = page.replace("__AUTH_KEY__", AUTH_KEY)
                page = page.replace("__MY_CH__", str(MY_CH))
                page = page.replace("__MODE__", "STOP" if is_halt else "RUN")
                page = page.replace("__RELAY_OPEN_CH__", str(RELAY_OPEN_CH))
                page = page.replace("__GATE_STATUS__", current_pump_status)
                page = page.replace("__VSYS__", f"{VSYS_CACHE:.2f}")
                page = page.replace("__V12__", f"{v12:.2f}")

                sync_js = (
                    "<script>"
                    "window.onload=function(){"
                    "var d=new Date();"
                    "var t=d.getFullYear()+','+(d.getMonth()+1)+','+d.getDate()+','+d.getHours()+','+d.getMinutes()+','+d.getSeconds();"
                    "fetch('/time?dt='+t);"
                    "};"
                    "</script>"
                )
                page += sync_js

                body_bytes = page.encode()
                header = f"HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body_bytes)}\r\nConnection: close\r\n\r\n"
                _send_all(conn, header.encode() + body_bytes)
                conn.close()

        except Exception as e:
            if DEBUG_MODE: print("AP HTTP Error:", e)
            try: conn.close()
            except: pass

    server.close(); ap.active(False)
    try: network.WLAN(network.STA_IF).active(False)
    except: pass
    machine.Pin(23, machine.Pin.OUT).low()
    gc.collect()
    time.sleep_ms(200)
    

# ==========================================
# ★ Main: APモード/USBモード 判定と実行
# ==========================================
skip_ap_flag = False
try:
    os.stat("skip_ap.flag")
    skip_ap_flag = True
    os.remove("skip_ap.flag") 
except:
    pass

load_config()
load_relay_config()
load_state()

if VSYS_CACHE >= USB_VSYS_THRESHOLD:
    print(f"High Voltage detected (VSYS: {VSYS_CACHE:.2f}V). Entering USB config mode.")
    process_usb_serial()
    load_config(); load_relay_config(); load_state()  

if not skip_ap_flag:
    run_ap_config_mode()
    load_config(); load_relay_config(); load_state() 

safe_shutdown_wifi()

# ==========================================
# ★ 究極の省電力ハック: Pico Wのクロックを48MHzへダウン
# ==========================================
machine.Pin(23, machine.Pin.OUT).low()
gc.collect()
time.sleep_ms(200) 

print("⚡ Switching CPU clock from 125MHz to 48MHz...")
machine.freq(48_000_000) 
uart.init(baudrate=9600, tx=uart_tx, rx=uart_rx, rxbuf=1024)
time.sleep_ms(50)

initialize_e220_startup()

print(f"\n--- GATE READY ---")
print(f"ID: {MY_FIELD_ID} | CH: {MY_CH} | VSYS: {VSYS_CACHE:.2f}V | Gate: {current_pump_status}")
print(f"CPU Freq: {machine.freq() / 1000000:.1f} MHz") 

last_check_time = time.ticks_ms()

# ★ 30分間COMMAND受信なし検出用タイマー。
#   このticksから30分経過してもCOMMANDが届かない場合はLoRa通信崩壊とみなし
#   initialize_e220_startup()でE220を再設定する。
_last_cmd_received_ticks = time.ticks_ms()

# ==========================================
# ★ 割り込みハンドラ (Wakeup用)
# ==========================================
wake_flag = False
def wake_handler(pin):
    global wake_flag
    wake_flag = True

aux.irq(trigger=machine.Pin.IRQ_FALLING, handler=wake_handler)

set_mode_wor_rx()

while True:
    tm_now = time.localtime(time.time() + 9 * 3600)
    if tm_now[3] == MAINT_HOUR and tm_now[4] == MAINT_MIN and tm_now[5] < 5:
        print("MAINT RESET")
        try:
            with open("skip_ap.flag", "w") as f: f.write("1")
        except: pass
        machine.freq(125_000_000)
        uart.init(baudrate=9600, tx=uart_tx, rx=uart_rx, rxbuf=1024)
        time.sleep_ms(500)
        safe_reboot()

    if time.ticks_diff(time.ticks_ms(), last_check_time) > 300000:
        manage_charging()
        last_check_time = time.ticks_ms()

    # ★ 30分間COMMAND受信なし → LoRa通信崩壊とみなしE220を再設定する。
    #   initialize_e220_startup()実行中のAUX遷移がwake_handlerを誤検知しないよう
    #   IRQを一時無効化してから実施し、完了後に再有効化する。
    if time.ticks_diff(time.ticks_ms(), _last_cmd_received_ticks) > 1_980_000:
        aux.irq(handler=None)
        wake_flag = False
        initialize_e220_startup()  # set_lora_ch(MY_CH) → set_mode_wor_rx()
        aux.irq(trigger=machine.Pin.IRQ_FALLING, handler=wake_handler)
        _last_cmd_received_ticks = time.ticks_ms()

    raw = None
    
    # ==========================================
    # ★ 改修: 2段階待ち受けロジック (ノーマルモード完全覚醒対応)
    # ==========================================
    if wake_flag or aux.value() == 0:
        if DEBUG_MODE:
            print("\nWaked up by E220! Waiting for Command...")
            
        aux.irq(handler=None)
        
        # 1. WAKEUPパケットのUART出力が完了するまで待機（AUX HIGH = 出力完了）
        st_a = time.ticks_ms()
        while aux.value() == 0:
            time.sleep_ms(10)
            if time.ticks_diff(time.ticks_ms(), st_a) > 5000: break
            
        # ★ AUX HIGH確認後にuart.init()を呼ぶ。
        #   E220はAUX LOWの最中にUARTへWAKEUPデータを出力するため、
        #   lightsleep復帰直後（UART出力中）のuart.init()はフレーミングエラーを引き起こす。
        #   AUX HIGH（UART出力完了の保証）後に初期化することで問題を根絶する。
        uart.init(baudrate=9600, tx=uart_tx, rx=uart_rx, rxbuf=1024)
        time.sleep_ms(200)
        if uart.any(): uart.read() # WARTUPデータのゴミを捨てる
        
        # --------------------------------------------------------
        # ★ 神の一手: E220をノーマルモードにして完全覚醒させる
        # --------------------------------------------------------
        set_mode_normal()
        
        # --------------------------------------------------------
        # 2. 本命パケットを待つ (10000ms 待機)
        # --------------------------------------------------------
        wait_for_command_ms = 10000 
        st_wait = time.ticks_ms()
        _cmd_buf = b""
        
        while time.ticks_diff(time.ticks_ms(), st_wait) < wait_for_command_ms:
            # ★ Fix2: AUXが0になったらAUX=1まで待つ（AUXパルスが長いパケット向け）
            if aux.value() == 0:
                st_b = time.ticks_ms()
                while aux.value() == 0:
                    time.sleep_ms(10)
                    if time.ticks_diff(time.ticks_ms(), st_b) > 5000: break
                time.sleep_ms(50)
            
            # ★ バッファ蓄積: '\n'到達まで蓄積してからパース。
            #   COMMANDパケット(≈50バイト)を50msポーリングで読むと部分読み取りが発生し、
            #   第1波でb'COMMAND,'を含むがFIELD_IDが切れて不一致→全処理スキップ→
            #   RESULTを送らず水位ノードがTIMEOUTする。'\n'終端まで蓄積して解消する。
            if uart.any():
                _cmd_buf += uart.read()
                if b'\n' not in _cmd_buf:
                    continue  # 終端未到達 → 蓄積継続
                raw = _cmd_buf; _cmd_buf = b""
                
                if b'COMMAND,' in raw or b'MANUAL,' in raw:
                    line = safe_ascii_convert(raw).strip()
                    l = line.split(',')
                    if len(l) > 2 and l[1] == AUTH_KEY and l[2] == MY_FIELD_ID:
                        if DEBUG_MODE: print("✅ Command Packet Received!")
                        break # 自分宛てなので待機を終了して処理へ進む
                    else:
                        if DEBUG_MODE: print("Ignored packet for other ID.")
                        raw = None # 他人宛てなので破棄し、自分のパケットを待ち続ける
            
            time.sleep_ms(50)
            


    # ==========================================
    # 本命パケット(raw)の処理
    # ==========================================
    if raw:
        try:
            line = safe_ascii_convert(raw).strip()
            if line:
                l = line.split(',')
                if len(l) > 1 and l[1] == AUTH_KEY:
                    fid = l[2] if len(l) > 2 else "?"
                    if fid == MY_FIELD_ID:
                        # ★ 自身のID宛てパケット受信 → 30分タイマーリセット
                        _last_cmd_received_ticks = time.ticks_ms()
                        if l[0] == "MANUAL":
                            action_cmd = l[3].strip()
                            _t_exec = time.ticks_ms()
                            st, vl, act_st = execute_op(action_cmd, maintenance=True)
                            # ★MANUAL_RES送信前の待機を動的に決定する。
                            #   モーター動作ケース（OPEN/CLOSE初回）はexecute_opが約15秒かかるため
                            #   2秒待機で十分。
                            #   no-changeケース（既に同一状態・KEEP等）はexecute_opが即returnするため
                            #   水位ノードのNORMALモード確立（COMMAND送信後約2.5秒）に間に合うよう
                            #   合計10秒以上待機する必要がある。
                            #   → execute_op経過時間を計測し、不足分を補完することで
                            #     ケースに関係なく合計約10秒を確保する。
                            _elapsed = time.ticks_diff(time.ticks_ms(), _t_exec)
                            _wait_ms = max(2000, 10000 - _elapsed)
                            time.sleep_ms(_wait_ms)
                            # ★ NORMALモードで送信。受信側（水位ノード）もNORMALで待ち受け。
                            set_mode_normal()
                            send_lora(f"MANUAL_RES,{AUTH_KEY},{MY_FIELD_ID},{st},,{vl:.2f},{act_st},{get_vsys():.2f},{action_cmd}\n")
                            
                        elif l[0] == "COMMAND":
                            _t_exec = time.ticks_ms()
                            st, vl, act_st = execute_op(l[3].strip(), maintenance=False)
                            # ★ RESULT送信前の待機を動的に決定する。
                            #   モーター動作ケース（OPEN/CLOSE初回）はexecute_opが約15秒かかるため
                            #   2秒待機で十分。
                            #   no-changeケース（既に同一状態・KEEP等）はexecute_opが即returnするため
                            #   水位ノードのNORMALモード確立（COMMAND送信後約2.5秒）に間に合うよう
                            #   合計15秒以上待機する必要がある。
                            #   → execute_op経過時間を計測し、不足分を補完することで
                            #     ケースに関係なく合計約10秒を確保する。
                            _elapsed = time.ticks_diff(time.ticks_ms(), _t_exec)
                            _wait_ms = max(2000, 10000 - _elapsed)
                            time.sleep_ms(_wait_ms)
                            # ★ NORMALモードで送信。受信側（水位ノード）もNORMALで待ち受け。
                            set_mode_normal()
                            send_lora(f"RESULT,{AUTH_KEY},{MY_FIELD_ID},{st},,{vl:.2f},{act_st},{get_vsys():.2f}\n")
                            
        except: pass

    # =========================================================
    # ★ 究極の安全策: 全ての処理が終わったココで、無条件に1回だけ戻す！
    # （自分宛て、他人宛て、エラー、タイムアウト、全てを安全に回収）
    # =========================================================
    set_mode_wor_rx()
    gc.collect()
    # 万が一UARTバッファにゴミが残っていれば捨てる
    if not raw:
        while uart.any(): uart.read()
    gc.collect()
    
    # ==========================================
    # 通常時は LightSleep で深い眠りにつく
    # ==========================================
    if not uart.any():
        gc.collect()
        # フラグをクリアし割り込みを再有効化
        wake_flag = False
        aux.irq(trigger=machine.Pin.IRQ_FALLING, handler=wake_handler)
        if DEBUG_MODE: 
            print("Zzz... (Normal Sleep)")
            time.sleep_ms(SLEEP_INTERVAL_MS)
        else: 
            machine.lightsleep(SLEEP_INTERVAL_MS)
        
        # ★ uart.init()はここでは呼ばない。
        #   AUX IRQで起床した場合、lightsleep復帰直後はすでにE220がWAKEUPパケットの
        #   UART出力を開始している（AUX LOW中にUART出力が行われるのがE220の仕様）。
        #   この最中にuart.init()を呼ぶとフレーミングエラーが発生するため、
        #   AUX HIGH（WAKEUP UART出力完了）を確認してから呼ぶ。
        #   タイムアウト復帰（wake_flag=False, AUX=HIGH）の場合のみここで初期化する。
        if not wake_flag and aux.value() == 1:
            uart.init(baudrate=9600, tx=uart_tx, rx=uart_rx, rxbuf=1024)
            gc.collect()
        
    time.sleep_ms(10)