'''
Copyright (C) 2026 AgriSynapse Project

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
'''

import machine, time, gc, os, json, sys, uselect, network, socket, onewire, ds18x20, rp2

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
# システム設定
# ==========================================
DEBUG_MODE   = False
AUTH_KEY     = "ここにシステム共通パスコードを半角英数字で入力してください"
CONFIG_FILE  = "config.json"
BASE_DIST_FILE = "base_dist.json"
STATE_FILE   = "state.txt"

AP_SSID_PREFIX   = "Pico-Setup-"  
AP_PASSWORD      = "picow1234" #APモード時のPicoWへのアクセス用パスワードは、適宜変更してください（システムで共通化すると便利です）    
AP_TIMEOUT_MS    = 3 * 60 * 1000  
USB_VSYS_THRESHOLD = 4.7　#USB接続判定のためDCDCコンバーターMP1584ENモジュールの出力電圧は4.0V程度に設定し、この値（4.7V）を超えないようにしてください           
#メンテナンスのための自動再起動時刻設定
MAINT_HOUR = 0
MAINT_MIN  = 0

# ==========================================
# ピン定義
# ==========================================
uart_tx = machine.Pin(0)
uart_rx = machine.Pin(1, machine.Pin.IN, machine.Pin.PULL_UP)
uart    = machine.UART(0, baudrate=9600, tx=uart_tx, rx=uart_rx, rxbuf=1024)

aux = machine.Pin(2, machine.Pin.IN, machine.Pin.PULL_UP)
m1  = machine.Pin(3, machine.Pin.OUT)
m0  = machine.Pin(4, machine.Pin.OUT)

sensor_vcc  = machine.Pin(22, machine.Pin.OUT)
trigger_pin = machine.Pin(21, machine.Pin.OUT)
echo_pin    = machine.Pin(20, machine.Pin.IN, machine.Pin.PULL_DOWN)

ds18b20_vcc = machine.Pin(15, machine.Pin.OUT)
ds18b20_vcc.value(0)
ds_data     = machine.Pin(6)
ds_sensor   = ds18x20.DS18X20(onewire.OneWire(ds_data))

# ==========================================
# VSYS計測・Wi-Fiゾンビ検出
# ==========================================
gc.collect()
machine.Pin(23, machine.Pin.OUT).high()
try:
    _wlan_sta = network.WLAN(network.STA_IF)
    _wlan_sta.active(False)
    _wlan_ap = network.WLAN(network.AP_IF)
    _wlan_ap.active(False)
except OSError:
    machine.Pin(23, machine.Pin.OUT).low()
    time.sleep_ms(500)
    pass

time.sleep_ms(100)                        
VSYS_CACHE = round(machine.ADC(29).read_u16() * 3.3 / 65535 * 3, 2)

# ==========================================
# 設定変数
# ==========================================
FIELD_ID   = "DEFAULT_FIELD"
TH_DRY     = 2.0
TH_FULL    = 6.0
OFFSET_MIN = 0
MY_CH      = 0
is_halt    = False
# ★ 水門ノードとの通信でRESULTがTIMEOUTした連続回数を記録する。
#   3回連続TIMEOUTでLoRa通信崩壊とみなし、initialize_e220_startup()でE220を再設定する。
_consecutive_timeouts = 0
# ★ 起動直後の最初のTIMEパケットでGWのデフォルト値をconfig.jsonに
#   書き込んでしまうことを防ぐフラグ。
#   毎起動時にTrueに設定し、最初のTIME受信時にFalseにリセットする。
#   RTC同期は初回でも実施するが、閾値・モードの保存はスキップする。
_ignore_first_time_sync = True
BASE_DIST  = -1.0

m0.value(0); m1.value(0)

# ==========================================
# LoRa ユーティリティ
# ==========================================
def wait_aux():
    st = time.ticks_ms()
    while aux.value() == 0:
        if time.ticks_diff(time.ticks_ms(), st) > 5000: break
        time.sleep_ms(10)
    # ★ AUX HIGH確認後、E220内部処理の完了に余裕を持たせる
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
        for h in ["DATA,", "TIME,", "RESULT,"]:
            idx = res.find(h)
            if idx != -1: return res[idx:].strip()
    except: pass
    return ""

def _wait_aux_mode_change():
    # ★ M0/M1変更後にAUXが一旦LOWへ落ちるのを最大50ms待ち、
    #   その後HIGHになるまで待つ2段階確認。
    #   E220はモード切替開始をAUX=LOWで通知するため、
    #   LOW遷移を見逃すとモード変更未完了のまま処理が進む。
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

def set_lora_ch(c):
    m0.value(1); m1.value(1); time.sleep_ms(20); wait_aux()
    cmd = bytearray([0xC2, 0x00, 0x06, 0x00, 0x00, 0x62, 0x00, int(c), 0x80])
    while uart.any(): uart.read()
    uart.write(cmd); time.sleep(0.6)
    while uart.any(): uart.read()
    set_mode_wor_rx()

def initialize_e220_startup():
    set_lora_ch(MY_CH)

def load_config():
    global FIELD_ID, TH_DRY, TH_FULL, OFFSET_MIN, is_halt, MY_CH, BASE_DIST, AUTH_KEY
    try:
        with open(CONFIG_FILE, "r") as f:
            d = json.load(f)
            AUTH_KEY   = d.get("auth", "KOKUFU_2026")
            FIELD_ID   = d.get("id", "DEFAULT_FIELD")
            TH_DRY     = float(d.get("dry", 3.0))
            TH_FULL    = float(d.get("full", 15.0))
            OFFSET_MIN = int(d.get("offset", 0))
            is_halt    = (d.get("mode", "RUN") == "STOP")
            MY_CH      = int(d.get("ch", 0))
            BASE_DIST  = float(d.get("base_dist", -1.0))
    except: pass
    try:
        with open(BASE_DIST_FILE, "r") as f:
            BASE_DIST = float(json.load(f).get("base_dist", BASE_DIST))
    except: pass

def save_config(i, d, f, o, m, c, a):
    try:
        with open(CONFIG_FILE, "w") as f_obj:
            json.dump({"id": i, "dry": float(d), "full": float(f),
                       "offset": int(o), "mode": m, "ch": int(c), "auth": a}, f_obj)
    except: pass

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            l = f.read().split(',')
            if len(l) >= 3: return l[0], l[1], l[2]
            if len(l) >= 2: return l[0], l[1], "UNKNOWN"
    except: pass
    return "0.00", "UNKNOWN", "UNKNOWN"

def save_state(v, s, a):
    try:
        with open(STATE_FILE, "w") as f: f.write(f"{v},{s},{a}")
    except: pass

# ==========================================
# センサー計測
# ==========================================
def calibrate_baseline():
    global BASE_DIST
    d = get_avg_distance(5)
    if d is not None:
        BASE_DIST = d
    else:
        BASE_DIST = 100.0
    try:
        with open(BASE_DIST_FILE, "w") as f: json.dump({"base_dist": BASE_DIST}, f)
    except: pass
    time.sleep(2)

def measure_distance(speed_of_sound):
    trigger_pin.value(0); time.sleep_us(5)
    trigger_pin.value(1); time.sleep_us(20)
    trigger_pin.value(0)
    try:
        dur = machine.time_pulse_us(echo_pin, 1, 30000)
        if dur < 0: return None
        dist = round((dur * speed_of_sound) / 2, 1)
        if dist > 300.0: return None
        return dist
    except: return None

def get_avg_distance(samples=5):
    sensor_vcc.value(1)   
    ds18b20_vcc.value(1)  
    time.sleep_ms(500)    
    current_temp = 25.0   
    try:
        roms = ds_sensor.scan()
        if roms:
            ds_sensor.convert_temp()
            time.sleep_ms(750) 
            t = ds_sensor.read_temp(roms[0])
            if t != 85.0 and t > -40.0: current_temp = t
    except: pass
    ds18b20_vcc.value(0)  
    sos = (331.5 + 0.6 * current_temp) * 100 / 1e6
    dists = []
    for _ in range(samples + 2):
        d = measure_distance(sos)
        if d is not None and 2.0 < d <= 300.0: dists.append(d)
        time.sleep_ms(60)
    sensor_vcc.value(0) 
    if len(dists) < 2: return None
    dists.sort(); trimmed = dists[1:-1] if len(dists) > 3 else dists
    return round(sum(trimmed) / len(trimmed), 1)

# ==========================================
# TDMA (絶対時間スロット) 睡眠計算ロジック
# ==========================================
def calc_sleep_ms(m, s, offset):
    current_sec_in_cycle = (m % 10) * 60 + s
    node_index = offset // 2
    target_sec = node_index * 40
    
    if target_sec <= current_sec_in_cycle:
        sleep_sec = (600 - current_sec_in_cycle) + target_sec
    else:
        sleep_sec = target_sec - current_sec_in_cycle
        
    return sleep_sec * 1000

# ==========================================
# ★ USBシリアル設定モード 
# ==========================================
def process_usb_serial():
    global AUTH_KEY
    print(f"=== USB CONFIG MODE (VSYS: {VSYS_CACHE:.2f}V) ===")
    import network
    network.WLAN(network.STA_IF).active(False) 
    time.sleep_ms(50)
    try: led = machine.Pin("LED", machine.Pin.OUT); led.value(0)
    except: led = None
    poller = uselect.poll(); poller.register(sys.stdin, uselect.POLLIN)
    buffer = ""; last_led_toggle = time.ticks_ms()
    while True:
        if rp2.bootsel_button() == 1:
            poller.unregister(sys.stdin); return
        res = poller.poll(10) 
        if res:
            char = sys.stdin.read(1)
            if char:
                if char in ('\n', '\r'):
                    line = buffer.strip(); buffer = ""
                    if "SET_CONFIG" in line:
                        l = line.split(',')
                        if len(l) >= 10:
                            AUTH_KEY = l[1].strip(); new_id = l[2].strip()
                            try: new_dry, new_full = float(l[3]), float(l[4])
                            except: new_dry, new_full = TH_DRY, TH_FULL
                            new_offset = int(l[5]); new_mode = l[8].strip(); new_ch = int(l[9])
                            if len(l) >= 14:
                                try: machine.RTC().datetime((int(l[11]), int(l[12]), int(l[13]), 0, int(l[6]), int(l[7]), 0, 0))
                                except: pass
                            save_config(new_id, new_dry, new_full, new_offset, new_mode, new_ch, AUTH_KEY)

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
                            safe_reboot()
                    elif "FORCE_CALIB" in line:
                        calibrate_baseline(); safe_reboot()
                    elif "START" in line:
                        poller.unregister(sys.stdin); return
                else: buffer += char
        if time.ticks_diff(time.ticks_ms(), last_led_toggle) > 500:
            if led: led.value(not led.value())
            last_led_toggle = time.ticks_ms()

def _send_all(sock, data):
    view = memoryview(data); pos = 0
    while pos < len(data): sent = sock.send(view[pos:pos + 1024]); pos += sent

# ==========================================
# ★ APモード設定サーバー (バグ完全修正版)
# ==========================================
def run_ap_config_mode():
    print(f"=== AP CONFIG MODE ===")
    print(f"SSID    : {AP_SSID_PREFIX}{FIELD_ID}")
    print(f"PASSWORD: {AP_PASSWORD}")
    print(f"URL     : http://192.168.4.1/")

    import rp2
    rp2.country('JP')

    ap = network.WLAN(network.AP_IF)
    ap.active(False)
    time.sleep_ms(200)
    ap.config(essid=AP_SSID_PREFIX + FIELD_ID,
              password=AP_PASSWORD)
    ap.ifconfig(("192.168.4.1", "255.255.255.0", "192.168.4.1", "192.168.4.2"))
    ap.active(True)
    while not ap.active():
        time.sleep_ms(100)

    try:
        with open("setup_sensor.html", "r") as f:
            html_content = f.read()
    except:
        html_content = "<html><body><h2>setup_sensor.html が見つかりません</h2></body></html>"

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 80))
    server.listen(3)
    server.settimeout(1.0)

    last_activity = time.ticks_ms()

    while True:
        gc.collect() 

        if time.ticks_diff(time.ticks_ms(), last_activity) > AP_TIMEOUT_MS:
            print("AP CONFIG: Timeout. Proceeding to normal operation.")
            break

        try:
            conn, addr = server.accept()
        except OSError:
            continue

        last_activity = time.ticks_ms()

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

            if "POST /save" in headers:
                params = {}
                for pair in body.split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        params[k] = v.replace("+", " ").replace("%3A", ":")

                new_id     = params.get("id", FIELD_ID).strip()
                new_auth   = params.get("auth", AUTH_KEY).strip()
                new_ch     = int(params.get("ch", str(MY_CH)))
                new_offset = int(params.get("offset", str(OFFSET_MIN)))
                new_mode   = params.get("mode", "RUN").strip()
                
                # HTMLから送られてきたDRY/FULLを受け取る
                try:
                    new_dry = float(params.get("dry", str(TH_DRY)))
                    new_full = float(params.get("full", str(TH_FULL)))
                except:
                    new_dry, new_full = TH_DRY, TH_FULL

                # 新しい閾値でPico内部に保存
                save_config(new_id, new_dry, new_full, new_offset, new_mode, new_ch, new_auth)

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
                    "Access-Control-Allow-Origin: *\r\n" # JSで処理するためCORS許可
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

            elif "POST /calibrate" in headers:
                calibrate_baseline()
                resp = (f"HTTP/1.0 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                        f"Access-Control-Allow-Origin: *\r\n"
                        f"Connection: close\r\n\r\n✅ キャリブレーション完了: {BASE_DIST:.1f}cm")
                _send_all(conn, resp.encode())
                conn.close()

            elif "POST /measure" in headers:
                #   現在の水位を測定してJSONで返す。
                #   BASE_DIST（キャリブレーション済み空底距離）から
                #   現在のセンサー距離を引いて水位を算出する。
                d = get_avg_distance(5)
                if d is not None and BASE_DIST > 0:
                    level_cm = round(BASE_DIST - d, 1)
                    body_str = json.dumps({"ok": True, "dist_cm": d, "base_cm": BASE_DIST, "level_cm": level_cm})
                elif d is not None:
                    body_str = json.dumps({"ok": False, "dist_cm": d, "base_cm": BASE_DIST, "level_cm": None, "error": "キャリブレーション未実施"})
                else:
                    body_str = json.dumps({"ok": False, "dist_cm": None, "base_cm": BASE_DIST, "level_cm": None, "error": "センサー計測失敗"})
                resp = (f"HTTP/1.0 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
                        f"Access-Control-Allow-Origin: *\r\n"
                        f"Connection: close\r\n\r\n{body_str}")
                _send_all(conn, resp.encode())
                conn.close()

            else:
                page = html_content.replace("__FIELD_ID__", FIELD_ID)
                page = page.replace("__AUTH_KEY__", AUTH_KEY)
                page = page.replace("__MY_CH__", str(MY_CH))
                page = page.replace("__OFFSET_MIN__", str(OFFSET_MIN))
                page = page.replace("__MODE__", "STOP" if is_halt else "RUN")
                page = page.replace("__BASE_DIST__", f"{BASE_DIST:.1f}")
                page = page.replace("__VSYS__", f"{VSYS_CACHE:.2f}")
                # 現在の閾値をHTMLのフォームの初期値として埋め込む
                page = page.replace("__TH_DRY__", f"{TH_DRY:.1f}")
                page = page.replace("__TH_FULL__", f"{TH_FULL:.1f}")

                body_bytes = page.encode()
                header = f"HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body_bytes)}\r\nConnection: close\r\n\r\n"
                _send_all(conn, header.encode() + body_bytes)
                conn.close()

        except Exception as e:
            if DEBUG_MODE: print("AP HTTP Error:", e)
            try: conn.close()
            except: pass

    server.close()
    safe_shutdown_wifi()

# ==========================================
# ★ Main
# ==========================================
skip_ap_flag = False
try:
    os.stat("skip_ap.flag"); skip_ap_flag = True; os.remove("skip_ap.flag") 
except: pass

load_config()
if VSYS_CACHE >= USB_VSYS_THRESHOLD:
    process_usb_serial(); load_config()
    try: os.stat("skip_ap.flag"); skip_ap_flag = True
    except: pass
if VSYS_CACHE >= 2.7:
    if not skip_ap_flag: run_ap_config_mode(); load_config() 
    safe_shutdown_wifi()
else: safe_shutdown_wifi()

initialize_e220_startup()
if BASE_DIST < 0: calibrate_baseline()

print(f"READY. ID:{FIELD_ID} CH:{MY_CH} OFFS:{OFFSET_MIN}")
# 再起動直後の衝突を防ぐため、初回のみオフセット秒数×5秒待機してタイミングをズラす
if not skip_ap_flag:
    time.sleep(OFFSET_MIN * 5)
mins_since_last_gw_sync = 30.0
prev_gate_status, prev_sensor_error, prev_sys_st = "UNKNOWN", False, "NORMAL"

while True:
    tm_now = time.localtime(time.time() + 9 * 3600)
    if tm_now[3] == MAINT_HOUR and tm_now[4] == MAINT_MIN and tm_now[5] < 5:
        try:
            with open("skip_ap.flag", "w") as f: f.write("1")
        except: pass
        safe_reboot()

    set_mode_wor_rx(); 
    while uart.any(): uart.read()

    current_dist = get_avg_distance(5)
    if current_dist is not None and BASE_DIST > 0:
        water_level = max(0.0, BASE_DIST - current_dist); sensor_error = False
    else: water_level = 0.0; sensor_error = True

    gate_12v, gate_status, gate_act_status = load_state()
    gate_vsys = "0.00"
    if TH_FULL > TH_DRY and not sensor_error:
        level_pct = max(0, min(100, int((water_level - TH_DRY) / (TH_FULL - TH_DRY) * 100)))
    else: level_pct = 0
    gate_cmd = "KEEP"
    if not is_halt and not sensor_error:
        if water_level <= TH_DRY: gate_cmd = "OPEN"
        elif water_level >= TH_FULL: gate_cmd = "CLOSE"
    else:
        # STOP中（またはセンサーエラー時）は、
        # 自分が保持している古い水門状態を強制的に"KEEP"に書き換えて送信する。
        # これにより、GAS側での「遠隔手動操作後の状態」の意図せぬ上書きを防ぐ。
        gate_status = "KEEP"
    op_status = "NO_ACTION"

    # ==========================================
    # 水門へのコマンド送信 (スロットを侵害しないよう20秒制限)
    # ==========================================
    if not is_halt:
        set_mode_wor_tx()
        send_lora("WAKEUP\n")
        #   WAKEUP送信完了後、E220内部のWORステートマシンが
        #   安定するまで2000ms待機してからNORMALモードに切り替え、
        #   さらに2000ms待機してからCOMMANDを送信する。
        #   これによりモード切替未完了のままCOMMANDを送信するリスクを排除する。
        time.sleep_ms(2000)
        set_mode_normal()
        time.sleep_ms(2000)
        send_lora(f"COMMAND,{AUTH_KEY},{FIELD_ID},{gate_cmd}\n")
        #   COMMAND送信後はNORMALモードのままRESULTパケットを待ち受ける。
        #   WOR RXのままだとE220がすぐライトスリープに入ってしまい、その間にRESULTパケットが届いて
        #   不定状態になるリスクがある。NORMALモードなら常時受信状態。
        #   水門ノードのRESULT送信はNORMALモードで正しく受信できる。
        wait_aux()
        time.sleep_ms(500)
        # ★ set_mode_wor_rx()はここでは呼ばない（NORMALモード維持）
        
        # ★ RESULTパケット受信バッファ蓄積ループ
        #   uart.any()がTrueになった時点でuart.read()を呼ぶと全バイト未到達の場合がある。
        #   no-changeケース（KEEP・同一状態）ではRESULTが素早く返るため特に発生しやすく、
        #   部分読み取りで「RESULT,」を含む第1波は len(l)<6 でスキップ、
        #   「RESULT,」を含まない第2波は即discardとなりTIMEOUTを引き起こす。
        #   '\n'（パケット終端）が届くまでバッファを蓄積してからパースする。
        st_result = time.ticks_ms(); found = False
        _recv_buf = b""
        while time.ticks_diff(time.ticks_ms(), st_result) < 30000:
            if aux.value() == 0 or uart.any():
                st_a = time.ticks_ms()
                while aux.value() == 0:
                    if time.ticks_diff(time.ticks_ms(), st_a) > 5000: break
                    time.sleep_ms(10)
                time.sleep_ms(20)
            if uart.any():
                _recv_buf += uart.read()
                if b'\n' not in _recv_buf:
                    continue  # 終端未到達 → 蓄積継続
                raw = _recv_buf; _recv_buf = b""
                try:
                    if b'RESULT,' not in raw: continue
                    l = safe_ascii_convert(raw).split(',')
                    if len(l) >= 6 and l[0] == "RESULT" and l[1] == AUTH_KEY:
                        op_status, gate_12v = l[3], l[5]
                        if len(l) >= 7: gate_act_status = l[6]
                        if len(l) >= 8: gate_vsys = l[7]
                        # ★ gate_cmd が"KEEP"の場合はgate_statusを上書きしない。
                        #   水門の実際の状態（OPEN/CLOSE）をstate.txtに保持し続ける。
                        if op_status == "SUCCESS" and gate_cmd not in ("KEEP", ""):
                            gate_status = gate_cmd
                        save_state(gate_12v, gate_status, gate_act_status); found = True; break
                except: pass
            time.sleep_ms(100)
        if not found:
            op_status = "TIMEOUT"
            # ★ 3回連続TIMEOUTでLoRa通信崩壊とみなしE220を再設定する。
            #   set_lora_ch(MY_CH)によりE220のチャンネル設定を書き直し、
            #   最後にset_mode_wor_rx()で正常なWOR RX状態に復帰させる。
            _consecutive_timeouts += 1
            if _consecutive_timeouts >= 3:
                initialize_e220_startup()
                _consecutive_timeouts = 0
        else:
            _consecutive_timeouts = 0  # RESULT受信成功 → カウンタリセット
        # ★ RESULT受信（またはTIMEOUT）後にWOR RXへ戻す
        set_mode_wor_rx()

    # ==========================================
    # ゲートウェイへの報告 (残りの10秒以内に完結)
    # ==========================================
    sys_st = "HALT" if is_halt else ("ERROR" if sensor_error else "NORMAL")
    should_report = (mins_since_last_gw_sync >= 25 or gate_status != prev_gate_status or sensor_error != prev_sensor_error or sys_st != prev_sys_st)

    if should_report:
        set_mode_wor_tx()
        # ★ d[13]にOFFSET_MINを追加。GWがドロップ判定（オフセット不一致チェック）に使用する。
        pkt = f"DATA,{AUTH_KEY},{FIELD_ID},{water_level:.1f},{level_pct},{VSYS_CACHE:.2f},{gate_status},{gate_cmd},{op_status},{gate_12v},{gate_act_status},{sys_st},{gate_vsys},{OFFSET_MIN}\n"
        send_lora(pkt)
        # ★ DATA送信(WOR TX)完了後、NORMALモードでTIMEパケットを待ち受ける。
        #   WOR RXモードに戻すと E220がライトスリープに入った直後にGWのTIMEが届いて
        #   不定状態になるリスクがある。NORMALモードなら常時受信状態のため
        #   タイミング依存性やWOR周期同期の問題を完全に排除できる。
        #   1. AUXが一旦LOWに落ちるのを最大100ms待つ（WOR TX完了通知）
        #   2. AUX HIGH（完全完了）まで待つ
        #   3. 1000ms安定待機（GWがTIME送信を開始するのは約1490ms後）
        #   4. NORMALモードに切り替えてTIMEを待つ
        _st_aux = time.ticks_ms()
        while aux.value() == 1:
            if time.ticks_diff(time.ticks_ms(), _st_aux) > 100: break
            time.sleep_ms(2)
        wait_aux()
        time.sleep_ms(1000)
        set_mode_normal()
        
        # ★ TIMEパケット受信バッファ蓄積ループ
        #   RESULTと同様、TIME≈63バイトも部分読み取りリスクがある。
        #   '\n'到達まで_recv_bufに蓄積してからパースする。
        st_time = time.ticks_ms(); sync_ok = False
        _recv_buf = b""
        while time.ticks_diff(time.ticks_ms(), st_time) < 15000:
            if aux.value() == 0 or uart.any():
                st_a = time.ticks_ms()
                while aux.value() == 0:
                    if time.ticks_diff(time.ticks_ms(), st_a) > 5000: break
                    time.sleep_ms(10)
                time.sleep_ms(20)
            if uart.any():
                _recv_buf += uart.read()
                if b'\n' not in _recv_buf:
                    continue  # 終端未到達 → 蓄積継続
                raw = _recv_buf; _recv_buf = b""
                try:
                    if b'TIME,' not in raw: continue
                    l = safe_ascii_convert(raw).split(',')
                    if len(l) >= 5 and l[0] == "TIME" and l[2] == FIELD_ID:
                    # 閾値とモードの更新（要素が8個以上ある場合のみ処理）
                        if len(l) >= 8:
                            new_dry = float(l[5])
                            new_full = float(l[6])
                            new_halt_str = l[7].strip()
                            new_is_halt = (new_halt_str == "STOP")
    
                            # ★ 起動後初回のTIMEはconfig.jsonへの書き込みをスキップする。
                            #   GWのFIELD_CACHEが未同期の状態でGAS側のデフォルト値が
                            #   書き込まれて先祖返りしてしまうことを防ぐ。2回目以降は通常通り処理する。
                            if _ignore_first_time_sync:
                                _ignore_first_time_sync = False
                                # 閾値・モードは適用しない（ローカルの設定値を維持）
                            else:
                                # ★ 寿命対策: 現在の変数と差分がある場合のみ保存を実行
                                if (TH_DRY != new_dry or TH_FULL != new_full or is_halt != new_is_halt):
                                    TH_DRY = new_dry
                                    TH_FULL = new_full
                                    is_halt = new_is_halt
                                    # 物理メモリ(Flash)へ書き込み
                                    save_config(FIELD_ID, TH_DRY, TH_FULL, OFFSET_MIN, new_halt_str, MY_CH, AUTH_KEY)
                                
                        # RTC同期は初回でも実施
                        if len(l) >= 11:
                            machine.RTC().datetime((int(l[8]), int(l[9]), int(l[10]), 0, int(l[3]), int(l[4]), 0, 0))
                        sync_ok = True; break
                except: pass
            time.sleep_ms(100)
            
        mins_since_last_gw_sync = 0.0
        # TIME受信で is_halt 等が変更された可能性があるので、
        # 過去状態(prev)に退避する前に sys_st を最新状態に再評価しておく。
        # (これをしないと次のループで「状態が変わった」と勘違いして2重送信してしまう)        
        sys_st = "HALT" if is_halt else ("ERROR" if sensor_error else "NORMAL")
        prev_gate_status, prev_sensor_error, prev_sys_st = gate_status, sensor_error, sys_st
    
    tm = time.localtime()
    slp_ms = calc_sleep_ms(tm[4], tm[5], OFFSET_MIN)
    mins_since_last_gw_sync += (slp_ms / 60000)
    
    gc.collect()
    m0.value(1); m1.value(1); time.sleep_ms(20)
    if DEBUG_MODE: time.sleep(slp_ms / 1000)
    else:
        machine.lightsleep(int(slp_ms))
        uart.init(baudrate=9600, tx=uart_tx, rx=uart_rx, rxbuf=1024)
        time.sleep_ms(50)