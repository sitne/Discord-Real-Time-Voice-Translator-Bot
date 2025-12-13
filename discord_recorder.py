import discord
import os
from dotenv import load_dotenv
import io
import asyncio
import wave
import langcodes
from google import genai
import time
from collections import defaultdict
import webrtcvad
import traceback
import audioop
import sqlite3 # JSONの代わりにSQLiteを使用
from groq import Groq, RateLimitError as GroqRateLimitError

# --- 設定読み込み ---
load_dotenv()
token = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True
bot = discord.Bot(intents=intents)

# データベースファイル名
DB_FILE = "bot_data.db"

# APIエラー時のクールダウン（秒）
API_ERROR_COOLDOWN = 60 
last_api_error_time = 0

# Groq用プロンプト
VALORANT_PROMPT = (
    "VALORANT,ヴァロラント,ジェット,レイズ,オーメン,セージ,サイファー,ヴァイパー,ブリーチ,ブリムストーン,フェニックス,レイナ,キルジョイ,スカイ,ソーヴァ,アストラ"
    "Jett,Raze,Omen,Sage,Cypher,Viper,Breach,Brimstone,Phoenix,Reyna,Killjoy,Skye,Sova,Astra"
    "Aサイト,Bサイト,ヘイブン,アセント,バインド,スプリット,ロータス,サンセット,スパイク,設置,解除,ピーク,エントリー,リテイク,ULT,アルティメット,"
    "제트,레이즈,오멘,세이지,사이퍼,브림스톤,피닉스,레이나,킬조이,스카이,소바,바인드,헤이븐,스플릿,어센트,로터스,선셋,아스트라"
    "스파이크,설치,해체,피킹,엔트리,리테이크,궁극기,궁"
)

# --- データベース初期化 ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        # ユーザー設定テーブル: user_id, source_lang, target_lang
        c.execute('''CREATE TABLE IF NOT EXISTS user_settings
                     (user_id INTEGER PRIMARY KEY, source_lang TEXT, target_lang TEXT)''')
        conn.commit()

# 設定の取得
def get_user_setting(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT source_lang, target_lang FROM user_settings WHERE user_id=?", (user_id,))
        row = c.fetchone()
        if row:
            return {"source": row[0], "target": row[1]}
        return None

# 設定の保存
def save_user_setting(user_id, source, target):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_settings (user_id, source_lang, target_lang) VALUES (?, ?, ?)",
                  (user_id, source, target))
        conn.commit()

init_db()
active_sinks = {}

# --- クライアント初期化 ---
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    print("Gemini API Client (Translation) initialized.")
else:
    gemini_client = None

if GROQ_API_KEY:
    groq_client = Groq(api_key=GROQ_API_KEY)
    print("Groq API Client (Whisper) initialized.")
else:
    groq_client = None
    print("警告: GROQ_API_KEYが設定されていません。")

class Silence(discord.AudioSource):
    def read(self):
        return b'\x00' * 3840

class AutoTranslateSink(discord.sinks.Sink):
    SPEECH_END_THRESHOLD_S = 1.2
    MIN_SPEECH_DURATION_S = 1.0
    CHECK_INTERVAL_S = 0.2

    def __init__(self, vc: discord.VoiceClient, target_channel: discord.TextChannel):
        super().__init__()
        self.vc = vc
        self.target_channel = target_channel
        self.vad = webrtcvad.Vad(3)
        self.speech_buffers = defaultdict(io.BytesIO)
        self.last_activity_time = defaultdict(float)
        self.is_speaking_map = defaultdict(bool)
        self.checker_task = bot.loop.create_task(self.check_for_silence())

    def write(self, data: bytes, user_id: int):
        # 設定がないユーザーは無視（負荷軽減）
        if get_user_setting(user_id) is None:
            return

        if not self.is_speaking_map.get(user_id):
            self.is_speaking_map[user_id] = True
        self.last_activity_time[user_id] = time.time()
        self.speech_buffers[user_id].write(data)

    async def check_for_silence(self):
        while True:
            await asyncio.sleep(self.CHECK_INTERVAL_S)
            current_time = time.time()
            users_to_check = list(self.is_speaking_map.keys())
            for user_id in users_to_check:
                if not self.is_speaking_map[user_id]:
                    continue
                if (current_time - self.last_activity_time[user_id]) > self.SPEECH_END_THRESHOLD_S:
                    self.is_speaking_map[user_id] = False
                    buffer = self.speech_buffers.pop(user_id, None)
                    if buffer:
                        asyncio.create_task(self.process_user_audio(user_id, buffer))

    def stop(self):
        if self.checker_task:
            self.checker_task.cancel()
        for user_id, buffer in list(self.speech_buffers.items()):
            asyncio.create_task(self.process_user_audio(user_id, buffer))
        self.speech_buffers.clear()

    def _stereo_to_mono(self, stereo_bytes: bytes) -> bytes:
        try:
            return audioop.tomono(stereo_bytes, 2, 1, 1)
        except:
            return b''
        
    def _is_audio_too_quiet(self, audio_bytes: bytes, threshold: int = 500) -> bool:
        try:
            rms = audioop.rms(audio_bytes, 2)
            return rms < threshold
        except:
            return True

    async def process_user_audio(self, user_id: int, audio_buffer: io.BytesIO):
        global last_api_error_time
        if not groq_client: return

        # APIエラーから一定期間は処理をスキップ（スパム防止）
        if time.time() - last_api_error_time < API_ERROR_COOLDOWN:
            return

        try:
            user_setting = get_user_setting(user_id)
            if not user_setting or "source" not in user_setting:
                return

            source_lang_code = user_setting["source"]
            target_lang_code = user_setting.get("target")

            audio_buffer.seek(0)
            original_stereo_bytes = audio_buffer.read()
            audio_buffer.close()

            mono_bytes = self._stereo_to_mono(original_stereo_bytes)
            if not mono_bytes: return

            if self._is_audio_too_quiet(mono_bytes, threshold=500):
                return

            bytes_per_second = 48000 * 2
            speech_duration_s = len(mono_bytes) / bytes_per_second
            
            if speech_duration_s < self.MIN_SPEECH_DURATION_S:
                return 

            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as f:
                f.setnchannels(1)
                f.setsampwidth(2)
                f.setframerate(48000)
                f.writeframes(mono_bytes)
            
            wav_bytes = wav_buffer.getvalue()
            wav_buffer.name = "audio.wav"
            wav_buffer.seek(0)

            # print(f"[PROCESS] User {user_id} duration: {speech_duration_s:.2f}s")

            # --- 1. Groq (Whisper) STT ---
            def transcribe_sync():
                return groq_client.audio.transcriptions.create(
                    file=("audio.wav", wav_bytes),
                    model="whisper-large-v3-turbo",
                    prompt=VALORANT_PROMPT,
                    language=source_lang_code,
                    response_format="json",
                    temperature=0.2,
                )

            transcription = await bot.loop.run_in_executor(None, transcribe_sync)
            original_text = transcription.text.strip()
            
            if not original_text: return

            # Embed作成
            user = await bot.fetch_user(user_id)
            embed = discord.Embed(description=f"**発言者:** {user.mention}", color=user.accent_color or discord.Colour.blue())
            embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
            
            source_lang_name = langcodes.Language.get(source_lang_code).language_name('ja')
            embed.add_field(name=f"原文 ({source_lang_name})", value=f"```{original_text}```", inline=False)

            # --- 2. Gemini Translation ---
            if gemini_client and target_lang_code and target_lang_code != "none" and target_lang_code != source_lang_code:
                target_lang_name = langcodes.Language.get(target_lang_code).language_name('ja')
                prompt = (
                    f"Translate the following text from {source_lang_name} to {target_lang_name}. "
                    f"Output ONLY the translated text.\n\nText:\n{original_text}"
                )
                response = await gemini_client.aio.models.generate_content(
                    model="gemma-3-27b-it", # 必要に応じて flash モデル等に変更
                    contents=prompt
                )
                translated_text = response.text.strip()
                if translated_text:
                    embed.add_field(name=f"翻訳 ({target_lang_name})", value=f"```{translated_text}```", inline=False)

            await self.target_channel.send(embed=embed)

        except GroqRateLimitError:
            print("Groq API Rate Limit Reached.")
            last_api_error_time = time.time()
            try:
                await self.target_channel.send("⚠️ **API制限に達しました。**\n一時的に翻訳を停止します。（約1分後に自動復帰します）")
            except: pass

        except Exception as e:
            # その他のエラー
            print(f"Error for user {user_id}: {e}")
            # traceback.print_exc()

# --- コマンド ---

@bot.slash_command(description="ボイスチャンネルに参加します。")
async def join(ctx):
    await ctx.defer()
    if ctx.guild.voice_client:
        try:
            await ctx.guild.voice_client.disconnect(force=True)
            await asyncio.sleep(0.5)
        except: pass

    if not ctx.author.voice:
        return await ctx.respond("ボイスチャンネルに参加していません。", ephemeral=True)

    try:
        vc = await ctx.author.voice.channel.connect()
        await ctx.guild.change_voice_state(channel=vc.channel, self_deaf=True)
        await asyncio.sleep(1.0)
        vc.play(Silence())
        await ctx.respond(f"**{vc.channel.name}** に参加しました。")
    except Exception as e:
        await ctx.respond(f"接続エラー: {e}", ephemeral=True)

@bot.slash_command(description="翻訳を開始します（テスト運用中）")
async def start(ctx):
    await ctx.defer()
    vc = ctx.guild.voice_client

    if not vc or not vc.is_connected():
        return await ctx.respond("先に `/join` でBotをVCに参加させてください。", ephemeral=True)
    
    if ctx.guild.id in active_sinks:
        return await ctx.respond("既に開始されています。", ephemeral=True)

    try:
        sink = AutoTranslateSink(vc=vc, target_channel=ctx.channel)
        vc.start_recording(sink, finished_callback)
        active_sinks[ctx.guild.id] = sink
        
        # テスト運用中の免責メッセージ
        msg = (
            "🔴 **翻訳を開始しました**\n"
            "※現在テスト運用中のため、API制限により予告なく停止したり、応答が遅れる場合があります。\n"
            "※`/set_language` で自分の言語を設定していないユーザーの声は無視されます。"
        )
        await ctx.respond(msg)
        
    except Exception as e:
        print(f"Start error: {e}")
        await ctx.respond(f"開始エラー: {e}", ephemeral=True)

async def finished_callback(sink, *args):
    sink.stop()

@bot.slash_command(description="リアルタイム翻訳を停止します。")
async def stop(ctx):
    vc = ctx.guild.voice_client
    if not vc:
        return await ctx.respond("接続していません。", ephemeral=True)

    if ctx.guild.id in active_sinks:
        vc.stop_recording()
        active_sinks.pop(ctx.guild.id, None)
        await ctx.respond("翻訳を停止しました。", ephemeral=True)
    else:
        await ctx.respond("翻訳は実行されていません。", ephemeral=True)

@bot.slash_command(description="切断します。")
async def leave(ctx):
    vc = ctx.guild.voice_client
    if not vc:
        return await ctx.respond("接続していません。", ephemeral=True)
    
    if ctx.guild.id in active_sinks:
        vc.stop_recording()
        active_sinks.pop(ctx.guild.id, None)
    
    await vc.disconnect(force=True)
    await ctx.respond("切断しました。")

@bot.slash_command(description="あなたの言語設定を行います。")
async def set_language(
    ctx: discord.ApplicationContext, 
    your_language: discord.Option(str, "話す言語", choices=[
        discord.OptionChoice(name="Japanese", value="ja"),
        discord.OptionChoice(name="Korean", value="ko"),
        discord.OptionChoice(name="English", value="en"),
    ]),
    translate_to: discord.Option(str, "翻訳先（なしも可）", choices=[
        discord.OptionChoice(name="Japanese", value="ja"),
        discord.OptionChoice(name="Korean", value="ko"),
        discord.OptionChoice(name="English", value="en"),
        discord.OptionChoice(name="なし(None)", value="none"),
    ])
):
    user_id = ctx.author.id
    source_lang = your_language.lower()
    target_lang = translate_to.lower()

    if source_lang == target_lang:
        target_lang = "none"

    # DBに保存
    save_user_setting(user_id, source_lang, target_lang if target_lang != "none" else None)

    source_name = langcodes.Language.get(source_lang).language_name('ja')
    msg = f"✅ 設定完了: **{source_name}** で話します。"
    
    if target_lang != "none":
        target_name = langcodes.Language.get(target_lang).language_name('ja')
        msg += f"\n➡️ **{target_name}** に翻訳します。"
    
    await ctx.respond(msg, ephemeral=True)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    # ステータスを更新してテスト中であることをアピール
    await bot.change_presence(activity=discord.Game(name="Test Run | /start"))

if __name__ == "__main__":
    bot.run(token)