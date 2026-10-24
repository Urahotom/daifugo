# -*- coding: utf-8 -*-
"""大富豪 (Daifugo / President) ゲーム実装.

仕様書に基づく 4 人対戦 (User x1, CPU x3) の大富豪。
- 52 枚 + ジョーカー 1 枚 = 53 枚
- 革命 (カードの強さが反転)
- 役: シングル / セット (2〜4 枚) / 階段 (同一マーク 3 連番以上, Joker 代用可)
- カード交換 (大富豪<->大貧民, 富豪<->貧民)
- 都落ち

厳密実装の重点:
  * 階段 (同一マーク 3 連番) の判定  -> check_straight()
  * カード交換ロジック               -> GameMaster._exchange_cards()

使い方:
  python daifugo.py            # 対話プレイ (あなたが User)
  python daifugo.py --auto     # CPU 4 人で自動対戦 (検証用)
  python daifugo.py --selftest # ロジックの自己テスト
"""

from __future__ import annotations

import datetime
import json
import os
import random
import sys
from collections import defaultdict
from itertools import combinations
from typing import List, Optional, Tuple

__version__ = "1.0.0"  # 53 枚 / 革命 / 階段 / カード交換 (Joker 交換対象) / 都落ち / 成績記録

# Windows コンソール (cp932 等) でもマーク記号や絵文字が出力できるよう UTF-8 にする
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------------------
# カードの定数表現
# ---------------------------------------------------------------------------
# 数字 (rank) は「強さの順序」そのものを使う:
#   3..10 はそのまま, J=11, Q=12, K=13, A=14, 2=15
# ジョーカーは特別な rank (JOKER) とし、常に最強扱いの整数で表す。
JOKER = 100
JOKER_STRENGTH = 10_000  # 常に最強 (通常の数字 3..15 より必ず大きい)

SUITS = ["♠", "♥", "♦", "♣"]
RANK_NAMES = {
    3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "10",
    11: "J", 12: "Q", 13: "K", 14: "A", 15: "2",
}

# 階段 (シーケンス) に参加できる数字の範囲。
# 大富豪の慣例に従い 3〜A (2 と Joker は連番そのものには含まない)。
# Joker は「任意の数字・マークの代用」として階段に組み込める。
STRAIGHT_MIN = 3
STRAIGHT_MAX = 14  # A
STRAIGHT_MAX_LEN = STRAIGHT_MAX - STRAIGHT_MIN + 1  # 12

# 役の種類
SINGLE = "single"
SET = "set"        # 同じ数字 2〜4 枚
STRAIGHT = "straight"

# 順位 (着順) に対応する役職名
RANK_TITLES = ["大富豪", "富豪", "貧民", "大貧民"]


def rank_strength(rank: int, revolution: bool) -> int:
    """指定 rank の強さを、革命状態を考慮した比較用整数で返す。

    通常時: 3<4<...<2  -> そのまま rank を返す (3..15)。
    革命時: 3>4>...>2  -> 反転させた値を返す (3 が最強, 2 が最弱)。
    Joker : 常に最強 (JOKER_STRENGTH)。
    """
    if rank == JOKER:
        return JOKER_STRENGTH
    if not revolution:
        return rank  # 3..15
    # 革命時: 数字の順序を完全に反転 (3 最強 / 2 最弱)。3..15 を 15..3 に写像。
    return 18 - rank  # 3->15, 4->14, ..., 14->4, 15->3


# ---------------------------------------------------------------------------
# Card クラス
# ---------------------------------------------------------------------------
class Card:
    """トランプ 1 枚。数字 (rank) とマーク (suit) を持つ。"""

    __slots__ = ("rank", "suit")

    def __init__(self, rank: int, suit: str):
        self.rank = rank
        self.suit = suit

    @property
    def is_joker(self) -> bool:
        return self.rank == JOKER

    def strength(self, revolution: bool) -> int:
        return rank_strength(self.rank, revolution)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Card)
            and self.rank == other.rank
            and self.suit == other.suit
        )

    def __hash__(self) -> int:
        return hash((self.rank, self.suit))

    def __repr__(self) -> str:
        if self.is_joker:
            return "🃏J"
        return f"{self.suit}{RANK_NAMES[self.rank]}"


def make_deck() -> List[Card]:
    """52 枚 + ジョーカー 1 枚のデッキを生成する。"""
    deck = [Card(rank, suit) for rank in range(3, 16) for suit in SUITS]
    deck.append(Card(JOKER, "★"))
    return deck


# ---------------------------------------------------------------------------
# 役の判定 (厳密実装)
# ---------------------------------------------------------------------------
# 戻り値の "combo" はタプル (kind, size, strength, key) で表す。
#   kind     : SINGLE / SET / STRAIGHT
#   size     : 使用枚数
#   strength : 比較用の強さ (革命考慮)。大きいほど強い。
#   key      : 代表 rank (表示・デバッグ用)
Combo = Tuple[str, int, int, int]


def check_straight(cards: List[Card], revolution: bool) -> Optional[Combo]:
    """階段 (同一マーク 3 連番以上) かどうかを厳密に判定する。

    条件:
      1. 枚数が 3 以上 (上限は 3〜A の 12 枚)。
      2. 非 Joker カードはすべて同一マークであること。
      3. 非 Joker カードの数字は 3〜A の範囲で、重複がないこと
         (2 と Joker は連番そのものには含めない)。
      4. Joker は欠番うめ / 端の延長に使える。
      5. 全カードを 1 つの連続した数字の並び [lo, lo+n-1] に
         収められ、かつその並びが 3〜A の範囲に収まること。

    成立すれば Combo (STRAIGHT) を、否则 None を返す。
    """
    n = len(cards)
    if n < 3 or n > STRAIGHT_MAX_LEN:
        return None

    nonjokers = [c for c in cards if not c.is_joker]
    jokers = [c for c in cards if c.is_joker]

    # (2) 非 Joker は同一マークでなければならない
    suits = {c.suit for c in nonjokers}
    if len(suits) > 1:
        return None

    # (3) 数字は 3〜A の範囲 / 重複不可
    ranks = sorted(c.rank for c in nonjokers)
    for r in ranks:
        if r < STRAIGHT_MIN or r > STRAIGHT_MAX:
            return None
    if len(set(ranks)) != len(ranks):
        return None

    j = len(jokers)

    # 非 Joker が無い (全て Joker) ケース: 3〜A 内に長さ n の並びを作れる
    if not ranks:
        top = STRAIGHT_MIN + n - 1  # 例: n=3 -> 3,4,5
        return (STRAIGHT, n, rank_strength(top, revolution), top)

    min_r = ranks[0]
    max_r = ranks[-1]
    width = max_r - min_r + 1
    internal_gaps = width - len(ranks)  # 内部の欠番数
    if internal_gaps > j:
        return None  # Joker が足りていない

    # 長さ n の窓 [lo, lo+n-1] が非 Joker をすべて含み、3〜A に収まるか。
    #   lo <= min_r, lo+n-1 >= max_r, lo >= 3, lo+n-1 <= 14
    lo_min = max(STRAIGHT_MIN, max_r - n + 1)
    lo_max = min(min_r, STRAIGHT_MAX - n + 1)
    if lo_min > lo_max:
        return None  # 範囲内に窓を置けない (例: A をまたぐ / 2 を含む)

    # 強さの代表値 = 窓の最大値。内部欠番をうめた残り Joker は上側へ延長。
    remaining = j - internal_gaps
    top = min(STRAIGHT_MAX, max_r + remaining)
    bottom = top - n + 1
    if bottom < STRAIGHT_MIN:
        bottom = STRAIGHT_MIN
        top = bottom + n - 1
    return (STRAIGHT, n, rank_strength(top, revolution), top)


def identify_combo(cards: List[Card], revolution: bool) -> Optional[Combo]:
    """選択したカード列が作る役を判定する。成立しなければ None。"""
    n = len(cards)
    if n == 0:
        return None

    # シングル
    if n == 1:
        c = cards[0]
        return (SINGLE, 1, c.strength(revolution), c.rank)

    nonjokers = [c for c in cards if not c.is_joker]
    jokers = [c for c in cards if c.is_joker]

    # セット (同じ数字 2〜4 枚)。Joker は任意の数字の代用。
    if n <= 4:
        if nonjokers:
            ranks = {c.rank for c in nonjokers}
            if len(ranks) == 1:
                r = next(iter(ranks))
                return (SET, n, rank_strength(r, revolution), r)
        else:
            # 全て Joker -> 同じ数字扱い (最強)
            return (SET, n, JOKER_STRENGTH, JOKER)

    # 階段
    straight = check_straight(cards, revolution)
    if straight is not None:
        return straight

    return None


def is_single_joker(combo: Optional[Combo]) -> bool:
    """combo が Joker 単体 (シングル 1 枚) か。"""
    return (
        combo is not None
        and combo[0] == SINGLE
        and combo[1] == 1
        and combo[3] == JOKER
    )


def is_three_of_three(combo: Optional[Combo]) -> bool:
    """combo が 3 のスリーカード (同じ数字 3 の 3 枚) か。"""
    return (
        combo is not None
        and combo[0] == SET
        and combo[1] == 3
        and combo[3] == 3
    )


def can_play(cards: List[Card], table: Optional[Combo], revolution: bool) -> Optional[Combo]:
    """場 (table) に対して cards が出せるならその Combo、無理なら None。

    table が None の場合 (親 / 場なし) は、成立する役なら何でも出せる。

    特殊ルール: **3 のスリーカードは単体の Joker を切れる** (革命時も可)。
    """
    combo = identify_combo(cards, revolution)
    if combo is None:
        return None
    if table is None:
        return combo
    # 特殊ルール: 3 のスリーカードは Joker 単体を倒せる (役・枚数の一致不要)
    if is_single_joker(table) and is_three_of_three(combo):
        return combo
    t_kind, t_size, t_strength, _ = table
    c_kind, c_size, c_strength, _ = combo
    if c_kind != t_kind or c_size != t_size:
        return None
    if c_strength <= t_strength:
        return None
    return combo


def triggers_revolution(cards: List[Card], revolution: bool) -> bool:
    """そのプレイが「革命の試み」になるか (4 枚のセットか)。

    対象は **4 枚のセット (同じ数字 4 枚、または Joker を代用に含む 4 枚)** のみ。
    階段 (連番) は、Joker を含んでいても対象にならない。
    戻り値が True でも **この場で革命は成立しない**。実際に `revolution` をトグルするのは、
    その 4 枚が誰も上がれず **場が流れた瞬間** (`_run_trick` / GUI `_trick` の `pending_rev`)。
    試み中に別の 4 枚セットで上がられると (革命返し) 前の試みは取り消される。
    """
    n = len(cards)
    if n != 4:
        return False
    combo = identify_combo(cards, revolution)
    return combo is not None and combo[0] == SET


# ---------------------------------------------------------------------------
# プレイ生成 (CPU 用)
# ---------------------------------------------------------------------------
def find_all_plays(
    hand: List[Card], table: Optional[Combo], revolution: bool
) -> List[Tuple[Combo, List[Card]]]:
    """hand から table に対して出せる全プレイを列挙する。"""
    plays: List[Tuple[Combo, List[Card]]] = []

    def add(cards: List[Card]) -> None:
        combo = can_play(cards, table, revolution)
        if combo is not None:
            plays.append((combo, list(cards)))

    if table is None:
        # 親: シングル / セット / 階段 のすべてを候補にする (CPU は最強選択しない)
        for c in hand:
            add([c])
        for size in (2, 3, 4):
            for cards in _set_candidates(hand, size, revolution):
                add(cards)
            for cards in _straight_candidates(hand, size + 1):
                add(cards)
        return plays

    kind, size, _, key = table
    if kind == SINGLE:
        for c in hand:
            add([c])
        # 特殊ルール: 場が Joker 単体なら 3 のスリーカードで切れる
        if key == JOKER:
            for cards in _set_candidates(hand, 3, revolution):
                add(cards)  # can_play が 3 のスリーカードのみを通す
    elif kind == SET:
        for cards in _set_candidates(hand, size, revolution):
            add(cards)
    elif kind == STRAIGHT:
        for cards in _straight_candidates(hand, size):
            add(cards)
    return plays


def _set_candidates(
    hand: List[Card], size: int, revolution: bool
) -> List[List[Card]]:
    """指定枚数のセット (同じ数字 + Joker 代用) の候補を作る。"""
    jokers = [c for c in hand if c.is_joker]
    groups: dict[int, List[Card]] = defaultdict(list)
    for c in hand:
        if not c.is_joker:
            groups[c.rank].append(c)

    out: List[List[Card]] = []
    for r, cs in groups.items():
        if len(cs) >= size:
            out.append(cs[:size])
        elif len(cs) + len(jokers) >= size:
            need = size - len(cs)
            out.append(cs + jokers[:need])
    # 全て Joker のセット
    if len(jokers) >= size:
        out.append(jokers[:size])
    return out


def _straight_candidates(hand: List[Card], size: int) -> List[List[Card]]:
    """hand から長さ size の階段の候補を列挙する (枚数が小さい前提)。"""
    if size < 3:
        return []
    out: List[List[Card]] = []
    for cards in combinations(hand, size):
        if check_straight(list(cards), False) is not None:
            out.append(list(cards))
    return out


# ---------------------------------------------------------------------------
# CPU 思考ルーチン用の評価ヘルパ
# ---------------------------------------------------------------------------
def norm_strength(rank: int, revolution: bool) -> float:
    """比較用強さを 0.0(最弱)〜1.0(最強) に正規化。Joker は常に 1.0。"""
    if rank == JOKER:
        return 1.0
    return (rank_strength(rank, revolution) - 3) / 12.0  # 3..15 -> 0.0..1.0


def hand_power(hand: List[Card], revolution: bool) -> float:
    """手札の総合的な強さ (大きいほど有利)。革命の損得判定に使う。

    革命で強弱が反転したとき、自分の手が相対的に強くなるかを測る。
    Joker は常に最強なので革命の影響を受けない。
    """
    return sum(norm_strength(c.rank, revolution) for c in hand)


# --- CPU スコアリングの重み (調整用) ---------------------------------------
W_CARD = 3.0       # 枚数削減の基本価値
W_WEAK = 12.0      # 弱いカードを捨てる価値
W_BATCH = 6.0      # 弱いカードをまとめて切るボーナス
W_STRAIGHT = 8.0   # (弱い) 連番をまとめて切るボーナス
W_JOKER = 26.0     # Joker を安易に切らない (温存)
W_TWO = 10.0       # 通常時の 2 を安易に切らない (温存)
W_REV = 20.0       # 革命の損得
W_EXCESS = 8.0     # 追従時に場へ過剰な強さで乗せるペナルティ
BONUS_READY = 15.0  # 手札を 1 枚まで減らす (あと 1 枚であがり) ボーナス


# ---------------------------------------------------------------------------
# Player クラス
# ---------------------------------------------------------------------------
class Player:
    """プレイヤー (手札管理 + CPU 思考ルーチン)。"""

    def __init__(self, name: str, is_user: bool = False):
        self.name = name
        self.is_user = is_user
        self.hand: List[Card] = []
        self.rank: Optional[int] = None  # 0..3 (着順)。確定したら代入。

    # --- 手札管理 ---------------------------------------------------------
    def deal(self, cards: List[Card]) -> None:
        self.hand.extend(cards)

    def sort_hand(self, revolution: bool) -> None:
        self.hand.sort(key=lambda c: (c.strength(revolution), c.suit, c.rank))

    def remove(self, cards: List[Card]) -> None:
        for c in cards:
            self.hand.remove(c)

    @property
    def is_finished(self) -> bool:
        return len(self.hand) == 0

    # --- CPU 思考 ----------------------------------------------------------
    # 方針:
    #   * 一手で全部出せるなら即あがり。
    #   * 弱いカードは「まとめて」切る (単独よりセット/連番で放出する方が有利)。
    #   * (弱い) 連番が切れるときは積極的にまとめて捨てる。
    #   * 革命が発動できる手は、反転が自分に有利なら大きく評価し優先する
    #     (逆に不利な反転を避ける)。
    #   * Joker や 2 といった強い支配札は安易に切らず温存する (あがり・決定的な
    #     場取り以外では使わない)。追従時は場へ過剰な強さで乗せない。
    def cpu_choose(
        self, table: Optional[Combo], revolution: bool
    ) -> Optional[List[Card]]:
        plays = find_all_plays(self.hand, table, revolution)
        if not plays:
            return None  # パス

        # 一手で手札を全部出せる手があれば、それが最善 (確実にあがり)。
        for _combo, cards in plays:
            if len(cards) >= len(self.hand):
                return cards

        leading = table is None
        best_cards: Optional[List[Card]] = None
        best_score = float("-inf")
        for combo, cards in plays:
            score = self._score_play(cards, combo, table, revolution, leading)
            if score > best_score:
                best_score = score
                best_cards = cards

        # 追従時: 見込みの薄い手 (強い支配札を切る等) はパスを優先する。
        if not leading and best_score < 0.0:
            return None
        return best_cards

    def _score_play(
        self,
        cards: List[Card],
        combo: Combo,
        table: Optional[Combo],
        revolution: bool,
        leading: bool,
    ) -> float:
        """1 プレイの良し悪しを点数化して返す (大きいほど良い)。"""
        n = len(cards)
        remaining = list(self.hand)
        for c in cards:
            remaining.remove(c)

        score = n * W_CARD  # 枚数削減

        weak_sum = 0.0
        ns_sum = 0.0
        for c in cards:
            ns = norm_strength(c.rank, revolution)
            ns_sum += ns
            if c.is_joker:
                score -= W_JOKER          # Joker は温存
            elif c.rank == 15 and not revolution:
                score -= W_TWO            # 通常時の 2 は支配札として温存
            if ns < 0.4:
                weak_sum += (0.4 - ns)    # 弱いカードの放出を評価
        score += weak_sum * W_WEAK

        avg_ns = ns_sum / n
        # 弱いカードをまとめて切るボーナス (連番・セットboth 対応)
        if n >= 2 and avg_ns < 0.45:
            score += (n - 1) * W_BATCH
        # 弱い連番はまとめて捨てる価値が高い
        if combo[0] == STRAIGHT and avg_ns < 0.5:
            score += W_STRAIGHT

        # 革命の発動/解消を織り込む (反転後の自分の手の強さで損得を測る)
        if triggers_revolution(cards, revolution):
            new_rev = not revolution
            benefit = hand_power(remaining, new_rev) - hand_power(remaining, revolution)
            score += benefit * W_REV

        # あと 1 枚であがりに近づくのは良い
        if len(remaining) == 1:
            score += BONUS_READY

        # 追従時: 場への上乗せが小さい (最小限の強さで取る) ほど良い
        if not leading and table is not None:
            excess = norm_strength(combo[3], revolution) - norm_strength(table[3], revolution)
            score -= excess * W_EXCESS

        return score

    # --- 交換用のカード選択 ----------------------------------------------
    # Joker も交換対象。大貧民/貧民は最強のカード (持っていれば Joker) を差し出すため、
    # 交換後に大貧民・貧民が Joker を持つことはあり得ない。
    def take_strongest(self, n: int) -> List[Card]:
        """最も強いカード n 枚を手札から取り出す (大貧民/貧民の差し出し)。Joker も対象。"""
        ordered = sorted(self.hand, key=lambda c: c.strength(False), reverse=True)
        chosen = ordered[:n]
        self.remove(chosen)
        return chosen

    def take_unwanted(self, n: int) -> List[Card]:
        """最も弱いカード n 枚を手札から取り出す (大富豪/富豪の「不要なカード」)。

        自動プレイのため「不要」= 最も弱い (手札に残しても出しにくい) カードとみなす。
        Joker は最強なので最弱選出では選ばれない (＝大富豪/富豪は Joker を渡さない)。
        """
        ordered = sorted(self.hand, key=lambda c: c.strength(False))
        chosen = ordered[:n]
        self.remove(chosen)
        return chosen

    def __repr__(self) -> str:
        return f"Player({self.name})"


# ---------------------------------------------------------------------------
# 成績記録 (連勝・順位実績) を本日分と累計で永続化
# ---------------------------------------------------------------------------
STATS_FILENAME = "daifugo_stats.json"


class StatsTracker:
    """プレイヤーごとの成績を **本日分** と **累計** で記録し、JSON に保存する。

    - 勝利 = そのラウンドの 1 位 (大富豪)。
    - 連勝 (streak) = 大富豪になり続けた回数。1 位以外でリセット。
    - 順位実績 = 大富豪/富豪/貧民/大貧民 の各回数。
    - 保存先 (既定) はこのモジュールと同じフォルダの `daifugo_stats.json`。
    """

    def __init__(self, path: Optional[str] = None, enabled: bool = True):
        self.enabled = enabled
        if path is None:
            base = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(base, STATS_FILENAME)
        self.path = path
        self.data = self._load() if enabled else {"version": 1, "players": {}}

    # --- 入出力 -----------------------------------------------------------
    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("version") == 1 and isinstance(d.get("players"), dict):
                return d
        except (OSError, ValueError):
            pass
        return {"version": 1, "players": {}}

    def _save(self) -> None:
        if not self.enabled:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # --- 記録 -------------------------------------------------------------
    @staticmethod
    def _blank() -> dict:
        return {"rounds": 0, "wins": 0, "ranks": [0, 0, 0, 0], "streak": 0, "best_streak": 0}

    def _player(self, name: str) -> dict:
        rec = self.data["players"].get(name)
        if rec is None:
            rec = {"cumulative": self._blank(), "daily": {}}
            self.data["players"][name] = rec
        return rec

    def record_round(self, ranking: List[Player], date: Optional[str] = None) -> None:
        """ranking = [大富豪, 富豪, 貧民, 大貧民] の着順。1 位 (index 0) を勝利とする。"""
        if not self.enabled:
            return
        if date is None:
            date = datetime.date.today().isoformat()
        for rank_idx, p in enumerate(ranking[:4]):
            rec = self._player(p.name)
            win = 1 if rank_idx == 0 else 0
            day = rec["daily"].setdefault(date, self._blank())
            for bucket in (rec["cumulative"], day):
                bucket["rounds"] += 1
                bucket["ranks"][rank_idx] += 1
                bucket["wins"] += win
                bucket["streak"] = bucket["streak"] + 1 if win else 0
                if bucket["streak"] > bucket["best_streak"]:
                    bucket["best_streak"] = bucket["streak"]
        self._save()

    # --- 参照 -------------------------------------------------------------
    def summary(self, name: str, date: Optional[str] = None) -> dict:
        """そのプレイヤーの {'today':..., 'cumulative':...} を返す (各は _blank 相当)。"""
        if date is None:
            date = datetime.date.today().isoformat()
        rec = self.data["players"].get(name)
        if not rec:
            return {"today": self._blank(), "cumulative": self._blank()}
        return {
            "today": rec["daily"].get(date, self._blank()),
            "cumulative": rec["cumulative"],
        }

    def all_names(self) -> List[str]:
        return sorted(self.data["players"].keys())

    @staticmethod
    def format_bucket(b: dict) -> str:
        r = b["ranks"]
        return (
            f"{b['rounds']}戦{b['wins']}勝 "
            f"(大富豪{r[0]}/富豪{r[1]}/貧民{r[2]}/大貧民{r[3]}) "
            f"連勝{b['streak']}(最長{b['best_streak']})"
        )


# ---------------------------------------------------------------------------
# GameMaster クラス
# ---------------------------------------------------------------------------
class GameMaster:
    """ゲーム進行の管理 (配布 / 巡番 / 流し / 革命 / 交換 / 都落ち)。"""

    def __init__(
        self,
        names: Optional[List[str]] = None,
        seed: Optional[int] = None,
        record_stats: bool = True,
        stats_path: Optional[str] = None,
    ):
        names = names or ["あなた", "CPU-A", "CPU-B", "CPU-C"]
        self.players = [Player(names[0], is_user=True)] + [
            Player(n) for n in names[1:]
        ]
        self.rng = random.Random(seed)
        self.revolution = False
        self.prev_ranking: Optional[List[Player]] = None  # 前回の着順 (0..3)
        self.round_no = 0
        self.stats = StatsTracker(path=stats_path, enabled=record_stats)

    # --- 配布 -------------------------------------------------------------
    def _deal(self) -> None:
        for p in self.players:
            p.hand = []
            p.rank = None
        deck = make_deck()
        self.rng.shuffle(deck)
        for i, card in enumerate(deck):
            self.players[i % len(self.players)].deal([card])
        for p in self.players:
            p.sort_hand(self.revolution)

    # --- カード交換 (厳密実装) -------------------------------------------
    def _exchange_cards(self) -> List[str]:
        """前回順位に基づく自動カード交換。ログのリストを返す。

        切る (差し出す) 順序は **大貧民 → 貧民 → 富豪 → 大富豪**。
        まず 4 人がそれぞれ手札から出すカードを選出 (Joker も交換対象) してから、
        まとめて受け渡しを行う (選出は元の手の状態で行うので、受け取った直後の
        カードをそのまま相手に返す、といったバグは起こらない)。
        """
        assert self.prev_ranking is not None
        ranking = self.prev_ranking  # [大富豪, 富豪, 貧民, 大貧民]
        if len(ranking) < 4:
            return []
        rich, middle_up, middle_down, poor = ranking[0], ranking[1], ranking[2], ranking[3]
        log: List[str] = []

        # 1) 大貧民: 最強 2 枚 (Joker 含む) を大富豪へ差し出す
        give_poor = poor.take_strongest(2)
        # 2) 貧民: 最強 1 枚 (Joker 含む) を富豪へ差し出す
        give_down = middle_down.take_strongest(1)
        # 3) 富豪: 最弱 1 枚を貧民へ返す
        give_up = middle_up.take_unwanted(1)
        # 4) 大富豪: 最弱 2 枚を大貧民へ返す
        give_rich = rich.take_unwanted(2)

        # まとめて受け渡し (枚数保存・二重計上なし)
        rich.hand.extend(give_poor)
        poor.hand.extend(give_rich)
        middle_up.hand.extend(give_down)
        middle_down.hand.extend(give_up)

        log.append(f"  大貧民({poor.name}) → 大富豪({rich.name}): {_fmt(give_poor)}")
        log.append(f"  貧民({middle_down.name}) → 富豪({middle_up.name}): {_fmt(give_down)}")
        log.append(f"  富豪({middle_up.name}) → 貧民({middle_down.name}): {_fmt(give_up)}")
        log.append(f"  大富豪({rich.name}) → 大貧民({poor.name}): {_fmt(give_rich)}")

        for p in self.players:
            p.sort_hand(self.revolution)
        return log

    # --- 1 ラウンド実施 ---------------------------------------------------
    def play_round(self, auto_user: bool = False) -> List[Player]:
        self.round_no += 1
        self.revolution = False
        self._deal()

        print("\n" + "=" * 60)
        print(f"第 {self.round_no} ラウンド開始")

        # 2 回目以降はカード交換
        if self.prev_ranking is not None:
            print("\n[カード交換]")
            for line in self._exchange_cards():
                print(line)

        # 前回大富豪 (都落ち判定の対象)。1 位になれなければ大貧民確定。
        prev_big_rich = self.prev_ranking[0] if self.prev_ranking else None

        ranking = self._run_trick(auto_user, prev_big_rich)
        self.prev_ranking = ranking

        print("\n[結果]")
        for i, p in enumerate(ranking):
            p.rank = i
            print(f"  {i + 1}位 {RANK_TITLES[i]}: {p.name}")

        # 成績記録 (本日 / 累計)
        if self.stats.enabled:
            self.stats.record_round(ranking)
            print("\n[成績]")
            for p in ranking:
                s = self.stats.summary(p.name)
                print(f"  {p.name}:")
                print(f"    本日: {StatsTracker.format_bucket(s['today'])}")
                print(f"    累計: {StatsTracker.format_bucket(s['cumulative'])}")
        return ranking

    def _run_trick(self, auto_user: bool, prev_big_rich: Optional[Player]) -> List[Player]:
        finished: List[Player] = []
        ochi: Optional[Player] = None  # 都落ちしたプレイヤー

        # ユーザーがあがり/都落ちで手番から外れた以降は CPU 同士の演出を省略し高速送りにする
        # (自動対戦は検証用なので従来どおり全表示)
        user_player: Optional[Player] = (
            None if auto_user else next((p for p in self.players if p.is_user), None)
        )
        fast = False

        # 巡番 (カードを切る順序): 順位が決まっている (2 巡目以降) は前回順位の逆順で
        #   1:大貧民 → 2:貧民 → 3:富豪 → 4:大富豪 の順に出す (大貧民が先出し)。
        #   初回は席順でランダムな親。
        if self.prev_ranking and len(self.prev_ranking) >= 4:
            active: List[Player] = list(reversed(self.prev_ranking))
            current: Player = active[0]  # 大貧民が先出し
        else:
            active = list(self.players)
            current = self.rng.choice(active)

        table: Optional[Combo] = None
        last_player: Optional[Player] = None
        passed: set = set()
        pending_rev = False  # 場が 4 枚セット (革命の試み) か。流れて初めて成立する。

        while len(active) > 1:
            if current not in active:
                current = active[0]

            # 流し判定: 場があり、自分以外 (最終出牌者を除く) が全員パス済み
            if table is not None:
                others = [p for p in active if p is not last_player]
                if others and all(p in passed for p in others):
                    # 場が流れる = 最後の出牌が勝ち。4 枚セットならここで革命が成立/解消する。
                    if pending_rev:
                        self.revolution = not self.revolution
                        if not fast:
                            print(f"  *** 革命{'成立' if self.revolution else '解消'}! ***")
                        for p in self.players:
                            p.sort_hand(self.revolution)
                        pending_rev = False
                    table = None
                    passed.clear()
                    if last_player in active:
                        current = last_player
                        if not fast:
                            print("  → 場が流れた。親が自由にだせる。")

            # プレイ選択
            if table is None:
                cards = self._choose_free(current, auto_user)
            else:
                cards = self._choose_follow(current, table, auto_user)

            idx = active.index(current)
            next_player = active[(idx + 1) % len(active)]

            if cards is None:
                passed.add(current)
                if not fast:
                    print(f"  {current.name}: パス")
            else:
                combo = identify_combo(cards, self.revolution)
                current.remove(cards)
                attempt = triggers_revolution(cards, self.revolution)
                if not fast:
                    self._announce_play(current, cards, combo)
                    if attempt and pending_rev:
                        print("  *** 革命返し! (それまでの革命は不成立) ***")
                    elif attempt:
                        print("  (4 枚セット — 場が流れると革命成立)")
                # 場が 4 枚セットなら「革命の試み」を保持 (流れるまで成立しない)
                pending_rev = attempt
                table = combo
                last_player = current
                passed.clear()

                if current.is_finished:
                    finished.append(current)
                    if not fast:
                        print(f"  ** {current.name} があがった！({len(finished)}位) **")
                    # 都落ち: 前回大富豪が 1 位を取れなかった瞬間
                    if (
                        len(finished) == 1
                        and prev_big_rich is not None
                        and finished[0] is not prev_big_rich
                        and prev_big_rich in active
                    ):
                        ochi = prev_big_rich
                        active.remove(ochi)
                        if not fast:
                            print(
                                f"  *** 都落ち: 前回大富豪 {ochi.name} は大貧民確定 "
                                f"(リタイア) ***"
                            )
                    active.remove(current)
                    if next_player is ochi or next_player not in active:
                        next_player = active[idx % len(active)] if active else None

            if not active:
                break

            # ユーザーがあがり/都落ちで手番から外れたら、以降の CPU 同士の演出を省略
            if user_player is not None and not fast and user_player not in active:
                fast = True
                print("  ── ここから CPU 同士の対戦 (演出省略・高速送り) ──")

            current = next_player

        # 残ったプレイヤー (手札あり) を着順に
        for p in active:
            finished.append(p)
        # 都落ちは必ず最後 (大貧民)
        if ochi is not None and ochi not in finished:
            finished.append(ochi)
        return finished[:4]

    # --- プレイ選択のラッパ ----------------------------------------------
    def _choose_free(self, player: Player, auto_user: bool) -> List[Card]:
        if player.is_user and not auto_user:
            return self._prompt_user(player, None)  # 親は必ず出す (内部で再入力)
        return player.cpu_choose(None, self.revolution)

    def _choose_follow(
        self, player: Player, table: Combo, auto_user: bool
    ) -> Optional[List[Card]]:
        if player.is_user and not auto_user:
            return self._prompt_user(player, table)  # None = パス
        return player.cpu_choose(table, self.revolution)

    # --- 入力・表示 -------------------------------------------------------
    def _prompt_user(
        self, player: Player, table: Optional[Combo]
    ) -> Optional[List[Card]]:
        """ユーザーに入力を促し、有効なプレイ (または パス=None) を返す。

        不正入力や親のパス要求は内部で再入力させる。
        """
        while True:
            player.sort_hand(self.revolution)
            print(f"\n場のカード: {_fmt_combo(table, self.revolution)}")
            print(f"你的手札 ({len(player.hand)}枚):")
            for i, c in enumerate(player.hand):
                print(f"  [{i}] {c}", end="")
                if (i + 1) % 4 == 0:
                    print()
            if len(player.hand) % 4 != 0:
                print()
            tip = (
                "数字 (カンマ区切り) / 'p' でパス"
                if table is not None
                else "出すカードの数字 (カンマ区切り)"
            )
            raw = input(f"  → {tip}: ").strip()
            if raw.lower() == "p":
                if table is None:
                    print("  親のときはパスできません。カードを出してください。")
                    continue
                return None  # パス
            try:
                idxs = [int(x) for x in raw.replace(" ", "").split(",") if x != ""]
            except ValueError:
                print("  入力が不正です。")
                continue
            if not idxs:
                print("  何も選択されていません。")
                continue
            try:
                cards = [player.hand[i] for i in idxs]
            except IndexError:
                print("  範囲外の番号です。")
                continue
            if can_play(cards, table, self.revolution) is None:
                print("  その組み合わせは出せません (場と役/枚数/強さを確認)。")
                continue
            return cards

    def _announce_play(
        self, player: Player, cards: List[Card], combo: Optional[Combo]
    ) -> None:
        kind = {SINGLE: "シングル", SET: "セット", STRAIGHT: "階段"}.get(
            combo[0] if combo else "", "?"
        )
        print(f"  {player.name}: {_fmt(cards)}  ({kind})")


# ---------------------------------------------------------------------------
# 表示ヘルパ
# ---------------------------------------------------------------------------
def _fmt(cards: List[Card]) -> str:
    return " ".join(str(c) for c in cards) if cards else "(なし)"


def _fmt_combo(table: Optional[Combo], revolution: bool) -> str:
    if table is None:
        return "(なし / 親)"
    kind, size, _, key = table
    kind_name = {SINGLE: "シングル", SET: f"{size}カード", STRAIGHT: f"{size}枚階段"}.get(kind, "?")
    key_name = "Joker" if key == JOKER else RANK_NAMES.get(key, str(key))
    return f"{kind_name} ({key_name})"


# ---------------------------------------------------------------------------
# 自己テスト
# ---------------------------------------------------------------------------
def _c(rank: int, suit: str = "♠") -> Card:
    return Card(rank, suit)


def selftest() -> int:
    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        print(f"  [{'OK' if cond else 'NG'}] {name}")
        if not cond:
            ok = False

    print("== 階段 (check_straight) ==")
    # 成立
    check("♠5,6,7 は階段", check_straight([_c(5), _c(6), _c(7)], False) is not None)
    check(
        "♠Q,K,A は階段 (top=A)",
        check_straight([_c(12), _c(13), _c(14)], False)[3] == 14,
    )
    check(
        "♠5,7,Joker(=6) は階段",
        check_straight([_c(5), _c(7), Card(JOKER, "★")], False) is not None,
    )
    check(
        "3〜A 12枚は階段",
        check_straight([_c(r) for r in range(3, 15)], False) is not None,
    )
    check(
        "Joker,Joker,♠5 は階段 (5,6,7)",
        check_straight([Card(JOKER, "★"), Card(JOKER, "★"), _c(5)], False) is not None,
    )
    # 不適
    check("♠5,6,♥7 はマーク違いで NG", check_straight([_c(5), _c(6), _c(7, "♥")], False) is None)
    check("♠A,2,3 は NG (2 は連番不可)", check_straight([_c(14), _c(15), _c(3)], False) is None)
    check("♠K,A,2 は NG", check_straight([_c(13), _c(14), _c(15)], False) is None)
    check("♠5,5,6 は重複で NG", check_straight([_c(5), _c(5, "♠"), _c(6)], False) is None)
    check("2枚は階段で NG", check_straight([_c(5), _c(6)], False) is None)
    check(
        "♠5,6,7,8,9,10,J,Q,K,A,2 は 2 を含み NG",
        check_straight([_c(r) for r in range(5, 16)], False) is None,
    )
    check(
        "内部欠番に Joker 不足は NG (♠3,7,Joker)",
        check_straight([_c(3), _c(7), Card(JOKER, "★")], False) is None,
    )

    print("== 強さと革命 ==")
    check("通常: 2 > A", rank_strength(15, False) > rank_strength(14, False))
    check("通常: Joker 最強", rank_strength(JOKER, False) > rank_strength(15, False))
    check("革命: 3 > 4", rank_strength(3, True) > rank_strength(4, True))
    check("革命: 3 > 2", rank_strength(3, True) > rank_strength(15, True))
    check("革命: Joker は依然最強", rank_strength(JOKER, True) > rank_strength(3, True))

    print("== 役の判定 (identify_combo) ==")
    check("♠7 1枚 = single", identify_combo([_c(7)], False)[0] == SINGLE)
    check("♠7♥7 = set(2)", identify_combo([_c(7), _c(7, "♥")], False)[0] == SET)
    check("♠7♥7♦7 = set(3)", identify_combo([_c(7), _c(7, "♥"), _c(7, "♦")], False)[1] == 3)
    check(
        "♠7 + Joker = set(2) の 7",
        identify_combo([_c(7), Card(JOKER, "★")], False)[0] == SET,
    )

    print("== 革命トリガ (triggers_revolution) ==")
    check("同じ数字 4 枚は発動", triggers_revolution([_c(7), _c(7, "♥"), _c(7, "♦"), _c(7, "♣")], False))
    check(
        "Joker 含む 4 セット (7,7,7,Joker) は発動",
        triggers_revolution([_c(7), _c(7, "♥"), _c(7, "♦"), Card(JOKER, "★")], False),
    )
    check(
        "4 枚の階段 (♠5,6,7,8) は発動しない",
        not triggers_revolution([_c(5), _c(6), _c(7), _c(8)], False),
    )
    check(
        "Joker 含む 4 枚の階段 (♠5,6,7,Joker) は発動しない",
        not triggers_revolution([_c(5), _c(6), _c(7), Card(JOKER, "★")], False),
    )
    check("3 枚 (7,7,7) は発動しない", not triggers_revolution([_c(7), _c(7, "♥"), _c(7, "♦")], False))

    print("== Joker 切り (3 のスリーカード) ==")
    joker_single = (SINGLE, 1, JOKER_STRENGTH, JOKER)
    three_3 = [_c(3), _c(3, "♥"), _c(3, "♦")]
    three_7 = [_c(7), _c(7, "♥"), _c(7, "♦")]
    check("3 のスリーカードは Joker 単体を切れる", can_play(three_3, joker_single, False) is not None)
    check("3 のスリーカードは Joker 単体を切れる (革命時)", can_play(three_3, joker_single, True) is not None)
    check("7 のスリーカードでは Joker を切れない", can_play(three_7, joker_single, False) is None)
    check("3 のペアでは Joker を切れない", can_play([_c(3), _c(3, "♥")], joker_single, False) is None)
    check("3 のスリーカードは Joker 以外 (A 単体) には出せない",
          can_play(three_3, (SINGLE, 1, rank_strength(14, False), 14), False) is None)
    # CPU が Joker 単体に対して 3 のスリーカードを候補に含むか
    p = Player("CPU")
    p.hand = [_c(3), _c(3, "♥"), _c(3, "♦"), _c(9)]
    plays = find_all_plays(p.hand, joker_single, False)
    check("find_all_plays: Joker 単体に 3 のスリーカードが候補に入る",
          any(len(cs) == 3 and all(c.rank == 3 for c in cs) for _combo, cs in plays))

    print("== カード交換ロジック ==")
    gm = GameMaster(seed=1)
    a, b, c_, d = gm.players
    a.hand = [_c(3), _c(4), _c(5), _c(6)]          # 大富豪 (弱い手札)
    d.hand = [_c(13), _c(14), _c(15), Card(JOKER, "★")]  # 大貧民 (強い手札 + Joker)
    b.hand = [_c(7), _c(8)]
    c_.hand = [_c(9), _c(10)]
    gm.prev_ranking = [a, b, c_, d]
    gm._exchange_cards()
    # 交換の切る順: 大貧民→貧民→富豪→大富豪。Joker も交換対象 (最強なので大貧民が差し出す)。
    # 大貧民の最強 2 枚 (Joker, 2) が大富豪へ、大富豪の最弱 2 枚 (3,4) が大貧民へ。
    check("大富豪が Joker を受け取った", any(x.is_joker for x in a.hand))
    check("大富豪が 2 を受け取った", any(x.rank == 15 for x in a.hand))
    check("大富豪から 3 が消えた", not any(x.rank == 3 for x in a.hand))
    check("大貧民に 3 が渡った", any(x.rank == 3 for x in d.hand))
    check("大貧民から Joker が消えた (渡した)", not any(x.is_joker for x in d.hand))
    check("大貧民から 2 が消えた", not any(x.rank == 15 for x in d.hand))
    # 富豪<->貧民 (1 枚)
    check("富豪が 10 を受け取った", any(x.rank == 10 for x in b.hand))
    check("貧民が 7 を受け取った", any(x.rank == 7 for x in c_.hand))

    # 貧民が Joker を持っていれば最強として差し出す (交換対象)。交換後に貧民は Joker を持たない。
    gm2 = GameMaster(seed=2)
    a2, b2, c2, d2 = gm2.players
    a2.hand = [_c(3), _c(4)]
    b2.hand = [_c(7), _c(8)]
    c2.hand = [_c(9), Card(JOKER, "★")]   # 貧民が Joker を持つ
    d2.hand = [_c(11), _c(12)]
    gm2.prev_ranking = [a2, b2, c2, d2]
    gm2._exchange_cards()
    check("貧民は Joker を差し出す (富豪が受け取る)", any(x.is_joker for x in b2.hand))
    check("貧民から Joker が消えた", not any(x.is_joker for x in c2.hand))
    check("貧民は 9 を保持", any(x.rank == 9 for x in c2.hand))

    print("== CPU 思考ルーチン ==")
    # (1) 弱いカードはまとめて切る (一对を単出より優先)
    p = Player("CPU")
    p.hand = [_c(3), _c(3, "♥"), _c(11), _c(13), _c(15), Card(JOKER, "★")]
    lead = p.cpu_choose(None, False)
    check(
        "CPU: 弱いカードはまとめて切る (一对)",
        lead is not None and len(lead) == 2 and all(c.rank == 3 for c in lead),
    )
    # (2) Joker を安易に切らずパスする (A の場に Joker でしか勝てない)
    p2 = Player("CPU")
    p2.hand = [_c(5), _c(9), Card(JOKER, "★")]
    table_a = (SINGLE, 1, rank_strength(14, False), 14)
    check("CPU: Joker を温存してパスする", p2.cpu_choose(table_a, False) is None)
    # (3) 自分に有利な革命は発動する (3 の 4 枚 + 低め中心の手札)
    p3 = Player("CPU")
    p3.hand = [_c(3), _c(3, "♥"), _c(3, "♦"), _c(3, "♣"), _c(4), _c(5), _c(6)]
    lead3 = p3.cpu_choose(None, False)
    check(
        "CPU: 有利な革命を発動する (3 の 4 枚)",
        lead3 is not None and len(lead3) == 4 and all(c.rank == 3 for c in lead3),
    )
    # (4) 一手で全出しできるなら即あがり
    p4 = Player("CPU")
    p4.hand = [_c(8), _c(8, "♥")]
    lead4 = p4.cpu_choose(None, False)
    check("CPU: 全出しできる手を優先 (あがり)", lead4 is not None and len(lead4) == 2)

    print("\n自己テスト結果:", "すべて OK" if ok else "一部 NG")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def _print_stats() -> int:
    """保存済みの成績 (本日 / 累計) を一覧表示する。"""
    st = StatsTracker()
    names = st.all_names()
    print(f"== 成績 ({os.path.basename(st.path)}) ==")
    if not names:
        print("  (記録なし)")
        return 0
    today = datetime.date.today().isoformat()
    print(f"  本日: {today}")
    for name in names:
        s = st.summary(name)
        print(f"  {name}:")
        print(f"    本日: {StatsTracker.format_bucket(s['today'])}")
        print(f"    累計: {StatsTracker.format_bucket(s['cumulative'])}")
    return 0


def main(argv: List[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--stats" in argv:
        return _print_stats()

    auto = "--auto" in argv
    seed = None
    for i, a in enumerate(argv):
        if a == "--seed" and i + 1 < len(argv):
            seed = int(argv[i + 1])

    print(f"大富豪 v{__version__}  (交換: 大貧民→貧民→富豪→大富豪 / Joker交換あり / 成績記録)")

    # 自動対戦は検証用なので成績を記録しない (実プレイのみ記録)
    gm = GameMaster(seed=seed, record_stats=not auto)
    try:
        while True:
            gm.play_round(auto_user=auto)
            if auto:
                # 自動対戦は 2 ラウンドまで (交換・都落ちの確認用)
                if gm.round_no >= 2:
                    break
                continue
            again = input("\nゲームを継続しますか? (y=続ける / n=終了): ").strip().lower()
            if again != "y":
                print("ゲームを終了します。")
                break
    except KeyboardInterrupt:
        print("\n中断しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
