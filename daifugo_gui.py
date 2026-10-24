# -*- coding: utf-8 -*-
"""大富豪 pygame GUI 版.

`daifugo.py` のゲームロジック (Card / Player / 役判定 / 交換) を再利用しつつ、
pygame による UI/UX と、外部ファイル不要の手合成 BGM / 効果音を備えた画面版。

起動:
    python daifugo_gui.py            # 対戦開始 (あなたが User)
    python daifugo_gui.py --seed 7   # 乱数シード固定
    python daifugo_gui.py --smoke    # ヘッドレス自己起動 (検証用・自動進行)

操作方法はゲーム内 [H] で表示。音楽 [M] / 効果音 [N] / 全画面 [F]。
"""

from __future__ import annotations

import os
import sys
import math
import random
from typing import List, Optional, Tuple, Generator

import warnings
warnings.filterwarnings("ignore")

import pygame
import numpy as np
from pygame.sndarray import make_sound

# 既存のロジックを再利用 (モデル層の単一真実を CLI 版と共有する)
from daifugo import (
    Card, Player, make_deck, identify_combo, can_play, triggers_revolution,
    rank_strength, JOKER, RANK_TITLES, RANK_NAMES, SINGLE, SET, STRAIGHT,
    StatsTracker, __version__,
)

# ---------------------------------------------------------------------------
# 定数 (レイアウト / 色)
# ---------------------------------------------------------------------------
VW, VH = 1280, 800  # 仮想解像度 (この座標系で描画し窓に拡大縮小)

COL_BG_IN = (34, 116, 74)
COL_BG_OUT = (8, 44, 30)
COL_GOLD = (236, 205, 128)
COL_GOLD_DK = (150, 120, 55)
COL_TEXT = (240, 240, 235)
COL_DIM = (170, 175, 170)
COL_PANEL = (18, 60, 42)
COL_PANEL_2 = (12, 44, 31)
COL_RED = (210, 60, 66)
COL_BLUE = (70, 120, 220)
COL_CARD = (248, 247, 242)
COL_CARD_BACK = (46, 70, 150)
COL_CARD_BACK2 = (30, 48, 110)
COL_SUIT_RED = (198, 42, 52)
COL_SUIT_BLK = (28, 30, 38)

FONT_CANDIDATES = "meiryo,msgothic,yugothic,meiryoui,notosansjapanese,arialunicodems"

# 座標リージョン
CENTER_RECT = pygame.Rect(430, 300, 420, 210)
SEAT_POS = {
    1: pygame.Rect(24, 250, 250, 300),    # 左 CPU
    2: pygame.Rect(515, 66, 250, 150),    # 上 CPU
    3: pygame.Rect(1006, 250, 250, 300),  # 右 CPU
}


# ===========================================================================
# Audio: 手合成による BGM と効果音 (外部アセット不要)
# ===========================================================================
class Audio:
    RATE = 44100

    def __init__(self):
        self.ok = False
        self.music_on = True
        self.sfx_on = True
        self.music = None
        self.sfx = {}
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(self.RATE, -16, 2, 512)
            self.ok = True
        except Exception:
            self.ok = False
            return
        try:
            self._gen_all()
        except Exception:
            self.ok = False

    # --- 波形生成 ---------------------------------------------------------
    def _tone(self, freq, dur, vol=0.3, wave="sine", attack=0.005, release=0.05):
        n = max(1, int(self.RATE * dur))
        t = np.arange(n) / self.RATE
        if wave == "sine":
            x = np.sin(2 * np.pi * freq * t)
        elif wave == "square":
            x = np.sign(np.sin(2 * np.pi * freq * t))
        elif wave == "tri":
            x = 2 / np.pi * np.arcsin(np.sin(2 * np.pi * freq * t))
        elif wave == "saw":
            x = 2 * (t * freq - np.floor(0.5 + t * freq))
        else:
            x = np.sin(2 * np.pi * freq * t)
        env = np.ones(n)
        a = min(n, int(self.RATE * attack))
        r = min(n, int(self.RATE * release))
        if a > 1:
            env[:a] *= np.linspace(0.0, 1.0, a)
        if r > 1:
            env[-r:] *= np.linspace(1.0, 0.0, r)
        return x * env * vol

    def _noise(self, dur, vol=0.3, decay=6.0):
        n = max(1, int(self.RATE * dur))
        x = np.random.uniform(-1, 1, n)
        env = np.exp(-decay * np.linspace(0, dur, n))
        return x * env * vol

    def _sweep(self, f0, f1, dur, vol=0.3, wave="sine"):
        n = max(1, int(self.RATE * dur))
        freqs = np.linspace(f0, f1, n)
        phase = 2 * np.pi * np.cumsum(freqs) / self.RATE
        x = np.sin(phase) if wave == "sine" else np.sign(np.sin(phase))
        env = np.ones(n)
        r = min(n, int(self.RATE * 0.08))
        if r > 1:
            env[-r:] *= np.linspace(1, 0, r)
        return x * env * vol

    def _seq(self, parts, gap=0.0):
        """parts: 波形配列のリスト。gap 秒あけて連結。"""
        out = [np.zeros(1)]
        for p in parts:
            out.append(p)
            if gap:
                out.append(np.zeros(int(self.RATE * gap)))
        return np.concatenate(out)

    def _to_sound(self, mono):
        mono = np.asarray(mono, dtype=np.float64)
        peak = float(np.max(np.abs(mono))) or 1.0
        data = (np.clip(mono / peak, -1, 1) * 32767).astype(np.int16)
        arr = np.column_stack((data, data))
        return make_sound(np.ascontiguousarray(arr))

    # --- 効果音 -----------------------------------------------------------
    def _gen_all(self):
        S = self.sfx
        S["click"] = self._to_sound(self._tone(880, 0.05, 0.25, "square"))
        S["select"] = self._to_sound(self._tone(1320, 0.04, 0.18, "square"))
        S["deal"] = self._to_sound(self._noise(0.06, 0.25, decay=40))
        S["play"] = self._to_sound(self._seq([
            self._tone(523, 0.07, 0.28, "square"),
            self._tone(784, 0.09, 0.26, "square")], gap=0.02))
        S["play_big"] = self._to_sound(self._seq([
            self._tone(392, 0.08, 0.3, "square"),
            self._tone(523, 0.08, 0.3, "square"),
            self._tone(784, 0.12, 0.3, "square")], gap=0.02))
        S["pass"] = self._to_sound(self._tone(150, 0.18, 0.22, "tri"))
        S["error"] = self._to_sound(self._seq([
            self._tone(160, 0.09, 0.3, "square"),
            self._tone(120, 0.12, 0.3, "square")], gap=0.01))
        S["rev"] = self._to_sound(self._sweep(180, 900, 0.45, 0.32, "saw"))
        S["agari"] = self._to_sound(self._seq([
            self._tone(523, 0.1, 0.3, "square"),
            self._tone(659, 0.1, 0.3, "square"),
            self._tone(784, 0.1, 0.3, "square"),
            self._tone(1046, 0.22, 0.32, "square")], gap=0.03))
        S["ochi"] = self._to_sound(self._seq([
            self._tone(400, 0.14, 0.3, "tri"),
            self._tone(300, 0.14, 0.3, "tri"),
            self._tone(200, 0.28, 0.3, "tri")], gap=0.02))
        S["win"] = self._to_sound(self._seq([
            self._tone(523, 0.12, 0.3, "square"),
            self._tone(659, 0.12, 0.3, "square"),
            self._tone(784, 0.12, 0.3, "square"),
            self._tone(1046, 0.12, 0.3, "square"),
            self._tone(1318, 0.34, 0.32, "square")], gap=0.04))
        self.music = self._to_sound(self._bgm())

    # --- BGM (A 短調ペンタトニックの軽量ループ) ---------------------------
    def _bgm(self):
        bpm = 112.0
        beat = 60.0 / bpm
        bar = 4 * beat
        bars = 8
        total = bar * bars
        N = int(self.RATE * total) + self.RATE
        buf = np.zeros(N)

        def add(sig, start, vol=1.0):
            s = int(start * self.RATE)
            if s < 0 or s >= N:
                return
            e = min(N, s + len(sig))
            if e > s:
                buf[s:e] += sig[: e - s] * vol

        chords = [
            [220.0, 261.63, 329.63],   # Am
            [174.61, 220.0, 261.63],   # F
            [261.63, 329.63, 392.0],   # C
            [196.0, 246.94, 293.66],   # G
        ]
        roots = [110.0, 87.31, 130.81, 98.0]
        mel = [
            440, 523.25, 587.33, 659.25, 587.33, 523.25, 440, 0,
            523.25, 587.33, 659.25, 783.99, 659.25, 587.33, 523.25, 440,
            523.25, 587.33, 659.25, 880, 783.99, 659.25, 587.33, 659.25,
            783.99, 880, 783.99, 659.25, 587.33, 523.25, 440, 0,
        ]
        for b in range(bars):
            for f in chords[b % 4]:
                add(self._tone(f, bar * 0.98, 0.10, "sine", 0.06, 0.4), b * bar)
        for i in range(bars * 4):
            add(self._tone(roots[(i // 4) % 4], beat * 0.8, 0.16, "tri"), i * beat)
        for i, f in enumerate(mel):
            if f > 0:
                add(self._tone(f, beat * 0.9, 0.14, "square", 0.004, 0.09), i * beat)
        return buf

    # --- 制御 -------------------------------------------------------------
    def play_sfx(self, name):
        if self.ok and self.sfx_on and name in self.sfx:
            self.sfx[name].play()

    def start_music(self):
        if self.ok and self.music_on and self.music is not None:
            self.music.play(loops=-1)
            self.music.set_volume(0.32)

    def stop_music(self):
        if self.ok and self.music is not None:
            self.music.stop()

    def toggle_music(self):
        self.music_on = not self.music_on
        if not self.music_on:
            self.stop_music()
        else:
            self.start_music()
        return self.music_on

    def toggle_sfx(self):
        self.sfx_on = not self.sfx_on
        return self.sfx_on


# ===========================================================================
# GameModel: 既存ロジックをジェネレータ状態機械で駆動する
# ===========================================================================
class GameModel:
    def __init__(self, names=None, seed=None, record_stats=True):
        names = names or ["あなた", "CPU-A", "CPU-B", "CPU-C"]
        self.players = [Player(names[0], is_user=True)] + [Player(n) for n in names[1:]]
        self.rng = random.Random(seed)
        self.revolution = False
        self.round_no = 0
        self.prev_ranking: Optional[List[Player]] = None
        self.stats = StatsTracker(enabled=record_stats)
        # 描画用状態
        self.table = None
        self.table_cards: List[Card] = []
        self.current: Optional[Player] = None
        self.last_player: Optional[Player] = None
        self.ranking: List[Player] = []
        self.exchange_log: List[str] = []

    def set_all_cpu(self):
        for p in self.players:
            p.is_user = False

    # --- 配布 / 交換 ------------------------------------------------------
    def _deal(self):
        for p in self.players:
            p.hand = []
            p.rank = None
        deck = make_deck()
        self.rng.shuffle(deck)
        for i, c in enumerate(deck):
            self.players[i % 4].hand.append(c)
        for p in self.players:
            p.sort_hand(self.revolution)

    def _do_exchange(self) -> List[str]:
        r = self.prev_ranking
        if not r or len(r) < 4:
            return []
        rich, up, down, poor = r[0], r[1], r[2], r[3]
        log = []
        # 切る順: 大貧民 → 貧民 → 富豪 → 大富豪。Joker も交換対象 (take_strongest が最強として優先して出す)。
        # 4 人分を先に選出 (元の手札基準) してからまとめて受け渡しする。
        gp = poor.take_strongest(2)   # 大貧民: 最強 2 枚
        gd = down.take_strongest(1)   # 貧民: 最強 1 枚
        gu = up.take_unwanted(1)      # 富豪: 最弱 1 枚
        gr = rich.take_unwanted(2)    # 大富豪: 最弱 2 枚
        rich.hand += gp
        poor.hand += gr
        up.hand += gd
        down.hand += gu
        log.append(f"大貧民 → 大富豪: {_fmt(gp)}")
        log.append(f"貧民 → 富豪: {_fmt(gd)}")
        log.append(f"富豪 → 貧民: {_fmt(gu)}")
        log.append(f"大富豪 → 大貧民: {_fmt(gr)}")
        for p in self.players:
            p.sort_hand(self.revolution)
        return log

    def start_round(self):
        self.round_no += 1
        self.revolution = False
        self._deal()
        self.exchange_log = self._do_exchange() if self.prev_ranking else []
        self.table = None
        self.table_cards = []
        self.last_player = None
        self.ranking = []

    # --- セッション (ジェネレータ) ---------------------------------------
    def session(self) -> Generator:
        while True:
            self.start_round()
            yield ("deal",)
            if self.exchange_log:
                yield ("exchange", list(self.exchange_log))
            yield from self._trick()
            self.stats.record_round(self.ranking)  # 成績を記録 (本日 / 累計)
            cont = yield ("result", list(self.ranking))
            if not cont:
                break

    def _trick(self) -> Generator:
        # 巡番 (カードを切る順序): 順位が決まっている (2 巡目以降) は前回順位の逆順で
        #   1:大貧民 → 2:貧民 → 3:富豪 → 4:大富豪 の順に出す (大貧民が先出し)。初回はランダム親。
        if self.prev_ranking and len(self.prev_ranking) >= 4:
            active = list(reversed(self.prev_ranking))
            current = active[0]  # 大貧民が先出し
        else:
            active = list(self.players)
            current = self.rng.choice(active)
        finished: List[Player] = []
        ochi: Optional[Player] = None
        self.table = None
        self.table_cards = []
        self.last_player = None
        passed = set()
        pending_rev = False  # 場が 4 枚セット (革命の試み)。流れて初めて成立。
        self.current = current

        while len(active) > 1:
            if current not in active:
                current = active[0]
                self.current = current

            # 流し判定
            if self.table is not None:
                others = [p for p in active if p is not self.last_player]
                if others and all(p in passed for p in others):
                    # 場が流れる = 最後の出牌が勝ち。4 枚セットならここで革命が成立/解消。
                    if pending_rev:
                        self.revolution = not self.revolution
                        for p in self.players:
                            p.sort_hand(self.revolution)
                        pending_rev = False
                        yield ("rev", self.revolution)
                    self.table = None
                    self.table_cards = []
                    passed = set()
                    if self.last_player in active:
                        current = self.last_player
                        self.current = current
                    yield ("info", "場が流れた — 親が自由にだせる", 1.2)

            # プレイ選択
            self.current = current
            if current.is_user:
                cards = (yield ("user", current))
            else:
                yield ("think", current)
                cards = current.cpu_choose(self.table, self.revolution)

            idx = active.index(current)
            next_player = active[(idx + 1) % len(active)]

            if cards is None:
                passed.add(current)
                yield ("pass", current)
            else:
                combo = identify_combo(cards, self.revolution)
                attempt = triggers_revolution(cards, self.revolution)
                current.remove(cards)
                status = None
                if attempt and pending_rev:
                    status = "return"     # 革命返し (それまでの革命は不成立)
                elif attempt:
                    status = "attempt"    # 革命の試み (流れると成立)
                pending_rev = attempt
                self.table = combo
                self.table_cards = list(cards)
                self.last_player = current
                passed = set()
                yield ("played", current, cards, combo, status)
                if current.is_finished:
                    finished.append(current)
                    yield ("info", f"{current.name} があがった！", 1.3)
                    if (len(finished) == 1 and self.prev_ranking
                            and self.prev_ranking[0] is not current
                            and self.prev_ranking[0] in active):
                        ochi = self.prev_ranking[0]
                        active.remove(ochi)
                        yield ("ochi", ochi)
                    active.remove(current)
                    if next_player is ochi or next_player not in active:
                        next_player = active[idx % len(active)] if active else None

            if not active:
                break
            current = next_player
            self.current = current

        for p in active:
            finished.append(p)
        if ochi is not None and ochi not in finished:
            finished.append(ochi)
        self.ranking = finished[:4]
        self.prev_ranking = self.ranking


# ===========================================================================
# 描画ヘルパ
# ===========================================================================
class Fonts:
    def __init__(self):
        self.cache = {}

    def get(self, size, bold=False):
        key = (size, bold)
        if key not in self.cache:
            self.cache[key] = pygame.font.SysFont(FONT_CANDIDATES, size, bold=bold)
        return self.cache[key]


FONTS = Fonts()


def text_surf(s, size, color=COL_TEXT, bold=False, shadow=True):
    f = FONTS.get(size, bold)
    img = f.render(s, True, color)
    if not shadow:
        return img
    sh = f.render(s, True, (0, 0, 0))
    pad = 5
    out = pygame.Surface((img.get_width() + pad, img.get_height() + pad), pygame.SRCALPHA)
    out.blit(sh, (2, 2))
    out.blit(img, (0, 0))
    return out


def draw_text(surf, s, size, pos, color=COL_TEXT, bold=False,
              center=False, right=False, shadow=True):
    img = text_surf(s, size, color, bold, shadow)
    r = img.get_rect()
    if center:
        r.center = pos
    elif right:
        r.topright = pos
    else:
        r.topleft = pos
    surf.blit(img, r)
    return r


def rrect(surf, color, rect, radius=12, border=0, border_color=None, alpha=255):
    rect = pygame.Rect(rect)
    if alpha < 255:
        s = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(s, (*color, alpha), s.get_rect(), border_radius=radius)
        surf.blit(s, rect.topleft)
    else:
        pygame.draw.rect(surf, color, rect, border_radius=radius)
    if border:
        pygame.draw.rect(surf, border_color or color, rect, border, border_radius=radius)


# ---------------------------------------------------------------------------
# カード描画
# ---------------------------------------------------------------------------
class CardRenderer:
    def __init__(self):
        self.front_cache = {}
        self.back_cache = {}

    def front(self, card: Card, w: int, h: int) -> pygame.Surface:
        key = (card.rank, card.suit, w, h)
        if key in self.front_cache:
            return self.front_cache[key]
        surf = pygame.Surface((w, h), pygame.SRCALPHA)
        r = surf.get_rect()
        rrect(surf, (210, 208, 200), (2, 3, w, h), radius=int(h * 0.09))
        rrect(surf, COL_CARD, (0, 0, w, h), radius=int(h * 0.09))
        pygame.draw.rect(surf, (180, 178, 168), (0, 0, w, h), 2, border_radius=int(h * 0.09))

        if card.is_joker:
            self._draw_joker(surf, w, h)
        else:
            name = RANK_NAMES[card.rank]
            suit = card.suit
            col = COL_SUIT_RED if suit in ("♥", "♦") else COL_SUIT_BLK
            draw_text(surf, name, int(h * 0.26), (int(w * 0.12), int(h * 0.05)), col, bold=True, shadow=False)
            draw_text(surf, suit, int(h * 0.22), (int(w * 0.12), int(h * 0.30)), col, shadow=False)
            draw_text(surf, suit, int(h * 0.5), (w // 2, h * 0.62), col, center=True, shadow=False)
        self.front_cache[key] = surf
        return surf

    def _draw_joker(self, surf, w, h):
        # 虹色の JOKER カード
        draw_text(surf, "★", int(h * 0.4), (w // 2, h * 0.42), (200, 60, 160), center=True, shadow=False)
        draw_text(surf, "JOKER", int(h * 0.16), (w // 2, h * 0.82), (150, 40, 150), center=True, bold=True, shadow=False)
        draw_text(surf, "J", int(h * 0.24), (int(w * 0.1), int(h * 0.04)), (200, 60, 160), bold=True, shadow=False)

    def back(self, w: int, h: int) -> pygame.Surface:
        key = (w, h)
        if key in self.back_cache:
            return self.back_cache[key]
        surf = pygame.Surface((w, h), pygame.SRCALPHA)
        rad = int(h * 0.09)
        rrect(surf, (210, 208, 200), (2, 3, w, h), radius=rad)
        rrect(surf, COL_CARD_BACK, (0, 0, w, h), radius=rad)
        inner = pygame.Rect(int(w * 0.12), int(h * 0.08), int(w * 0.76), int(h * 0.84))
        rrect(surf, COL_CARD_BACK2, inner, radius=int(rad * 0.6))
        # 市松模様
        s = max(6, w // 8)
        for yy in range(inner.top, inner.bottom, s):
            for xx in range(inner.left, inner.right, s):
                if ((xx - inner.left) // s + (yy - inner.top) // s) % 2 == 0:
                    pygame.draw.rect(surf, (58, 84, 170), (xx, yy, s, s))
        pygame.draw.rect(surf, COL_GOLD, inner, 2, border_radius=int(rad * 0.6))
        draw_text(surf, "★", int(h * 0.3), (w // 2, h // 2), COL_GOLD, center=True, shadow=False)
        self.back_cache[key] = surf
        return surf


CARDS = CardRenderer()


def combo_label(combo, revolution) -> str:
    if combo is None:
        return ""
    kind, size, _, key = combo
    key_name = "Joker" if key == JOKER else RANK_NAMES.get(key, str(key))
    if kind == SINGLE:
        return f"シングル {key_name}"
    if kind == SET:
        return f"{size}カード {key_name}"
    return f"{size}枚階段 {key_name}"


def _fmt(cards: List[Card]) -> str:
    return " ".join(str(c) for c in cards) if cards else "なし"


# ---------------------------------------------------------------------------
# ボタン
# ---------------------------------------------------------------------------
class Button:
    def __init__(self, rect: pygame.Rect, label: str, color=COL_GOLD_DK, enabled=True):
        self.rect = rect
        self.label = label
        self.color = color
        self.enabled = enabled
        self.hover = False

    def handle(self, ev) -> bool:
        if not self.enabled:
            return False
        if ev.type == pygame.MOUSEMOTION:
            self.hover = self.rect.collidepoint(ev.pos)
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1 and self.rect.collidepoint(ev.pos):
            return True
        return False

    def draw(self, surf):
        base = self.color if self.enabled else (70, 74, 70)
        if self.enabled and self.hover:
            base = tuple(min(255, c + 40) for c in self.color)
        rrect(surf, (0, 0, 0), self.rect.move(2, 3), radius=10, alpha=90)
        rrect(surf, base, self.rect, radius=10)
        edge = COL_GOLD if self.enabled else (110, 112, 110)
        pygame.draw.rect(surf, edge, self.rect, 2, border_radius=10)
        col = COL_TEXT if self.enabled else COL_DIM
        draw_text(surf, self.label, 24, self.rect.center, col, bold=True, center=True)


# ===========================================================================
# App: メインループ / 入力 / 描画
# ===========================================================================
class App:
    def __init__(self, seed=None, auto=False):
        pygame.display.init()
        pygame.font.init()
        pygame.display.set_caption("大富豪 — pygame Edition")
        self.icon = pygame.Surface((32, 32), pygame.SRCALPHA)
        pygame.draw.rect(self.icon, COL_GOLD, (2, 2, 28, 28), border_radius=6)
        pygame.display.set_icon(self.icon)

        self.fullscreen = False
        self.screen = pygame.display.set_mode((VW, VH), pygame.RESIZABLE)
        self.base = pygame.Surface((VW, VH))
        self.bg = self._build_bg()
        self.clock = pygame.time.Clock()
        self._compute_scale()

        self.audio = Audio()
        self.auto = auto

        self.screen_name = "title"
        self.model: Optional[GameModel] = None
        self.stats = StatsTracker()  # タイトル表示用の成績 (読み込み専用)
        self.gen = None
        self.gen_started = False
        self.pending = None
        self.timer = 0.0
        self.user_turn: Optional[Player] = None
        self.sel: set = set()
        self.card_rects: List[Tuple[pygame.Rect, int]] = []
        self.toasts: List[List] = []  # [text, t0, dur]
        self.show_result: Optional[List[Player]] = None
        self.show_help = False
        self.show_exchange: Optional[List[str]] = None
        self.pop_t = 0.0
        self.time = 0.0
        self.auto_elapsed = 0.0
        self.fast = False  # ユーザーあがり/都落ち後、CPU 同士の演出を省略して高速送り

    # --- スケール ---------------------------------------------------------
    def _compute_scale(self):
        w, h = self.screen.get_size()
        self.scale = min(w / VW, h / VH)
        self.sw = max(1, int(VW * self.scale))
        self.sh = max(1, int(VH * self.scale))
        self.ox = (w - self.sw) // 2
        self.oy = (h - self.sh) // 2

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            info = pygame.display.Info()
            self.screen = pygame.display.set_mode((info.current_w, info.current_h), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode((VW, VH), pygame.RESIZABLE)
        self._compute_scale()

    def to_virtual(self, mp):
        return ((mp[0] - self.ox) / self.scale, (mp[1] - self.oy) / self.scale)

    # --- ゲーム制御 -------------------------------------------------------
    def start_game(self):
        self.model = GameModel(seed=None, record_stats=not self.auto)
        if self.auto:
            self.model.set_all_cpu()
        self.gen = self.model.session()
        self.gen_started = False
        self.pending = None
        self.user_turn = None
        self.sel = set()
        self.show_result = None
        self.fast = False
        self.screen_name = "play"
        self.audio.start_music()
        self._advance(None)

    def _advance(self, value):
        if self.gen is None:
            return
        try:
            if not self.gen_started:
                tok = next(self.gen)
                self.gen_started = True
            else:
                tok = self.gen.send(value)
        except StopIteration:
            self.gen = None
            self.pending = None
            self.screen_name = "title"
            self.stats = StatsTracker()  # タイトル表示の成績を再読込
            return
        self.pending = tok
        self._on_token(tok)

    def _fast_forward(self):
        """ユーザーが手番から外れた後、CPU 同士の手番演出を省略して結果まで一気に進める。

        ジェネレータは各ステップでモデル状態 (手札 / 場 / 革命 / 着順) を更新するため、
        演出 (_on_token のタイマ・効果音・トースト) を飛ばして回すだけで正しい最終状態になる。
        """
        guard = 0
        while self.gen is not None and guard < 5000:
            guard += 1
            try:
                tok = self.gen.send(None)
            except StopIteration:
                self.gen = None
                self.pending = None
                self.screen_name = "title"
                return
            self.pending = tok
            kind = tok[0]
            if kind in ("result", "user"):
                self._on_token(tok)  # 結果表示 (または保険としてユーザー入力へ)
                return
            # think / played / pass / info / ochi / rev は演出を省略して次へ

    def _on_token(self, tok):
        self.user_turn = None
        self.show_exchange = None
        kind = tok[0]
        if kind == "deal":
            self.fast = False
            self.timer = 1.0
            self.audio.play_sfx("deal")
            self.toast("カードを配信中...")
        elif kind == "exchange":
            self.timer = 2.6
            self.show_exchange = tok[1]
            self.audio.play_sfx("select")
        elif kind == "info":
            self.timer = tok[2]
            self.toast(tok[1])
        elif kind == "think":
            self.timer = 0.6
        elif kind == "played":
            self.timer = 0.85
            self.sel = set()
            self.pop_t = 1.0
            status = tok[4]  # None / "attempt" / "return"
            big = len(tok[2]) >= 3 or status is not None
            self.audio.play_sfx("play_big" if big else "play")
            if status == "attempt":
                self.toast("革命の試み… (流れると成立)")
            elif status == "return":
                self.audio.play_sfx("rev")
                self.toast("革命返し! (革命は不成立)")
            # ユーザーがあがったら、この 1 手を見せた後は CPU 同士を高速送りに切り替え
            if tok[1].is_user and tok[1].is_finished and not self.auto:
                self.audio.play_sfx("agari")
                self.toast("あなたが上がりました！")
                self.timer = 1.2
                self.fast = True
        elif kind == "rev":
            self.timer = 1.3
            self.audio.play_sfx("rev")
            self.toast("革命成立!" if tok[1] else "革命解消!")
        elif kind == "pass":
            self.timer = 0.5
            self.audio.play_sfx("pass")
        elif kind == "ochi":
            self.timer = 1.9
            self.audio.play_sfx("ochi")
            self.toast(f"都落ち: {tok[1].name} は大貧民確定")
            # ユーザーが都落ちした場合も、この演出の後は CPU 同士を高速送りに
            if tok[1].is_user and not self.auto:
                self.fast = True
        elif kind == "user":
            self.user_turn = tok[1]
            self.sel = set()
        elif kind == "result":
            self.show_result = tok[1]
            self.audio.play_sfx("win")

    def toast(self, text, dur=1.6):
        self.toasts.append([text, self.time, dur])

    # --- 入力 -------------------------------------------------------------
    def handle_events(self):
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False
            if ev.type == pygame.VIDEORESIZE:
                self._compute_scale()
            if ev.type == pygame.KEYDOWN:
                if not self.handle_key(ev.key):
                    return False
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                pos = self.to_virtual(ev.pos)
                if not self.handle_click(pos, 1):
                    return False
        return True

    def handle_key(self, key) -> bool:
        if key == pygame.K_ESCAPE:
            if self.show_help:
                self.show_help = False
            elif self.screen_name == "play" and not self.auto:
                self.go_to_title()
            else:
                return False
            return True
        if key == pygame.K_m:
            self.audio.toggle_music()
            self.toast("音楽: " + ("ON" if self.audio.music_on else "OFF"))
        elif key == pygame.K_n:
            self.audio.toggle_sfx()
            self.toast("効果音: " + ("ON" if self.audio.sfx_on else "OFF"))
        elif key == pygame.K_f:
            self.toggle_fullscreen()
        elif key == pygame.K_h:
            self.show_help = not self.show_help
        elif key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            if self.screen_name == "title":
                self.start_game()
            elif self.show_result is not None:
                self._continue_from_result()
            else:
                self.try_play()
        elif key == pygame.K_SPACE:
            if self.screen_name == "title":
                self.start_game()
            elif self.show_result is not None:
                self._continue_from_result()
            elif self.user_turn is not None:
                self.try_pass()
        return True

    def handle_click(self, pos, button) -> bool:
        if button != 1:
            return True
        if self.screen_name == "title":
            self.start_game()
            return True
        if self.show_help:
            self.show_help = False
            return True
        if self.show_result is not None:
            for b in self._result_buttons():
                if b.rect.collidepoint(pos):
                    self.audio.play_sfx("click")
                    if b.label.startswith("終了"):
                        self.go_to_title()
                    else:
                        self._continue_from_result()
                    return True
            return True  # 結果画面でボタン以外をクリックしても何もしない
        if self.user_turn is not None:
            # ボタン
            for b in self._buttons():
                if b.enabled and b.rect.collidepoint(pos):
                    self.audio.play_sfx("click")
                    if b.label.startswith("出す"):
                        self.try_play()
                    elif b.label.startswith("パス"):
                        self.try_pass()
                    elif b.label.startswith("解除"):
                        self.sel = set()
                    return True
            # カード選択
            for rect, i in reversed(self.card_rects):
                if rect.collidepoint(pos):
                    if i in self.sel:
                        self.sel.discard(i)
                    else:
                        self.sel.add(i)
                    self.audio.play_sfx("select")
                    return True
        return True

    def _buttons(self):
        following = self.model.table is not None
        sel_cards = [self.model.players[0].hand[i] for i in sorted(self.sel) if i < len(self.model.players[0].hand)]
        can = self.user_turn is not None and can_play(sel_cards, self.model.table, self.model.revolution) is not None
        play_b = Button(pygame.Rect(1010, 636, 210, 56), "出す  (Enter)", COL_GOLD_DK, enabled=bool(can))
        pass_b = Button(pygame.Rect(1010, 700, 210, 48), "パス  (Space)", (60, 90, 140), enabled=bool(following))
        clr_b = Button(pygame.Rect(840, 700, 150, 48), "解除", (70, 74, 70), enabled=bool(self.sel))
        return [play_b, pass_b, clr_b]

    # --- 結果画面の継続 / 終了 --------------------------------------------
    def _result_rect(self) -> pygame.Rect:
        box = pygame.Rect(0, 0, 620, 500)
        box.center = (VW // 2, VH // 2)
        return box

    def _result_buttons(self):
        box = self._result_rect()
        cont = Button(pygame.Rect(box.centerx - 165, box.bottom - 78, 150, 52), "続ける", COL_GOLD_DK, True)
        quit_b = Button(pygame.Rect(box.centerx + 15, box.bottom - 78, 150, 52), "終了", (130, 62, 62), True)
        return [cont, quit_b]

    def _continue_from_result(self):
        self.audio.play_sfx("click")
        self.show_result = None
        self._advance(True)

    def go_to_title(self):
        """ゲームを終了してタイトルへ戻る (成績は再読込)。"""
        self.gen = None
        self.pending = None
        self.gen_started = False
        self.show_result = None
        self.user_turn = None
        self.sel = set()
        self.screen_name = "title"
        self.stats = StatsTracker()  # タイトル表示の成績を再読込
        self.audio.stop_music()

    def try_play(self):
        if self.user_turn is None:
            return
        hand = self.model.players[0].hand
        cards = [hand[i] for i in sorted(self.sel) if i < len(hand)]
        if can_play(cards, self.model.table, self.model.revolution) is None:
            self.audio.play_sfx("error")
            self.toast("その組み合わせは出せません")
            return
        self._advance(cards)

    def try_pass(self):
        if self.user_turn is None or self.model.table is None:
            return
        self._advance(None)

    # --- 更新 -------------------------------------------------------------
    def update(self, dt):
        self.time += dt
        self.pop_t = max(0.0, self.pop_t - dt * 2.5)
        self.toasts = [t for t in self.toasts if self.time - t[1] < t[2]]
        if self.screen_name != "play" or self.show_help:
            return
        if self.show_result is not None:
            if self.auto:
                self.auto_elapsed += dt
                if self.auto_elapsed > 1.5:
                    self.auto_elapsed = 0.0
                    self.show_result = None
                    self._advance(True)
            return
        if self.pending is None:
            self._advance(None)
            return
        kind = self.pending[0]
        if kind == "result":
            pass
        elif kind == "user":
            if self.auto:
                # 自動モードでは user は発生しないはずだが保険
                self._advance(None)
        elif self.fast:
            # あがり後は手番演出を省略。トリガの手番 (あがり/都落ち) を見せてから高速送り
            self.timer -= dt
            if self.timer <= 0:
                self._fast_forward()
        else:
            self.timer -= dt
            if self.timer <= 0:
                self._advance(None)

    # --- 描画 -------------------------------------------------------------
    def draw(self):
        b = self.base
        self._draw_background(b)
        if self.screen_name == "title":
            self._draw_title(b)
        else:
            self._draw_play(b)
        self._draw_toasts(b)
        if self.show_exchange:
            self._draw_exchange(b)
        if self.show_result is not None:
            self._draw_result(b)
        if self.show_help:
            self._draw_help(b)
        # 仮想 -> 窓へ拡大
        self.screen.fill((0, 0, 0))
        scaled = pygame.transform.smoothscale(self.base, (self.sw, self.sh))
        self.screen.blit(scaled, (self.ox, self.oy))
        pygame.display.flip()

    def _build_bg(self):
        surf = pygame.Surface((VW, VH))
        cx, cy = VW // 2, VH // 2
        maxd = math.hypot(cx, cy)
        d = int(maxd)
        while d > 0:  # 外側から内側へ塗る (後描画で内側が覆う)
            t = d / maxd
            col = tuple(int(COL_BG_IN[i] + (COL_BG_OUT[i] - COL_BG_IN[i]) * t) for i in range(3))
            ry = int(d * VH / VW)
            pygame.draw.ellipse(surf, col, (cx - d, cy - ry - 40, d * 2, ry * 2))
            d -= 8
        pygame.draw.rect(surf, (0, 0, 0), (0, 0, VW, 4))
        pygame.draw.rect(surf, (0, 0, 0), (0, VH - 4, VW, 4))
        return surf

    def _draw_background(self, surf):
        surf.blit(self.bg, (0, 0))

    def _draw_title(self, surf):
        draw_text(surf, "大 富 豪", 96, (VW // 2, 230), COL_GOLD, bold=True, center=True)
        draw_text(surf, f"— pygame Edition  v{__version__} —", 24, (VW // 2, 312), COL_TEXT, center=True)
        # あなたの累計成績
        s = self.stats.summary("あなた")
        cum = s["cumulative"]
        draw_text(surf, "あなたの成績", 24, (VW // 2, 372), COL_GOLD, bold=True, center=True)
        draw_text(surf, "累計: " + StatsTracker.format_bucket(cum), 22, (VW // 2, 410), COL_TEXT, center=True)
        draw_text(surf, "本日: " + StatsTracker.format_bucket(s["today"]), 22, (VW // 2, 444), COL_TEXT, center=True)
        pulse = int(180 + 75 * math.sin(self.time * 4))
        draw_text(surf, "クリック / Enter / Space でスタート", pulse, (VW // 2, 520), COL_TEXT, center=True)
        draw_text(surf, "[H] 遊び方   [M] 音楽   [N] 効果音   [F] 全画面", 22, (VW // 2, 580), COL_DIM, center=True)
        if not self.audio.ok:
            draw_text(surf, "※ 音声デバイスが見つからないため無音で動作します", 20, (VW // 2, 636), COL_RED, center=True)

    def _draw_play(self, surf):
        m = self.model
        # 上部バー
        rrect(surf, COL_PANEL_2, (0, 0, VW, 54), radius=0, alpha=200)
        draw_text(surf, f"第 {m.round_no} ラウンド", 26, (20, 12), COL_TEXT, bold=True)
        if m.revolution:
            rrect(surf, (150, 40, 60), (220, 10, 150, 34), radius=8)
            draw_text(surf, "革命中!", 24, (295, 27), COL_TEXT, bold=True, center=True)
        else:
            rrect(surf, (40, 70, 55), (220, 10, 150, 34), radius=8)
            draw_text(surf, "平常", 24, (295, 27), COL_DIM, center=True)
        draw_text(surf, "[H]遊び方  [M]音楽:" + ("ON" if self.audio.music_on else "OFF")
                  + "  [N]効果音:" + ("ON" if self.audio.sfx_on else "OFF") + "  [F]全画面",
                  18, (VW - 16, 16), COL_DIM, right=True)

        # 場
        self._draw_table(surf)
        # 座席 (CPU)
        for i in (1, 2, 3):
            self._draw_seat(surf, i)
        # 手番表示
        if m.current is not None:
            self._draw_turn_marker(surf)
        # ユーザー手札
        self._draw_hand(surf)
        # ボタン
        if self.user_turn is not None:
            mpos = self.to_virtual(pygame.mouse.get_pos())
            for b in self._buttons():
                b.hover = b.enabled and b.rect.collidepoint(mpos)
                b.draw(surf)
        # 状態メッセージ
        self._draw_status(surf)

    def _draw_table(self, surf):
        m = self.model
        rrect(surf, COL_PANEL, CENTER_RECT, radius=18, border=3, border_color=COL_GOLD_DK, alpha=150)
        draw_text(surf, "場", 22, (CENTER_RECT.centerx, CENTER_RECT.top + 16), COL_DIM, center=True)
        if m.table_cards:
            n = len(m.table_cards)
            cw, ch = 96, 138
            scale = 1.0 + 0.12 * self.pop_t
            cw, ch = int(cw * scale), int(ch * scale)
            step = min(cw - 8, 200 // max(1, n - 1)) if n > 1 else 0
            total = cw + (n - 1) * step
            x0 = CENTER_RECT.centerx - total // 2
            y0 = CENTER_RECT.centery - ch // 2 + 8
            for i, c in enumerate(m.table_cards):
                img = CARDS.front(c, cw, ch)
                surf.blit(img, (x0 + i * step, y0))
            draw_text(surf, combo_label(m.table, m.revolution), 24,
                      (CENTER_RECT.centerx, CENTER_RECT.bottom - 22), COL_GOLD, bold=True, center=True)
            if m.last_player is not None:
                draw_text(surf, f"親: {m.last_player.name}", 20,
                          (CENTER_RECT.centerx, CENTER_RECT.top + 40), COL_TEXT, center=True)
        else:
            draw_text(surf, "自由にだせます", 26, (CENTER_RECT.centerx, CENTER_RECT.centery), COL_DIM, center=True)

    def _draw_seat(self, surf, idx):
        m = self.model
        p = m.players[idx]
        rect = SEAT_POS[idx]
        active = m.current is p
        glow = (math.sin(self.time * 5) * 0.5 + 0.5) if active else 0.0
        border = COL_GOLD if active else COL_GOLD_DK
        rrect(surf, COL_PANEL, rect, radius=16, border=4 if active else 2,
              border_color=border, alpha=210)
        # タイトル (前回順位)
        title = ""
        if m.prev_ranking and p in m.prev_ranking:
            title = RANK_TITLES[m.prev_ranking.index(p)]
        draw_text(surf, p.name, 26, (rect.x + 16, rect.y + 12), COL_TEXT, bold=True)
        if title:
            draw_text(surf, title, 20, (rect.right - 14, rect.y + 14), COL_GOLD, right=True)
        # 残り枚数 (カード裏の束)
        n = len(p.hand)
        draw_text(surf, f"残り {n} 枚", 20, (rect.x + 16, rect.y + 46), COL_DIM)
        show = min(n, 8)
        bw, bh = 46, 66
        bx = rect.x + 16
        by = rect.y + 76
        for k in range(show):
            surf.blit(CARDS.back(bw, bh), (bx + k * 8, by))
        if active:
            rrect(surf, (255, 230, 150), rect.inflate(10, 10), radius=18, alpha=int(70 * glow))

    def _draw_turn_marker(self, surf):
        m = self.model
        p = m.current
        label = "あなたの番" if p.is_user else f"{p.name} の番"
        col = COL_GOLD if p.is_user else COL_TEXT
        if p.is_user:
            draw_text(surf, "▶ " + label, 30, (VW // 2, VH - 214), col, bold=True, center=True)
        else:
            draw_text(surf, label, 22, (VW // 2, 70), col, center=True)

    def _draw_hand(self, surf):
        m = self.model
        hand = m.players[0].hand
        n = len(hand)
        if n == 0:
            return
        cw, ch = 96, 138
        margin = 40
        avail = VW - 2 * margin - 260  # 右のボタン領域を避ける
        step = min(cw - 6, avail // max(1, n - 1)) if n > 1 else 0
        total = cw + (n - 1) * step
        x0 = margin + (avail - total) // 2
        base_y = VH - ch - 24
        mouse = self.to_virtual(pygame.mouse.get_pos())
        self.card_rects = []
        for i, c in enumerate(hand):
            x = x0 + i * step
            y = base_y
            if i in self.sel:
                y -= 26
            else:
                # ホバー
                if self.user_turn is not None and x <= mouse[0] <= x + cw and base_y <= mouse[1] <= base_y + ch:
                    y -= 12
            rect = pygame.Rect(x, y, cw, ch)
            self.card_rects.append((rect, i))
            surf.blit(CARDS.front(c, cw, ch), (x, y))
            if i in self.sel:
                pygame.draw.rect(surf, COL_GOLD, (x, y, cw, ch), 3, border_radius=12)

    def _draw_status(self, surf):
        m = self.model
        if self.user_turn is not None:
            following = m.table is not None
            msg = "カードを選んで [出す] をクリック (Enter)。" + ("[パス] 可 (Space)。" if following else "親なので必ず出してください。")
            draw_text(surf, msg, 22, (40, VH - 214), COL_GOLD, bold=True)
        elif m.current is not None:
            draw_text(surf, f"{m.current.name} が考えています...", 22, (40, VH - 214), COL_DIM)

    def _draw_toasts(self, surf):
        y = 120
        for text, t0, dur in self.toasts:
            a = 1.0
            el = self.time - t0
            if el < 0.2:
                a = el / 0.2
            elif el > dur - 0.3:
                a = max(0, (dur - el) / 0.3)
            img = text_surf(text, 30, COL_GOLD, bold=True)
            pad = 14
            box = pygame.Rect(0, 0, img.get_width() + pad * 2, img.get_height() + pad)
            box.center = (VW // 2, y)
            rrect(surf, (0, 0, 0), box, radius=12, alpha=int(180 * a))
            img.set_alpha(int(255 * a))
            surf.blit(img, (box.centerx - img.get_width() // 2, box.centery - img.get_height() // 2))
            y += box.height + 10

    def _draw_exchange(self, surf):
        self._overlay(surf, 150)
        box = pygame.Rect(0, 0, 760, 60 + 60 * len(self.show_exchange))
        box.center = (VW // 2, VH // 2)
        rrect(surf, COL_PANEL_2, box, radius=18, border=3, border_color=COL_GOLD)
        draw_text(surf, "カード交換", 34, (box.centerx, box.top + 34), COL_GOLD, bold=True, center=True)
        for i, line in enumerate(self.show_exchange):
            draw_text(surf, line, 22, (box.x + 30, box.top + 74 + i * 40), COL_TEXT)

    def _draw_result(self, surf):
        self._overlay(surf, 170)
        box = pygame.Rect(0, 0, 620, 500)
        box.center = (VW // 2, VH // 2)
        rrect(surf, COL_PANEL_2, box, radius=20, border=3, border_color=COL_GOLD)
        draw_text(surf, f"第 {self.model.round_no} ラウンド 結果", 34, (box.centerx, box.top + 40), COL_GOLD, bold=True, center=True)
        for i, p in enumerate(self.show_result):
            yy = box.top + 92 + i * 52
            rrect(surf, (30, 78, 55), (box.x + 40, yy, 540, 44), radius=10)
            draw_text(surf, f"{i + 1} 位", 24, (box.x + 60, yy + 22), COL_TEXT, bold=True, center=False)
            draw_text(surf, RANK_TITLES[i], 24, (box.x + 150, yy + 22), COL_GOLD, center=False)
            draw_text(surf, p.name, 24, (box.right - 60, yy + 22), COL_TEXT, right=True)
        # あなたの成績 (本日 / 累計)
        me = self.model.players[0].name
        s = self.model.stats.summary(me)
        sy = box.top + 92 + 4 * 52 + 14
        pygame.draw.line(surf, COL_GOLD_DK, (box.x + 40, sy), (box.right - 40, sy), 2)
        draw_text(surf, f"{me} の成績", 22, (box.x + 40, sy + 16), COL_GOLD, bold=True)
        draw_text(surf, "本日: " + StatsTracker.format_bucket(s["today"]), 20, (box.x + 40, sy + 50), COL_TEXT)
        draw_text(surf, "累計: " + StatsTracker.format_bucket(s["cumulative"]), 20, (box.x + 40, sy + 80), COL_TEXT)
        # 継続 / 終了 の選択
        mpos = self.to_virtual(pygame.mouse.get_pos())
        for b in self._result_buttons():
            b.hover = b.rect.collidepoint(mpos)
            b.draw(surf)
        draw_text(surf, "続ける / 終了 を選択 (Enter=続ける, Esc=終了)", 18,
                  (box.centerx, box.bottom - 96), COL_DIM, center=True)

    def _draw_help(self, surf):
        self._overlay(surf, 200)
        box = pygame.Rect(0, 0, 820, 660)
        box.center = (VW // 2, VH // 2)
        rrect(surf, COL_PANEL_2, box, radius=20, border=3, border_color=COL_GOLD)
        draw_text(surf, "遊び方", 40, (box.centerx, box.top + 40), COL_GOLD, bold=True, center=True)
        lines = [
            "■ 目的: 手札を早く出し切って順位を決める (大富豪/富豪/貧民/大貧民)。",
            "",
            "■ 出すカード: 手札をクリックで選択 → [出す]。選択解除は [解除]。",
            "■ パス: 場に付られないときは [パス] (親は必須)。",
            "",
            "■ 役: シングル / 同じ数字2〜4枚 / 同一マーク3連番以上の階段。",
            "   Joker は任意の数字・マークの代用 (階段は3〜Aまで)。",
            "■ 強さ: 3<...<A<2<Joker。革命中は 3 が最強に反転 (Joker は常に最強)。",
            "■ 特殊: 3のスリーカードで Joker 単体を切れる (革命時も可)。",
            "■ 革命: 同じ数字4枚 or Joker含む4枚セットが場に残り流れると成立/解消。",
            "   (試み中に強い4枚で返すと=革命返しで取り消し。階段は対象外。)",
            "■ 交換: 大貧民→貧民→富豪→大富豪の順。大貧民⇄大富豪(2枚)/貧民⇄富豪(1枚)。",
            "   Joker も交換対象 (大貧民・貧民は最強として差し出す)。",
            "■ 都落ち: 前回大富豪が1位を取れず、他の人が先に上がると大貧民確定。",
            "",
            "■ 操作: Enter=出す  Space=パス/次  M=音楽  N=効果音  F=全画面  Esc=タイトル",
        ]
        for i, ln in enumerate(lines):
            draw_text(surf, ln, 22, (box.x + 40, box.top + 100 + i * 34), COL_TEXT)
        draw_text(surf, "クリック / H で閉じる", 22, (box.centerx, box.bottom - 30), COL_DIM, center=True)

    def _overlay(self, surf, alpha):
        s = pygame.Surface((VW, VH), pygame.SRCALPHA)
        s.fill((0, 0, 0, alpha))
        surf.blit(s, (0, 0))

    # --- メインループ -----------------------------------------------------
    def run(self, max_seconds=None):
        running = True
        elapsed = 0.0
        while running:
            dt = self.clock.tick(60) / 1000.0
            dt = min(dt, 0.05)
            elapsed += dt
            if not self.handle_events():
                running = False
            self.update(dt)
            self.draw()
            if max_seconds is not None and elapsed > max_seconds:
                running = False
        pygame.quit()


def main(argv):
    smoke = "--smoke" in argv
    if smoke:
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        os.environ["SDL_AUDIODRIVER"] = "dummy"
    seed = None
    for i, a in enumerate(argv):
        if a == "--seed" and i + 1 < len(argv):
            seed = int(argv[i + 1])
    app = App(seed=seed, auto=smoke)
    if smoke:
        # 自動で対戦を開始し、数秒走らせて終了 (描画/音声/進行のクラッシュ有無を確認)
        app.start_game()
        app.run(max_seconds=6.0)
        print("SMOKE OK: GUI ran without runtime errors")
    else:
        app.run()


if __name__ == "__main__":
    main(sys.argv[1:])
