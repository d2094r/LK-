import json
import logging
import os
import random
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv('DISCORD_TOKEN')
DATA_FILE = Path(__file__).with_name('lk_words.json')
MAX_WORD_LENGTH = 100
PAGE_SIZE = 15

INITIAL_WORDS = ['は～い～ざ～い～', '死ね', 'しね', 'し、ね', '黙れクソ馬', '黙れ音Mader', 'はいはい', 'もうこいつに社員車撮らせない', 'X67', 'Roblox', 'ロブロックス', 'INS', '円明寺は床下切るのが適正ですよ…', 'ちんちん']

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('lk_bot')


def load_data():
    if not DATA_FILE.exists():
        return {}
    try:
        with DATA_FILE.open('r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError('保存データが辞書形式ではありません')
        return data
    except (OSError, json.JSONDecodeError, ValueError):
        logger.exception('語録ファイルを読み込めません。修復するまで起動を停止します。')
        raise


def save_data():
    # 一時ファイルに書いてから置き換え、書き込み途中での破損を抑えます。
    fd, tmp_name = tempfile.mkstemp(prefix='.lk_words_', suffix='.tmp', dir=DATA_FILE.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_name, DATA_FILE)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


data = load_data()


def get_words(guild_id: int):
    return data.get(str(guild_id), [])


def initialize_guild(guild_id: int):
    key = str(guild_id)
    if key not in data:
        data[key] = INITIAL_WORDS.copy()
        save_data()
        logger.info('サーバー %s に初期語録 %d 件を登録しました', guild_id, len(INITIAL_WORDS))


intents = discord.Intents.default()
intents.message_content = True


class LKBot(commands.Bot):
    async def setup_hook(self):
        synced = await self.tree.sync()
        logger.info('%d 個のスラッシュコマンドを同期しました', len(synced))


bot = LKBot(command_prefix='!', intents=intents)


@bot.event
async def on_ready():
    logger.info('ログイン完了: %s (ID: %s)', bot.user, bot.user.id)
    for guild in bot.guilds:
        initialize_guild(guild.id)


@bot.event
async def on_guild_join(guild: discord.Guild):
    initialize_guild(guild.id)


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or message.author.bot or message.webhook_id is not None:
        return
    content = message.content.casefold()
    if not content:
        return
    if any(word.casefold() in content for word in get_words(message.guild.id)):
        try:
            await message.reply('LK語録を検出しました！', mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            logger.warning('返信権限がありません: チャンネルID %s', message.channel.id)
        except discord.HTTPException:
            logger.exception('検出通知の送信に失敗しました')


@bot.tree.command(name='lk', description='登録されたLK語録をランダムに発言します')
@app_commands.guild_only()
async def lk(interaction: discord.Interaction):
    items = get_words(interaction.guild_id)
    if not items:
        await interaction.response.send_message('LK語録はまだ登録されていません。', ephemeral=True)
        return
    await interaction.response.send_message(random.choice(items),
                                            allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='lk_add', description='LK語録を追加します（サーバー管理権限が必要）')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(word='新しく登録するLK語録（100文字以内）')
async def lk_add(interaction: discord.Interaction, word: str):
    word = word.strip()
    if not word or len(word) > MAX_WORD_LENGTH:
        await interaction.response.send_message('1～100文字の語録を指定してください。', ephemeral=True)
        return
    items = get_words(interaction.guild_id)
    if any(old.casefold() == word.casefold() for old in items):
        await interaction.response.send_message('その語録はすでに登録されています。', ephemeral=True)
        return
    data[str(interaction.guild_id)] = [*items, word]
    save_data()
    await interaction.response.send_message(f'LK語録を追加しました：{word}', ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='lk_remove', description='LK語録を削除します（サーバー管理権限が必要）')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(word='削除する語録（登録時と同じ文字列）')
async def lk_remove(interaction: discord.Interaction, word: str):
    items = get_words(interaction.guild_id)
    target = next((item for item in items if item.casefold() == word.strip().casefold()), None)
    if target is None:
        await interaction.response.send_message('指定した語録は登録されていません。', ephemeral=True)
        return
    data[str(interaction.guild_id)] = [item for item in items if item != target]
    save_data()
    await interaction.response.send_message(f'LK語録を削除しました：{target}', ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='lk_list', description='登録済みLK語録の一覧を表示します')
@app_commands.guild_only()
@app_commands.describe(page='表示するページ（省略時は1）')
async def lk_list(interaction: discord.Interaction, page: int = 1):
    items = get_words(interaction.guild_id)
    if not items:
        await interaction.response.send_message('LK語録はまだ登録されていません。', ephemeral=True)
        return
    pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE
    if page < 1 or page > pages:
        await interaction.response.send_message(f'ページは1～{pages}を指定してください。', ephemeral=True)
        return
    start = (page - 1) * PAGE_SIZE
    lines = [f'{i + 1}. {item}' for i, item in enumerate(items[start:start + PAGE_SIZE], start)]
    result = f'【LK語録一覧】{len(items)}件（{page}/{pages}ページ）\n' + '\n'.join(lines)
    # 100文字の語録15件でもDiscordの1メッセージ文字数制限内に収まります。
    await interaction.response.send_message(result, ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = 'この操作には「サーバーの管理」権限が必要です。'
    else:
        logger.error('コマンド実行エラー: %r', error)
        msg = '処理中にエラーが発生しました。Botのログを確認してください。'
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == '__main__':
    if not TOKEN:
        raise SystemExit('環境変数 DISCORD_TOKEN にBotトークンを設定してください。')
    bot.run(TOKEN)
