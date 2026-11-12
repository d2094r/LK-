"""日本語の頻出語・短い言い回しを抽出する小さな学習モジュール。

保存するのは集計結果だけ。元メッセージの保存・外部AI送信は行わない。
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from janome.tokenizer import Tokenizer

# 極端に一般的な語句は登録しない。必要なら手動の /lk_add で登録できる。
STOPWORDS = {
    "これ", "それ", "あれ", "ここ", "そこ", "あそこ", "どこ", "誰", "何", "今日", "昨日", "明日",
    "自分", "私", "俺", "あなた", "人", "こと", "もの", "やつ", "ほう", "感じ", "たち",
    "です", "ます", "でした", "ました", "する", "した", "して", "ある", "いる", "ない",
    "思う", "言う", "見る", "なる", "できる", "ほしい", "ちょっと", "なんか", "かな",
    "はい", "いい", "そう", "うん", "あの", "まあ", "普通", "ほんと", "これから",
    "笑", "www", "wwww", "そうです", "ありがとう", "よろしく", "お願い", "了解",
    "the", "this", "that", "and", "for", "you", "http", "https", "discord",
}

URL_RE = re.compile(r"https?://\S+|www\.\S+|discord\.gg/\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"<[@#!&][^>]{1,64}>|<a?:\w+?:\d+>")
EMAIL_RE = re.compile(r"[\w.+'-]+@[\w.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s-]{7,}\d)(?!\w)")
LONG_ID_RE = re.compile(r"\d{5,}")
SENTENCE_SPLIT_RE = re.compile(r"[\n\r。！？!?；;]+")
ALPHA_RE = re.compile(r"[A-Za-z\u3040-\u30ff\u3400-\u9fff]")

TOKENIZER = Tokenizer()


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def prepare(text: str) -> str:
    # ユーザー名やURLなどの個人・外部情報を候補に含めない。
    text = URL_RE.sub(" ", text)
    text = MENTION_RE.sub(" ", text)
    text = EMAIL_RE.sub(" ", text)
    text = PHONE_RE.sub(" ", text)
    text = re.sub(r"```[\s\S]*?```|`[^`]+`", " ", text)
    return text[:500]


def valid_candidate(text: str) -> bool:
    if not 4 <= len(text) <= 40:
        return False
    if text.startswith(("@", "#", "/")) or any(x in text for x in ("@", "http", "www.", "<", ">", "`")):
        return False
    if LONG_ID_RE.search(text) or not ALPHA_RE.search(text):
        return False
    if text.casefold() in STOPWORDS or normalize(text) in STOPWORDS:
        return False
    if len(re.sub(r"[^A-Za-z]", "", text)) == len(text) and len(text) < 5:
        return False
    # 長すぎる数値・一般的な言い回しの断片を抑える。
    if text.count(" ") > 3 or text.startswith(("です", "ます")):
        return False
    return True


def content_token(pos: str) -> bool:
    parts = pos.split(",")
    major = parts[0]
    # 人名などの固有名詞は自動登録の対象にしない（手動での登録は可能）。
    if major == "名詞" and ("人名" in parts or "数" in parts or "代名詞" in parts):
        return False
    if major == "名詞" and "非自立" in parts:
        return False
    return major in ("名詞", "動詞", "形容詞", "副詞", "感動詞")


def message_candidates(message: str) -> set[str]:
    """1発言につき各候補は最大1回のみ数える。"""
    result = set()
    for section in SENTENCE_SPLIT_RE.split(prepare(message)):
        section = section.strip()
        if not section:
            continue
        # 正式な口癖になりやすい「短い文章全体」も候補にする。
        if valid_candidate(section) and len(section) <= 32:
            result.add(section)
        tokens = [t for t in TOKENIZER.tokenize(section)
                  if t.surface.strip() and t.part_of_speech.split(",")[0] != "記号"]
        for start in range(len(tokens)):
            if not content_token(tokens[start].part_of_speech):
                continue
            for end in range(start + 1, min(start + 6, len(tokens)) + 1):
                segment = tokens[start:end]
                if not content_token(segment[-1].part_of_speech):
                    continue
                phrase = "".join(t.surface for t in segment).strip()
                # 2以上の内容語を持つ語句、または単独で長さのある単語。
                content_count = sum(content_token(t.part_of_speech) for t in segment)
                if len(segment) > 1 and content_count < 2:
                    continue
                if valid_candidate(phrase):
                    result.add(phrase)
    return result


@dataclass(frozen=True)
class Suggestion:
    word: str
    count: int
    score: float


def suggest(messages: Iterable[str], existing: Iterable[str], *,
            min_count: int = 4, max_new: int = 6, max_candidates: int = 15000) -> tuple[list[Suggestion], int]:
    counter: Counter[str] = Counter()
    representative: dict[str, str] = {}
    total_messages = 0
    for text in messages:
        total_messages += 1
        for phrase in message_candidates(text):
            key = normalize(phrase)
            counter[key] += 1
            representative.setdefault(key, phrase)
            if len(counter) > max_candidates:
                break
    if max_new <= 0:
        return [], total_messages
    known = {normalize(w) for w in existing}
    choices = []
    for key, count in counter.items():
        if count < min_count or key in known:
            continue
        # ターゲット発言の少なくとも3%に登場する語句を学習する。
        if total_messages and count / total_messages < 0.03:
            continue
        phrase = representative[key]
        if any(key in old for old in known if len(key) >= len(phrase)):
            continue
        # 典型的な口癖を優先：頻度×語句長（長いフレーズをわずかに優遇）。
        score = count * (1 + math.log1p(min(len(key), 22)) / 7)
        choices.append(Suggestion(word=phrase, count=count, score=score))
    choices.sort(key=lambda x: (-x.score, -x.count, -len(x.word), x.word))
    selected = []
    for candidate in choices:
        k = normalize(candidate.word)
        if any(k in normalize(x.word) or normalize(x.word) in k for x in selected):
            continue
        selected.append(candidate)
        if len(selected) >= max_new:
            break
    return selected, total_messages
