"""LK語録Bot：既存の自動検出・/lk に加え、指定ユーザーの発言履歴から頻出語を自動登録。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

from learner import normalize, suggest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lk_bot")

TOKEN = os.getenv("DISCORD_TOKEN", "")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("LK_DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
WORDS_FILE = DATA_DIR / "lk_words.json"
SETTINGS_FILE = DATA_DIR / "lk_settings.json"
INITIAL_FILE = BASE_DIR / "lk_words.json"   # Docker image内の初期語録

INITIAL_WORDS = [
    "は～い～ざ～い～", "死ね", "しね", "し、ね", "黙れクソ馬", "黙れ音Mader",
    "はいはい", "もうこいつに社員車撮らせない", "X67", "Roblox", "ロブロックス",
    "INS", "円明寺は床下切るのが適正ですよ…", "ちんちん",
]

SCAN_DAYS = 180                    # 過去180日間
SCAN_PER_CHANNEL = 800             # チャンネルごとの取得上限（対象者以外も含む）
SCAN_TOTAL_MAX = 12000             # 1サーバーの取得上限
MIN_OCCURRENCES = 4               # 異なる4メッセージ以上で候補に
MAX_AUTO_ADD = 6                  # 1回あたりの自動追加数
MAX_WORDS_PER_SERVER = 250
PAGE_SIZE = 12


def remove_trailing_commas(source: str) -> str:
    """文字列の内部には触れず、配列・オブジェクトの末尾の余分なカンマだけを除去。"""
    result = []
    in_string = False
    escaped = False

    for i, ch in enumerate(source):
        if in_string:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            result.append(ch)
        elif ch == ",":
            j = i + 1
            while j < len(source) and source[j].isspace():
                j += 1
            if j < len(source) and source[j] in "]}":
                continue
            result.append(ch)
        else:
            result.append(ch)

    return "".join(result)


def read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)

    original = path.read_text(encoding="utf-8")
    try:
        result = json.loads(original)
    except json.JSONDecodeError:
        corrected = remove_trailing_commas(original)
        if corrected == original:
            raise  # 余分なカンマ以外のエラーを勝手に変更しない
        result = json.loads(corrected)  # 修正後もJSONとして妥当か必ず検証
        if not isinstance(result, dict):
            raise ValueError(f"{path} の形式が不正です。辞書形式ではありません")

        backup = path.with_name(path.name + ".invalid.bak")
        try:
            if not backup.exists():
                backup.write_text(original, encoding="utf-8")
            # 置換前に完全なJSONを別ファイルへ書き、原本の部分書き込みを防ぐ
            atomic_write(path, result)
            logger.warning("不正な末尾カンマを修復しました: %s（バックアップ: %s）", path, backup)
        except OSError:
            logger.exception("JSONをメモリ上で修復しましたが、ファイルを保存できません: %s", path)

    if not isinstance(result, dict):
        raise ValueError(f"{path} の形式が不正です。保存ファイルを確認してください")
    return result


def atomic_write(path: Path, value: dict):
    fd, temp_name = tempfile.mkstemp(prefix=".lk_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(value, fp, ensure_ascii=False, indent=2)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


# 初めて永続ボリュームを接続した場合、イメージ内の初期データを取り込む。
words = read_json(WORDS_FILE if WORDS_FILE.exists() else INITIAL_FILE, {})
settings = read_json(SETTINGS_FILE, {})
scan_locks: dict[int, asyncio.Lock] = {}


def ensure_guild(guild_id: int):
    key = str(guild_id)
    if key not in words:
        words[key] = INITIAL_WORDS.copy()
        atomic_write(WORDS_FILE, words)
    if key not in settings:
        settings[key] = {"target_user_id": None, "auto_enabled": False, "last_scan_at": None, "blocked_auto_words": []}
        atomic_write(SETTINGS_FILE, settings)


def get_words(guild_id: int) -> list[str]:
    return words.get(str(guild_id), [])


def get_settings(guild_id: int) -> dict:
    ensure_guild(guild_id)
    return settings[str(guild_id)]


def save_words():
    atomic_write(WORDS_FILE, words)


def save_settings():
    atomic_write(SETTINGS_FILE, settings)


intents = discord.Intents.default()
intents.message_content = True


class LKBot(commands.Bot):
    async def setup_hook(self):
        # グローバル同期に加え、すでに登録済みの2サーバーには即時反映用の同期も行う。
        try:
            synced = await self.tree.sync()
            logger.info("グローバルコマンドを %d 件同期しました", len(synced))
        except discord.HTTPException:
            logger.exception("グローバルコマンドの同期に失敗しました")
        for raw_guild_id in words:
            if not raw_guild_id.isdecimal():
                continue
            guild = discord.Object(id=int(raw_guild_id))
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            except (discord.HTTPException, app_commands.CommandSyncFailure):
                logger.warning("ギルド %s のコマンド同期をスキップ", raw_guild_id, exc_info=True)
        if not periodic_learning.is_running():
            periodic_learning.start()


bot = LKBot(command_prefix="!", intents=intents,
            allowed_mentions=discord.AllowedMentions.none())


@bot.event
async def on_ready():
    logger.info("ログイン完了: %s (ID: %s)", bot.user, bot.user.id)
    for guild in bot.guilds:
        ensure_guild(guild.id)


@bot.event
async def on_guild_join(guild: discord.Guild):
    ensure_guild(guild.id)


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None or message.author.bot or message.webhook_id is not None:
        return
    content = normalize(message.content)
    if not content:
        return
    if any(normalize(word) in content for word in get_words(message.guild.id)):
        try:
            await message.reply("LK語録を検出しました！", mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            logger.warning("通知できません（チャンネルID: %s）", message.channel.id)
        except discord.HTTPException:
            logger.exception("LK語録検出通知の送信に失敗")


@dataclass
class ScanReport:
    checked: int
    target_messages: int
    channels: int
    failed_channels: int
    added: list[tuple[str, int]]


async def scan_guild(guild: discord.Guild) -> ScanReport | None:
    """対象者の過去メッセージを解析。チャット本文はディスクに保存しない。"""
    cfg = get_settings(guild.id)
    target_id = cfg.get("target_user_id")
    if not target_id:
        raise ValueError("対象者未設定です。先に /lk_target で対象者を指定してください。")

    lock = scan_locks.setdefault(guild.id, asyncio.Lock())
    if lock.locked():
        return None

    async with lock:
        cutoff = discord.utils.utcnow() - timedelta(days=SCAN_DAYS)
        channels = [*guild.text_channels, *guild.threads]
        # 重複チャンネルを除去。対象は閲覧可能な通常チャンネルとアクティブなスレッド。
        channels = list({channel.id: channel for channel in channels}.values())
        me = guild.me
        sample_texts: list[str] = []
        checked = 0
        readable = 0
        failed = 0
        for channel in channels:
            if checked >= SCAN_TOTAL_MAX:
                break
            if me:
                perms = channel.permissions_for(me)
                if not (perms.view_channel and perms.read_message_history):
                    continue
            try:
                readable += 1
                async for message in channel.history(
                    limit=min(SCAN_PER_CHANNEL, SCAN_TOTAL_MAX - checked),
                    after=cutoff, oldest_first=False
                ):
                    checked += 1
                    if message.author.id == int(target_id) and message.content and not message.author.bot:
                        sample_texts.append(message.content)
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
                logger.warning("履歴を読めないチャンネルがありました: %s", channel.id)

        existing = get_words(guild.id)
        remaining = MAX_WORDS_PER_SERVER - len(existing)
        selected, _ = suggest(sample_texts, [*existing, *cfg.get("blocked_auto_words", [])], min_count=MIN_OCCURRENCES,
                              max_new=max(0, min(MAX_AUTO_ADD, remaining)))
        added = []
        for candidate in selected:
            if normalize(candidate.word) not in {normalize(w) for w in existing}:
                existing.append(candidate.word)
                added.append((candidate.word, candidate.count))
        if added:
            save_words()
        cfg["last_scan_at"] = discord.utils.utcnow().isoformat()
        save_settings()
        return ScanReport(checked, len(sample_texts), readable, failed, added)


def format_scan(report: ScanReport | None, target: int) -> str:
    if report is None:
        return "現在、このサーバーの履歴を分析中です。分析終了後に再度お試しください。"
    intro = (
        f"【LK語録 自動分析】\n対象ユーザーID: {target}\n"
        f"確認した履歴: {report.checked}件 / 対象者の発言: {report.target_messages}件\n"
        f"確認チャンネル: {report.channels}件（読み取り失敗: {report.failed_channels}件）\n"
    )
    if report.added:
        intro += "自動追加: " + str(len(report.added)) + "語\n"
        intro += "\n".join(f"・{word}（{count}件で使用）" for word, count in report.added)
    else:
        intro += "追加0語：条件を満たす未登録の語句がありませんでした。"
    return intro[:1900]


@tasks.loop(hours=12)
async def periodic_learning():
    for guild in bot.guilds:
        cfg = get_settings(guild.id)
        if not cfg.get("auto_enabled") or not cfg.get("target_user_id"):
            continue
        # 手動の分析直後などは無駄な二重取得を避ける。
        last = cfg.get("last_scan_at")
        if last:
            try:
                when = discord.utils.parse_time(last)
                if when and discord.utils.utcnow() - when < timedelta(hours=8):
                    continue
            except (TypeError, ValueError):
                pass
        try:
            report = await scan_guild(guild)
            if report:
                logger.info("ギルド %s 自動学習: %d件の発言から%d語追加", guild.id,
                            report.target_messages, len(report.added))
        except Exception:
            logger.exception("自動学習に失敗: guild=%s", guild.id)


@periodic_learning.before_loop
async def before_periodic_learning():
    await bot.wait_until_ready()


@bot.tree.command(name="lk", description="登録されたLK語録をランダムに1つ発言します")
@app_commands.guild_only()
async def lk(interaction: discord.Interaction):
    items = get_words(interaction.guild_id)
    if not items:
        await interaction.response.send_message("LK語録は登録されていません。", ephemeral=True)
        return
    await interaction.response.send_message(random.choice(items))


@bot.tree.command(name="lk_add", description="LK語録を手動で追加します（サーバー管理者専用）")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def lk_add(interaction: discord.Interaction, word: str):
    word = word.strip()
    if not 1 <= len(word) <= 100:
        await interaction.response.send_message("1～100文字の語録を入力してください。", ephemeral=True)
        return
    items = get_words(interaction.guild_id)
    if len(items) >= MAX_WORDS_PER_SERVER:
        await interaction.response.send_message("登録上限に達しました。", ephemeral=True)
        return
    if any(normalize(x) == normalize(word) for x in items):
        await interaction.response.send_message("すでに登録されています。", ephemeral=True)
        return
    items.append(word)
    save_words()
    # 手動で追加し直した語句は自動学習の除外リストから解除する。
    cfg = get_settings(interaction.guild_id)
    cfg["blocked_auto_words"] = [x for x in cfg.get("blocked_auto_words", []) if x != normalize(word)]
    save_settings()
    await interaction.response.send_message(f"登録しました：{word}", ephemeral=True)


@bot.tree.command(name="lk_remove", description="LK語録を削除します（サーバー管理者専用）")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def lk_remove(interaction: discord.Interaction, word: str):
    items = get_words(interaction.guild_id)
    before = len(items)
    items[:] = [x for x in items if normalize(x) != normalize(word.strip())]
    if len(items) == before:
        await interaction.response.send_message("その語録は見つかりません。", ephemeral=True)
        return
    save_words()
    # 削除した語句が次の自動分析で復活しないよう除外する。
    cfg = get_settings(interaction.guild_id)
    blocked = cfg.setdefault("blocked_auto_words", [])
    candidate = normalize(word)
    if candidate not in blocked:
        blocked.append(candidate)
        save_settings()
    await interaction.response.send_message("削除しました（自動学習による再登録も防止します）。", ephemeral=True)


@bot.tree.command(name="lk_list", description="登録済みLK語録の一覧を表示します")
@app_commands.guild_only()
async def lk_list(interaction: discord.Interaction, page: int = 1):
    items = get_words(interaction.guild_id)
    if not items:
        await interaction.response.send_message("語録は登録されていません。", ephemeral=True)
        return
    pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE
    if not 1 <= page <= pages:
        await interaction.response.send_message(f"ページは1～{pages}を入力してください。", ephemeral=True)
        return
    begin = (page - 1) * PAGE_SIZE
    lines = [f"{i + 1}. {x}" for i, x in enumerate(items[begin:begin + PAGE_SIZE], begin)]
    await interaction.response.send_message(
        f"【LK語録一覧】{len(items)}語（{page}/{pages}ページ）\n" + "\n".join(lines),
        ephemeral=True,
    )


@bot.tree.command(name="lk_target", description="学習対象のユーザーを設定し、履歴を分析します（管理者専用）")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(user="学習対象者（Discordのユーザー選択から指定）")
async def lk_target(interaction: discord.Interaction, user: discord.User):
    if user.bot:
        await interaction.response.send_message("Botは分析対象に指定できません。", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    cfg = get_settings(interaction.guild_id)
    cfg["target_user_id"] = str(user.id)
    cfg["auto_enabled"] = True
    cfg["last_scan_at"] = None
    save_settings()
    try:
        report = await scan_guild(interaction.guild)
        await interaction.followup.send(
            f"対象を **{user.display_name}** に設定し、12時間ごとの自動分析を有効にしました。\n"
            + format_scan(report, user.id), ephemeral=True)
    except (ValueError, OSError, discord.HTTPException):
        logger.exception("/lk_target の初回分析に失敗")
        await interaction.followup.send(
            "対象の登録は完了しましたが、履歴の分析に失敗しました。Botのログと読み取り権限を確認してください。",
            ephemeral=True,
        )


@bot.tree.command(name="lk_scan", description="現在の対象ユーザーの履歴を再分析して語録を自動追加します")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def lk_scan(interaction: discord.Interaction):
    cfg = get_settings(interaction.guild_id)
    if not cfg.get("target_user_id"):
        await interaction.response.send_message("先に /lk_target で対象者を設定してください。", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        report = await scan_guild(interaction.guild)
        await interaction.followup.send(format_scan(report, int(cfg["target_user_id"])), ephemeral=True)
    except (ValueError, OSError, discord.HTTPException):
        logger.exception("/lk_scan の分析に失敗")
        await interaction.followup.send("履歴分析でエラーが発生しました。ログを確認してください。", ephemeral=True)


@bot.tree.command(name="lk_auto", description="12時間ごとの自動語録追加をON/OFFします（管理者専用）")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(enabled="有効にする場合はTrue、停止する場合はFalse")
async def lk_auto(interaction: discord.Interaction, enabled: bool):
    cfg = get_settings(interaction.guild_id)
    if enabled and not cfg.get("target_user_id"):
        await interaction.response.send_message("先に /lk_target で対象者を指定してください。", ephemeral=True)
        return
    cfg["auto_enabled"] = enabled
    save_settings()
    await interaction.response.send_message(
        f"12時間ごとの自動学習を{'有効' if enabled else '無効'}にしました。", ephemeral=True)


@bot.tree.command(name="lk_status", description="LK語録・学習設定の状態を表示します")
@app_commands.guild_only()
async def lk_status(interaction: discord.Interaction):
    cfg = get_settings(interaction.guild_id)
    await interaction.response.send_message(
        f"語録数: {len(get_words(interaction.guild_id))}\n"
        f"学習対象ID: {cfg.get('target_user_id') or '未設定'}\n"
        f"定期自動学習: {'ON' if cfg.get('auto_enabled') else 'OFF'}\n"
        f"前回の分析: {cfg.get('last_scan_at') or '未実施'}",
        ephemeral=True,
    )


@bot.tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "この操作には「サーバーの管理」権限が必要です。"
    else:
        logger.error("スラッシュコマンドエラー: %r", error)
        msg = "処理中にエラーが発生しました。Northflankのログを確認してください。"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        logger.warning("エラーメッセージの送信に失敗")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN が未設定です。Northflank の Runtime variables を確認してください。")
    bot.run(TOKEN)
