'''
Copyright (C) 2026 AgriSynapse Project

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
'''

import machine, time, gc, json, sys, uselect, os

# ==========================================
# 無印Picoを48MHzで駆動
# ==========================================
machine.freq(48_000_000)

# ==========================================
# システム設定
# ==========================================
DEBUG_MODE   = False  #本番運用時はFalseにしてください
AUTH_KEY     = "ここにシステム共通パスコードを半角英数字で入力してください"
#ここに、CloudflareWorkersにデプロイした「管理用GASの302リダイレクト追従用プロキシサーバーJavaScript」のURLを入力してください↓
GAS_HOST     = "https://gas-proxy.XXXXXXXXXX.workers.dev/"
GAS_PATH     = "/"
USB_VOLT     = 4.7 #USB接続判定のため、DCDCコンバーターMP1584ENモジュールの出力電圧は4.0V程度に設定し、この値（4.7V）を超えないようにしてください

#デフォルトのAPN設定は１NCE IoT SIM用です
APN          = "iot.1nce.net"   
APN_USER     = ""
APN_PASS     = ""
AUTH_TYPE    = 0
IP_TYPE      = "IP"
BANDS        = "1,8,18,19,26"

MAINT_HOUR, MAINT_MIN = 0, 15
CONFIG_FILE  = "gw_config.json"
FIELD_CACHE_FILE = "field_cache.json"  # ★ キャッシュ保存用ファイル

# ==========================================
# ピン定義
# ==========================================
m0 = machine.Pin(4, machine.Pin.OUT)
m1 = machine.Pin(3, machine.Pin.OUT)

aux = machine.Pin(2, machine.Pin.IN, machine.Pin.PULL_UP)

uart = machine.UART(0, baudrate=9600, tx=machine.Pin(0), rx=machine.Pin(1), rxbuf=4096)
sim_uart = machine.UART(1, baudrate=115200, tx=machine.Pin(8), rx=machine.Pin(9), rxbuf=4096)
lte_pwr = machine.Pin(18, machine.Pin.OUT)  

led = machine.Pin(25, machine.Pin.OUT)
adc_12v = machine.ADC(26)   
vsys_adc = machine.ADC(29)   
charge_ctrl = machine.Pin(28, machine.Pin.OUT) 
CHARGE_STOP_VOLT  = 13.8
CHARGE_START_VOLT = 12.8

LISTEN_CH = 0
MY_GW_ID = "GW_MAIN"
GW_OFFSET = 0  
FIELD_CHANNELS = {}
FIELD_CACHE = {}
FIELD_OFFSETS = {}  # ★ フィールドごとのオフセット値（GASから取得）
BATCH_CACHE = {}
CHUNK_QUEUE = []
LORA_CMD_QUEUE = []
LORA_CMD_FILE = "lora_cmd_queue.json"  

EVENT_LOGS = []
MAX_EVENTS = 20

m0.value(0); m1.value(0)
charge_ctrl.value(0)
lte_pwr.value(0)     

# ==========================================
# ユーティリティ・イベントロガー
# ==========================================
def log_event(code_num):
    if DEBUG_MODE: print(f"EVENT: {code_num}")
    try:
        _t = time.localtime()
        _ts = f"@{_t[3]:02d}:{_t[4]:02d}"
    except:
        _ts = ""
    EVENT_LOGS.append(str(code_num) + _ts)
    if len(EVENT_LOGS) > MAX_EVENTS:
        EVENT_LOGS.pop(0)

def get_vsys():
    led.value(1); time.sleep_ms(5)
    v = round((vsys_adc.read_u16() * 3.3 / 65535) * 3, 2)
    led.value(0); return v

def read_12v():    return (adc_12v.read_u16() * 3.3 / 65535) * 4.9
def get_12v_avg(): return sum([read_12v() for _ in range(10)]) / 10

def manage_charging():
    v = get_12v_avg()
    if v > CHARGE_STOP_VOLT:    charge_ctrl.value(0)
    elif v < CHARGE_START_VOLT: charge_ctrl.value(1)
    return v

def startup_led_pattern():
    loops = 3 if DEBUG_MODE else 10
    for _ in range(loops):
        led.value(1); time.sleep_ms(100)
        led.value(0); time.sleep_ms(100)
        led.value(1); time.sleep_ms(100)
        led.value(0); time.sleep_ms(700)

# ==========================================
# LoRa (E220) 制御ロジック
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

def send_lora(msg):
    uart.write(msg)
    time.sleep_ms(len(msg) + 50)
    wait_aux()

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
    except: return ""

def set_lora_ch(ch):
    target_ch = int(ch)
    m0.value(1); m1.value(1); time.sleep_ms(20); wait_aux()
    while uart.any(): uart.read()
    
    for attempt in range(3):
        cmd = bytearray([0xC2, 0x00, 0x06, 0x00, 0x00, 0x62, 0x00, target_ch, 0x80])
        while uart.any(): uart.read()
        uart.write(cmd); wait_aux()
        while uart.any(): uart.read()
        uart.write(bytearray([0xC1, 0x00, 0x06])); wait_aux()
        resp = uart.read()
        if resp and len(resp) >= 8 and resp[7] == target_ch: break
        time.sleep(0.2)
        
    set_mode_wor_rx()

def initialize_e220_startup():
    m0.value(1); m1.value(1); time.sleep_ms(20); wait_aux()
    cmd = bytearray([0xC0, 0x00, 0x06, 0x00, 0x00, 0x62, 0x00, LISTEN_CH, 0x80])
    while uart.any(): uart.read()
    uart.write(cmd); wait_aux()
    set_mode_wor_rx()

# ==========================================
# LTE (SIM7080G) 制御ロジック
# ==========================================
def _sim_flush():
    time.sleep_ms(30)
    while sim_uart.any(): sim_uart.read()

def _sim_write(cmd): sim_uart.write((cmd + "\r\n").encode())

def _sim_read_until(expect, timeout_ms=5000):
    buf = ""
    st = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), st) < timeout_ms:
        if sim_uart.any():
            try: buf += sim_uart.read().decode("utf-8", "ignore")
            except: pass
            if expect in buf: break
        time.sleep_ms(10)
    return buf

def sim_cmd(cmd, expect="OK", timeout_ms=5000):
    _sim_flush(); _sim_write(cmd)
    resp = _sim_read_until(expect, timeout_ms)
    return (expect in resp), resp

def sim_power_on():
    lte_pwr.value(0); time.sleep_ms(1000)    
    lte_pwr.value(1); time.sleep_ms(3000) 
    _sim_flush() 

def lte_power_off():
    sim_cmd("AT+CPOWD=1", timeout_ms=3000) 
    time.sleep_ms(1000)
    lte_pwr.value(0)
    _sim_flush()

def sim_wait_ready(timeout_s=20):
    st = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), st) < timeout_s * 1000:
        sim_uart.write(b"AT\r\n"); time.sleep_ms(100); _sim_flush() 
        ok, _ = sim_cmd("AT", "OK", 1000)
        if ok: return True
        time.sleep_ms(500)
    return False

def sim_wakeup():
    for _ in range(5):
        sim_uart.write(b"AT\r\n"); time.sleep_ms(100); _sim_flush()
        ok, _ = sim_cmd("AT", "OK", 1000)
        if ok: return True
        time.sleep_ms(300)
    return False

def _lte_wait_registered(timeout_s=180):
    st = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), st) < timeout_s * 1000:
        ok, resp = sim_cmd("AT+CEREG?", "+CEREG:", 3000)
        if ok and (",1," in resp or ",1\r" in resp or ",1\n" in resp or ",5," in resp or ",5\r" in resp or ",5\n" in resp):
            return True
        time.sleep_ms(3000)
    return False

def lte_init():
    sim_cmd("ATE0"); sim_cmd("AT+CMEE=2"); sim_cmd("AT+CMNB=1")
    sim_cmd("AT+IPR=115200"); sim_cmd("AT+CSCLK=0")
    sim_cmd('AT+CEDRXS=1,4,"0101"')
    sim_cmd(f'AT+CBANDCFG="CAT-M",{BANDS}') 
    sim_cmd(f'AT+CGDCONT=1,"{IP_TYPE}","{APN}"')
    sim_cmd("AT+CFUN=1")
    time.sleep(2)
    ok, _ = sim_cmd("AT+CPIN?", "READY", 3000)
    if not ok: return False
        
    sim_cmd("AT+COPS=0") 
    if not _lte_wait_registered(180): return False
    
    sim_cmd(f'AT+CNCFG=0,1,"{APN}","{APN_USER}","{APN_PASS}",{AUTH_TYPE}')
    ok, _ = sim_cmd("AT+CNACT=0,1", "+APP PDP: 0,ACTIVE", 20000)
    if not ok:
        sim_cmd("AT+CNACT=0,1", "OK", 10000); time.sleep_ms(3000)
        
    ok_chk, _ = sim_cmd("AT+CNACT?", "+CNACT: 0,1", 5000) 
    return ok_chk

def ensure_lte_connected():
    if not sim_wakeup():
        sim_power_on()
        if not sim_wait_ready(15): return False
        return lte_init()
    ok, _ = sim_cmd("AT+CNACT?", "+CNACT: 0,1", 2000)
    if ok: return True
    return lte_init()

def lte_sync_time():
    ok, resp = sim_cmd("AT+CCLK?", "+CCLK:", 5000)
    if not ok: return False
    try:
        idx = resp.find('"')
        clk_str = resp[idx+1:resp.find('"', idx + 1)]
        date_part, time_tz = clk_str.split(',')
        yy, mm, dd = date_part.split('/')
        hh, mi, ss = time_tz[:8].split(':')
        machine.RTC().datetime((2000+int(yy), int(mm), int(dd), 0, int(hh), int(mi), int(ss), 0))
        return True
    except: return False

def _https_connect(bodylen, headerlen=350):
    for attempt in range(3):
        sim_cmd("AT+SHDISC", "OK", 2000); time.sleep_ms(500)
        sim_cmd('AT+CSSLCFG="sslversion",1,3')
        domain = GAS_HOST.replace("https://", "").replace("http://", "").strip("/")
        sim_cmd(f'AT+CSSLCFG="sni",1,"{domain}"')
        sim_cmd('AT+CSSLCFG="ignorertctime",1,1')
        sim_cmd('AT+SHSSL=1,""')
        
        host = GAS_HOST if not GAS_HOST.endswith('/') else GAS_HOST[:-1]
        sim_cmd(f'AT+SHCONF="URL","{host}"')
        sim_cmd(f'AT+SHCONF="BODYLEN",{bodylen}')
        sim_cmd(f'AT+SHCONF="HEADERLEN",{headerlen}')
        time.sleep_ms(1000)
        
        ok, _ = sim_cmd("AT+SHCONN", "OK", 15000)
        if ok: return True
            
        log_event(103)  
        time.sleep_ms(1000)
        
        sim_cmd("AT+CNACT=0,0", "OK", 5000)
        time.sleep_ms(2000)
        sim_cmd("AT+CNACT=0,1", "ACTIVE", 15000)
        time.sleep_ms(2000)
        
    return False

def _extract_gas_data(text):
    if not text: return None
    return text.strip()

def _https_read_response(resp_str):
    try:
        line = [l for l in resp_str.split('\n') if '+SHREQ:' in l][0]
        parts = line.split(',')
        status_code = int(parts[1].strip())
        resp_size   = int(parts[2].strip())
        
        if status_code != 200 or resp_size == 0: return status_code, None
        time.sleep_ms(500)  
        
        body = ""
        offset = 0
        while offset < resp_size:
            read_size = min(resp_size - offset, 500) 
            ok2, read_resp = sim_cmd(f"AT+SHREAD={offset},{read_size}", "\r\nOK\r\n", 20000)
            if not ok2: break
            
            shread_idx = read_resp.find("+SHREAD:")
            if shread_idx != -1:
                data_start = read_resp.find('\n', shread_idx) + 1
                chunk = read_resp[data_start:]
                
                ok_idx = chunk.rfind("\r\nOK\r\n")
                if ok_idx != -1: chunk = chunk[:ok_idx]
                else:
                    ok_idx = chunk.rfind("\nOK\n")
                    if ok_idx != -1: chunk = chunk[:ok_idx]
                body += chunk
            
            offset += read_size
            time.sleep_ms(100) 
        return status_code, body
    except Exception: return 0, None
    
def https_post_json_to_gas(json_str):
    if not ensure_lte_connected(): return None
    exact_body_len = len(json_str.encode('utf-8'))

    sim_cmd('AT+CEDRXS=0')
    time.sleep_ms(300)

    if not _https_connect(exact_body_len, 350):
        sim_cmd('AT+CEDRXS=1,4,"0101"')
        return None

    def _send_req(path):
        sim_cmd("AT+SHCHEAD")
        sim_cmd('AT+SHAHEAD="Content-Type","application/json"')
        sim_cmd('AT+SHAHEAD="Connection","close"')
        
        ok, _ = sim_cmd(f'AT+SHBOD={exact_body_len},10000', ">", 5000)
        if ok:
            b_data = json_str.encode('utf-8')
            for i in range(0, len(b_data), 128):
                sim_uart.write(b_data[i:i+128]); time.sleep_ms(20) 
            body_resp = _sim_read_until("OK", 10000)
            if "OK" not in body_resp: return False, ""
        else: return False, ""
        
        ok, req_resp = sim_cmd(f'AT+SHREQ="{path}",3', "+SHREQ:", 40000)
        return ok, req_resp

    ok, resp = _send_req(GAS_PATH)
    if not ok:
        sim_cmd("AT+SHDISC")
        sim_cmd('AT+CEDRXS=1,4,"0101"')
        return None

    status, body = _https_read_response(resp)
    sim_cmd("AT+SHDISC")

    sim_cmd('AT+CEDRXS=1,4,"0101"')

    if status == 200: return _extract_gas_data(body)
    return None

def https_get_from_gas(query):
    if not ensure_lte_connected(): return None
    if not _https_connect(10, 350): return None

    sim_cmd("AT+SHCHEAD")
    sim_cmd('AT+SHAHEAD="Connection","close"')

    query_str = "&".join([f"{k}={v}" for k, v in query.items()])
    path = f"{GAS_PATH}?{query_str}"

    ok, resp = sim_cmd(f'AT+SHREQ="{path}",1', "+SHREQ:", 25000)
    if not ok: sim_cmd("AT+SHDISC"); return None

    status, body = _https_read_response(resp)
    sim_cmd("AT+SHDISC") 

    if status == 200: return _extract_gas_data(body)
    return None

def sync_fields():
    global FIELD_CHANNELS, FIELD_CACHE, FIELD_OFFSETS
    result = https_get_from_gas({"get_id_list": "1"})
    if result:
        try:
            data = json.loads(result)
            if isinstance(data, list):
                cache_changed = False
                for i in data:
                    fid = i['id']
                    # チャンネルマッピングの更新
                    FIELD_CHANNELS[fid] = i['ch']
                    # ★ オフセット値もフィールドごとに保持する（ドロップ判定に使用）
                    FIELD_OFFSETS[fid] = int(i.get('offset', -1))
                    
                    # ★ 重要: FIELD_CACHE (閾値, モード) の先行構築
                    val_str = f"{i['dry']},{i['full']},{i['mode']}"
                    if FIELD_CACHE.get(fid) != val_str:
                        FIELD_CACHE[fid] = val_str
                        cache_changed = True
                
                # 差分があれば不揮発メモリに保存（メモリ寿命保護）
                if cache_changed:
                    save_field_cache()
                    if DEBUG_MODE: print("FIELD_CACHE fully synced from GAS.")
        except Exception as e:
            if DEBUG_MODE: print("Sync fields error:", e)
            
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

# ==========================================
# ★ フラッシュ寿命保護対応: FIELD_CACHE の保存・読み込み
# ==========================================
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
    global LISTEN_CH, AUTH_KEY, MY_GW_ID, APN, APN_USER, APN_PASS, AUTH_TYPE, IP_TYPE, BANDS, GW_OFFSET
    try:
        with open(CONFIG_FILE, "r") as f:
            c = json.load(f)
            LISTEN_CH = int(c.get('ch', 0))
            if 'auth'      in c: AUTH_KEY  = c['auth']
            if 'gw_id'     in c: MY_GW_ID  = c['gw_id']
            if 'apn'       in c: APN       = c['apn']
            if 'apn_user'  in c: APN_USER  = c['apn_user']
            if 'apn_pass'  in c: APN_PASS  = c['apn_pass']
            if 'auth_type' in c: AUTH_TYPE = c['auth_type']
            if 'ip_type'   in c: IP_TYPE   = c['ip_type']
            if 'bands'     in c: BANDS     = c['bands']
            if 'gw_offset' in c: GW_OFFSET = int(c['gw_offset'])
    except: pass

# ==========================================
# USBシリアル設定モード
# ==========================================
def process_usb_serial():
    global AUTH_KEY, LISTEN_CH, MY_GW_ID, APN, APN_USER, APN_PASS, AUTH_TYPE, IP_TYPE, BANDS, GW_OFFSET
    import rp2  
    
    v_sys = get_vsys()
    if v_sys < USB_VOLT: return
        
    print(f"=== USB CONFIG MODE (VSYS: {v_sys}V) ===")
    print("Press BOOTSEL button to skip and start.")
    
    poller = uselect.poll()
    poller.register(sys.stdin, uselect.POLLIN)
    buffer = ""
    while True:
        if rp2.bootsel_button() == 1:
            print("BOOTSEL pressed: Exiting USB config mode.")
            poller.unregister(sys.stdin)
            led.value(0)
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
                            AUTH_KEY, LISTEN_CH = l[1], int(l[2])
                            if len(l) >= 6 and l[5] not in ("NO_CHANGE", ""): MY_GW_ID = l[5].strip()
                            if len(l) >= 11 and l[6] != "NO_CHANGE":
                                APN = l[6].strip(); APN_USER = "" if l[7] == "_EMPTY_" else l[7].strip()
                                APN_PASS = "" if l[8] == "_EMPTY_" else l[8].strip()
                                AUTH_TYPE = int(l[9].strip()); IP_TYPE = l[10].strip()
                            if len(l) >= 12 and l[11].strip() not in ("NO_CHANGE", ""):
                                try: GW_OFFSET = int(l[11].strip())
                                except: pass
                            if len(l) >= 13 and l[12] != "NO_CHANGE": BANDS = ",".join(l[12:]).strip()
                            try:
                                with open(CONFIG_FILE, "w") as f:
                                    json.dump({"ch": LISTEN_CH, "auth": AUTH_KEY, "gw_id": MY_GW_ID, "apn": APN, "apn_user": APN_USER, "apn_pass": APN_PASS, "auth_type": AUTH_TYPE, "ip_type": IP_TYPE, "bands": BANDS, "gw_offset": GW_OFFSET}, f)
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

                            for _ in range(10): led.value(1); time.sleep(0.05); led.value(0); time.sleep(0.05)
                            time.sleep(1); machine.reset()
                    elif "START" in line:
                        poller.unregister(sys.stdin); led.value(0); return
                    buffer = ""
                else: buffer += char
        led.value(not led.value())

# ==========================================
# Main
# ==========================================
load_config()
load_lora_cmd_queue() 
load_field_cache()    # ★ 起動時にキャッシュをフラッシュメモリから復元
process_usb_serial()
startup_led_pattern()

_lte_ok = ensure_lte_connected()
if _lte_ok:
    lte_sync_time()
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
    "id": MY_GW_ID, "volt": "0.0", "level": "0", "p_v": f"{get_vsys():.2f}",
    "g_status": "KEEP", "gate": "KEEP", "status": "SUCCESS", "gate_12v": f"{get_12v_avg():.2f}",
    "gate_v": "0.00", "act_st": "NORMAL", "rssi": "0", "s_err": "NORMAL",
    "e_log": "101|102" if _lte_ok else "101"
}
CHUNK_QUEUE.append([gw_boot_status])

last_sync_min   = -1
last_charge_chk = time.ticks_ms()

manual_cmd_deadline = None
if len(LORA_CMD_QUEUE) > 0:
    manual_cmd_deadline = time.ticks_add(time.ticks_ms(), 35 * 60 * 1000)
    
last_post_minute = -1
last_manual_send_time = 0           
MANUAL_COOLDOWN_MS = 20000          

set_mode_wor_rx()

while True:
    if time.ticks_diff(time.ticks_ms(), last_charge_chk) > 300000:
        manage_charging()
        last_charge_chk = time.ticks_ms()

    raw = None
    
    # ==========================================
    # E220からの直接UART受信処理
    # ==========================================
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

                    urgent = False
                    if d[8] not in ("SUCCESS", "NO_ACTION"): urgent = True
                    if sys_st not in ("NORMAL", "HALT"): urgent = True
                    if act_st not in ("NORMAL", "UNKNOWN"): urgent = True
                    if d[8] == "SUCCESS" and d[7] in ("OPEN", "CLOSE"): urgent = True

                    g_res = ""
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
                    tm_n = time.localtime()
                    resp = f"TIME,{AUTH_KEY},{field_id},{tm_n[3]},{tm_n[4]},{g_res},{tm_n[0]},{tm_n[1]},{tm_n[2]}\n"
                    send_lora(resp)
                    log_event(f"208:{field_id}" if _time_has_stop else f"207:{field_id}")
                    if tg_ch != LISTEN_CH: 
                        set_lora_ch(LISTEN_CH)
                    else:
                        set_mode_wor_rx()

                    if len(LORA_CMD_QUEUE) > 0:
                        _target_id = LORA_CMD_QUEUE[0].get("id")
                        
                        if field_id == _target_id:
                            _cmd_data = LORA_CMD_QUEUE.pop(0)
                            _action   = _cmd_data.get("cmd")
                            
                            if _action:
                                _tg_ch = FIELD_CHANNELS.get(_target_id, LISTEN_CH)
                                if _tg_ch != LISTEN_CH: set_lora_ch(_tg_ch)
                                
                                if _time_has_stop:
                                    if DEBUG_MODE: print(f"Route A [STOP+MANUAL]: waiting safe window...")
                                    time.sleep_ms(10000)
                                else:
                                    if DEBUG_MODE: print(f"Route A [RUN+MANUAL]: 500ms stabilize...")
                                    time.sleep_ms(500)
                                
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
                                
                                if len(LORA_CMD_QUEUE) == 0:
                                    manual_cmd_deadline = None
                                
                elif d[0] == "MANUAL_RES" and len(d) >= 8:
                    field_id = d[2]
                    act_st = d[6] if len(d) >= 7 else "UNKNOWN"
                    gate_v = d[7] if len(d) >= 8 else "0.00"
                    
                    data_dict = {
                        "id": field_id, "volt": "", "level": "", "p_v": d[4],
                        "g_status": d[8], "gate": "MANUAL", "status": d[3],
                        "gate_12v": d[5], "gate_v": gate_v, "act_st": act_st,
                        "rssi": str(rssi), "s_err": "NORMAL"
                    }
                    urgent = True 

                    if urgent: 
                        if field_id in BATCH_CACHE:
                            CHUNK_QUEUE.append([BATCH_CACHE.pop(field_id)])
                        CHUNK_QUEUE.append([data_dict])
                    else:      
                        BATCH_CACHE[field_id] = data_dict

                    set_mode_wor_rx()
        except Exception as e:
            set_lora_ch(LISTEN_CH)
            set_mode_wor_rx()

    # ==========================================
    # LTE バッチ送信 & コマンド処理
    # ==========================================
    tm = time.localtime()

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
                    if _tg_ch != LISTEN_CH:
                        set_lora_ch(LISTEN_CH)
                    save_lora_cmd_queue()
                
                last_manual_send_time = time.ticks_ms()
                
                if len(LORA_CMD_QUEUE) > 0:
                    manual_cmd_deadline = time.ticks_add(time.ticks_ms(), MANUAL_COOLDOWN_MS)
                else:
                    manual_cmd_deadline = None

    if tm[3] == MAINT_HOUR and tm[4] == MAINT_MIN and time.ticks_ms() > 60000:
        try:
            with open("skip_e220.flag", "w") as f: f.write("1")
        except: pass        
        machine.reset()

    _sync_min_a = (15 + GW_OFFSET) % 60
    _sync_min_b = (45 + GW_OFFSET) % 60
    if DEBUG_MODE: should_sync = (tm[4] % 5 == 0 and tm[5] < 20 and tm[4] != last_sync_min)
    else:          should_sync = ((tm[4] == _sync_min_a or tm[4] == _sync_min_b) and tm[5] < 20 and tm[4] != last_sync_min)

    if should_sync:
        e_log_str = "|".join(EVENT_LOGS)
        
        gw_dict = {
            "id": MY_GW_ID, "volt": "0.0", "level": "0", "p_v": f"{get_vsys():.2f}",
            "g_status": "KEEP", "gate": "KEEP", "status": "SUCCESS",
            "gate_12v": f"{get_12v_avg():.2f}", "gate_v": "0.00", "act_st": "NORMAL",
            "rssi": "0", "s_err": "NORMAL",
            "e_log": e_log_str
        }
        EVENT_LOGS.clear()
        
        batch_list = [gw_dict]
        for fid, d in BATCH_CACHE.items(): batch_list.append(d)

        chunk_size = 8
        for i in range(0, len(batch_list), chunk_size):
            CHUNK_QUEUE.append(batch_list[i:i+chunk_size])
            
        BATCH_CACHE.clear() 
        last_sync_min = tm[4]

    if 25 <= tm[5] <= 30 and tm[4] != last_post_minute:
        if len(CHUNK_QUEUE) > 0:
            last_post_minute = tm[4]
            chunk = CHUNK_QUEUE[0]
            res = https_post_json_to_gas(json.dumps(chunk))
            
            if res is not None:
                CHUNK_QUEUE.pop(0) 
                try:
                    parsed = json.loads(res)
                    if "CONFIG" in parsed:
                        # ★ 差分更新ロジック: 変更があった場合のみフラッシュメモリへ書き込む
                        cache_changed = False
                        for k, v in parsed["CONFIG"].items():
                            if FIELD_CACHE.get(k) != v:
                                FIELD_CACHE[k] = v
                                cache_changed = True
                                
                        if cache_changed:
                            save_field_cache()
                            if DEBUG_MODE: print("FIELD_CACHE updated and saved to flash.")
                            
                    if "COMMANDS" in parsed and isinstance(parsed["COMMANDS"], list):
                        for cmd_obj in parsed["COMMANDS"]: LORA_CMD_QUEUE.append(cmd_obj)
                        if len(parsed["COMMANDS"]) > 0:
                            save_lora_cmd_queue()  
                            manual_cmd_deadline = time.ticks_add(time.ticks_ms(), 35 * 60 * 1000)
                except Exception: pass
            time.sleep_ms(1000)
            
    if 35 <= tm[5] <= 40 and len(LORA_CMD_QUEUE) > 0 and manual_cmd_deadline is None:
        if time.ticks_diff(time.ticks_ms(), last_manual_send_time) > MANUAL_COOLDOWN_MS:
            if aux.value() == 1 and not uart.any():
                cmd_data = LORA_CMD_QUEUE.pop(0)
                target_id = cmd_data.get("id")
                action = cmd_data.get("cmd")
                
                if target_id and action:
                    tg_ch = FIELD_CHANNELS.get(target_id, LISTEN_CH)
                    if tg_ch != LISTEN_CH: set_lora_ch(tg_ch)
                        
                    set_mode_wor_tx()
                    
                    send_lora("WAKEUP\n")
                    time.sleep_ms(1500)
                    set_mode_normal()
                    time.sleep_ms(1500)
                    send_lora(f"MANUAL,{AUTH_KEY},{target_id},{action}\n")
                    
                    log_event(f"202:{target_id}" if action == "OPEN" else f"205:{target_id}") 
                    
                    if tg_ch != LISTEN_CH: 
                        set_lora_ch(LISTEN_CH)
                    
                    save_lora_cmd_queue()  
                    last_manual_send_time = time.ticks_ms()
                    
                    if len(LORA_CMD_QUEUE) == 0:
                        manual_cmd_deadline = None

    gc.collect()
    time.sleep_ms(50)