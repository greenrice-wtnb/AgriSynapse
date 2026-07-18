'''
Copyright (C) 2026 AgriSynapse Project

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
'''

import machine, network, urequests, time, gc, ntptime, json, sys, uselect, socket, os

# ==========================================
# 必須: クロックを標準の125MHzに強制初期化
# ==========================================
machine.freq(125_000_000)
# ==========================================
# 設定・定数
# ==========================================
DEBUG_MODE = False
AUTH_KEY = "ここにシステム共通パスコードを半角英数字で入力してください" 
DEFAULT_SSID = "WiFiの接続先SSIDを入力してください"
DEFAULT_PASS = "WiFiの接続用パスワードを入力してください"
#ここに、CloudflareWorkersにデプロイした「管理用GASの302リダイレクト追従用プロキシサーバーJavaScript」のURLを入力してください↓
GAS_URL = "https://gas-proxy.XXXXXXXX.workers.dev/"
#メンテナンスのための自動再起動時刻設定
MAINT_HOUR, MAINT_MIN = 0, 15
CONFIG_FILE  = "gw_config.json"
WIFI_FILE    = "wifi_conf.json"
FIELD_CACHE_FILE = "field_cache.json"  # ★ キャッシュ保存用ファイル

EVENT_LOGS = []
MAX_EVENTS = 20

def log_event(code_num):
    if DEBUG_MODE: print(f"EVENT: {code_num}")
    try:
        _t = time.localtime(time.time() + 9 * 3600)
        _ts = f"@{_t[3]:02d}:{_t[4]:02d}"
    except:
        _ts = ""
    EVENT_LOGS.append(str(code_num) + _ts)
    if len(EVENT_LOGS) > MAX_EVENTS:
        EVENT_LOGS.pop(0)

# ==========================================
# ハードウェア初期化 ＆ VSYS初期計測
# ==========================================
wlan = network.WLAN(network.STA_IF)

try:
    wlan.active(False)
    time.sleep(0.1)
    wlan.active(True)
except OSError as e:
    if "EPERM" in str(e) or (len(e.args) > 0 and e.args[0] == 1):
        log_event(104) 
        time.sleep(0.5)
        machine.reset()

time.sleep(1.0)

try:
    wl_gpio1 = machine.Pin('WL_GPIO1', machine.Pin.OUT)
    wl_gpio1.value(1)
    time.sleep_ms(20)
    cached_vsys = round((machine.ADC(29).read_u16() * 3.3 / 65535) * 3, 2)
    wl_gpio1.value(0)
except Exception as e:
    cached_vsys = 5.00

led = machine.Pin("LED", machine.Pin.OUT)
m0 = machine.Pin(4, machine.Pin.OUT)
m1 = machine.Pin(3, machine.Pin.OUT)
aux = machine.Pin(2, machine.Pin.IN, machine.Pin.PULL_UP)
uart = machine.UART(0, baudrate=9600, tx=machine.Pin(0), rx=machine.Pin(1), rxbuf=2048)
router_pwr = machine.Pin(22, machine.Pin.OUT)

adc_12v = machine.ADC(26)
charge_ctrl = machine.Pin(27, machine.Pin.OUT)
charge_ctrl.value(0)
CHARGE_STOP_VOLT = 13.8
CHARGE_START_VOLT = 12.8

LISTEN_CH = 10
MY_GW_ID = "GW_MAIN" 
GW_OFFSET = 0  # ★ GAS送信タイミングオフセット（分）
FIELD_CHANNELS = {}
FIELD_CACHE = {} 
FIELD_OFFSETS = {}  # ★ フィールドごとのオフセット値（GASから取得）
curr_ssid = DEFAULT_SSID
curr_pass = DEFAULT_PASS

BATCH_CACHE = {}
CHUNK_QUEUE = []
LORA_CMD_QUEUE = [] 
PENDING_SYNC = False
LORA_CMD_FILE = "lora_cmd_queue.json"

m0.value(0); m1.value(0); time.sleep(0.1)

# ==========================================
# LED制御・ハードウェア監視関数
# ==========================================
def set_led(state):
    try: led.value(state)
    except: pass

def toggle_led():
    try: led.value(not led.value())
    except: pass

def startup_led_pattern():
    for _ in range(5):
        set_led(1); time.sleep_ms(100)
        set_led(0); time.sleep_ms(100)
        set_led(1); time.sleep_ms(100)
        set_led(0); time.sleep_ms(700)

def read_12v(): return (adc_12v.read_u16() * 3.3 / 65535) * 4.9 
def get_12v_avg(): return sum([read_12v() for _ in range(10)]) / 10

def manage_charging():
    v = get_12v_avg()
    if v > CHARGE_STOP_VOLT: charge_ctrl.value(0) 
    elif v < CHARGE_START_VOLT: charge_ctrl.value(1) 
    return v

# ==========================================
# 通信＆システム関数
# ==========================================
def wait_aux():
    st = time.ticks_ms()
    while aux.value() == 0:
        if time.ticks_diff(time.ticks_ms(), st) > 5000: break
        time.sleep_ms(10)
    time.sleep_ms(20)

def send_lora(msg):
    set_led(1) 
    uart.write(msg)
    time.sleep_ms(len(msg) + 50) 
    wait_aux()
    set_led(0)

def clean_uart_data(b_data):
    try:
        res = ""
        for b in b_data:
            if 0x20 <= b <= 0x7E: res += chr(b)
            elif b == 0x0A or b == 0x0D: res += "\n" 
        lines = res.split('\n')
        valid_line = ""
        for line in lines:
            if "DATA," in line or "MANUAL_RES," in line:
                valid_line = line
                break
        return valid_line.strip()
    except Exception: return ""

def _wait_aux_mode_change():
    st = time.ticks_ms()
    while aux.value() == 1:
        if time.ticks_diff(time.ticks_ms(), st) > 50: break
        time.sleep_ms(2)
    wait_aux()

def set_mode_wor_tx():
    m0.value(1); m1.value(0)
    time.sleep_ms(20)
    _wait_aux_mode_change()

def set_mode_wor_rx():
    m0.value(0); m1.value(1)
    time.sleep_ms(20)
    _wait_aux_mode_change()

def set_mode_normal():
    m0.value(0); m1.value(0)
    time.sleep_ms(20)
    _wait_aux_mode_change()

def set_lora_ch(ch):
    target_ch = int(ch)
    m0.value(1); m1.value(1)
    wait_aux(); time.sleep_ms(20)
    while uart.any(): uart.read() 
    for attempt in range(3):
        cmd = bytearray([0xC2, 0x00, 0x06, 0x00, 0x00, 0x62, 0x00, target_ch, 0x80])
        while uart.any(): uart.read()
        uart.write(cmd); time.sleep(0.6)
        while uart.any(): uart.read()
        uart.write(bytearray([0xC1, 0x00, 0x06])); time.sleep(0.3)
        resp = uart.read()
        if resp and len(resp) >= 8 and resp[7] == target_ch: break
        time.sleep(0.2)
    set_mode_wor_rx()
    while uart.any(): uart.read()

def initialize_e220_startup():
    m0.value(1); m1.value(1); time.sleep(0.5)
    cmd = bytearray([0xC0, 0x00, 0x06, 0x00, 0x00, 0x62, 0x00, LISTEN_CH, 0x80])
    while uart.any(): uart.read()
    uart.write(cmd); time.sleep(0.8)
    set_mode_wor_rx()

def save_field_cache():
    try:
        with open(FIELD_CACHE_FILE, "w") as f:
            json.dump(FIELD_CACHE, f)
    except: pass

def load_field_cache():
    global FIELD_CACHE
    try:
        with open(FIELD_CACHE_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, dict):
                FIELD_CACHE = data
    except: pass

def load_config():
    global LISTEN_CH, AUTH_KEY, MY_GW_ID, GW_OFFSET
    try:
        with open(CONFIG_FILE, "r") as f:
            c = json.load(f)
            LISTEN_CH = int(c.get('ch', 10))
            if 'auth' in c: AUTH_KEY = c['auth']
            if 'gw_id' in c: MY_GW_ID = c['gw_id']
            if 'gw_offset' in c: GW_OFFSET = int(c['gw_offset'])
    except: pass

def load_wifi_conf():
    global curr_ssid, curr_pass
    try:
        with open(WIFI_FILE, "r") as f:
            d = json.load(f); curr_ssid = d['ssid']; curr_pass = d['pass']
    except: pass

def save_lora_cmd_queue():
    try:
        with open(LORA_CMD_FILE, "w") as f:
            json.dump(LORA_CMD_QUEUE, f)
    except: pass

def load_lora_cmd_queue():
    global LORA_CMD_QUEUE
    try:
        with open(LORA_CMD_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, list) and len(data) > 0:
                LORA_CMD_QUEUE = data
    except: pass

def setup_wifi(ntp=False):
    if wlan.isconnected():
        if ntp: sync_ntp()
        return True
    router_pwr.value(1) 
    try:
        wlan.connect(curr_ssid, curr_pass)
    except OSError as e:
        if "EPERM" in str(e) or (len(e.args) > 0 and e.args[0] == 1):
            log_event(104)
            time.sleep(1.0)
            machine.reset()

    for _ in range(20):
        if wlan.isconnected():
            if ntp: sync_ntp()
            return True
        time.sleep(1)
        
    try: wlan.connect(DEFAULT_SSID, DEFAULT_PASS)
    except OSError: pass
            
    for _ in range(10):
        if wlan.isconnected(): return True
        time.sleep(1)
    return False

def sync_ntp():
    try: ntptime.host = "ntp.nict.jp"; ntptime.settime()
    except: pass

def _extract_gas_data(text):
    if not text: return None
    return text.strip()

# ==========================================
# 通信関数群
# ==========================================
def sync_fields():
    global FIELD_CHANNELS, FIELD_CACHE, FIELD_OFFSETS
    if not setup_wifi(): return
    set_led(1)
    try:
        try: res = urequests.get(f"{GAS_URL}?get_id_list=1", timeout=15.0)
        except TypeError: res = urequests.get(f"{GAS_URL}?get_id_list=1")
            
        text = res.content.decode('utf-8') if hasattr(res, 'content') else res.text
        res.close()
        extracted = _extract_gas_data(text)
        if extracted:
            data = json.loads(extracted)
            if isinstance(data, list):
                cache_changed = False
                for i in data:
                    fid = i['id']
                    FIELD_CHANNELS[fid] = i['ch']
                    # ★ オフセット値もフィールドごとに保持する（ドロップ判定に使用）
                    FIELD_OFFSETS[fid] = int(i.get('offset', -1))
                    
                    # ★ 閾値キャッシュの同期
                    val_str = f"{i['dry']},{i['full']},{i['mode']}"
                    if FIELD_CACHE.get(fid) != val_str:
                        FIELD_CACHE[fid] = val_str
                        cache_changed = True
                
                if cache_changed:
                    save_field_cache()
    except Exception: pass
    set_led(0)
    
def https_post_json_to_gas(json_str):
    if not setup_wifi(): return None
    set_led(1)
    try:
        headers = {'Content-Type': 'application/json'}
        b_data = json_str.encode('utf-8')
        try: res = urequests.post(GAS_URL, data=b_data, headers=headers, timeout=30.0)
        except TypeError: res = urequests.post(GAS_URL, data=b_data, headers=headers)
            
        try: text = res.content.decode('utf-8')
        except: text = res.text
        res.close()
        extracted = _extract_gas_data(text)
        if extracted:
            try:
                json.loads(extracted)
                set_led(0)
                return extracted
            except Exception: pass
        set_led(0)
        return None
    except Exception:
        set_led(0)
        return None

# ==========================================
# ★ USB設定シリアル待受 (BOOTSEL対応版)
# ==========================================
def process_usb_serial(v_sys):
    global AUTH_KEY, LISTEN_CH, MY_GW_ID, curr_ssid, curr_pass, GW_OFFSET
    import rp2  
    
    if v_sys < 4.6: return
        
    print(f"=== USB CONFIG MODE (VSYS: {v_sys}V) ===")
    print("Press BOOTSEL button to skip and start.")
    
    poller = uselect.poll()
    poller.register(sys.stdin, uselect.POLLIN)
    buffer = ""
    while True:
        if rp2.bootsel_button() == 1:
            print("BOOTSEL pressed: Exiting USB config mode.")
            poller.unregister(sys.stdin)
            set_led(0)
            time.sleep_ms(500)
            return

        res = poller.poll(500)
        if res:
            char = sys.stdin.read(1)
            if char:
                if char == '\n' or char == '\r':
                    line = buffer.strip()
                    if "SET_GW_CONFIG" in line:
                        l = line.split(',')
                        if len(l) >= 3:
                            AUTH_KEY = l[1]; LISTEN_CH = int(l[2])
                            new_ssid = curr_ssid; new_pass = curr_pass
                            if len(l) >= 5:
                                if l[3].strip() != "NO_CHANGE": new_ssid = l[3].strip()
                                if l[4].strip() != "NO_CHANGE": new_pass = l[4].strip()
                                try:
                                    with open(WIFI_FILE, "w") as f: json.dump({"ssid": new_ssid, "pass": new_pass}, f)
                                except: pass
                            if len(l) >= 6 and l[5] not in ("NO_CHANGE", ""):
                                MY_GW_ID = l[5].strip()
                            
                            # TDMAオフセット取得 (l[11])
                            if len(l) >= 12 and l[11].strip() not in ("NO_CHANGE", ""):
                                try: GW_OFFSET = int(l[11].strip())
                                except: pass

                            try:
                                with open(CONFIG_FILE, "w") as f: 
                                    json.dump({"ch": LISTEN_CH, "auth": AUTH_KEY, "gw_id": MY_GW_ID, "gw_offset": GW_OFFSET}, f)
                            except: pass

                            # 設定変更された時のみ、E220のEEPROM(不揮発メモリ)に永続保存
                            try:
                                # E220をConfigモード(M0=1, M1=1)へ移行
                                m0.value(1); m1.value(1); time.sleep_ms(20)
                                
                                # 0xC2(RAM)ではなく、0xC0(EEPROM)を使用して永続書き込みコマンドを生成
                                _eeprom_cmd = bytearray([0xC0, 0x00, 0x06, 0x00, 0x00, 0x62, 0x00, LISTEN_CH, 0x80])
                                
                                # UARTバッファをクリアして送信
                                while uart.any(): uart.read()
                                uart.write(_eeprom_cmd)
                                time.sleep(0.6) # EEPROMへの物理書き込み完了を長めに待つ
                                while uart.any(): uart.read()
                                
                                if DEBUG_MODE: print(f"✅ Channel {LISTEN_CH} saved to E220 EEPROM.")
                            except Exception as e:
                                if DEBUG_MODE: print("EEPROM Save Error:", e)                            

                            for _ in range(10): set_led(1); time.sleep(0.05); set_led(0); time.sleep(0.05)
                            time.sleep(1); machine.reset()
                            
                    elif "START" in line:
                        poller.unregister(sys.stdin); set_led(0); return
                    buffer = ""
                else: buffer += char
        toggle_led()
        
    poller.unregister(sys.stdin)
    set_led(0)

# ==========================================
# Main Logic
# ==========================================
load_wifi_conf()
load_config()
load_lora_cmd_queue() 
load_field_cache() # ★ 起動時にキャッシュを復元

tm_check = time.localtime(time.time() + 9*3600)
is_synced = (tm_check[0] > 2023)
is_scheduled_reboot = (14 <= tm_check[4] <= 16)
is_maint_reboot = (tm_check[3] == MAINT_HOUR and (MAINT_MIN - 1 <= tm_check[4] <= MAINT_MIN + 1))

if is_synced and (is_scheduled_reboot or is_maint_reboot) and not DEBUG_MODE:
    pass
else:
    process_usb_serial(cached_vsys)

startup_led_pattern()

setup_wifi(True)
sync_fields()
# ==========================================
# ★ E220 EEPROM寿命保護付きの初期化
# ==========================================
skip_e220_init = False
try:
    os.stat("skip_e220.flag")
    skip_e220_init = True
    os.remove("skip_e220.flag") # 確認したらすぐ消す
except:
    pass

if not skip_e220_init:
    if DEBUG_MODE: print("Cold Boot: Saving channel to E220 EEPROM.")
    initialize_e220_startup()
else:
    if DEBUG_MODE: print("Warm Boot: Skipping EEPROM write.")
    set_mode_wor_rx() # スキップした場合も確実に受信モードへ移行させる

gw_boot_status = {
    "id": MY_GW_ID, "volt": "0.0", "level": "0",
    "p_v": f"{cached_vsys:.2f}", "g_status": "KEEP", "gate": "KEEP",
    "status": "SUCCESS", "gate_12v": f"{get_12v_avg():.2f}",
    "gate_v": "0.00", "act_st": "NORMAL", "rssi": "0", "s_err": "NORMAL",
    "e_log": "101|102" if wlan.isconnected() else "101"
}
CHUNK_QUEUE.append([gw_boot_status])

last_sync_min = -1
last_charge_check = time.ticks_ms()
http_fail_count = 0
last_post_minute = -1  

manual_cmd_deadline = None
if len(LORA_CMD_QUEUE) > 0:
    manual_cmd_deadline = time.ticks_add(time.ticks_ms(), 35 * 60 * 1000)

last_manual_send_time = 0           
MANUAL_COOLDOWN_MS = 20000          

set_mode_wor_rx()

while True:
    if time.ticks_diff(time.ticks_ms(), last_charge_check) > 5000:
        manage_charging()
        last_charge_check = time.ticks_ms()

    raw = None
    tm = time.localtime(time.time() + 9*3600)

    # ★ タイムアウト強制送信ルート (ルートC)
    if manual_cmd_deadline is not None and len(LORA_CMD_QUEUE) > 0:
        if time.ticks_diff(time.ticks_ms(), manual_cmd_deadline) >= 0:
            if time.ticks_diff(time.ticks_ms(), last_manual_send_time) > MANUAL_COOLDOWN_MS:
                log_event(206)
                _cmd_data = LORA_CMD_QUEUE.pop(0)
                _target_id = _cmd_data.get("id")
                _action    = _cmd_data.get("cmd")
                
                if _target_id and _action:
                    _tg_ch = FIELD_CHANNELS.get(_target_id, LISTEN_CH)
                    if _tg_ch != LISTEN_CH: set_lora_ch(_tg_ch)
                    set_mode_wor_tx()
                    
                    send_lora("WAKEUP\n")
                    time.sleep_ms(1500)
                    set_mode_normal()
                    time.sleep_ms(1500)
                    send_lora(f"MANUAL,{AUTH_KEY},{_target_id},{_action}\n")
                    
                    log_event(f"202:{_target_id}" if _action == "OPEN" else f"205:{_target_id}")
                    if _tg_ch != LISTEN_CH: set_lora_ch(LISTEN_CH)
                    save_lora_cmd_queue()
                
                last_manual_send_time = time.ticks_ms()
                
                if len(LORA_CMD_QUEUE) > 0:
                    manual_cmd_deadline = time.ticks_add(time.ticks_ms(), MANUAL_COOLDOWN_MS)
                else:
                    manual_cmd_deadline = None
    
    if tm[3] == MAINT_HOUR and tm[4] == MAINT_MIN:
        if time.ticks_ms() > 60000:
            try:
                with open("skip_e220.flag", "w") as f: f.write("1")
            except: pass            
            router_pwr.value(0)
            time.sleep(2)
            machine.reset()
    
    # ----------------------------------------------------
    # LoRaの受信処理 (最優先: 0秒〜24秒)
    # ----------------------------------------------------
    if aux.value() == 0 or uart.any():
        st_aux = time.ticks_ms()
        while aux.value() == 0:
            if time.ticks_diff(time.ticks_ms(), st_aux) > 5000: break
            time.sleep_ms(10)
        time.sleep_ms(20)
        
        if uart.any():
            try:
                _gw_buf = uart.read()
                if _gw_buf:
                    _deadline = time.ticks_add(time.ticks_ms(), 200)
                    while b'\n' not in _gw_buf:
                        if time.ticks_diff(time.ticks_ms(), _deadline) >= 0: break
                        if uart.any(): _gw_buf += uart.read()
                        else: time.sleep_ms(5)
                    raw = _gw_buf
                    if not raw or (b'DATA,' not in raw and b'MANUAL_RES,' not in raw):
                        raw = None
            except: pass

    if raw:
        try:
            if len(raw) > 0:
                rssi = -(256 - raw[-2]) if raw[-1] == 0x0A and len(raw) > 1 else -(256 - raw[-1])
            
            valid_data = clean_uart_data(raw)
            d = valid_data.split(',')

            if len(d) >= 2 and d[1] == AUTH_KEY:
                if d[0] == "DATA" and len(d) >= 12:
                    field_id = d[2]
                    act_st = d[10] if len(d) >= 11 else "UNKNOWN"
                    sys_st = d[11] if len(d) >= 12 else "NORMAL"
                    gate_v = d[12] if len(d) >= 13 else "0.00"
                    
                    data_dict = {
                        "id": field_id, "volt": d[3], "level": d[4], "p_v": d[5],
                        "g_status": d[6], "gate": d[7], "status": d[8],
                        "gate_12v": d[9], "gate_v": gate_v, "act_st": act_st,
                        "rssi": str(rssi), "s_err": sys_st
                    }

                    urgent = (d[8] not in ("SUCCESS", "NO_ACTION")) or \
                             (sys_st not in ("NORMAL", "HALT")) or \
                             (act_st not in ("NORMAL", "UNKNOWN")) or \
                             (d[8] == "SUCCESS" and d[7] in ("OPEN", "CLOSE"))

                    if urgent: 
                        if field_id in BATCH_CACHE:
                            CHUNK_QUEUE.append([BATCH_CACHE.pop(field_id)])
                        CHUNK_QUEUE.append([data_dict])
                    else:      
                        BATCH_CACHE[field_id] = data_dict
                        
                    g_res = FIELD_CACHE.get(field_id, "2.0,6.0,RUN")
                    tg_ch = FIELD_CHANNELS.get(field_id, LISTEN_CH)

                    # ★ TIMEパケット送信前の3条件ドロップ判定
                    #   以下のいずれか1つでも該当した場合はTIMEを返さない。
                    #   未登録の水位ノードにGWのデフォルト値を書き込んでしまうバグを防ぐ。
                    _pkt_offset = int(d[13]) if len(d) >= 14 else -1
                    _reg_offset = FIELD_OFFSETS.get(field_id, -1)
                    _drop_reason = None
                    if field_id not in FIELD_CACHE:
                        _drop_reason = f"未登録:{field_id}"
                    elif tg_ch != LISTEN_CH:
                        _drop_reason = f"CH不一致:{field_id}(登録:{tg_ch}/受信:{LISTEN_CH})"
                    elif _reg_offset >= 0 and _pkt_offset >= 0 and _pkt_offset != _reg_offset:
                        _drop_reason = f"OFS不一致:{field_id}(登録:{_reg_offset}/受信:{_pkt_offset})"

                    if _drop_reason:
                        if DEBUG_MODE: print(f"DROP TIME: {_drop_reason}")
                        set_mode_wor_rx()
                        continue

                    if tg_ch != LISTEN_CH: set_lora_ch(tg_ch)

                    _g_res_parts = g_res.split(',')
                    _time_has_stop = (len(_g_res_parts) >= 3 and _g_res_parts[2].strip() == "STOP")

                    time.sleep_ms(1200)
                    set_mode_normal()
                    resp = f"          TIME,{AUTH_KEY},{field_id},{tm[3]},{tm[4]},{g_res},{tm[0]},{tm[1]},{tm[2]}\n"
                    send_lora(resp)
                    log_event(f"208:{field_id}" if _time_has_stop else f"207:{field_id}")
                    
                    if tg_ch != LISTEN_CH: set_lora_ch(LISTEN_CH)
                    else: set_mode_wor_rx()

                    # ルートA: TIMEパケット送信後のMANUAL発火
                    if len(LORA_CMD_QUEUE) > 0:
                        _target_id = LORA_CMD_QUEUE[0].get("id")
                        if field_id == _target_id:
                            _cmd_data = LORA_CMD_QUEUE.pop(0)
                            _action   = _cmd_data.get("cmd")
                            if _action:
                                _tg_ch = FIELD_CHANNELS.get(_target_id, LISTEN_CH)
                                if _tg_ch != LISTEN_CH: set_lora_ch(_tg_ch)
                                if _time_has_stop: time.sleep_ms(10000)
                                else: time.sleep_ms(500)
                                set_mode_wor_tx()
                                send_lora("WAKEUP\n")
                                time.sleep_ms(1500); set_mode_normal(); time.sleep_ms(1500)
                                send_lora(f"MANUAL,{AUTH_KEY},{_target_id},{_action}\n")
                                log_event(f"202:{_target_id}" if _action == "OPEN" else f"205:{_target_id}")
                                if _tg_ch != LISTEN_CH: set_lora_ch(LISTEN_CH)
                                save_lora_cmd_queue()
                                last_manual_send_time = time.ticks_ms()
                                if len(LORA_CMD_QUEUE) == 0: manual_cmd_deadline = None
                                    
                elif d[0] == "MANUAL_RES" and len(d) >= 8:
                    field_id = d[2]
                    data_dict = {
                        "id": field_id, "volt": "", "level": "", "p_v": d[4],
                        "g_status": d[8], "gate": "MANUAL", "status": d[3],
                        "gate_12v": d[5], "gate_v": d[7], "act_st": d[6],
                        "rssi": str(rssi), "s_err": "NORMAL"
                    }
                    if field_id in BATCH_CACHE: CHUNK_QUEUE.append([BATCH_CACHE.pop(field_id)])
                    CHUNK_QUEUE.append([data_dict])
                    set_mode_wor_rx()
        except:
            set_lora_ch(LISTEN_CH); set_mode_wor_rx()
    
    # ----------------------------------------------------
    # バッチ処理のスケジュール登録 (TDMA対応: 0秒〜20秒)
    # ----------------------------------------------------
    _sync_min_a = (15 + GW_OFFSET) % 60
    _sync_min_b = (45 + GW_OFFSET) % 60
    
    if DEBUG_MODE: should_sync = (tm[4] % 5 == 0 and tm[5] < 20 and tm[4] != last_sync_min)
    else:          should_sync = ((tm[4] == _sync_min_a or tm[4] == _sync_min_b) and tm[5] < 20 and tm[4] != last_sync_min)
        
    if should_sync:
        PENDING_SYNC = True
        e_log_str = "|".join(EVENT_LOGS)
        gw_dict = {
            "id": MY_GW_ID, "volt": "0.0", "level": "0",
            "p_v": f"{cached_vsys:.2f}", "g_status": "KEEP", "gate": "KEEP",
            "status": "SUCCESS", "gate_12v": f"{get_12v_avg():.2f}",
            "gate_v": "0.00", "act_st": "NORMAL", "rssi": "0", "s_err": "NORMAL",
            "e_log": e_log_str
        }
        EVENT_LOGS.clear()
        batch_list = [gw_dict]
        for fid, d in BATCH_CACHE.items(): batch_list.append(d)
        chunk_size = 20
        for i in range(0, len(batch_list), chunk_size): CHUNK_QUEUE.append(batch_list[i:i+chunk_size])
        BATCH_CACHE.clear(); last_sync_min = tm[4]

    # ----------------------------------------------------
    # スマート・スケジューラ 1 (Wi-Fi送信：25秒〜30秒)
    # ----------------------------------------------------
    if 25 <= tm[5] <= 30 and tm[4] != last_post_minute:
        if aux.value() == 1 and not uart.any(): 
            last_post_minute = tm[4] 
            if PENDING_SYNC:
                sync_fields(); PENDING_SYNC = False; time.sleep_ms(1000)
            elif len(CHUNK_QUEUE) > 0:
                chunk = CHUNK_QUEUE[0]
                res = https_post_json_to_gas(json.dumps(chunk))
                if res is not None:
                    http_fail_count = 0; CHUNK_QUEUE.pop(0) 
                    try:
                        parsed = json.loads(res)
                        if "CONFIG" in parsed:
                            # ★ 寿命対策: 差分がある場合のみフラッシュ保存
                            cache_changed = False
                            for k, v in parsed["CONFIG"].items():
                                if FIELD_CACHE.get(k) != v:
                                    FIELD_CACHE[k] = v
                                    cache_changed = True
                            if cache_changed:
                                save_field_cache()
                        if "COMMANDS" in parsed and isinstance(parsed["COMMANDS"], list):
                            for cmd_obj in parsed["COMMANDS"]: LORA_CMD_QUEUE.append(cmd_obj)
                            if len(parsed["COMMANDS"]) > 0:
                                save_lora_cmd_queue()
                                manual_cmd_deadline = time.ticks_add(time.ticks_ms(), 35 * 60 * 1000)
                    except: pass
                else:
                    http_fail_count += 1
                    if http_fail_count >= 3:
                        log_event(105); try: wlan.disconnect() except: pass
                        time.sleep(2); setup_wifi(); http_fail_count = 0 
                time.sleep_ms(1000)

    # ----------------------------------------------------
    # スマート・スケジューラ 2 (LoRa送信：35秒〜40秒)
    # ----------------------------------------------------
    if 35 <= tm[5] <= 40 and len(LORA_CMD_QUEUE) > 0 and manual_cmd_deadline is None:
        if time.ticks_diff(time.ticks_ms(), last_manual_send_time) > MANUAL_COOLDOWN_MS:
            if aux.value() == 1 and not uart.any():
                cmd_data = LORA_CMD_QUEUE.pop(0)
                _target_id = cmd_data.get("id"); _action = cmd_data.get("cmd")
                if _target_id and _action:
                    _tg_ch = FIELD_CHANNELS.get(_target_id, LISTEN_CH)
                    if _tg_ch != LISTEN_CH: set_lora_ch(_tg_ch)
                    set_mode_wor_tx(); send_lora("WAKEUP\n")
                    time.sleep_ms(1500); set_mode_normal(); time.sleep_ms(1500)
                    send_lora(f"MANUAL,{AUTH_KEY},{_target_id},{_action}\n")
                    log_event(f"202:{_target_id}" if _action == "OPEN" else f"205:{_target_id}") 
                    if _tg_ch != LISTEN_CH: set_lora_ch(LISTEN_CH)
                    save_lora_cmd_queue(); last_manual_send_time = time.ticks_ms()
                    if len(LORA_CMD_QUEUE) == 0: manual_cmd_deadline = None

    time.sleep_ms(50); gc.collect()