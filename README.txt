LK Bot - セットアップ（Windows）

1. Python 3.10以降をインストール
2. このフォルダで PowerShell を開き、次を実行
   py -3 -m pip install -r requirements.txt
3. Discord Developer Portalでアプリを作成しBotトークンを取得
4. Bot > Privileged Gateway Intents > MESSAGE CONTENT INTENTを有効化
5. Installationでbot + applications.commandsスコープを選びサーバーに導入
   Bot権限: View Channels / Send Messages / Read Message History
6. PowerShellで次を実行（トークンは公開しない）
   $env:DISCORD_TOKEN = 'ここにBotトークン'
   py -3 lk_bot.py

コマンド:
/lk                  ランダム発言
/lk_add word:文字列   語録追加（「サーバーの管理」権限）
/lk_remove word:文字列 語録削除（同上）
/lk_list page:1       語録一覧

保存先: lk_words.json（サーバー初回認識時に初期語録14件を自動生成）
一度登録したサーバーの語録は、再起動時に初期化・上書きされません。
初期語録を変更する場合は /lk_add と /lk_remove を使用してください。

注意: スラッシュコマンドが反映されるまで時間がかかる場合があります。
全ユーザーのメッセージ本文の検出には、DiscordのMessage Content Intentが必要です。
監視できるのはBotが閲覧できるチャンネル内の発言だけです。
履歴メッセージの分析機能は本バージョンには含まれていません。
