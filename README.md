# Discordリアルタイム音声翻訳Bot (Discord Real-Time Voice Translator Bot)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Discordのボイスチャンネルでの会話をリアルタイムで文字起こしし、指定された言語へ翻訳してテキストチャンネルに投稿するBotです。国際的な友人とのゲームプレイや、多言語でのコミュニケーションをサポートします。

This is a Discord bot that performs real-time transcription and translation of conversations in a voice channel, posting the results to a text channel. It's designed to support multilingual communication, such as gaming with international friends.

## ✨ 主な機能 (Features)

*   **高精度なリアルタイム文字起こし**: `faster-whisper (turbo)` モデルを使用し、高速かつ高精度に音声をテキスト化します。
*   **多言語へのリアルタイム翻訳**: Googleの `Gemini API` を利用して、文字起こしされたテキストを瞬時に翻訳します。
*   **VADによる無音検出**: VAD (Voice Activity Detection) 技術により、発言の切れ目をインテリジェントに検出し、適切な単位で処理を実行します。
*   **ユーザーごとの言語設定**: ユーザー一人ひとりが自分の話す言語（文字起こし元）と翻訳してほしい言語（翻訳先）を個別に設定できます。
*   **簡単な操作**: すべての操作は直感的なスラッシュコマンド (`/`) で行えます。
*   **VALORANT用語に特化**: プロンプトにVALORANTの専門用語を含めることで、ゲーム内のコミュニケーションの認識精度を向上させています。

## 🛠️ 使用技術 (Tech Stack)

*   **Backend**: Python 3.8+
*   **Discord API Wrapper**: [Py-cord](https://pycord.dev/)
*   **Speech-to-Text**: [faster-whisper](https://github.com/guillaumekln/faster-whisper)
*   **Translation**: [Google Gemini API](https://ai.google.dev/)
*   **Voice Activity Detection**: [webrtcvad](https://github.com/wiseman/py-webrtcvad)

---

## 🚀 セットアップガイド (Setup Guide)

このBotの性能を最大限に引き出すには、**NVIDIA製GPU**の使用が不可欠です。

### 1. 前提条件 (Prerequisites)

*   **Python 3.8** 以上
*   **Git**
*   **NVIDIA GPU** と最新のグラフィックドライバ
*   **[CUDA Toolkit](https://developer.nvidia.com/cuda-toolkit-archive)** (`faster-whisper`が対応するバージョン)

### 2. インストール手順 (Installation)

1.  **リポジトリをクローンします。**
    ```bash
    git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY_NAME.git
    cd YOUR_REPOSITORY_NAME
    ```

2.  **【最重要】PyTorchをGPU対応版でインストールします。**

    ⚠️ **このステップはあなたのGPU環境に依存するため、手動で行う必要があります。**

    まず、以下のPyTorch公式サイトにアクセスしてください。
    👉 **[PyTorch公式サイト - Get Started](https://pytorch.org/get-started/locally/)**

    サイトで、お使いの環境（OS, パッケージマネージャ `pip`, Compute Platform `CUDA`）を選択し、表示されたコマンドをコピーして実行してください。これにより、あなたのGPUに最適化されたPyTorchがインストールされます。

    **(例) CUDA 12.1 環境の場合:**
    ```bash
    pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
    ```
    > **Note:** この手順をスキップしたり、`requirements.txt` を先にインストールしたりすると、BotはCPUで動作し、著しくパフォーマンスが低下します。

3.  **必要なライブラリをインストールします。**

    PyTorchのインストールが完了したら、残りのライブラリを`requirements.txt`からインストールします。
    ```bash
    pip install -r requirements.txt
    ```

4.  **環境変数を設定します。**

    .envファイルを作成し、DISCORD_TOKENとGEMINI_API_KEYを記述します。
    ```env
    # .env

    # あなたのDiscord Botのトークン
    DISCORD_TOKEN="YOUR_DISCORD_BOT_TOKEN_HERE"

    # Google AI Studioで取得したGemini APIキー
    GEMINI_API_KEY="YOUR_GEMINI_API_KEY_HERE"
    ```

5.  **Botの起動 (Running the Bot)**

    すべての設定が完了したら、以下のコマンドでBotを起動します。
    ```bash
    python discord_recorder.py
    ```

## 📝 使い方 (How to Use)

Botの操作はすべてスラッシュコマンドで行います。

- `/join`
  あなたが参加しているボイスチャンネルにBotを参加させます。

- `/start`
  リアルタイムの文字起こしと翻訳を開始します。結果はこのコマンドを実行したテキストチャンネルに投稿されます。

- `/stop`
  リアルタイム処理を停止します。Botはボイスチャンネルに接続したままです。

- `/leave`
  Botをボイスチャンネルから切断させます。

- `/set_language [your_language] [translate_to]`
  あなたの言語設定を行います。
  - `your_language`: あなたが話す言語（文字起こしの精度に影響します）。
  - `translate_to`: どの言語に翻訳してほしいか。翻訳が不要な場合は、`your_language` と同じ言語を選ぶか、なしに相当する選択肢を選びます。

# 📜 ライセンス (License)

このプロジェクトはMITライセンスの下で公開されています。
# 🙏 謝辞 (Acknowledgements)

このプロジェクトは、以下の素晴らしいオープンソースライブラリやサービスなしには実現できませんでした。

- [Py-cord](https.github.com/Pycord-Development/pycord)
- [faster-whisper](https://github.com/guillaumekln/faster-whisper)
- [Google Gemini](https://gemini.google.com/)