import discord
import os
from dotenv import load_dotenv
import json
import io
import asyncio
import wave
from faster_whisper import WhisperModel
import langcodes
from google import genai
import time
from collections import defaultdict
import webrtcvad
import traceback
import audioop

# .envやIntents、JSON関連の関数は変更なし
load_dotenv()
token = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True
bot = discord.Bot(intents=intents)

USER_LANGUAGES_FILE = "user_languages.json"

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
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_languages(data):
    with open(USER_LANGUAGES_FILE, 'w') as f:
        json.dump(data, f, indent=4)

user_languages = load_languages()
active_recordings = {}
active_sinks = {}

# モデルとAPIの初期化
print("Whisperモデルをロード中...")
whisper_model = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
print("Whisperモデルのロード完了。")

if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)
    print("Gemini API Clientの初期化完了。")
else:
    client = None
    print("警告: GEMINI_API_KEYが設定されていないため、翻訳機能は無効です。")

# 無音オーディオソース
class Silence(discord.AudioSource):
    def read(self):
        return b'\x00\x00\x00\x00'

class AutoTranslateSink(discord.sinks.Sink):
    SPEECH_END_THRESHOLD_S = 2.5
    MIN_SPEECH_DURATION_S = 2.0
    CHECK_INTERVAL_S = 0.2

    def __init__(self, vc: discord.VoiceClient, target_channel: discord.TextChannel):
        super().__init__()
        self.vc = vc
        self.target_channel = target_channel
        self.vad = webrtcvad.Vad(3) # 0,1,2,3の4段階。3が最もノイズを除去する
        self.speech_buffers = defaultdict(io.BytesIO)
        self.last_activity_time = defaultdict(float)
        self.is_speaking_map = defaultdict(bool)
        self.checker_task = bot.loop.create_task(self.check_for_silence())

    def write(self, data: bytes, user_id: int):
        if user_id not in user_languages:
            return
        if not self.is_speaking_map.get(user_id):
            self.is_speaking_map[user_id] = True
            print(f"[INFO] User {user_id} started speaking.")
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
                time_since_last_audio = current_time - self.last_activity_time[user_id]
                if time_since_last_audio > self.SPEECH_END_THRESHOLD_S:
                    print(f"[INFO] Speech ended for user {user_id} ({time_since_last_audio:.2f}s silence). Processing audio.")
                    self.is_speaking_map[user_id] = False
                    buffer = self.speech_buffers.pop(user_id, None)
                    if buffer:
                        asyncio.create_task(self.process_user_audio(user_id, buffer))

    def stop(self):
        print("[INFO] Stopping sink and cleaning up...")
        if self.checker_task:
            self.checker_task.cancel()
        for user_id, buffer in list(self.speech_buffers.items()):
            print(f"[INFO] Processing leftover buffer for user {user_id}")
            asyncio.create_task(self.process_user_audio(user_id, buffer))
        self.speech_buffers.clear()

    def _stereo_to_mono(self, stereo_bytes: bytes) -> bytes:
        try:
            return audioop.tomono(stereo_bytes, 2, 1, 1)
        except audioop.error as e:
            print(f"[ERROR] Audioop error: {e}")
            return b''

    def _calculate_speech_duration_ms(self, pcm_data: bytes, sample_rate: int, frame_duration_ms: int) -> int:
        frame_size = (sample_rate * frame_duration_ms // 1000) * 2
        speech_frames = 0
        for i in range(0, len(pcm_data), frame_size):
            frame = pcm_data[i:i+frame_size]
            if len(frame) < frame_size:
                continue
            try:
                if self.vad.is_speech(frame, sample_rate):
                    speech_frames += 1
            except Exception:
                pass
        return speech_frames * frame_duration_ms
    
    async def process_user_audio(self, user_id: int, audio_buffer: io.BytesIO):
        try:
            # ▼▼▼ 変更点 1: ユーザーの言語設定を取得 ▼▼▼
            user_setting = user_languages.get(user_id)
            if not user_setting or "source" not in user_setting:
                # 設定がない、またはsourceが未設定のユーザーは処理しない
                return

            source_lang_code = user_setting["source"]
            target_lang_code = user_setting.get("target") # targetはなくても良い

            audio_buffer.seek(0)
            original_stereo_bytes = audio_buffer.read()
            audio_buffer.close()

            mono_audio_bytes = self._stereo_to_mono(original_stereo_bytes)
            if not mono_audio_bytes: return

            speech_duration_s = self._calculate_speech_duration_ms(mono_audio_bytes, 48000, 30) / 1000.0
            if speech_duration_s < self.MIN_SPEECH_DURATION_S:
                print(f"[INFO] Discarding audio from user {user_id} due to short speech duration ({speech_duration_s:.2f}s).")
                return

            wav_data = io.BytesIO()
            with wave.open(wav_data, 'wb') as f:
                f.setnchannels(2); f.setsampwidth(2); f.setframerate(48000); f.writeframes(original_stereo_bytes)
            wav_data.seek(0)
            
            # ▼▼▼ 変更点 2: `language`引数で文字起こし言語を強制指定 ▼▼▼
            print(f"[PROCESS] Transcribing for user {user_id} in language: '{source_lang_code}'")
            segments, info = await bot.loop.run_in_executor(
                None, 
                lambda: whisper_model.transcribe(
                    wav_data, 
                    beam_size=5, 
                    initial_prompt=VALORANT_PROMPT,
                    language=source_lang_code  # <-- ここが最重要！
                )
            )
            
            original_text = " ".join([segment.text for segment in segments]).strip()
            if not original_text: return

            user = await bot.fetch_user(user_id)
            embed = discord.Embed(description=f"**発言者:** {user.mention}", color=user.accent_color or discord.Colour.blue())
            embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
            
            source_lang_name = langcodes.Language.get(source_lang_code).language_name('ja')
            embed.add_field(name=f"原文 ({source_lang_name})", value=f"```{original_text}```", inline=False)
            
            # ▼▼▼ 変更点 3: 翻訳ロジックを新しい設定に対応 ▼▼▼
            if client and target_lang_code and target_lang_code != source_lang_code:
                lang_code, translated_text = await self.translate_text(original_text, source_lang_code, target_lang_code)
                if translated_text:
                    target_lang_name = langcodes.Language.get(lang_code).language_name('ja')
                    embed.add_field(name=f"翻訳 ({target_lang_name})", value=f"```{translated_text}```", inline=False)

            await self.target_channel.send(embed=embed)
        
        except Exception as e:
            print(f"Error in process_user_audio for user {user_id}: {e}"); traceback.print_exc()

    async def translate_text(self, text, source_lang, target_lang):
        # (このメソッド自体は変更なし、呼び出し元が変わっただけ)
        if not client: return target_lang, None
        try:
            source_lang_name_en = langcodes.Language.get(source_lang).language_name('en')
            target_lang_name_en = langcodes.Language.get(target_lang).language_name('en')
            prompt = f"Translate the following text from {source_lang_name_en} to {target_lang_name_en}. Respond ONLY with the translated text...\n\nOriginal text:\n\"\"\"\n{text}\n\"\"\""
            response = await client.aio.models.generate_content(model="gemma-3-27b-it", contents=prompt)
            response_text = response.text.strip()
            if '"error"' in response_text and '"code"' in response_text:
                print(f"API error: {response_text}")
                return target_lang, "APIでエラーが発生しました。"
            return target_lang, response_text
        except Exception as e:
            print(f"API exception: {e}")
            return target_lang, "API接続エラー。"


async def finished_callback(sink: AutoTranslateSink, *args):
    """レコーディングが終了したときに呼び出される"""
    print("Recording finished. Calling sink.stop().")
    sink.stop()
    # 変更点: active_sinksからの削除はコマンド側で行うため、ここでは何もしない

# Botイベント
@bot.event
async def on_ready():
    print("Bot is ready.")
    print(f"{len(user_languages)}件のユーザー言語設定をロードしました。")
    print("起動時のボイス接続チェックを実行中...")
    for guild in bot.guilds:
        if guild.voice_client:
            print(f"サーバー「{guild.name}」でゾンビ接続を発見。強制切断します。")
            await guild.voice_client.disconnect(force=True)
            # 変更点: 状態管理変数の名前に合わせる
            active_sinks.pop(guild.id, None)

# 追加: 予期せぬ切断時のクリーンアップ処理
@bot.event
async def on_voice_state_update(member, before, after):
    # ボット自身が切断された場合のみ処理
    if member.id != bot.user.id or not before.channel or after.channel:
        return

    guild_id = before.channel.guild.id
    print(f"BotがサーバーID {guild_id} のVCから切断されました。クリーンアップを実行します。")
    
    # 録音中だった場合は、Sinkを停止して後処理を行う
    if guild_id in active_sinks:
        sink = active_sinks.pop(guild_id)
        sink.stop()
        print(f"サーバーID {guild_id} のアクティブなSinkを停止・クリーンアップしました。")


# --- コマンドの再編成 ---

@bot.slash_command(description="ボイスチャンネルに参加します。")
async def join(ctx: discord.ApplicationContext):
    if not ctx.author.voice:
        return await ctx.respond("エラー: あなたがボイスチャンネルに参加していません。", ephemeral=True)
    
    if ctx.guild.voice_client:
        return await ctx.respond("既にボイスチャンネルに接続しています。", ephemeral=True)

    try:
        vc = await ctx.author.voice.channel.connect()
        # タイムアウト防止と、ボットが話さないようにするための設定
        await vc.guild.change_voice_state(channel=vc.channel, self_mute=True)
        # vc.play(Silence()) 
        await ctx.respond(f"**{vc.channel.name}** に参加しました。\n`/start` コマンドで翻訳を開始できます。", ephemeral=True)
    except Exception as e:
        print(f"Error in join command: {e}")
        traceback.print_exc()
        await ctx.respond(f"ボイスチャンネルへの接続中にエラーが発生しました: {e}", ephemeral=True)

@bot.slash_command(description="リアルタイム翻訳を開始します。")
async def start(ctx: discord.ApplicationContext):
    vc = ctx.guild.voice_client
    global user_languages
    user_languages = load_languages()  # 最新のユーザー言語設定をロード
    if not vc:
        return await ctx.respond("エラー: まず`/join`コマンドでボイスチャンネルに参加させてください。", ephemeral=True)

    if ctx.guild.id in active_sinks:
        return await ctx.respond("既にこのサーバーで翻訳が開始されています。", ephemeral=True)

    try:
        # 録音を開始
        sink = AutoTranslateSink(vc=vc, target_channel=ctx.channel)
        vc.start_recording(sink, finished_callback)
        active_sinks[ctx.guild.id] = sink
        
        print(f"Recording started in guild {ctx.guild.id} for channel {ctx.channel.name}")
        await ctx.respond(f"このチャンネルへのリアルタイム翻訳を開始しました。\n`/stop` コマンドで停止できます。", ephemeral=True)
        
    except Exception as e:
        print(f"Error in start command: {e}")
        traceback.print_exc()
        await ctx.respond(f"翻訳の開始中にエラーが発生しました: {e}", ephemeral=True)


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


if __name__ == "__main__":
    bot.run(token)