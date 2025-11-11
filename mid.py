import network
import socket
import ujson
import ntptime
import time
import uasyncio as asyncio
from machine import Pin, PWM, I2C
from ssd1306 import SSD1306_I2C
import dht
import utime

# === Wi-Fi 設定 ===

ip = ""

# === 硬體設定 ===
button = Pin(17, Pin.IN, Pin.PULL_UP)
button_next = Pin(21, Pin.IN, Pin.PULL_UP)

buzzer = PWM(Pin(6))
buzzer.duty(0)

# alarms_file = "alarms.json"
# alarms = []  # 儲存鬧鐘時間

# === OLED 初始化 ===
i2c = I2C(0, scl=Pin(7), sda=Pin(5))
oled = SSD1306_I2C(128, 64, i2c)

# === DHT11 初始化 ===
dht_pin = Pin(18)
sensor = dht.DHT11(dht_pin)

# === 全域變數 ===
temperature = None
humidity = None
last_sync = 0
current_alarm_index = 0


# === Wi-Fi 連線 ===
def connect_wifi():
    global ip
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    try:
        with open("wifi.txt", "r") as f:
            lines = f.read().splitlines()
            ssid, password = lines[0], lines[1]
    except:
        print("未找到 wifi.txt，進入設定模式")
        return None

    if wlan.isconnected():
        ip = wlan.ifconfig()[0]
        print("Wi-Fi 已連線:", ip)
        return ip

    print(f"Connecting to {ssid}...")
    try:
        wlan.connect(ssid, password)
    except OSError as e:
        print("Wi-Fi 連線失敗:", e)
        return None

    # 等待連線
    for _ in range(20):
        if wlan.isconnected():
            break
        time.sleep(0.5)
        print(".", end="")

    if wlan.isconnected():
        ip = wlan.ifconfig()[0]
        print("\nWi-Fi 已連線:", ip)
        return ip
    else:
        print("\nWi-Fi 連線超時")
        return None

# === Wi-Fi 設定模式 (AP 模式) ===
async def AP_client(reader, writer):
    request = await reader.read(1024)
    request = request.decode("utf-8")
    path = request.split(" ")[1]

    response = ""
    if path.startswith("/save?"):
        try:
            params = path.split("?")[1]
            kv = {k: v for k, v in [p.split("=") for p in params.split("&")]}
            ssid = kv.get("ssid", "")
            pwd = kv.get("pwd", "")
            with open("wifi.txt", "w") as f:
                f.write(f"{ssid}\n{pwd}")
            response = """
            <!DOCTYPE html>
            <html lang="zh-Hant">
            <head>
                <meta charset="UTF-8">
                <title>設定完成</title>
            </head>
            <body>
                <h3>已儲存，請重新啟動 ESP32！</h3>
            </body>
            </html>
            """
        except Exception as e:
            response = f"""
            <!DOCTYPE html>
            <html lang="zh-Hant">
            <head><meta charset="UTF-8"><title>錯誤</title></head>
            <body><h3>錯誤：{e}</h3></body></html>
            """
    else:
        response = """
        <!DOCTYPE html>
        <html lang="zh-Hant">
        <head>
            <meta charset="UTF-8">
            <title>設定 Wi-Fi</title>
            <style>
                body { font-family: Arial, sans-serif; text-align: center; margin-top: 40px; }
                input { margin: 6px; padding: 6px; }
            </style>
        </head>
        <body>
            <h2>設定 Wi-Fi</h2>
            <form action="/save">
              SSID: <input name="ssid"><br>
              密碼: <input name="pwd" type="password"><br><br>
              <input type="submit" value="儲存">
            </form>
        </body>
        </html>
        """

    # 關鍵修改：加入 UTF-8 的 Content-Type
    header = "HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    writer.write(header.encode("utf-8"))
    writer.write(response.encode("utf-8"))
    await writer.drain()
    await writer.aclose()


async def start_ap_server():
    wlan = network.WLAN(network.AP_IF)
    wlan.active(True)
    wlan.config(essid="ESP32_Setup", password="12345678")

    oled.fill(0)
    oled.text("SET WIFI SSID:", 0, 0)
    oled.text("ESP32_Setup", 0, 16)
    oled.text("Web IP:", 0, 32)
    oled.text("192.168.4.1", 0, 48)
    oled.show()

    server = await asyncio.start_server(AP_client, "0.0.0.0", 80)
    print("開啟設定頁 http://192.168.4.1")
    async with server:
        await server.wait_closed()

# === 時間同步 ===
def sync_time():
    try:
        ntptime.settime()
        print("時間同步完成")
    except Exception as e:
        print("同步失敗:", e)

def get_local_time():
    return time.localtime(time.time() + 8 * 3600)  # 台灣時區

# === DHT11 讀取任務 ===
async def read_dht_task():
    global temperature, humidity
    while True:
        try:
            sensor.measure()
            temperature = sensor.temperature()
            humidity = sensor.humidity()
            #print(temperature)
        except Exception as e:
            print("讀取 DHT11 失敗:", e)
        await asyncio.sleep(10)  # 每3秒更新一次
        
def get_next_alarm():
    """
    找到下一個即將響的鬧鐘時間
    回傳 (hour, minute) tuple，如果沒有鬧鐘回傳 None
    """
    now = get_local_time()
    now_minutes = now[3] * 60 + now[4]  # 現在時間轉成分鐘

    # 將鬧鐘時間也轉成分鐘
    alarm_minutes = [(a["hour"]*60 + a["minute"], a) for a in alarms]

    # 計算距離現在最近的鬧鐘（可以跨天）
    min_diff = 24*60 + 1
    next_alarm = None
    for am, a in alarm_minutes:
        diff = am - now_minutes
        if diff < 0:
            diff += 24*60  # 跨天
        if diff < min_diff:
            min_diff = diff
            next_alarm = a

    return next_alarm


# === OLED 顯示任務 ===
async def display_task():
    global current_alarm_index

    last_button_state = 1  # 記錄上一狀態避免重複觸發

    while True:
        t = get_local_time()
        year, month, mday, hour, minute, second, _, _ = t
        date_str = f"{year:04d}-{month:02d}-{mday:02d}"
        time_str = f"{hour:02d}:{minute:02d}:{second:02d}"

        oled.fill(0)
        if ip:
            oled.text(ip, 0, 0)
        else:
            oled.text("Wi-Fi...", 0, 0)
        oled.text(date_str, 0, 10)
        oled.text(time_str, 0, 20)

        # 溫濕度顯示
        if temperature is not None:
            oled.text(f"T:{temperature:2d}C", 0, 30)
        else:
            oled.text("T:--C", 0, 30)
        if humidity is not None:
            oled.text(f"H:{humidity:2d}%", 64, 30)
        else:
            oled.text("H:--%", 64, 30)

        # 檢查按鈕是否被按下（防抖）
        button_state = button_next.value()
        if last_button_state == 1 and button_state == 0:  # 偵測下降沿
            if len(alarms) > 0:
                current_alarm_index = (current_alarm_index + 1) % len(alarms)
                print(f"切換顯示到第 {current_alarm_index+1} 個鬧鐘")
        last_button_state = button_state

        # 顯示當前選擇的鬧鐘
        if len(alarms) > 0:
            a = alarms[current_alarm_index]
            oled.text(f"Alarm:{a['hour']:02d}:{a['minute']:02d}", 0, 40)
            if a.get("weekdays"):  # 週期鬧鐘
                days_str = ",".join(a["weekdays"])
                oled.text(f"Days: {days_str}", 0, 50)
            elif a.get("date"):  # 單次鬧鐘
                oled.text(f"Date: {a['date']}", 0, 50)
            else:  # 每天響
                oled.text("Every day", 0, 50)
        else:
            oled.text("No Alarms", 0, 48)

        oled.show()
        await asyncio.sleep(0.4)


# === 非同步鬧鐘聲音 ===
# === 音階與樂曲 ===
NOTE_FREQS = {
    'C4': 262, 'D4': 294, 'E4': 330, 'F4': 349, 'G4': 392,
    'A4': 440, 'B4': 494, 'C5': 523, 'Bb4':466, 'G3': 196,
    'E5': 659, 'D#5': 622, 'D5': 587, 'Ab4': 415, 'REST': 0
}

NOTES_TWINKLE = [
    ('C4', 500), ('C4', 500), ('G4', 500), ('G4', 500),
    ('A4', 500), ('A4', 500), ('G4', 1000),
    ('F4', 500), ('F4', 500), ('E4', 500), ('E4', 500),
    ('D4', 500), ('D4', 500), ('C4', 1000),
]
# === 播放音樂（非同步版本） ===
async def play_song_async(speaker, notes, stop_event):
    """非同步播放歌曲，可由 stop_event 控制停止"""
    for note, duration in notes:
        if stop_event.is_set():
            break
        freq = NOTE_FREQS.get(note, 0)
        if freq == 0:
            speaker.duty(0)
        else:
            speaker.freq(freq)
            speaker.duty(512)
        await asyncio.sleep(duration / 1000)  # 毫秒轉秒
    speaker.duty(0)


# === 非同步鬧鐘響起 ===
async def ring_buzzer(duration=10):
    print("鬧鐘響起，播放音樂！")
    start = time.time()

    # 重新初始化 speaker（確保可用）
    global buzzer
    buzzer = PWM(Pin(6))
    buzzer.duty(0)

    # 播放音樂（可改 NOTES_TWINKLE → NOTES_1, NOTES_2...）
    await play_song_async(buzzer, NOTES_TWINKLE)

    buzzer.duty(0)
    print("音樂播放結束")
    
alarms = []

def load_alarms():
    global alarms
    try:
        with open("alarms.json", "r") as f:
            alarms = ujson.load(f)
    except:
        alarms = []

def save_alarms():
    with open("alarms.json", "w") as f:
        ujson.dump(alarms, f)

# === 網頁伺服器 ===
WEEKDAYS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

async def handle_client(reader, writer):
    request = await reader.read(1024)
    request = request.decode("utf-8")
    path = request.split(" ")[1]
    response = ""

    # 新增鬧鐘
    if path.startswith("/add?"):
        try:
            params = path.split("?")[1]
            kv = {k:v for k,v in [p.split("=") for p in params.split("&")]}
            hour = int(kv.get("hour",0))
            minute = int(kv.get("minute",0))
            weekdays = [d for d in WEEKDAYS if kv.get(d,"off")=="on"]
            alarms.append({"hour":hour,"minute":minute,"weekdays":weekdays})
            save_alarms()
        except Exception as e:
            print("Add error:", e)
        response = "<meta http-equiv='refresh' content='0; url=/'/>"

    # 刪除鬧鐘
    elif path.startswith("/delete?"):
        try:
            params = path.split("?")[1]
            kv = {k:v for k,v in [p.split("=") for p in params.split("&")]}
            idx = int(kv.get("id",-1))
            if 0 <= idx < len(alarms):
                del alarms[idx]
                save_alarms()
        except Exception as e:
            print("Delete error:", e)
        response = "<meta http-equiv='refresh' content='0; url=/'/>"

    # 主頁
    else:
        response = f"""
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>ESP32 鬧鐘</title>
<style>
body {{
    font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
    background:#f2f2f7; margin:0; padding:0;
}}
.container {{
    max-width:400px; margin:20px auto; padding:0 10px;
}}
h2 {{ text-align:center; color:#1c1c1e; margin:20px 0; }}
form {{
    background:#fff; border-radius:20px; padding:20px; box-shadow:0 4px 12px rgba(0,0,0,0.1);
}}
.picker {{
    display:flex; justify-content:center; align-items:center; margin-bottom:15px;
}}
.picker select {{
    font-size:20px; padding:10px; margin:0 5px; border-radius:10px; border:1px solid #ccc;
    background:#fff; -webkit-appearance: none; appearance:none;
}}
.weekdays {{
    display: flex;
    justify-content: space-between;
    margin-bottom: 15px;
}}
.day-btn {{
    flex:1;
    text-align: center;
    margin:0 3px;
}}
.day-btn input[type=checkbox] {{
    display: none;
}}
.day-btn span {{
    display: inline-block;
    width: 40px; height: 40px;
    line-height: 40px;
    border-radius: 50%;
    background: #e5e5ea;
    color: #1c1c1e;
    font-weight: 600;
    transition: all 0.3s;
    cursor: pointer;
    user-select: none;
}}
.day-btn input[type=checkbox]:checked + span {{
    background: #0a84ff;
    color: #fff;
    box-shadow: 0 2px 6px rgba(10, 132, 255, 0.3);
}}
.day-btn span:hover {{
    background: #d1d1d6;
}}
button {{
    width:100%; background:#0a84ff; color:white; border:none; border-radius:20px; padding:12px; font-size:18px;
}}
ul {{
    list-style:none; padding:0; margin:20px 0;
}}
li {{
    background:#fff; margin:8px 0; border-radius:15px; padding:12px;
    display:flex; justify-content:space-between; align-items:center; box-shadow:0 2px 6px rgba(0,0,0,0.1);
}}
a.delete {{ color:#ff3b30; text-decoration:none; font-weight:bold; }}
</style>
</head>
<body>
<div class="container">
<h2>ESP32 鬧鐘</h2>
<form action="/add">
<div class="picker">
    <select name="hour">""" + "".join([f'<option value="{i}">{i:02d}</option>' for i in range(24)]) + """</select>
    <span>:</span>
    <select name="minute">""" + "".join([f'<option value="{i}">{i:02d}</option>' for i in range(60)]) + """</select>
</div>
<div class="weekdays">
"""
        for d in WEEKDAYS:
            response += f'''
    <label class="day-btn">
        <input type="checkbox" name="{d}">
        <span>{d}</span>
    </label>
            '''
        response += """
</div>
<button type="submit">新增鬧鐘</button>
</form>

<ul>
"""
        for i,a in enumerate(alarms):
            days = ",".join(a.get("weekdays",[])) or "每天"
            response += f'<li>{a["hour"]:02d}:{a["minute"]:02d} ({days}) <a class="delete" href="/delete?id={i}">刪除</a></li>'
        response += "</ul></div></body></html>"

    writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n")
    writer.write(response.encode("utf-8"))
    await writer.drain()
    await writer.aclose()


    

async def start_webserver():
    load_alarms()
    server = await asyncio.start_server(handle_client, "0.0.0.0", 80)
    print("WebServer 啟動中 http://<ESP32_IP>/")
    async with server:
        await server.wait_closed()
# ===== 鬧鐘檢查任務 =====
async def alarm_task(snooze_minutes=5, max_ring_time=60):
    """鬧鐘任務，支援手動停止、自動停止、延後"""
    while True:
        now = get_local_time()
        h, m = now[3], now[4]
        today_weekday = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][now[6]]
        today_date = f"{now[0]:04d}-{now[1]:02d}-{now[2]:02d}"

        for a in alarms:
            if not a.get("enabled", True):
                continue

            # === 是否今日需響 ===
            is_today = False
            if a.get("date"):  # 指定日期鬧鐘
                is_today = a["date"] == today_date
            elif a.get("weekdays"):  # 指定星期
                is_today = today_weekday in a["weekdays"]
            else:  # 無指定 → 每天
                is_today = True

            # === 鬧鐘觸發 ===
            if is_today and a["hour"] == h and a["minute"] == m:
                print(f"鬧鐘響起: {h:02d}:{m:02d}")
                
                stop_event = asyncio.Event()
                ring_task = asyncio.create_task(play_song_async(buzzer, NOTES_TWINKLE, stop_event))
                start_time = time.time()

                while True:
                    await asyncio.sleep(0.1)

                    # 手動停止（任一按鈕）
                    if button.value() == 0 or button_next.value() == 0:
                        print("鬧鐘手動停止")
                        stop_event.set()
                        break

                    # 自動停止超時
                    if time.time() - start_time > max_ring_time:
                        print("鬧鐘自動停止（超過最大時間）")
                        stop_event.set()
                        break

                await ring_task  # 等待播放結束
                buzzer.duty(0)

                # === 延後功能 ===
                if button_next.value() == 0:  # 按下「延後」
                    new_minute = m + snooze_minutes
                    new_hour = h + new_minute // 60
                    new_minute %= 60
                    new_hour %= 24
                    delayed_alarm = {
                        "hour": new_hour,
                        "minute": new_minute,
                        "weekdays": [],  # 單次響
                        "enabled": True
                    }
                    alarms.append(delayed_alarm)
                    save_alarms()
                    print(f"延後 {snooze_minutes} 分鐘 → 新鬧鐘 {new_hour:02d}:{new_minute:02d}")

                # 若是單次鬧鐘，響完後停用
                if not a.get("weekdays") and not a.get("date"):
                    a["enabled"] = False
                    save_alarms()

                await asyncio.sleep(60)  # 避免同分鐘重複觸發

        await asyncio.sleep(1)


# === 主程式 ===
async def main():
    global ip
    oled.fill(0)
    oled.text("Initializing...", 0, 0)
    oled.show()

    ip = connect_wifi()

    if not ip:
        # 若無法連線，開啟 Wi-Fi 設定模式
        await start_ap_server()
        return

    # --- 若成功連線 Wi-Fi，進入原本的功能 ---
    sync_time()
    load_alarms()

    tasks = [
        asyncio.create_task(read_dht_task()),     # DHT11 溫濕度
        asyncio.create_task(display_task()),      # OLED 顯示
        asyncio.create_task(alarm_task()),        # 鬧鐘檢查
        asyncio.create_task(start_webserver()),   # 鬧鐘設定網頁
    ]

    await asyncio.gather(*tasks)


# 執行主程式
try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("程式結束")



