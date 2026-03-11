import streamlit as st
import google.generativeai as genai
from openai import OpenAI
import edge_tts
import asyncio
import os
import sqlite3
import string
try:
    from pydub import AudioSegment
    HAS_PYDUB = True
except ImportError:
    HAS_PYDUB = False
from PIL import Image
import io
import re
import time
import base64
import random
import json
import uuid
import urllib.parse 
import streamlit.components.v1 as components
import copy

# ================= 0. 全局權限攔截 (Global Auth Interceptor) =================
st.set_page_config(page_title="AI 智能默書 ((v198))", page_icon="📝", layout="wide")

# [V184 Fix] 最優先檢查：如果 URL 包含 role=student，直接鎖定為學生模式
query_params = st.query_params
if query_params.get("role") == "student":
    st.session_state.is_student_mode = True
    if 'mode' not in st.session_state or st.session_state.mode == 'home':
        st.session_state.mode = 'revision'

# ================= 1. 資料庫初始化 =================
DB_NAME = "dictation.db"
DB_PATH = os.path.join(os.getcwd(), DB_NAME)

def init_db():
    """初始化 SQLite 資料庫"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS sessions
                     (sid TEXT PRIMARY KEY, data TEXT, created_at REAL)''')
        conn.commit()
        conn.close()
    except Exception as e:
        st.error(f"資料庫初始化失敗: {e}")

init_db()

# 初始化 Session State
if 'user_id' not in st.session_state: st.session_state.user_id = str(uuid.uuid4())
if 'mode' not in st.session_state: st.session_state.mode = 'home' 
if 'input_mode' not in st.session_state: st.session_state.input_mode = None
if 'input_source' not in st.session_state: st.session_state.input_source = None 

# 資料儲存區
if 'raw_vocab_text' not in st.session_state: st.session_state.raw_vocab_text = ""
if 'raw_sentence_text' not in st.session_state: st.session_state.raw_sentence_text = ""
if 'active_list' not in st.session_state: st.session_state.active_list = []
if 'runtime_list' not in st.session_state: st.session_state.runtime_list = [] 
if 'favorites' not in st.session_state: st.session_state.favorites = [] 

# 設定與標題
if 'custom_title' not in st.session_state: st.session_state.custom_title = "自律補習社"
if 'dictation_info' not in st.session_state: st.session_state.dictation_info = ""
if 'target_mode_pending' not in st.session_state: st.session_state.target_mode_pending = None 
if 'show_settings_popup' not in st.session_state: st.session_state.show_settings_popup = False 
if 'show_copy_link_dialog' not in st.session_state: st.session_state.show_copy_link_dialog = False

# 提取頁設定狀態
if 'extract_vocab_from_text' not in st.session_state: st.session_state.extract_vocab_from_text = False
if 'extract_only_paragraphs' not in st.session_state: st.session_state.extract_only_paragraphs = False

def sanitize_settings(settings):
    """確保設定值合法"""
    valid_speeds = [0.6, 0.8, 1.0, 1.2]
    if settings.get("speed") not in valid_speeds: settings["speed"] = 0.8
    return settings

if 'settings' not in st.session_state: 
    st.session_state.settings = {
        "lang": "中文", 
        "sub_lang": "廣東話", 
        "speed": 0.8, 
        "repeat": 20, 
        "interval": 5, 
        "blur": True, 
        "read_seq": True, 
        "random_order": False
    }
else:
    st.session_state.settings = sanitize_settings(st.session_state.settings)

# 再次確保 student mode 狀態
if 'is_student_mode' not in st.session_state: 
    st.session_state.is_student_mode = (st.query_params.get("role") == "student")

if 'use_vocab_mode' not in st.session_state: st.session_state.use_vocab_mode = False 
if 'auto_extract_vocab' not in st.session_state: st.session_state.auto_extract_vocab = True 

if 'current_index' not in st.session_state: st.session_state.current_index = 0
if 'expanded_items' not in st.session_state: st.session_state.expanded_items = set()

# ================= 2. 輔助函式 (Helpers) =================
# Short ID 邏輯
def create_short_link(data_dict):
    """將資料存入 SQLite 並回傳 6 位數 SID"""
    sid = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    data_dict['is_student_mode'] = True # 確保資料本身標記為學生
    json_str = json.dumps(data_dict, ensure_ascii=False)
    
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO sessions (sid, data, created_at) VALUES (?, ?, ?)", 
                  (sid, json_str, time.time()))
        conn.commit()
        conn.close()
        return sid
    except Exception as e:
        st.error(f"連結生成失敗: {e}")
        return None

def load_data_from_sid(sid):
    """從 SQLite 讀取資料"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT data FROM sessions WHERE sid=?", (sid,))
        result = c.fetchone()
        conn.close()
        if result:
            return json.loads(result[0])
        return None
    except:
        return None

# [V184 Fix] 動態網址複製列
def render_copy_row(label, params_suffix, extra_info="", info_version_label=""):
    unique_id = f"copy_{uuid.uuid4().hex[:8]}"
    extra_info = extra_info or ""
    info_version_label = info_version_label or ""
    extra_info_js = json.dumps(extra_info, ensure_ascii=False)
    info_version_label_js = json.dumps(info_version_label, ensure_ascii=False)

    html_code = f"""
    <div style="display: flex; flex-direction: column; gap: 8px; margin-bottom: 15px;">

        <div style="display: flex; align-items: flex-end; gap: 10px;">
            <div style="flex-grow: 1;">
                <div style="font-size: 14px; font-weight: bold; margin-bottom: 5px; color: #333;">{label}</div>
                <input type="text" id="input_{unique_id}" readonly style="
                    width: 100%; padding: 8px; border-radius: 5px; border: 1px solid #ccc; 
                    background-color: #f9f9f9; color: #555; font-family: monospace;">
            </div>
            <button onclick="copyToClip('{unique_id}')" id="btn_{unique_id}" style="
                height: 38px; padding: 0 15px; border-radius: 5px; border: 1px solid #00acc1; 
                background-color: white; color: #00acc1; cursor: pointer; font-weight: bold; white-space: nowrap;">
                📋 複製
            </button>
        </div>

        <div>
            <div style="font-size: 13px; font-weight: bold; margin-bottom: 5px; color: #666;">
                默書資訊{info_version_label if info_version_label else ""}
            </div>
            <textarea id="info_{unique_id}" readonly style="
                width: 100%; min-height: 80px; padding: 8px; border-radius: 5px; border: 1px solid #ccc; 
                background-color: #f9f9f9; color: #555; font-family: sans-serif; resize: vertical;"></textarea>
        </div>

    </div>

    <script>
        const baseUrl = window.parent.location.href.split('?')[0];
        const finalUrl = baseUrl + "{params_suffix}";
        const extraInfo = {extra_info_js};
        const infoVersionLabel = {info_version_label_js};

        const inputEl = document.getElementById('input_{unique_id}');
        const infoEl = document.getElementById('info_{unique_id}');
        inputEl.value = finalUrl;
        infoEl.value = extraInfo;

        function copyToClip(id) {{
            const linkText = document.getElementById('input_' + id).value;
            const infoText = document.getElementById('info_' + id).value.trim();
            const titleText = infoVersionLabel ? ("默書資訊：" + infoVersionLabel) : "默書資訊：";
            const finalText = infoText ? (linkText + "\\n\\n" + titleText + "\\n" + infoText) : linkText;

            navigator.clipboard.writeText(finalText).then(function() {{
                const btn = document.getElementById('btn_' + id);
                const originalText = btn.innerText;
                btn.innerText = '✅ 已複製';
                btn.style.backgroundColor = '#e0f7fa';
                setTimeout(() => {{
                    btn.innerText = originalText;
                    btn.style.backgroundColor = 'white';
                }}, 2000);
            }}, function(err) {{
                console.error('Async: Could not copy text: ', err);
            }});
        }}
    </script>
    """
    components.html(html_code, height=190)
HISTORY_FILE = "dictation_history.json"
FAV_FILE = "dictation_favorites.json"

def save_history_local(data_dict):
    data_dict["timestamp"] = time.time(); data_dict["date_str"] = time.strftime("%Y-%m-%d %H:%M")
    data_dict["custom_title"] = st.session_state.custom_title
    data_dict["dictation_info"] = st.session_state.dictation_info
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f: json.dump(data_dict, f, ensure_ascii=False)
    except: pass

def load_history_local():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return None
    return None

def load_favorites():
    if os.path.exists(FAV_FILE):
        try:
            with open(FAV_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: return []
    return []

def save_favorites(fav_list):
    try:
        with open(FAV_FILE, "w", encoding="utf-8") as f: json.dump(fav_list, f, ensure_ascii=False)
    except: pass

def toggle_favorite(text):
    favs = load_favorites()
    exists = False
    for item in favs:
        if item['text'] == text: exists = True; break
    if exists: favs = [f for f in favs if f['text'] != text]
    else: favs.append({"text": text, "type": "word"})
    save_favorites(favs)
    return favs

def is_favorite(text):
    favs = load_favorites()
    for item in favs:
        if item['text'] == text: return True
    return False

def detect_language(text_list):
    total_chars = 0
    ascii_chars = 0
    for item in text_list:
        txt = item['text']
        total_chars += len(txt)
        ascii_chars += len([c for c in txt if ord(c) < 128])
    
    if total_chars == 0: return "中文"
    ratio = ascii_chars / total_chars
    return "英文" if ratio > 0.5 else "中文"

# ================= 1.5 初始化檢查 (Short ID) =================
# ================= 1.5 初始化檢查 (Short ID) =================
# 只在第一次載入 sid 時初始化，避免每次 rerun 都把 mode 強制改回 revision
current_sid = st.query_params.get("sid")

if "loaded_sid" not in st.session_state:
    st.session_state.loaded_sid = None

if current_sid and st.session_state.loaded_sid != current_sid:
    data = load_data_from_sid(current_sid)

    if data:
        st.session_state.active_list = data.get("active_list", [])
        st.session_state.settings = sanitize_settings(
            data.get("settings", st.session_state.settings)
        )
        if "custom_title" in data:
            st.session_state.custom_title = data["custom_title"]
        if "dictation_info" in data:
            st.session_state.dictation_info = data["dictation_info"]

        st.session_state.is_student_mode = True
        st.session_state.mode = "revision"
        st.session_state.runtime_list = st.session_state.active_list
        st.session_state.loaded_sid = current_sid
    else:
        st.error("❌ 連結已失效或過期，請聯繫老師重新發送。")
        st.stop()

# ================= 3. 全域 CSS =================
st.markdown("""
    <style>
        .stStatusWidget {visibility: hidden;}
        #MainMenu {visibility: hidden;}
        header {visibility: hidden;}
        
        .sticky-header {
            position: fixed; top: 0; left: 0; width: 100%;
            background: white; z-index: 9999; padding: 12px 0;
            text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            border-bottom: 3px solid #00acc1;
            font-size: 18px; font-weight: bold; color: #006064; font-family: sans-serif;
        }
        .block-container { padding-top: 70px !important; }

        .list-card {
            background-color: white; border-radius: 12px; padding: 10px 16px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.05); border: 1px solid #e0e0e0;
            display: flex; align-items: center;
            height: auto !important; 
            min-height: 60px !important; 
            width: 100%;
        }
        .list-index-badge {
            width: 28px; height: 28px; border-radius: 8px;
            display: flex; align-items: center; justify-content: center;
            font-weight: bold; color: white; margin-right: 12px; font-size: 13px; flex-shrink: 0;
            align-self: flex-start;
            margin-top: 2px;
        }
        .bg-vocab { background-color: #009688; }
        .bg-sent { background-color: #039be5; }
        .list-text { 
            font-size: 16px; color: #333; flex-grow: 1; font-weight: 500; 
            white-space: pre-wrap; 
            word-wrap: break-word;
            overflow: visible;
        }

        .inline-big-card {
            background-color: #fff9c4; border-radius: 15px; padding: 20px;
            margin-top: 10px; margin-bottom: 20px;
            box-shadow: inset 0 0 10px rgba(0,0,0,0.05);
            border: 2px dashed #fbc02d; text-align: center;
        }
        .big-card-text { font-size: 32px; font-weight: bold; color: #333; margin-bottom: 15px; line-height: 1.4; }

        .delete-btn-wrapper button {
            height: 60px !important; width: 60px !important; min-width: 60px !important;
            border-radius: 12px !important; background-color: white !important;
            border: 1px solid #e0e0e0 !important; color: #ef5350 !important;
            padding: 0 !important; margin: 0 !important;
            display: flex; align-items: center; justify-content: center;
            box-shadow: 0 2px 5px rgba(0,0,0,0.05) !important;
        }
        
        .dictation-display {
            background: white; border-radius: 20px; padding: 40px 20px;
            text-align: center; box-shadow: 0 8px 20px rgba(0,0,0,0.08);
            border: 1px solid #f1f5f9; margin: 15px 0; min-height: 200px; 
            display: flex; align-items: center; justify-content: center;
        }
        .blur-text { color: transparent !important; text-shadow: 0 0 25px rgba(0,0,0,0.6) !important; font-size: 36px; font-weight: bold; user-select: none; }
        .clear-text { color: #1e293b; font-size: 36px; font-weight: bold; }
        .setting-val { font-size: 14px; font-weight: bold; color: #00acc1; cursor: pointer; }

        .ctrl-btn.nav-btn { display: none !important; }

        div[data-testid="stButton"] button[kind="secondary"]:has(span[data-testid="stIconMaterial"]) {
            border-radius: 50% !important; width: 60px !important; height: 60px !important; padding: 0 !important;
            display: flex !important; align-items: center !important; justify-content: center !important;
            border: 1px solid #e0e0e0 !important; background-color: white !important; color: #00acc1 !important;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1) !important; margin: 0 auto !important;
        }
        div[data-testid="stButton"] button[kind="secondary"]:has(span[data-testid="stIconMaterial"]):hover {
            background-color: #e0f7fa !important; transform: scale(1.05);
        }
        div[data-testid="stButton"] button[kind="secondary"]:has(span[data-testid="stIconMaterial"]) span { font-size: 32px !important; }
        div[data-testid="stButton"] button[kind="secondary"]:has(span[data-testid="stIconMaterial"]) p { display: none !important; }

        @media (max-width: 640px) {
            .block-container { padding-top: 60px !important; }
            div[data-testid="stHorizontalBlock"]:has(.list-card) { gap: 5px !important; }
            div[data-testid="stHorizontalBlock"] button { width: 100% !important; padding: 0.4rem !important; }
        }
    </style>
    """, unsafe_allow_html=True)

# ================= 5. API & Logic =================
try:
    GOOGLE_API_KEY = st.secrets.get("GOOGLE_API_KEY", "AIzaSyBGhXCAEvt_broRWNunJ2D8PAvr4bbvFJA")
    SECONDARY_API_KEY = st.secrets.get("SECONDARY_API_KEY", "sk-37VcDJDRz2bAmQtSRv2vnoKELsPERPlMEzAjH43si58FbdIQ")
except:
    GOOGLE_API_KEY = "AIzaSyBGhXCAEvt_broRWNunJ2D8PAvr4bbvFJA"
    SECONDARY_API_KEY = "sk-37VcDJDRz2bAmQtSRv2vnoKELsPERPlMEzAjH43si58FbdIQ"

SECONDARY_BASE_URL = "https://lingkeapi.com/v1"
TARGET_MODEL = "gemini-2.0-flash" 
if 'active_provider' not in st.session_state: st.session_state['active_provider'] = None

def check_api():
    if st.session_state['active_provider']: return
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel(TARGET_MODEL)
        model.generate_content("Hi", request_options={'timeout': 5})
        st.session_state['active_provider'] = 'google'
    except: st.session_state['active_provider'] = 'secondary'
check_api()

def call_ai_vision(image):
    try: image.seek(0)
    except: pass
    if image.mode != 'RGB': image = image.convert('RGB')
    
    is_auto = st.session_state.get('auto_extract_vocab', True)
    only_vocab = st.session_state.get('use_vocab_mode', False)
    extract_vocab = st.session_state.get('extract_vocab_from_text', False)
    extract_para_only = st.session_state.get('extract_only_paragraphs', False)
    
    vocab_instruction = "3. **提取詞語 (vocab)**：從識別到的內容中，主動挑選重點生字。" if is_auto else "3. **提取詞語 (vocab)**：只有當圖片中有明確的「詞語表」或「生字欄」時才提取，否則回傳空陣列。"
    sentence_instruction = "1. **提取句子 (sentences)**：忽略所有句子，sentences 欄位必須回傳空陣列 []。" if only_vocab else "1. **提取句子 (sentences)**：提取完整的句子。"
    if extract_vocab: vocab_instruction = "3. **提取詞語 (vocab)**：請務必從文本內容中識別並提取重點生字/詞彙。"
    if extract_para_only: vocab_instruction = "3. **提取詞語 (vocab)**：忽略所有生字提取，vocab 欄位必須回傳空陣列 []。"
        
    warning_prompt = "【重要】：請忽略所有標題（如「默書」、「請參考以下內容」、「溫習」、「日期」等引導語）。只提取實際的學習內容。"
    # [V184 Fix] 格式指令
    format_prompt = "【格式要求】：請將整段文字合併為一行回傳，不要在逗號、頓號或括號後換行。"

    provider = st.session_state['active_provider']
    prompt_text = f"""
    你是一個專業的教材編輯。請分析這張圖片的文字內容。
    【嚴格過濾規則】：1. 丟棄頁首頁尾。2. 丟棄填空題符號。3. 合併斷句。
    {warning_prompt}
    {format_prompt}
    【任務步驟】：{sentence_instruction} {vocab_instruction}
    請回傳純 JSON 格式：{{"vocab": ["詞1"], "sentences": ["句1"]}}
    """
    try:
        if provider == 'google':
            genai.configure(api_key=GOOGLE_API_KEY)
            model = genai.GenerativeModel(TARGET_MODEL)
            if image.width > 1024 or image.height > 1024: image.thumbnail((1024, 1024))
            response = model.generate_content([prompt_text, image], generation_config={"response_mime_type": "application/json"})
            text_res = response.text.strip().replace('```json','').replace('```','')
        else:
            buffered = io.BytesIO(); image.save(buffered, format="JPEG")
            b64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
            client = OpenAI(api_key=SECONDARY_API_KEY, base_url=SECONDARY_BASE_URL)
            response = client.chat.completions.create(
                model=TARGET_MODEL,
                messages=[{"role": "user", "content": [{"type": "text", "text": prompt_text}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
                response_format={"type": "json_object"}
            )
            text_res = response.choices[0].message.content
        
        # [V184 Fix] 1. 強制換行 2. 清除行首逗號
        data = json.loads(text_res)
        new_sentences = []
        for s in data.get("sentences", []):
            s = s.replace('\n', '') 
            s = re.sub(r'([.。?？!！])', r'\1\n', s)
            lines = [line.strip() for line in s.split('\n') if line.strip()]
            valid_lines = []
            for l in lines:
                l = l.lstrip(',，、；; ')
                if l and l not in ".。?？!！": valid_lines.append(l)
            new_sentences.extend(valid_lines)
            
        data["sentences"] = new_sentences
        return json.dumps(data)
            
    except Exception as e: return json.dumps({"vocab": [], "sentences": [], "error": str(e)})

def call_ai_text(prompt):
    provider = st.session_state['active_provider']
    try:
        if provider == 'google':
            genai.configure(api_key=GOOGLE_API_KEY)
            model = genai.GenerativeModel(TARGET_MODEL)
            return model.generate_content(prompt).text
        else:
            client = OpenAI(api_key=SECONDARY_API_KEY, base_url=SECONDARY_BASE_URL)
            res = client.chat.completions.create(model=TARGET_MODEL, messages=[{"role": "user", "content": prompt}])
            return res.choices[0].message.content
    except Exception as e: return str(e)

VOICE_MAP = { "廣東話": "zh-HK-HiuGaaiNeural", "英文": "en-GB-SoniaNeural", "普通話": "zh-CN-XiaoxiaoNeural" }

def convert_punctuation_to_text(text, lang):
    if "英文" in lang: replacements = { ".": " full stop. ", ",": " comma, ", "?": " question mark? ", "!": " exclamation mark! " }
    else: replacements = { "，": "，逗號，", "。": "。句號。", "？": "？問號？", "！": "！感嘆號！", ",": "，逗號，", ".": "。句號。", "?": "？問號？", "!": "！感嘆號！" }
    processed_text = text
    for symbol, spoken in replacements.items(): processed_text = processed_text.replace(symbol, spoken)
    return processed_text

# [V189 Fix] 更新參數：加入 item_type 以判斷是否為生字
@st.cache_data(show_spinner=False)
def generate_audio_safe(text, voice, repeat_count, interval_sec, read_seq=False, seq_idx=0, lang="廣東話", item_type='word'):
    if not text.strip(): return None
    final_text = convert_punctuation_to_text(text, lang)
    
    prefix = ""
    # [V189 Fix] 只有生字 (word) 且開啟朗讀序號時，才加序號
    if read_seq and seq_idx > 0 and item_type == 'word':
        if "英文" in lang: prefix = f"Number {seq_idx}. "
        else: prefix = f"第 {seq_idx} 個，"
            
    content_to_read = prefix + final_text
    mp3_data = None
    for _ in range(2):
        try:
            async def _gen():
                communicate = edge_tts.Communicate(content_to_read, voice, rate="+0%") 
                data = b""
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio": data += chunk["data"]
                return data
            mp3_data = asyncio.run(_gen()) 
            if mp3_data: break
        except: time.sleep(0.5)
    return mp3_data

# [V186 Fix] 增加 autoplay 參數控制
def play_audio_tag(audio_bytes, speed=1.0, autoplay=True):
    if not audio_bytes: return
    b64 = base64.b64encode(audio_bytes).decode()
    rand_id = str(uuid.uuid4())
    autoplay_attr = "autoplay" if autoplay else ""
    html = f"""
    <div style="width:100%; margin-top:5px;">
        <audio id="{rand_id}" controls {autoplay_attr} style="width: 100%; height: 30px;">
            <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
        </audio>
        <script>
            var audio = document.getElementById("{rand_id}");
            audio.playbackRate = {speed};
            if ("{autoplay_attr}") {{
                 audio.play().catch(function(error) {{ console.log("Autoplay blocked."); }});
            }}
        </script>
    </div>
    """
    st.markdown(html, unsafe_allow_html=True)

# [V189 Fix] 默書播放器：間隔邏輯更新 (Wait AFTER audio) + CSS Card Height
# [V191 Fix] Increased iframe height to 650 and enabled scrolling
def render_playlist_player(playlist_data, settings, start_index=0):
    playlist_json = json.dumps(playlist_data)
    random_id = str(uuid.uuid4())

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/howler/2.2.4/howler.min.js"></script>
        <link rel="stylesheet" href="https://fonts.googleapis.com/icon?family=Material+Icons">
        <style>
            body {{ font-family: sans-serif; margin: 0; padding: 0; background: transparent; user-select: none; overflow-y: auto; }}
            .card-container {{ 
                background: white; border-radius: 20px; padding: 40px 20px; 
                text-align: center; box-shadow: 0 8px 20px rgba(0,0,0,0.08); 
                border: 1px solid #f1f5f9; margin: 20px 0; 
                min-height: 180px; height: auto;
                display: flex; align-items: center; justify-content: center; 
                margin-bottom: 20px; 
            }}
            .text-content {{ 
                font-size: 36px; font-weight: bold; color: #1e293b; 
                transition: all 0.3s; 
                white-space: pre-wrap;
                word-wrap: break-word; 
            }}
            .text-blur {{ color: transparent !important; text-shadow: 0 0 25px rgba(0,0,0,0.6) !important; }}
            .player-box {{ background: white; border-radius: 15px; padding: 15px; border: 1px solid #e0e0e0; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
            .slider-row {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }}
            .time-txt {{ font-size: 12px; color: #666; min-width: 40px; text-align: center; }}
            input[type=range] {{ -webkit-appearance: none; width: 100%; margin: 0 10px; background: transparent; }}
            input[type=range]:focus {{ outline: none; }}
            input[type=range]::-webkit-slider-runnable-track {{ width: 100%; height: 6px; cursor: pointer; background: #e0f7fa; border-radius: 5px; }}
            input[type=range]::-webkit-slider-thumb {{ height: 18px; width: 18px; border-radius: 50%; background: #00acc1; cursor: pointer; -webkit-appearance: none; margin-top: -6px; box-shadow: 0 1px 3px rgba(0,0,0,0.3); }}
            .btn-row {{ display: flex; align-items: center; justify-content: center; gap: 20px; }}
            .ctrl-btn {{ background: white; color: #00acc1; border: 1px solid #e0e0e0; border-radius: 50%; width: 60px; height: 60px; cursor: pointer; display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 6px rgba(0,0,0,0.1); transition: transform 0.1s; outline: none; }}
            .ctrl-btn:active {{ transform: scale(0.95); background-color: #e0f7fa; }}
            .material-icons {{ font-size: 32px; }}
            .settings-row {{ display: flex; justify-content: space-around; margin-top: 20px; padding-top: 15px; border-top: 1px solid #eee; }}
            .setting-item {{ display: flex; flex-direction: column; align-items: center; position: relative; }}
            .setting-val {{ font-size: 14px; font-weight: bold; color: #00acc1; cursor: pointer; }}
            select {{
                appearance: none; -webkit-appearance: none; border: none; background: transparent;
                text-align: center; padding: 2px 5px; border-radius: 4px;
            }}
            select:focus {{ outline: none; background-color: #e0f7fa; }}
            .icon-box {{ color: #666; margin-bottom: 2px; }}
            .track-info {{ margin-top: 10px; font-size: 14px; color: #666; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="player-box">
            <div class="slider-row">
                <span id="currTime" class="time-txt">0:00</span>
                <input type="range" id="seekSlider" min="0" value="0" step="0.1">
                <span id="durTime" class="time-txt">0:00</span>
            </div>
            <div class="btn-row">
                <button class="ctrl-btn" onclick="playPrev()"><span class="material-icons">skip_previous</span></button>
                <button class="ctrl-btn" onclick="togglePlay()"><span id="playIcon" class="material-icons">play_arrow</span></button>
                <button class="ctrl-btn" onclick="playNext()"><span class="material-icons">skip_next</span></button>
            </div>
            <div id="trackInfo" class="track-info"></div>
            <div class="settings-row">
                <div class="setting-item" onclick="toggleBlur()" style="cursor:pointer">
                    <span class="material-icons icon-box" style="font-size:20px">visibility_off</span>
                    <span id="blurLabel" class="setting-val">{'隱藏' if settings['blur'] else '顯示'}</span>
                </div>
                <div class="setting-item">
                    <span class="material-icons icon-box" style="font-size:20px">speed</span>
                    <select id="speedSelect" class="setting-val" onchange="updateSpeed(this.value)">
                        {''.join([f'<option value="{s}" {"selected" if s==settings["speed"] else ""}>{s}x</option>' for s in [0.6, 0.8, 1.0, 1.2]])}
                    </select>
                </div>
                <div class="setting-item">
                    <span class="material-icons icon-box" style="font-size:20px">repeat</span>
                    <select id="repeatSelect" class="setting-val" onchange="updateRepeat(this.value)">
                        {''.join([f'<option value="{r}" {"selected" if r==settings["repeat"] else ""}>{r}次</option>' for r in [1, 3, 5, 10, 20, 99]])}
                    </select>
                </div>
                <div class="setting-item">
                    <span class="material-icons icon-box" style="font-size:20px">hourglass_empty</span>
                    <select id="intSelect" class="setting-val" onchange="updateInterval(this.value)">
                        {''.join([f'<option value="{i}" {"selected" if i==settings["interval"] else ""}>{i}秒</option>' for i in [3, 5, 8, 10, 15]])}
                    </select>
                </div>
            </div>
        </div>

        <div class="card-container">
            <div id="textContent" class="text-content {'text-blur' if settings['blur'] else ''}">Loading...</div>
        </div>

        <script>
            const playlist = {playlist_json};
            const storageKey = "dictation_player_settings_v196";

            const defaultSettings = {{
                speed: {settings['speed']},
                repeat: {settings['repeat']},
                interval: {settings['interval']},
                blur: {str(settings['blur']).lower()}
            }};

            function loadSavedSettings() {{
                try {{
                    const raw = localStorage.getItem(storageKey);
                    if (!raw) return defaultSettings;
                    const saved = JSON.parse(raw);
                    return {{
                        speed: saved.speed ?? defaultSettings.speed,
                        repeat: saved.repeat ?? defaultSettings.repeat,
                        interval: saved.interval ?? defaultSettings.interval,
                        blur: saved.blur ?? defaultSettings.blur
                    }};
                }} catch (e) {{
                    return defaultSettings;
                }}
            }}

            function saveSettings() {{
                try {{
                    localStorage.setItem(storageKey, JSON.stringify(currentSettings));
                }} catch (e) {{
                    console.log("saveSettings failed", e);
                }}
            }}

            let currentSettings = loadSavedSettings();
            let currentIndex = {start_index};
            let sound = null;
            let raf = null;
            let playedCount = 0;
            let timeoutId = null;

            // 初始化 UI 顯示為 localStorage / 現時設定
            document.getElementById("speedSelect").value = String(currentSettings.speed);
            document.getElementById("repeatSelect").value = String(currentSettings.repeat);
            document.getElementById("intSelect").value = String(currentSettings.interval);
            document.getElementById("blurLabel").innerText = currentSettings.blur ? "隱藏" : "顯示";

            loadTrack(currentIndex);

            function loadTrack(index) {{
                if (index < 0) index = 0;
                if (index >= playlist.length) index = playlist.length - 1;

                currentIndex = index;
                playedCount = 0;

                document.getElementById('trackInfo').innerText = "第 " + (currentIndex + 1) + " / " + playlist.length + " 句";
                const track = playlist[index];
                const textEl = document.getElementById('textContent');
                textEl.innerText = track.text;

                if (currentSettings.blur) textEl.classList.add('text-blur');
                else textEl.classList.remove('text-blur');

                if (sound) {{
                    sound.stop();
                    sound.unload();
                }}
                clearTimeout(timeoutId);

                if (track.audio_base64) {{
                    sound = new Howl({{
                        src: ['data:audio/mp3;base64,' + track.audio_base64],
                        html5: true,
                        rate: currentSettings.speed,
                        autoplay: true,
                        onplay: function() {{
                            document.getElementById('playIcon').innerText = 'pause';
                            raf = requestAnimationFrame(step);
                        }},
                        onpause: function() {{
                            document.getElementById('playIcon').innerText = 'play_arrow';
                            cancelAnimationFrame(raf);
                        }},
                        onend: function() {{
                            playedCount++;
                            let waitTime = currentSettings.interval * 1000;

                            if (playedCount < currentSettings.repeat) {{
                                timeoutId = setTimeout(() => {{
                                    if (sound) {{
                                        sound.rate(currentSettings.speed);
                                        sound.play();
                                    }}
                                }}, waitTime);
                            }} else {{
                                document.getElementById('playIcon').innerText = 'play_arrow';
                                if (currentIndex < playlist.length - 1) {{
                                    timeoutId = setTimeout(() => {{
                                        playNext();
                                    }}, waitTime);
                                }}
                            }}
                        }},
                        onstop: function() {{
                            document.getElementById('playIcon').innerText = 'play_arrow';
                        }},
                        onload: function() {{
                            document.getElementById('durTime').innerText = formatTime(sound.duration());
                            document.getElementById('seekSlider').max = sound.duration();
                        }}
                    }});
                }} else {{
                    document.getElementById('durTime').innerText = "0:00";
                }}
            }}

            function updateSpeed(val) {{
                currentSettings.speed = parseFloat(val);
                if (sound) sound.rate(currentSettings.speed);
                saveSettings();
            }}

            function updateRepeat(val) {{
                currentSettings.repeat = parseInt(val);
                saveSettings();
            }}

            function updateInterval(val) {{
                currentSettings.interval = parseInt(val);
                saveSettings();
            }}

            function toggleBlur() {{
                currentSettings.blur = !currentSettings.blur;
                const el = document.getElementById('textContent');
                if (currentSettings.blur) el.classList.add('text-blur');
                else el.classList.remove('text-blur');
                document.getElementById('blurLabel').innerText = currentSettings.blur ? "隱藏" : "顯示";
                saveSettings();
            }}

            function togglePlay() {{
                if (sound) {{
                    if (sound.playing()) sound.pause();
                    else {{
                        sound.rate(currentSettings.speed);
                        sound.play();
                    }}
                }}
            }}

            function playPrev() {{
                if (currentIndex > 0) loadTrack(currentIndex - 1);
            }}

            function playNext() {{
                if (currentIndex < playlist.length - 1) loadTrack(currentIndex + 1);
            }}

            const seekSlider = document.getElementById('seekSlider');

            function formatTime(secs) {{
                var m = Math.floor(secs / 60) || 0;
                var s = Math.floor(secs - m * 60) || 0;
                return m + ':' + (s < 10 ? '0' : '') + s;
            }}

            function step() {{
                if (sound && sound.playing()) {{
                    var seek = sound.seek() || 0;
                    seekSlider.value = seek;
                    document.getElementById('currTime').innerText = formatTime(seek);
                    raf = requestAnimationFrame(step);
                }}
            }}

            seekSlider.addEventListener('input', function() {{
                if (sound) sound.seek(seekSlider.value);
            }});
        </script>
    </body>
    </html>
    """
    components.html(html, height=650, scrolling=True)

# ================= 6. 密碼檢查 =================
def check_password():
    if st.secrets.get("ENV") == "LOCAL": return True
    
    # [V182 Fix] 終極檢查：直接看 URL 參數，如果 role=student 就不擋 (ByPass Check)
    if st.query_params.get("role") == "student": return True
    
    if st.session_state.get("is_student_mode", False): return True
    if st.session_state.get("password_correct", False): return True
    
    with st.container(border=True):
        st.markdown("<h3 style='text-align: center;'>🔐 請輸入密碼 (Admin)</h3>", unsafe_allow_html=True)
        pwd = st.text_input("Password", type="password")
        if pwd == "aiisthebest":  
            st.session_state["password_correct"] = True; st.rerun()
        elif pwd: st.error("❌ 密碼錯誤")
    return False

if not check_password(): st.stop()

# [V171] 範圍選擇 Dialog
@st.dialog("請選擇範圍")
def entry_dialog(target_mode):
    current_lang = st.session_state.settings["lang"]
    
    if current_lang == "中文":
        st.write("🗣️ 請選擇發音語言：")
        sub_lang = st.radio("方言", ["廣東話", "普通話"], horizontal=True, label_visibility="collapsed", key="sub_lang_entry")
        st.session_state.settings["sub_lang"] = sub_lang
        st.divider()
    
    st.write("您想針對哪些內容進行練習？")
    
    full_list = st.session_state.active_list
    vocab_list = [x for x in full_list if x['type'] == 'word']
    sent_list = [x for x in full_list if x['type'] == 'sentence']
    
    col1, col2, col3 = st.columns(3)
    btn_vocab = col1.button(f"生字 ({len(vocab_list)})", disabled=len(vocab_list)==0, use_container_width=True)
    btn_para = col2.button(f"段落 ({len(sent_list)})", disabled=len(sent_list)==0, use_container_width=True)
    btn_all = col3.button(f"全部 ({len(full_list)})", use_container_width=True)
    
    if len(vocab_list) > 0:
        st.divider()
        st.markdown("👇 **以下設定只適用於「默生字」**")
        c1, c2 = st.columns(2)
        new_seq = c1.toggle("朗讀序號", value=st.session_state.settings.get('read_seq', True))
        new_random = c2.toggle("亂序播放", value=st.session_state.settings.get('random_order', False))
        st.session_state.settings['read_seq'] = new_seq
        st.session_state.settings['random_order'] = new_random

    if btn_vocab:
        st.session_state.runtime_list = vocab_list
        if st.session_state.settings.get("random_order", False): 
            random.shuffle(st.session_state.runtime_list)
        st.session_state.mode = target_mode
        if target_mode == 'dictation': st.session_state.current_index = 0
        st.session_state.target_mode_pending = None
        st.session_state.show_copy_link_dialog = False
        st.rerun()
        
    if btn_para:
        st.session_state.runtime_list = sent_list
        st.session_state.mode = target_mode
        if target_mode == 'dictation': st.session_state.current_index = 0
        st.session_state.target_mode_pending = None
        st.session_state.show_copy_link_dialog = False
        st.rerun()
        
    if btn_all:
        st.session_state.runtime_list = full_list
        st.session_state.mode = target_mode
        if target_mode == 'dictation': st.session_state.current_index = 0
        st.session_state.target_mode_pending = None
        st.session_state.show_copy_link_dialog = False
        st.rerun()

# [V184 Fix] 雙連結複製彈窗 (UI Optim + Dynamic Link)
@st.dialog("🔗 建立學生連結")
def copy_link_dialog():
    current_lang = st.session_state.settings["lang"]
    
    if current_lang == "中文":
        st.write("請選擇要複製的語言版本：")
        
        # 廣東話
        settings_canton = copy.deepcopy(st.session_state.settings)
        settings_canton["sub_lang"] = "廣東話"
        data_canton = {
            "active_list": st.session_state.active_list,
            "settings": settings_canton,
            "custom_title": st.session_state.custom_title,
            "dictation_info": st.session_state.dictation_info
        }
        sid_canton = create_short_link(data_canton)
        render_copy_row(
    "廣東話版本",
    f"?sid={sid_canton}&role=student",
    st.session_state.dictation_info,
    "（廣東話版）"
)
        
        st.divider()
        
        # 普通話
        settings_manda = copy.deepcopy(st.session_state.settings)
        settings_manda["sub_lang"] = "普通話"
        data_manda = {
            "active_list": st.session_state.active_list,
            "settings": settings_manda,
            "custom_title": st.session_state.custom_title,
            "dictation_info": st.session_state.dictation_info
        }
        sid_manda = create_short_link(data_manda)
        render_copy_row(
    "普通話版本",
    f"?sid={sid_manda}&role=student",
    st.session_state.dictation_info,
    "（國語版）"
)

    else:
        # 英文或其他
        data_normal = {
            "active_list": st.session_state.active_list,
            "settings": st.session_state.settings,
            "custom_title": st.session_state.custom_title,
            "dictation_info": st.session_state.dictation_info
        }
        sid_normal = create_short_link(data_normal)
        st.success("✅ 連結已生成！")
        render_copy_row(
            "學生連結",
            f"?sid={sid_normal}&role=student",
            st.session_state.dictation_info
        )

    st.info("💡 提示：連結已包含「學生模式」權限，無需密碼。")

# ================= 8. 介面流程控制 =================

# --- MODE 1: 首頁 (Home) ---
if st.session_state.get('mode', 'home') == 'home':
    st.markdown("""
    <style>
    div[data-testid="stButton"] button[kind="primary"] {
        height: 180px !important;
        width: 100% !important;
        border-radius: 24px !important;
        background: linear-gradient(135deg, #e0f7fa 0%, #b2ebf2 100%) !important;
        color: #006064 !important;
        border: none !important;
        box-shadow: 0 4px 15px rgba(0,188,212, 0.15) !important;
    }
    div[data-testid="stButton"] button[kind="primary"] p {
        font-size: 24px !important;
        font-weight: 800 !important;
    }
    </style>
    """, unsafe_allow_html=True)

    st.title("📝 默書神隊友 ((v198))")

    local_hist = load_history_local()
    if local_hist:
        if "custom_title" in local_hist:
            st.session_state.custom_title = local_hist["custom_title"]
        if "dictation_info" in local_hist:
            st.session_state.dictation_info = local_hist["dictation_info"]

        if st.button(
            f"🔄 繼續上次練習 ({local_hist.get('date_str','')})",
            use_container_width=True,
            type="secondary"
        ):
            st.session_state.active_list = local_hist.get("active_list", [])
            st.session_state.settings = sanitize_settings(
                local_hist.get("settings", st.session_state.settings)
            )
            st.session_state.mode = 'confirm'
            st.rerun()

    c1, c2 = st.columns(2)

    if c1.button("📷 拍照 / 上傳", key="camup", type="primary", use_container_width=True):
        st.session_state.mode = 'input'
        st.session_state.input_source = "upload"
        st.rerun()

    if c2.button("✍️ 手動輸入", key="manual_home", type="primary", use_container_width=True):
        st.session_state.mode = 'input'
        st.session_state.input_source = "manual"
        st.rerun()

    if st.button("⭐ 溫習收藏字庫", use_container_width=True):
        favs = load_favorites()
        if not favs:
            st.toast("尚無收藏內容")
            time.sleep(1)
        else:
            st.session_state.active_list = favs
            st.session_state.runtime_list = favs
            st.session_state.mode = 'revision'
            st.rerun()

    with st.expander("更多選項"):
        if st.button("🤖 AI 出題", type="secondary", use_container_width=True):
            st.session_state.mode = 'input'
            st.session_state.input_source = "ai"
            st.rerun()

    if st.session_state.show_settings_popup:
        @st.dialog("⚙️ 設定與確認")
        def show_dialog_home():
            render_settings_dialog_content()
        show_dialog_home()

# --- MODE 2: 輸入與前置設定 (Input) ---
elif st.session_state.mode == 'input':
    st.title("📸 提取內容"); st.divider()
    
    src = st.session_state.input_source
    img = None
    
    if src == "upload":
        img = st.file_uploader(
            "📷 拍照或上傳圖片",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
            help="在手機 / iPad 上可直接選擇拍照或照片圖庫"
        )
    elif src == "ai":
        st.info("🤖 請輸入主題，AI 將為您生成詞語和句子")
        topic = st.text_input("輸入主題")
        if st.button("✨ AI 生成", type="primary", use_container_width=True):
            if topic:
                with st.spinner("🤖 AI 正在思考中..."):
                    prompt = f"請為小學生設計關於「{topic}」的默書內容。請回傳純 JSON 格式：{{\"vocab\": [\"5-8個詞語\"], \"sentences\": [\"3-5個句子\"]}}"
                    res = call_ai_text(prompt)
                    try:
                        data = json.loads(res.replace("```json", "").replace("```", ""))
                        st.session_state.raw_vocab_text = "\n".join(data.get("vocab", []))
                        st.session_state.raw_sentence_text = "\n".join(data.get("sentences", []))
                        st.session_state.mode = 'edit'
                        st.rerun()
                    except: st.error("生成失敗，請重試")
            else: st.warning("請輸入主題")
    elif src == "manual":
        st.info("✍️ 請直接點擊下一步，進入編輯頁面手動輸入")
    
    if src == "upload":
        with st.container(border=True):
            st.write("⚙️ 提取設定")
            c_set1, c_set2 = st.columns(2)
            st.session_state.extract_vocab_from_text = c_set1.toggle("從文中抽取生字", value=st.session_state.extract_vocab_from_text, help="AI 會嘗試從課文中抓取重點詞彙")
            st.session_state.extract_only_paragraphs = c_set2.toggle("只抽取段落 (忽略生字)", value=st.session_state.extract_only_paragraphs, help="如果您不需要提取詞語表，請開啟此項")

    if st.button("🚀 開始提取" if src != "manual" else "🚀 開始編輯", type="primary", use_container_width=True):
        if src == "manual":
            st.session_state.mode = 'edit'; st.rerun()
        elif img:
            imgs_to_process = img if isinstance(img, list) else [img]
            all_v = []
            all_s = []
            
            try:
                with st.spinner(f"🔍 AI 正在識別 {len(imgs_to_process)} 張圖片..."):
                    for i, f in enumerate(imgs_to_process):
                        try:
                            pil_img = Image.open(f)
                            res = call_ai_vision(pil_img)
                            data = json.loads(res)
                            if "error" in data:
                                st.error(f"第 {i+1} 張圖片識別錯誤: {data['error']}")
                            else:
                                all_v.extend(data.get("vocab", []))
                                all_s.extend(data.get("sentences", []))
                        except json.JSONDecodeError:
                            json_match = re.search(r'\{.*\}', res, re.DOTALL)
                            if json_match:
                                data = json.loads(json_match.group())
                                all_v.extend(data.get("vocab", []))
                                all_s.extend(data.get("sentences", []))
                            else:
                                st.error(f"第 {i+1} 張圖片回傳格式錯誤")
                        except Exception as e:
                            st.error(f"第 {i+1} 張圖片處理失敗: {str(e)}")
                            
                if all_v or all_s:
                    # [V184 Fix] 清除多餘空行 + 行首逗號
                    raw_v = "\n".join(all_v)
                    raw_s = "\n".join(all_s)
                    st.session_state.raw_vocab_text = re.sub(r'\n+', '\n', raw_v).strip()
                    st.session_state.raw_sentence_text = re.sub(r'\n+', '\n', raw_s).strip()
                    st.session_state.mode = 'edit'
                    st.rerun()
                else:
                    st.warning("未能識別到有效文字，請重試")
            except Exception as e:
                st.error(f"系統錯誤: {str(e)}")
        else:
            st.warning("請先拍攝或上傳圖片")
            
    if st.button("🔙 返回", use_container_width=True): st.session_state.mode = 'home'; st.rerun()

# --- MODE 3: 編輯內容 (Edit) ---
elif st.session_state.mode == 'edit':
    st.subheader("✏️ 編輯內容")

    # 保底：如果 raw 欄位是空，但 active_list 有資料，就自動從 active_list 還原
    if (not st.session_state.raw_vocab_text and not st.session_state.raw_sentence_text) and st.session_state.active_list:
        vocab_lines = [item["text"] for item in st.session_state.active_list if item["type"] == "word"]
        sent_lines = [item["text"] for item in st.session_state.active_list if item["type"] == "sentence"]

        st.session_state.raw_vocab_text = "\n".join(vocab_lines)
        st.session_state.raw_sentence_text = "\n".join(sent_lines)

    st.session_state.raw_vocab_text = st.text_area("詞語", st.session_state.raw_vocab_text, height=400)
    st.session_state.raw_sentence_text = st.text_area("句子", st.session_state.raw_sentence_text, height=600)


    c1, c2 = st.columns(2)
    if c1.button("🔙 返回", use_container_width=True): st.session_state.mode = 'home'; st.rerun()
    if c2.button("下一步 (設定與確認) ➡️", type="primary", use_container_width=True):
        # [V184 Fix] Clean
        v_lines = [l.strip() for l in re.sub(r'\n+', '\n', st.session_state.raw_vocab_text).split('\n') if l.strip()]
        s_lines = [l.strip() for l in re.sub(r'\n+', '\n', st.session_state.raw_sentence_text).split('\n') if l.strip()]
        st.session_state.active_list = [{"text": x, "type": "word"} for x in v_lines] + [{"text": x, "type": "sentence"} for x in s_lines]
        save_history_local({"active_list": st.session_state.active_list, "settings": st.session_state.settings})
        st.session_state.mode = 'confirm'; st.rerun()

# --- MODE 4: 設定與確認 (Confirm) ---
elif st.session_state.mode == 'confirm':
    st.subheader("⚙️ 設定與確認")
    
    if st.session_state.active_list:
        detected = detect_language(st.session_state.active_list)
        if st.session_state.settings["lang"] not in ["中文", "英文"]:
            st.session_state.settings["lang"] = detected

    if st.session_state.target_mode_pending:
        entry_dialog(st.session_state.target_mode_pending)
    elif st.session_state.show_copy_link_dialog:
        copy_link_dialog()

    with st.container(border=True):
        st.markdown('<div class="setting-label">🗣️ 語言</div>', unsafe_allow_html=True)
        new_lang = st.radio("語言", ["中文", "英文"], index=["中文", "英文"].index(st.session_state.settings["lang"]), horizontal=True, label_visibility="collapsed")
        st.markdown('<div class="setting-label">⚡ 語速</div>', unsafe_allow_html=True)
        speed_opts = {"特慢 (-40%)": -40, "慢 (-20%)": -20, "正常 (0%)": 0, "快 (+20%)": 20}
        curr_spd = st.session_state.settings["speed"]
        if isinstance(curr_spd, float): 
            if curr_spd < 0.7: curr_spd = -40
            elif curr_spd < 0.9: curr_spd = -20
            elif curr_spd < 1.1: curr_spd = 0
            else: curr_spd = 20
        try: def_idx = list(speed_opts.values()).index(curr_spd)
        except: def_idx = 2
        speed_label = st.radio("語速", list(speed_opts.keys()), index=def_idx, horizontal=True, label_visibility="collapsed")
        new_speed = speed_opts[speed_label]
        st.markdown('<div class="setting-label">🔄 朗讀次數</div>', unsafe_allow_html=True)
        new_repeat = st.number_input("朗讀次數", min_value=1, max_value=100, value=st.session_state.settings.get("repeat", 20), step=1, label_visibility="collapsed")
        st.markdown('<div class="setting-label">⏳ 重複間隔</div>', unsafe_allow_html=True)
        int_opts = [3, 5, 8, 10, 15]
        try: i_idx = int_opts.index(st.session_state.settings["interval"])
        except: i_idx = 1
        new_interval = st.radio("間隔", int_opts, index=i_idx, horizontal=True, label_visibility="collapsed")
        
        c_b1, c_b2, c_b3 = st.columns(3)
        new_blur = c_b1.toggle("模糊顯示", value=st.session_state.settings["blur"])
        
        float_speed = 1.0
        if new_speed == -40: float_speed = 0.6
        elif new_speed == -20: float_speed = 0.8
        elif new_speed == 0: float_speed = 1.0
        elif new_speed == 20: float_speed = 1.2

        st.session_state.settings.update({
            "lang": new_lang, "speed": float_speed, "repeat": new_repeat, "interval": new_interval, "blur": new_blur
        })
        
        st.markdown('<div class="setting-label">🏫 標題設定</div>', unsafe_allow_html=True)
        st.caption("默書頁標題")
        st.session_state.custom_title = st.text_input(
            "默書頁標題",
            value=st.session_state.custom_title,
            label_visibility="collapsed"
        )

        st.markdown('<div class="setting-label">📝 默書資訊</div>', unsafe_allow_html=True)
        st.caption("這段文字會在產生學生連結時，放在連結下方一同複製")
        st.session_state.dictation_info = st.text_area(
            "默書資訊",
            value=st.session_state.dictation_info,
            height=100,
            label_visibility="collapsed",
            placeholder="例如：\n第六課默書\n日期：3/10\n範圍：第1段至第3段"
        )

    st.markdown("### 📝 默書內容")
    for i, item in enumerate(st.session_state.active_list):
        bg_class = "bg-vocab" if item["type"] == "word" else "bg-sent"
        st.markdown(f"""<div class="list-card"><div class="list-index-badge {bg_class}">{i+1}</div><div class="list-text">{item['text']}</div></div>""", unsafe_allow_html=True)

    st.divider()
    c1, c2, c3 = st.columns([1, 1, 1], vertical_alignment="bottom")
    with c1:
        if st.button("🔙 返回編輯", type="primary", use_container_width=True): st.session_state.mode = 'edit'; st.rerun()
    with c2:
        if st.button("🔗 複製學生連結", type="secondary", use_container_width=True):
            st.session_state.show_copy_link_dialog = True
            st.session_state.target_mode_pending = None
            st.rerun()
        
        def trigger_popup(target):
            # [V185 Fix] Revision mode goes direct
            if target == 'revision':
                st.session_state.runtime_list = st.session_state.active_list
                st.session_state.mode = 'revision'
                st.session_state.show_copy_link_dialog = False
                if st.session_state.settings["lang"] == "中文":
                     st.session_state.target_mode_pending = 'revision_direct' # New flag for direct entry dialog
                     st.rerun()
                else:
                    st.rerun()
            else:
                # Dictation mode still needs full popup
                st.session_state.show_copy_link_dialog = False
                st.session_state.target_mode_pending = target
                st.rerun()

        if st.button("📖 進入溫習模式", type="primary", use_container_width=True):
            # [V185 Fix] 直接進入，不選範圍
            if st.session_state.settings["lang"] == "中文":
                 trigger_popup('revision')
            else:
                 st.session_state.runtime_list = st.session_state.active_list
                 st.session_state.mode = 'revision'
                 st.rerun()
            
    with c3:
        if st.button("🚀 直接開始默書", type="primary", use_container_width=True):
            trigger_popup('dictation')

# [V185 Add] 簡化版 Dialog (只選方言)
if st.session_state.target_mode_pending == 'revision_direct':
    @st.dialog("請選擇發音")
    def simple_dialect_dialog():
        st.write("🗣️ 請選擇發音語言：")
        sub_lang = st.radio("方言", ["廣東話", "普通話"], horizontal=True)
        if st.button("開始溫習", type="primary", use_container_width=True):
            st.session_state.settings["sub_lang"] = sub_lang
            st.session_state.runtime_list = st.session_state.active_list
            st.session_state.mode = 'revision'
            st.session_state.target_mode_pending = None
            st.rerun()
    simple_dialect_dialog()

# --- MODE 5: 溫習模式 (Revision) [V186 Major Update] ---
elif st.session_state.mode == 'revision':
    # 1. 置頂標題
    st.markdown(f'<div class="sticky-header">{st.session_state.custom_title}</div>', unsafe_allow_html=True)
    
    # 2. 頂部控制列
    c_ctrl1, c_ctrl2, c_ctrl3 = st.columns(3)
    if c_ctrl1.button("📂 全部展開", use_container_width=True):
        st.session_state.expanded_items = set(range(len(st.session_state.runtime_list)))
        st.rerun()
    if c_ctrl2.button("📁 全部收起", use_container_width=True):
        st.session_state.expanded_items = set()
        st.rerun()
    if c_ctrl3.button("🔗 複製連結", use_container_width=True):
        st.session_state.show_copy_link_dialog = True
        st.rerun()
        
    if st.session_state.show_copy_link_dialog:
        copy_link_dialog()

    target_list = st.session_state.get('runtime_list', st.session_state.active_list)
    lang_setting = st.session_state.settings["lang"]
    if lang_setting == "中文":
        voice = VOICE_MAP[st.session_state.settings.get("sub_lang", "廣東話")]
    else:
        voice = VOICE_MAP["英文"]

    for i, item in enumerate(target_list):
        bg_class = "bg-vocab" if item["type"] == "word" else "bg-sent"
        
        # [V188] 三欄式佈局：[8, 1, 1] 更緊湊
        col_card, col_fav, col_exp = st.columns([8, 1, 1], vertical_alignment="center")
        
        with col_card:
            st.markdown(f"""<div class="list-card"><div class="list-index-badge {bg_class}">{i+1}</div><div class="list-text">{item['text']}</div></div>""", unsafe_allow_html=True)
        
        # 收藏按鈕
        with col_fav:
            is_fav = is_favorite(item['text'])
            fav_icon = "⭐" if is_fav else "☆"
            fav_type = "primary" if is_fav else "secondary"
            if st.button(fav_icon, key=f"fav_btn_{i}", type=fav_type):
                toggle_favorite(item['text'])
                st.rerun()

        # 展開/收起按鈕 (點擊切換)
        with col_exp:
            is_exp = i in st.session_state.expanded_items
            btn_txt = "➖" if is_exp else "➕"
            if st.button(btn_txt, key=f"exp_btn_{i}"):
                if is_exp: st.session_state.expanded_items.remove(i)
                else: st.session_state.expanded_items.add(i)
                st.rerun()

        # 展開後的內容
        if i in st.session_state.expanded_items:
            # 大字卡
            st.markdown(f"""<div class="inline-big-card"><div class="big-card-text">{item['text']}</div></div>""", unsafe_allow_html=True)
            
            # [V187 Fix] 使用設定中的語速
            spd = st.session_state.settings.get('speed', 1.0)
            
            # 單一 Soundbar，無序號，預設靜音 (autoplay=False)
            audio = generate_audio_safe(item['text'], voice, 1, 0, False, 0, lang_setting)
            play_audio_tag(audio, speed=spd, autoplay=False) 
            st.markdown("---")

    st.divider()
    
    # 底部按鈕
    c_b1, c_b2 = st.columns(2)
    if c_b1.button("🚀 進入默書", type="primary", use_container_width=True):
        st.session_state.mode = 'dictation'; st.session_state.current_index = 0; st.rerun()
    if c_b2.button("⚙️ 返回設定", type="secondary", use_container_width=True):
        if st.session_state.get("is_student_mode", False):
            st.session_state.is_student_mode = False 
            st.session_state.mode = 'home'
        else:
            st.session_state.mode = 'confirm'
        st.rerun()

# --- MODE 6: 默書模式 (Dictation) ---
elif st.session_state.mode == 'dictation':
    st.markdown(f'<div class="sticky-header">{st.session_state.custom_title}</div>', unsafe_allow_html=True)
    active_list = st.session_state.get('runtime_list', st.session_state.active_list)
    idx = st.session_state.current_index
    if idx >= len(active_list): idx = len(active_list) - 1
    
    new_idx = st.slider(
        "左右拖曳切換題目", 
        1, len(active_list), idx + 1,
        help="您可以拖曳滑桿來快速切換題目"
    ) - 1
    
    if new_idx != idx:
        st.session_state.current_index = new_idx
        st.rerun()
    
    if 'full_playlist_cache' not in st.session_state or st.session_state.get('playlist_source_id') != str(len(active_list)):
        with st.spinner("🚀 正在為您準備所有語音，請稍候... (只需一次)"):
            lang_setting = st.session_state.settings["lang"]
            if lang_setting == "中文":
                voice = VOICE_MAP[st.session_state.settings.get("sub_lang", "廣東話")]
            else:
                voice = VOICE_MAP["英文"]
                
            full_playlist = []
            for idx_p, item in enumerate(active_list):
                # [V189 Fix] Pass item type to check if sequence is needed
                audio_bytes = generate_audio_safe(
                    item['text'], voice,
                    st.session_state.settings["repeat"],
                    st.session_state.settings["interval"],
                    st.session_state.settings.get('read_seq', False), 
                    idx_p+1, 
                    lang_setting,
                    item['type'] # Pass the type (word/sentence)
                )
                full_playlist.append({
                    "text": item['text'],
                    "audio_base64": base64.b64encode(audio_bytes).decode() if audio_bytes else ""
                })
            st.session_state.full_playlist_cache = full_playlist
            st.session_state.playlist_source_id = str(len(active_list))
    
    render_playlist_player(st.session_state.full_playlist_cache, st.session_state.settings, start_index=st.session_state.current_index)

    st.divider()
    c_back, c_exit = st.columns(2)
    if c_back.button("📖 返回溫書", type="primary", use_container_width=True): st.session_state.mode = 'revision'; st.rerun()
    if c_exit.button("❌ 結束 (Home)", type="primary", use_container_width=True):
        if st.session_state.get("is_student_mode", False):
            st.session_state.is_student_mode = False
            st.session_state["password_correct"] = False
        st.session_state.mode = 'home'; st.rerun()
    
    # [V189 Fix] 默書頁面新增複製連結
    if st.button("🔗 複製學生連結", type="secondary", use_container_width=True):
        st.session_state.show_copy_link_dialog = True
        st.rerun()

    if st.session_state.show_copy_link_dialog:
        copy_link_dialog()

    if st.button("🏆 完成核對", type="primary", use_container_width=True): st.session_state.mode = 'check'; st.rerun()

# --- MODE 7: 核對 (Check) ---
elif st.session_state.mode == 'check':
    st.balloons()
    st.title("✅ 核對時間")
    with st.container(border=True):
        st.markdown("### 📝 完整內容清單")
        target_list = st.session_state.get('runtime_list', st.session_state.active_list)
        for i, item in enumerate(target_list):
            st.markdown(f"**{i+1}.** {item['text']}")
    if st.button("🏠 回首頁", type="primary", use_container_width=True): 
        if st.session_state.get("is_student_mode", False):
            st.session_state.is_student_mode = False
            st.session_state["password_correct"] = False
        st.session_state.mode = 'home'; st.rerun()
