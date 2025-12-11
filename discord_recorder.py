import discord
import os
from dotenv import load_dotenv
import json
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

# ▼▼▼ 追加: Groqライブラリ ▼▼▼
from groq import Groq

load_dotenv()
token = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY") # .envに GROQ_API_KEY を追加してください

intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True
bot = discord.Bot(intents=intents)

USER_LANGUAGES_FILE = "user_languages.json"

# GroqのWhisperにもプロンプトとして渡せます
VALORANT_PROMPT = (
    "VALORANT,ヴァロラント,ジェット,レイズ,オーメン,セージ,サイファー,ヴァイパー,ブリーチ,ブリムストーン,フェニックス,レイナ,キルジョイ,スカイ,ソーヴァ,アストラ"
    "Jett,Raze,Omen,Sage,Cypher,Viper,Breach,Brimstone,Phoenix,Reyna,Killjoy,Skye,Sova,Astra"
    "Aサイト,Bサイト,ヘイブン,アセント,バインド,スプリット,ロータス,サンセット,スパイク,設置,解除,ピーク,エントリー,リテイク,ULT,アルティメット,"
    "제트,레이즈,오멘,세이지,사이퍼,브림스톤,피닉스,레이나,킬조이,스카이,소바,바인드,헤이븐,스플릿,어센트,로터스,선셋,아스트라"
    "스파이크,설치,해체,피킹,엔트리,리테이크,궁극기,궁"
)

def load_languages():
    try:
        with open(USER_LANGUAGES_FILE, 'r') as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except:
        return {}

def save_languages(data):
    with open(USER_LANGUAGES_FILE, 'w') as f:
        json.dump(data, f, indent=4)

user_languages = load_languages()
active_sinks = {}

# ▼▼▼ クライアント初期化 ▼▼▼
if GEMINI_API_KEY:
    # 翻訳用 (Gemini 2.5 Flash など)
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    print("Gemini API Client (Translation) initialized.")
else:
    gemini_client = None

if GROQ_API_KEY:
    # 文字起こし用 (Whisper Large V3)
    groq_client = Groq(api_key=GROQ_API_KEY)
    print("Groq API Client (Whisper) initialized.")
else:
    groq_client = None
    print("警告: GROQ_API_KEYが設定されていません。")

class Silence(discord.AudioSource):
    def read(self):
        return b'\x00' * 3840

class AutoTranslateSink(discord.sinks.Sink):
    SPEECH_END_THRESHOLD_S = 1.5 # 少し短くしてもGroqなら速いのでOK
    MIN_SPEECH_DURATION_S = 1.5
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
        if user_id not in user_languages:
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
        """音声が小さすぎる場合はTrue"""
        try:
            rms = audioop.rms(audio_bytes, 2)  # 2 = sample width
            return rms < threshold
        except:
            return True

    async def process_user_audio(self, user_id: int, audio_buffer: io.BytesIO):
        if not groq_client: return

        try:
            user_setting = user_languages.get(user_id)
            if not user_setting or "source" not in user_setting:
                return

            source_lang_code = user_setting["source"]
            target_lang_code = user_setting.get("target")

            audio_buffer.seek(0)
            original_stereo_bytes = audio_buffer.read()
            audio_buffer.close()

            mono_bytes = self._stereo_to_mono(original_stereo_bytes)
            if not mono_bytes: return

            # 音量チェックを追加
            if self._is_audio_too_quiet(mono_bytes, threshold=500):
                # print(f"[SKIP] Audio too quiet for user {user_id}")
                return

            # 48000Hz (Sample Rate) * 2 bytes/sample (16-bit) = 96000 bytes/second (モノラル)
            bytes_per_second = 48000 * 2
            speech_duration_s = len(mono_bytes) / bytes_per_second
            
            if speech_duration_s < self.MIN_SPEECH_DURATION_S:
                # print(f"[SKIP] Audio too short ({speech_duration_s:.2f}s) for user {user_id}. Min: {self.MIN_SPEECH_DURATION_S}s.")
                return 

            # ここでWAVファイルを作成 (Groqに送るため)
            wav_buffer = io.BytesIO()

            with wave.open(wav_buffer, 'wb') as f:
                f.setnchannels(1) # WhisperはモノラルでOK
                f.setsampwidth(2)
                f.setframerate(48000)
                f.writeframes(mono_bytes)
            
            wav_bytes = wav_buffer.getvalue()
            # Groq APIはファイル名が必要なため、ダミーの名前をつける
            wav_buffer.name = "audio.wav"
            wav_buffer.seek(0)

            print(f"[PROCESS] Sending audio to Groq (Whisper) for user {user_id}...")

            # --- 1. Groqで文字起こし (STT) ---
            # run_in_executorで非同期コンテキストをブロックしないように実行
            def transcribe_sync():
                return groq_client.audio.transcriptions.create(
                    file=("audio.wav", wav_bytes), # バイナリを直接渡す
                    model="whisper-large-v3-turbo",
                    prompt=VALORANT_PROMPT,
                    language=source_lang_code,
                    response_format="json",
                    temperature=0.2,
                )

            transcription = await bot.loop.run_in_executor(None, transcribe_sync)
            original_text = transcription.text.strip()
            
            if not original_text: return

            # --- 結果表示の準備 ---
            user = await bot.fetch_user(user_id)
            embed = discord.Embed(description=f"**発言者:** {user.mention}", color=user.accent_color or discord.Colour.blue())
            embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
            
            source_lang_name = langcodes.Language.get(source_lang_code).language_name('ja')
            embed.add_field(name=f"原文 ({source_lang_name})", value=f"```{original_text}```", inline=False)

            # --- 2. Geminiで翻訳 (Translation) ---
            if gemini_client and target_lang_code and target_lang_code != "none" and target_lang_code != source_lang_code:
                
                target_lang_name = langcodes.Language.get(target_lang_code).language_name('ja')
                
                # 音声ではなく「テキスト」を送るので、制限に引っかかりにくい
                prompt = (
                    f"Translate the following text from {source_lang_name} to {target_lang_name}. "
                    f"Output ONLY the translated text.\n\nText:\n{original_text}"
                )
                
                # 軽量なモデルを使用
                response = await gemini_client.aio.models.generate_content(
                    model="gemma-3-27b-it", 
                    contents=prompt
                )
                
                translated_text = response.text.strip()
                if translated_text:
                    embed.add_field(name=f"翻訳 ({target_lang_name})", value=f"```{translated_text}```", inline=False)

            await self.target_channel.send(embed=embed)

        except Exception as e:
            print(f"Error for user {user_id}: {e}")
            traceback.print_exc()

# --- コマンド周り (前回の修正済みバージョンを使用) ---

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
        
        if vc.is_connected():
            vc.play(Silence())
            await ctx.respond(f"**{vc.channel.name}** に参加しました。\n`/start` で翻訳を開始できます。")
        else:
            await ctx.respond("接続タイムアウト。", ephemeral=True)
            
    except Exception as e:
        print(f"Join error: {e}")
        await ctx.respond(f"接続エラー: {e}", ephemeral=True)

@bot.slash_command(description="開始")
async def start(ctx):
    await ctx.defer()
    vc = ctx.guild.voice_client
    global user_languages
    user_languages = load_languages()

    if not vc or not vc.is_connected():
        return await ctx.respond("`/join` してください。", ephemeral=True)
    
    if ctx.guild.id in active_sinks:
        return await ctx.respond("既に開始されています。", ephemeral=True)

    try:
        sink = AutoTranslateSink(vc=vc, target_channel=ctx.channel)
        vc.start_recording(sink, finished_callback)
        active_sinks[ctx.guild.id] = sink
        await ctx.respond("翻訳を開始しました。", ephemeral=True)
    except Exception as e:
        print(f"Start error: {e}")
        await ctx.respond(f"開始エラー: {e}", ephemeral=True)

async def finished_callback(sink, *args):
    sink.stop()
    # コマンド側でのクリーンアップはleave/stopに任せる

@bot.slash_command(description="リアルタイム翻訳を停止します。")
async def stop(ctx: discord.ApplicationContext):
    vc = ctx.guild.voice_client
    if not vc:
        return await ctx.respond("ボイスチャンネルに接続していません。", ephemeral=True)

    if ctx.guild.id not in active_sinks:
        return await ctx.respond("翻訳は現在実行されていません。", ephemeral=True)

    try:
        vc.stop_recording()
        active_sinks.pop(ctx.guild.id, None)
        await ctx.respond("リアルタイム翻訳を停止しました。\nボイスチャンネルには接続したままです。", ephemeral=True)
    except Exception as e:
        print(f"Error in stop command: {e}")
        traceback.print_exc()
        await ctx.respond(f"翻訳の停止中にエラーが発生しました: {e}", ephemeral=True)


@bot.slash_command(description="ボイスチャンネルから切断します。")
async def leave(ctx: discord.ApplicationContext):
    vc = ctx.guild.voice_client
    if not vc:
        return await ctx.respond("ボイスチャンネルに接続していません。", ephemeral=True)
    
    try:
        # もし録音中なら停止する
        if ctx.guild.id in active_sinks:
            vc.stop_recording()
            active_sinks.pop(ctx.guild.id, None)
        
        await vc.disconnect(force=True)
        await ctx.respond("ボイスチャンネルから切断しました。")
        
    except Exception as e:
        print(f"Error in leave command: {e}")
        traceback.print_exc()
        await ctx.respond(f"切断中にエラーが発生しました: {e}", ephemeral=True)


@bot.slash_command(description="あなたの話す言語と、希望する翻訳先言語を設定します。")
async def set_language(
    ctx: discord.ApplicationContext, 
    your_language: discord.Option(
        str,
        "あなたが主に話す言語（文字起こしに使われます）",
        choices=[
            discord.OptionChoice(name="Japanese", value="ja"),
            discord.OptionChoice(name="Korean", value="ko"),
        ]
    ),
    translate_to: discord.Option(
        str,
        "どの言語に翻訳してほしいですか？（「なし」も選べます）",
        choices=[
            discord.OptionChoice(name="Japanese", value="ja"),
            discord.OptionChoice(name="Korean", value="ko"),
        ]
    )
):
    global user_languages
    user_id = ctx.author.id
    
    source_lang = your_language.lower()
    target_lang = translate_to.lower()

    if source_lang == target_lang:
        target_lang = "none" # 話す言語と翻訳先が同じなら、翻訳は不要

    # 新しい形式で設定を保存
    user_languages[user_id] = {
        "source": source_lang,
        "target": target_lang if target_lang != "none" else None
    }
    save_languages(user_languages)

    source_name = langcodes.Language.get(source_lang).language_name('ja')
    
    if target_lang == "none" or target_lang is None:
        response_message = f"設定を更新しました。\n- あなたの発言は **{source_name}**として文字起こしされます。\n- 翻訳は**行われません**。"
    else:
        target_name = langcodes.Language.get(target_lang).language_name('ja')
        response_message = f"設定を更新しました。\n- あなたの発言は **{source_name}** として文字起こしされます。\n- 翻訳先は **{target_name}** です。"

    await ctx.respond(response_message, ephemeral=True)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

# stop, leave, set_language コマンドは
# 以前のコード（Turn 1 または Turn 5 の内容）をそのまま使ってください。

if __name__ == "__main__":
    bot.run(token)