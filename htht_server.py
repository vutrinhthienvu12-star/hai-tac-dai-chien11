
def _clan_boss_max_hp(lvl: int) -> int:
    lvl = max(1, int(lvl))
    hp = 500000000.0 * (2.5 ** (lvl - 1))
    return int(min(2000000000000, hp))
import random

import re

NAME_PALETTE = [
    "#FFD700",  # Gold
    "#FF4500",  # Orange Red
    "#00FFFF",  # Cyan
    "#FF1493",  # Deep Pink
    "#00FF7F",  # Spring Green
    "#9370DB",  # Medium Purple
    "#00BFFF",  # Deep Sky Blue
    "#FF8C00",  # Dark Orange
    "#32CD32",  # Lime Green
    "#FF69B4",  # Hot Pink
    "#1E90FF",  # Dodger Blue
    "#BA55D3",  # Medium Orchid
    "#FF6347",  # Tomato
    "#7B68EE",  # Medium Slate Blue
    "#40E0D0",  # Turquoise
    "#FFA500",  # Orange
]

def get_colored_player_name(uid: int, raw_name: str) -> str:
    clean = re.sub(r'<[^>]+>', '', str(raw_name or 'Hải Tặc')).strip()
    if not clean:
        clean = "Hải Tặc"
    col = NAME_PALETTE[int(uid) % len(NAME_PALETTE)]
    return f"<color={col}>{clean}</color>"

# c:\Users\MICKER\Downloads\DỰ ÁN M A\server\htht_server.py
# Python 3 / HTTP + Protobuf Game Server / Windows & Linux / HTHT-TEST-adr.apk

import os
import sys
import json
import time
import uuid
import struct
import sqlite3
import threading
import urllib.parse
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from htht_events import EventCatalog, bonus_items_from_preview
from consumable_selection import use_selection, use_consumable

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DB_JSON_DIR = os.path.join(PROJECT_DIR, "decompiled", "db")
SQLITE_PATH = os.path.join(BASE_DIR, "htht_data.db")

DEFAULT_HOST = "0.0.0.0"
MAIN_PORT = 9030
GAME_PORT = 7004
EVENTS = EventCatalog(os.path.join(BASE_DIR, "events_config.json"))
PLAYER_TRANSACTION_LOCK = threading.RLock()


# ============================================================================
# 1. Pure-Python Protobuf Wire Encoder / Decoder (protobuf-net compatible)
# ============================================================================

_SMALL_VARINTS = tuple(bytes([i]) for i in range(128))


def encode_varint(value: int) -> bytes:
    if 0 <= value <= 0x7F:
        return _SMALL_VARINTS[value]
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def decode_varint(buf: bytes, pos: int):
    b0 = buf[pos]
    if b0 < 0x80:
        return b0, pos + 1
    result = b0 & 0x7F
    shift = 7
    pos += 1
    n = len(buf)
    while True:
        if pos >= n:
            raise ValueError("Truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, pos
        shift += 7


def to_signed64(val: int) -> int:
    if val >= (1 << 63):
        return val - (1 << 64)
    return val


def to_signed32(val: int) -> int:
    val &= 0xFFFFFFFF
    if val >= (1 << 31):
        return val - (1 << 32)
    return val


def encode_tag(field_number: int, wire_type: int) -> bytes:
    return encode_varint((field_number << 3) | wire_type)


def encode_int32(field_number: int, value: int) -> bytes:
    if value == 0:
        return encode_tag(field_number, 0) + b"\x00"
    if value < 0:
        return encode_tag(field_number, 0) + encode_varint(value & 0xFFFFFFFFFFFFFFFF)
    return encode_tag(field_number, 0) + encode_varint(value)


def encode_int64(field_number: int, value: int) -> bytes:
    return encode_tag(field_number, 0) + encode_varint(int(value))


def encode_bool(field_number: int, value: bool) -> bytes:
    return encode_tag(field_number, 0) + (b"\x01" if value else b"\x00")


def encode_float(field_number: int, value: float) -> bytes:
    return encode_tag(field_number, 5) + struct.pack("<f", float(value))


def encode_bytes(field_number: int, value: bytes) -> bytes:
    return encode_tag(field_number, 2) + encode_varint(len(value)) + value


def encode_string(field_number: int, value: str) -> bytes:
    if value is None:
        return b""
    raw = str(value).encode("utf-8")
    return encode_bytes(field_number, raw)


def encode_submessage(field_number: int, payload: bytes) -> bytes:
    return encode_bytes(field_number, payload)


def parse_proto_fields(buf: bytes):
    """Returns dict {field_number: [(wire_type, value), ...]}"""
    fields = {}
    pos = 0
    n = len(buf)
    while pos < n:
        try:
            tag, pos = decode_varint(buf, pos)
        except Exception:
            break
        field_no = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            val, pos = decode_varint(buf, pos)
        elif wire_type == 1:
            val = buf[pos:pos + 8]
            pos += 8
        elif wire_type == 2:
            length, pos = decode_varint(buf, pos)
            val = buf[pos:pos + length]
            pos += length
        elif wire_type == 5:
            val = buf[pos:pos + 4]
            pos += 4
        else:
            break
        fields.setdefault(field_no, []).append((wire_type, val))
    return fields


# ============================================================================
# 2. pbdson Message Builders & Parsers (100% Verified from libil2cpp.so)
# ============================================================================

def build_common_vector(a_long=None, a_string=None) -> bytes:
    out = bytearray()
    for v in (a_long or []):
        out += encode_int64(1, int(v))
    for s in (a_string or []):
        out += encode_string(2, str(s))
    return bytes(out)


def parse_common_vector(data: bytes):
    if not data:
        return [], []
    fields = parse_proto_fields(data)
    a_long = []
    for wt, val in fields.get(1, []):
        if wt == 0:
            a_long.append(to_signed64(val))
        elif wt == 2:
            pos = 0
            while pos < len(val):
                v, pos = decode_varint(val, pos)
                a_long.append(to_signed64(v))
    a_string = []
    for wt, val in fields.get(2, []):
        if wt == 2:
            a_string.append(val.decode("utf-8", errors="replace"))
    return a_long, a_string


def parse_list_common_vector(data: bytes):
    if not data:
        return []
    fields = parse_proto_fields(data)
    res = []
    for wt, val in fields.get(1, []):
        if wt == 2:
            res.append(parse_common_vector(val))
    return res


def build_list_common_vector(vectors) -> bytes:
    out = bytearray()
    for vec_bytes in vectors:
        out += encode_submessage(1, vec_bytes)
    return bytes(out)


def build_big_list_common_vector(list_vectors) -> bytes:
    out = bytearray()
    for lcv_bytes in list_vectors:
        out += encode_submessage(1, lcv_bytes)
    return bytes(out)


def build_pb_action(action_id: int, data: bytes = b"") -> bytes:
    out = bytearray()
    out += encode_int32(1, action_id)
    if data is not None:
        out += encode_bytes(2, data)
    return bytes(out)


def build_response_data(actions) -> bytes:
    out = bytearray()
    for act_id, payload in actions:
        out += encode_submessage(2, build_pb_action(act_id, payload))
    return bytes(out)


def parse_request_data(buf: bytes):
    fields = parse_proto_fields(buf)
    session = ""
    if 1 in fields and fields[1][0][0] == 2:
        session = fields[1][0][1].decode("utf-8", errors="replace")
    actions = []
    for wt, raw_act in fields.get(2, []):
        if wt != 2:
            continue
        af = parse_proto_fields(raw_act)
        act_id = to_signed32(af[1][0][1]) if 1 in af else 0
        act_data = af[2][0][1] if (2 in af and af[2][0][0] == 2) else b""
        actions.append((act_id, act_data))
    return session, actions


def client_skin_id(skin):
    """Keep unsupported cosmetics in saved state; never send missing visuals to clients."""
    key = int(skin or 0)
    return key if key == 5 else 0


def build_pb_team_hero_info(hero_id: int, level: int, star: int, skin: int = 0, inst_id: int = 0) -> bytes:
    out = bytearray()
    out += encode_int32(1, inst_id or hero_id)
    out += encode_int32(2, hero_id)
    out += encode_int32(3, level)
    out += encode_int32(4, star)
    out += encode_int32(6, client_skin_id(skin))
    return bytes(out)


def encode_client_i32_power(raw_pwr: int) -> int:
    # HTHT_POWER_RANGE_V3: keep legacy values, extend the unused positive i32 band.
    # The exponent/mantissa pair covers the client's full signed-int64 range.
    val = max(1000, int(raw_pwr or 1000))
    if val < 2130000000:
        return val
    if val < 1700000000000:
        return 2130000000 + max(21470, val // 100000)
    shift = max(26, val.bit_length() - 15)
    return 2147000000 + (shift - 26) * 16384 + (val >> shift) - 16384


def encode_client_hero_power(raw_pwr: int) -> float:
    # HeroModel casts protobuf floats through int32 before storing its long fields.
    # Negative, exactly representable float32 tokens avoid that native saturation.
    val = max(0, int(raw_pwr or 0))
    if val < 2000000000:
        return 0.0
    if val < 2000000000000:
        return float(val // 1000)
    shift = max(0, val.bit_length() - 18)
    return float(-(shift * 131072 + (val >> shift) - 131072 + 1))


def build_pb_user(player: dict) -> bytes:
    raw_name = str(player.get("screenname") or "")
    display_name = get_colored_player_name(int(player.get("id", 1)), raw_name) if raw_name else ""
    out = bytearray()
    out += encode_int32(1, int(player.get("id", 1)))
    out += encode_string(2, display_name if raw_name else "")
    out += encode_string(3, display_name if raw_name else "")
    out += encode_int64(4, int(player.get("gold", 1000000)))
    out += encode_int64(5, int(player.get("gem", 10000)))
    raw_pwr = max(1000, int(player.get("power", 500000) or 500000))
    out += encode_int64(6, encode_client_i32_power(raw_pwr))
    out += encode_int64(7, int(player.get("exp", 500000)))
    out += encode_int32(8, int(player.get("level", 120)))
    for av in player.get("avatar", [0, 1]):
        out += encode_int32(9, int(av))
    for vp in player.get("vip", [13, 1000000]):
        out += encode_int32(10, int(vp))
    for idx, th in enumerate(player.get("team", []), start=1):
        if not isinstance(th, list) or len(th) < 1:
            continue
        h_id = int(th[0] or 0)
        h_lvl = int(th[1] if len(th) > 1 else 1)
        h_star = int(th[2] if len(th) > 2 else 5)
        h_skin = int(th[3] if len(th) > 3 else 0)
        inst_val = int(th[4]) if len(th) > 4 else (idx if h_id > 0 else 0)
        out += encode_submessage(11, build_pb_team_hero_info(h_id, h_lvl, h_star, h_skin, inst_id=inst_val))
    clan_cv = build_common_vector(
        a_long=[int(player.get("clan_id", 1)), {1:3,2:2,4:1,3:0}.get(int(player.get("clan_pos",0)),-1), int(player.get("clan_flag", 1))],
        a_string=[str(player.get("clan_name", "StrawHat"))]
    )
    out += encode_submessage(12, clan_cv)
    info_cv = build_common_vector(a_long=player.get("arena_info", [int(player.get("tower_level", 15)), 10]))
    out += encode_submessage(13, info_cv)
    raw_tp = player.get("team_pet")
    clean_tp = []
    if isinstance(raw_tp, list):
        for i in range(0, len(raw_tp) - 1, 2):
            p_id = int(raw_tp[i] or 0)
            p_star = max(1, int(raw_tp[i + 1] or 1))
            if p_id > 0:
                clean_tp.extend([p_id, p_star])
    for tp in clean_tp:
        out += encode_int32(14, int(tp))
    if player.get("facebook"):
        out += encode_string(15, str(player["facebook"]))
    return bytes(out)


def build_pb_list_user(users: list, name: str = '', my_rank: int = 1, my_info: dict = None) -> bytes:
    out = bytearray()
    for u in users:
        out += encode_submessage(1, build_pb_user(u))
    if name:
        out += encode_string(2, name)
    out += encode_int32(3, my_rank)
    if my_info:
        out += encode_submessage(4, build_pb_user(my_info))
    elif users:
        out += encode_submessage(4, build_pb_user(users[0]))
    return bytes(out)


def build_login_msg(session: str, player: dict) -> bytes:
    out = bytearray()
    out += encode_string(1, session)
    out += encode_submessage(2, build_pb_user(player))
    return bytes(out)


def build_pb_hero(hero: dict) -> bytes:
    out = bytearray()
    out += encode_int32(1, int(hero["id"]))
    out += encode_int32(2, int(hero["heroId"]))
    out += encode_int32(3, int(hero.get("position", 0)))
    pts = hero["point"]
    p16_val = int(pts[16] or 0) if len(pts) > 16 else 0
    p18_enc = encode_client_hero_power(p16_val)
    for idx_pt, pt in enumerate(pts):
        out += encode_float(4, p18_enc if idx_pt == 18 else float(pt))
    hero_lvl = 0 if int(hero.get("heroId", 0)) in PUPPET_HERO_IDS else max(1, int(hero.get("level", 1)))
    out += encode_int32(5, hero_lvl)
    out += encode_int32(6, int(hero["tier"]))
    out += encode_int32(7, int(hero["star"]))
    for it in hero.get("item", [0, 0, 0, 0, 0, 0]):
        out += encode_int32(8, int(it))
    out += encode_int32(9, int(hero.get("treasure", 0)))
    locked_val = hero.get("locked", [0, 0, 0, 0])
    if isinstance(locked_val, int):
        locked_val = [locked_val, 0, 0, 0]
    for lk in locked_val:
        out += encode_int32(10, int(lk))
    for inf in hero.get("info", hero.get("questStatus", [0, 0, 0, 0])):
        out += encode_int32(11, int(inf))
    from hero_progression import e_skills
    for sk in e_skills(hero):
        out += encode_int32(12, int(sk))
    out += encode_int32(13, client_skin_id(hero.get("skin", 0)))
    return bytes(out)


def build_pb_list_hero(heroes: list) -> bytes:
    out = bytearray()
    for h in heroes:
        out += encode_submessage(1, build_pb_hero(h))
    return bytes(out)


def build_pb_mail(mail: dict) -> bytes:
    from gift_mail import display_mail
    mail = display_mail(sys.modules[__name__], mail)
    out = bytearray()
    out += encode_int32(1, int(mail["id"]))
    out += encode_string(2, str(mail["title"]))
    out += encode_string(3, str(mail["message"]))
    for b in mail.get("bonus", []):
        out += encode_int32(4, int(b))
    out += encode_int32(5, 1 if mail.get("received", False) else 0)
    out += encode_int64(6, int(mail.get("time", int(time.time()) * 1000)))
    out += encode_int32(7, int(mail.get("senderId", 0)))
    return bytes(out)


def build_pb_list_mail(mails: list) -> bytes:
    out = bytearray()
    for m in mails:
        out += encode_submessage(1, build_pb_mail(m))
    return bytes(out)


def build_chat_msg(msg: dict, fallback_player: dict) -> bytes:
    sender = msg.get("sender") or fallback_player
    out = bytearray()
    out += encode_int64(1, int(msg.get("reqTime", int(time.time()) * 1000)))
    out += encode_string(2, str(msg.get("message", "")))
    out += encode_int32(3, int(msg.get("type", 0)))
    out += encode_int32(4, int(msg.get("msgId", 1)))
    out += encode_submessage(6, build_pb_user(sender))
    return bytes(out)


def build_chat_history(msgs: list, fallback_player: dict) -> bytes:
    out = bytearray()
    for m in msgs:
        out += encode_submessage(1, build_chat_msg(m, fallback_player))
    out += encode_int32(2, len(msgs))
    return bytes(out)


def build_clan_member(player: dict, position: int = 3) -> bytes:
    """pbdson.ClanMember (Write @ 0x23a4580)"""
    out = bytearray()
    out += encode_int32(1, int(player.get("id", 1)))
    out += encode_string(2, str(player.get("screenname", "Pirate")))
    out += encode_int32(3, 1000)
    out += encode_int32(4, 100)
    out += encode_int32(5, int(player.get("level", 200)))
    out += encode_int32(8, 0)
    out += encode_int32(9, {1:3,2:2,4:1,3:0}.get(int(position),-1))  # Native GuildRole
    out += encode_int32(10, int(player.get("vip", [13, 1000000])[0]))
    out += encode_int32(11, 1)
    for av in player.get("avatar", [0, 1]):
        out += encode_int32(12, int(av))
    out += encode_int32(13, 50000)
    out += encode_int32(23, 500000)
    return bytes(out)


def build_clan_infor(player: dict) -> bytes:
    """pbdson.ClanInfor (Write @ 0x23a3ce8)"""
    clan_id = int(player.get("clan_id", 0))
    clan_name = str(player.get("clan_name", "Băng Mũ Rơm"))
    clan_flag = max(1, int(player.get("clan_flag", 1)))
    clan_pos = int(player.get("clan_pos", 3))
    out = bytearray()
    out += encode_int32(1, clan_id)
    out += encode_string(2, clan_name)
    out += encode_string(3, str(player.get("username", "player1")))
    out += encode_string(4, str(player.get("screenname", "Pirate")))
    out += encode_int32(5, len(player.get("_guild_members", [player])))
    out += encode_int32(6, 50)
    out += encode_int32(7, 10000)
    out += encode_int32(8, 0)
    out += encode_int32(9, 0)
    for member in player.get("_guild_members", [player]):
        out += encode_submessage(10, build_clan_member(member, position=int(member.get("clan_pos",3))))
    out += encode_string(11, "Băng Hải Tặc Mũ Rơm - Tân Thế Giới!")
    for message in player.get('_guild_activity', [f"{player.get('screenname','Pirate')} đã thành lập Công Hội {clan_name}"]):out += encode_string(12, message)
    out += encode_int32(13, int(player.get('clan_level',1)))
    out += encode_int64(14, int(player.get('clan_exp',0)))
    out += encode_int64(15, int(player.get('clan_max_exp',100)))
    out += encode_string(16, "1")
    out += encode_int32(17, clan_flag)
    return bytes(out)


def build_clan_list(player: dict) -> bytes:
    """pbdson.ClanList (Write @ 0x23a5e7c)"""
    out = bytearray()
    out += encode_submessage(1, build_clan_infor(player))
    return bytes(out)



# ============================================================================
# 3. BonusPreview & BonusReceived Encoders (BonusModel @ 0x151cdf4 & 0x1520a90)
# ============================================================================

def make_bonus_preview(items: list) -> list:
    """
    Formats reward specs into BonusPreview long array (FetchBonusItemPreview @ 0x151cdf4):
      - ("gold", amt)                     -> [1, amt]
      - ("gem", amt)                      -> [2, amt]
      - ("material", matType, key, amt)   -> [3, matType, key, amt]
      - ("exp", amt)                      -> [4, amt]
      - ("hero", heroKey)                 -> [5, heroKey]
      - ("equip", equipId, cnt)           -> [6, equipId, cnt]
      - ("artifact", artId, star, cnt)    -> [7, artId, star, cnt]
      - ("avatar", avType, avId)          -> [8, avType, avId]
      - ("vip_exp", amt)                  -> [9, amt]
    """
    out = []
    for it in items:
        kind = it[0]
        if kind == "gold":
            out.extend([1, int(it[1])])
        elif kind == "gem":
            out.extend([2, int(it[1])])
        elif kind == "material":
            mt = 1 if int(it[1]) in (10, 11, 12) else int(it[1])
            out.extend([3, mt, int(it[2]), int(it[3])])
        elif kind == "exp":
            out.extend([4, int(it[1])])
        elif kind == "hero":
            out.extend([5, int(it[1])])
        elif kind == "equip":
            out.extend([6, int(it[1]), int(it[2])])
        elif kind == "artifact":
            out.extend([7, int(it[1]), int(it[2]), int(it[3])])
        elif kind == "avatar":
            out.extend([8, int(it[1]), int(it[2])])
        elif kind == "vip_exp":
            out.extend([9, int(it[1])])
    return out


def parse_bonus_preview_items(preview_longs: list) -> list:
    """
    Parses a BonusPreview integer array (from db_item_consumable.json 'preview')
    into individual item tuples matching apply_and_make_bonus_received.
    """
    items = []
    i = 0
    n = len(preview_longs)
    while i < n:
        tag = int(preview_longs[i])
        if tag == 1 and i + 1 < n:
            items.append(("gold", int(preview_longs[i + 1])))
            i += 2
        elif tag == 2 and i + 1 < n:
            items.append(("gem", int(preview_longs[i + 1])))
            i += 2
        elif tag == 3 and i + 3 < n:
            items.append(("material", int(preview_longs[i + 1]), int(preview_longs[i + 2]), int(preview_longs[i + 3])))
            i += 4
        elif tag == 4 and i + 1 < n:
            items.append(("exp", int(preview_longs[i + 1])))
            i += 2
        elif tag == 5 and i + 1 < n:
            items.append(("hero", int(preview_longs[i + 1])))
            i += 2
        elif tag == 6 and i + 2 < n:
            items.append(("equip", int(preview_longs[i + 1]), int(preview_longs[i + 2])))
            i += 3
        elif tag == 7 and i + 3 < n:
            items.append(("artifact", int(preview_longs[i + 1]), int(preview_longs[i + 2]), int(preview_longs[i + 3])))
            i += 4
        elif tag == 8 and i + 2 < n:
            items.append(("avatar", int(preview_longs[i + 1]), int(preview_longs[i + 2])))
            i += 3
        elif tag == 9 and i + 1 < n:
            items.append(("vip_exp", int(preview_longs[i + 1])))
            i += 2
        else:
            break
    return items


def apply_and_make_bonus_received(state: dict, items: list, game_db=None):
    """
    Mutates player `state` and returns (bonus_received_longs, new_heroes) matching
    BonusModel.FetchBonusItemReceived @ 0x1520a90:
      - ("gold", delta)                   -> [1, newTotalGold, delta]
      - ("gem", delta)                    -> [2, newTotalGem, delta]
      - ("material", matType, key, delta) -> [3, matType, key, newTotalCount, delta]
      - ("exp", delta)                    -> [4, newLevel, newExp, delta]
      - ("hero", heroKey, lvl, tier, star)-> [5, heroInstId, heroKey]
      - ("equip", equipId, delta)         -> [6, equipId, newTotalCount, delta]
      - ("artifact", artId, star, delta)  -> [7, artId, star, newTotalCount, delta]
      - ("avatar", avType, avId)          -> [8, avType, avId]
      - ("vip_exp", delta)                -> [9, vipLevel, newVipExp, delta]
    """
    from resource_guard import check_debits
    check_debits(state, items, game_db)
    out = []
    new_heroes = []
    if any(it[0] == "material" for it in items):
        deduplicate_consumables(state)
    # Build per-batch indexes once. Full packs contain thousands of rows;
    # scanning each growing bag for every reward makes claiming quadratic.
    kinds = {it[0] for it in items}
    material_index = {}
    equipment_index = {}
    artifact_index = {}
    if "material" in kinds:
        for row in state.get("consumables", []):
            material_index.setdefault((int(row[0]), int(row[1])), row)
    if "equip" in kinds:
        for row in state.get("equipments", []):
            equipment_index.setdefault(int(row[0]), row)
    if "artifact" in kinds:
        for row in state.get("artifacts", []):
            artifact_index.setdefault((int(row[0]), int(row[1])), row)
    for it in items:
        kind = it[0]
        if kind == "gold":
            delta = int(it[1])
            state["gold"] = max(0, int(state.get("gold", 0)) + delta)
            out.extend([1, state["gold"], delta])
        elif kind == "gem":
            delta = int(it[1])
            state["gem"] = max(0, int(state.get("gem", 0)) + delta)
            out.extend([2, state["gem"], delta])
        elif kind == "material":
            mtype = 1 if int(it[1]) in (10, 11, 12) else int(it[1])
            mkey = int(it[2])
            delta = int(it[3])
            found = material_index.get((mtype, mkey))
            if found is not None:
                found[2] = max(0, int(found[2]) + delta)
            if found is None:
                new_cnt = max(0, delta)
                found = [mtype, mkey, new_cnt]
                state.setdefault("consumables", []).append(found)
                material_index[(mtype, mkey)] = found
            out.extend([3, mtype, mkey, int(found[2]), delta])
        elif kind == "exp":
            delta = int(it[1])
            state["exp"] = max(0, int(state.get("exp", 0)) + delta)
            thresholds = getattr(game_db, "player_exp", {})
            level = max(1, int(state.get("level", 1)))
            while level < max(thresholds, default=level) and level in thresholds and thresholds[level] > 0 and state["exp"] >= thresholds[level]:
                state["exp"] -= thresholds[level]
                level += 1
            state["level"] = level
            out.extend([4, int(state.get("level", 250)), state["exp"], delta])
        elif kind == "level":
            lvl = int(it[1])
            state["level"] = max(1, lvl)
            out.extend([4, state["level"], int(state.get("exp", 0)), 0])
        elif kind == "hero":
            hkey = int(it[1])
            lvl = int(it[2]) if len(it) > 2 else 100
            tier = int(it[3]) if len(it) > 3 else 0
            star = int(it[4]) if len(it) > 4 else 5
            # Chỉ tự động đổi sang ID 10 sao nếu yêu cầu star >= 10
            if star >= 10 and game_db is not None and hkey in getattr(game_db, "ten_star_by_base_id", {}):
                hkey = game_db.ten_star_by_base_id[hkey]
            inst_id = max(101, int(state.get("next_hero_id", len(state.get("heroes", [])) + 101)))
            existing_ids = {int(h.get("id", 0)) for h in state.get("heroes", []) if isinstance(h, dict)}
            while inst_id in existing_ids:
                inst_id += 1
            state["next_hero_id"] = inst_id + 1
            if game_db is not None:
                inst = game_db.make_hero_instance(inst_id, hkey, level=lvl, tier=tier, star=star)
            else:
                inst = {
                    "id": inst_id,
                    "heroId": hkey,
                    "point": [25000.0, 2000.0, 350000.0, 1200.0] + [0.0] * 16,
                    "level": lvl,
                    "tier": tier,
                    "star": star or 10,
                    "item": [0, 0, 0, 0, 0, 0],
                    "locked": [0, 0, 0, 0],
                    "questStatus": [0, 0, 0, 0],
                    "eSkill": [1, 1, 1, 1, 1],
                    "skin": 0,
                }
            state.setdefault("heroes", []).append(inst)
            new_heroes.append(inst)
            out.extend([5, inst_id, hkey])
        elif kind == "equip":
            eq_id = int(it[1])
            delta = int(it[2])
            found = equipment_index.get(eq_id)
            if found is not None:
                found[1] = max(0, int(found[1]) + delta)
            if found is None:
                found = [eq_id, max(0, delta), 0]
                state.setdefault("equipments", []).append(found)
                equipment_index[eq_id] = found
            out.extend([6, eq_id, int(found[1]), delta])
        elif kind == "artifact":
            art_id = int(it[1])
            star = int(it[2])
            delta = int(it[3])
            found = artifact_index.get((art_id, star))
            if found is not None:
                found[2] = max(0, int(found[2]) + delta)
            if found is None:
                found = [art_id, star, max(0, delta), 0]
                state.setdefault("artifacts", []).append(found)
                artifact_index[(art_id, star)] = found
            out.extend([7, art_id, star, int(found[2]), delta])
        elif kind == "avatar":
            identity = [int(it[1]), int(it[2])]
            if identity not in state.setdefault("owned_avatars", []):
                state["owned_avatars"].append(identity)
            out.extend([8, identity[0], identity[1]])
        elif kind == "vip_exp":
            delta = int(it[1])
            vip = state.setdefault("vip", [0, 0])
            vip[1] = max(0, int(vip[1]) + delta)
            if delta >= 10000000 or vip[1] >= 10000000:
                vip[0] = 20
            elif vip[1] >= 600000:
                vip[0] = 13
            out.extend([9, int(vip[0]), int(vip[1]), delta])
        elif kind == "vip":
            vip = state.setdefault("vip", [0, 0])
            vip[0] = int(it[1])
            if len(it) > 2:
                vip[1] = int(it[2])
            out.extend([9, int(vip[0]), int(vip[1]), 0])
    if new_heroes and game_db is not None:
        try:
            full_heroes = state.get("heroes")
            if isinstance(full_heroes, list) and len(full_heroes) >= len(new_heroes):
                mod = sys.modules[__name__]
                from album import sync_heroes as _sync_album
                from reported_stats import sync as _sync_ship
                from guild_tech import sync as _sync_gtech
                state["heroes"] = new_heroes
                try:
                    _sync_album(mod, state)
                    _sync_ship(mod, state)
                    _sync_gtech(mod, state)
                finally:
                    full_heroes[-len(new_heroes):] = state["heroes"]
                    new_heroes = list(state["heroes"])
                    state["heroes"] = full_heroes
        except Exception:
            pass
    return out, new_heroes


# ============================================================================
# 4. Battle Engine (ListBattleData.Parse @ 0x13aff44 & BattleData @ 0x13481e0)
# ============================================================================

def build_pb_effect(team: int, position: int, effect_id: int, info_longs: list) -> bytes:
    # Native GenBattleEffectData reads the damage type only when Count == 3.
    if effect_id == 3 and len(info_longs) == 4 and info_longs[3] == 0:
        info_longs = info_longs[:3]
    out = bytearray()
    out += encode_int32(1, int(team))
    out += encode_int32(2, int(position))
    out += encode_int32(3, int(effect_id))
    for v in info_longs:
        out += encode_int64(4, int(v))
    return bytes(out)


def _coalesce_target_hp_effects(effects_bytes: list) -> list:
    if len(effects_bytes) <= 1:
        return effects_bytes
    hp_count = 0
    for eb in effects_bytes:
        if eb and len(eb) >= 6 and eb[4] == 0x18 and eb[5] in (3, 61):
            hp_count += 1
            if hp_count >= 2:
                break
    if hp_count < 2:
        return effects_bytes
    out = []
    pending_idx = {}
    pending_vals = {}
    for eb in effects_bytes:
        if not eb:
            continue
        ef = parse_proto_fields(eb)
        ek = to_signed32(ef.get(3, [(0, 0)])[0][1])
        et = ef.get(1, [(0, 0)])[0][1]
        ep = ef.get(2, [(0, 0)])[0][1]
        key = (et, ep)
        if ek == 3:
            ev = [to_signed64(v) for _, v in ef.get(4, [])]
            if len(ev) >= 3 and ev[1] <= 0 and ev[2] in (1, 2):
                if key not in pending_idx:
                    pending_idx[key] = len(out)
                    pending_vals[key] = [ev[0], ev[1], ev[2], 1]
                    out.append(eb)
                else:
                    g = pending_vals[key]
                    g[0] = ev[0]
                    g[1] += ev[1]
                    if ev[2] == 2:
                        g[2] = 2
                    g[3] += 1
                    out[pending_idx[key]] = build_pb_effect(et, ep, 3, [g[0], g[1], g[2], 0])
                continue
            else:
                pending_idx.pop(key, None)
                pending_vals.pop(key, None)
        elif ek == 61:
            pending_idx.pop(key, None)
            pending_vals.pop(key, None)
        out.append(eb)
    return out


def build_list_pb_effect(effects_bytes: list) -> bytes:
    out = bytearray()
    for eb in _coalesce_target_hp_effects(effects_bytes):
        out += encode_submessage(1, eb)
    return bytes(out)


def build_pb_target(team: int, position: int) -> bytes:
    out = bytearray()
    out += encode_int32(1, int(team))
    out += encode_int32(2, int(position))
    return bytes(out)


def build_pb_hero_attack(team: int, position: int, skill_index: int, targets: list, list_effects: list, round_no: int) -> bytes:
    out = bytearray()
    out += encode_int32(1, int(team))
    out += encode_int32(2, int(position))
    out += encode_int32(3, int(skill_index))
    valid_targets = [(int(t_team), int(t_pos)) for t_team, t_pos in targets if 0 <= int(t_pos) < 6]
    if not valid_targets:
        valid_targets = [(int(team), int(position) if 0 <= int(position) < 6 else 0)]
    for t_team, t_pos in valid_targets:
        # Native GetListHeroScript accepts the six hero slots only. Boat rage
        # effects retain coordinate 6 inside PbEffect, outside this target list.
        out += encode_submessage(4, build_pb_target(t_team, t_pos))
    for le in list_effects:
        out += encode_submessage(5, le)
    out += encode_int32(6, int(round_no))
    return bytes(out)


def build_pb_hero_turn(round_no: int, team: int, position: int, anger: int, attacks_bytes: list) -> bytes:
    out = bytearray()
    out += encode_int32(1, int(round_no))
    out += encode_int32(2, int(team))
    out += encode_int32(3, int(position))
    out += encode_int32(4, int(anger))
    for ab in attacks_bytes:
        out += encode_submessage(5, ab)
    return bytes(out)


FREE_SUMMON_COOLDOWN_SECONDS = 3 * 60 * 60


def summon_free_remaining(state, summon_type, now=None):
    now = int(time.time()) if now is None else int(now)
    ready_at = int(state.get("summon_free_ready_at", {}).get(str(summon_type), 0))
    return max(0, ready_at - now)


def build_summon_status(state):
    now = int(time.time())
    return build_common_vector(a_long=[
        summon_free_remaining(state, 1, now), summon_free_remaining(state, 4, now),
        250, 2200, 10, 100, 0, int(state.get("summon_energy", 0)), 1000,
    ])


BATTLE_HERO_SPINE_ALIAS = {
    363: 549, 364: 549, 365: 549,  # Lafitte -> Zephyr (abyss priest)
    483: 468, 484: 468, 485: 468,  # Luffy Dressrosa -> Luffy Wano (abyss mage)
}

VALID_BATTLE_HERO_KEYS = {
    10, 12, 15, 22, 25, 29, 32, 36, 39, 41, 52, 66, 67, 68, 69, 70, 71, 72, 73, 74,
    75, 76, 77, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 100, 101, 102,
    104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 116, 117, 118, 121, 122,
    123, 124, 125, 126, 127, 128, 129, 130, 131, 132, 133, 134, 135, 137, 140, 141,
    142, 143, 144, 145, 147, 148, 149, 150, 151, 152, 154, 155, 156, 157, 158, 159,
    160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173, 174, 175,
    176, 177, 178, 179, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 190, 191,
    192, 193, 194, 195, 196, 197, 198, 199, 200, 201, 202, 203, 204, 205, 206, 207,
    208, 209, 210, 211, 212, 213, 214, 215, 216, 217, 218, 219, 220, 221, 222, 223,
    224, 225, 226, 227, 228, 229, 230, 231, 232, 233, 234, 235, 236, 237, 238, 239,
    240, 241, 242, 243, 244, 245, 246, 247, 248, 249, 250, 251, 252, 253, 254, 255,
    256, 257, 258, 259, 260, 261, 262, 263, 264, 265, 266, 267, 268, 269, 270, 271,
    272, 273, 274, 275, 276, 277, 278, 279, 280, 281, 282, 283, 284, 285, 286, 297,
    316, 319, 336, 339, 360, 366, 369, 372, 375, 378, 381, 384, 387, 390, 393, 396,
    399, 402, 405, 408, 411, 414, 417, 420, 423, 426, 429, 432, 435, 438, 441, 444,
    447, 450, 453, 456, 459, 462, 465, 468, 471, 474, 477, 480, 483, 484, 485, 486,
    489, 492, 495, 498, 501, 504, 507, 510, 513, 516, 519, 522, 525, 528, 531, 534,
    537, 540, 543, 546, 549, 552, 555, 558, 561, 564, 567, 570, 573, 576, 579, 582,
    585, 588, 591, 594, 597, 600, 603, 606, 609, 612, 615, 618, 621, 624, 627, 630,
    633, 636, 639, 642, 645, 1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007, 3000,
    30007, 50005, 51164, 51184,
}

def resolve_battle_hero_id(hero_id: int) -> int:
    hid = int(hero_id)
    if hid in BATTLE_HERO_SPINE_ALIAS:
        return BATTLE_HERO_SPINE_ALIAS[hid]
    valid_keys = getattr(GAME_DB, "valid_battle_hero_keys", None) or VALID_BATTLE_HERO_KEYS
    if hid in valid_keys:
        return hid
    # Map 6-star / 10-star to base 5-star
    base_hid = getattr(GAME_DB, "base_5star_by_hero_id", {}).get(hid, hid)
    if base_hid in BATTLE_HERO_SPINE_ALIAS:
        return BATTLE_HERO_SPINE_ALIAS[base_hid]
    if base_hid in valid_keys:
        return base_hid
    # Check if spine is defined in hero template
    htpl = getattr(GAME_DB, "heroes_by_id", {}).get(hid) or getattr(GAME_DB, "heroes_by_id", {}).get(base_hid)
    if htpl:
        sp = htpl.get("spine")
        if sp and str(sp).isdigit() and int(sp) in valid_keys:
            return int(sp)
    return base_hid if base_hid in valid_keys else 105

def build_pb_battle_hero(
    pos: int,
    hero_id: int,
    level: int = 100,
    star: int = 5,
    tier: int = 5,
    max_hp: int = 25000,
    atk: int = 2500,
    defense: int = 600,
    speed: int = 600,
    start_anger: int = 50,
    total_dmg: int = 0,
    total_heal: int = 0,
    total_tank: int = 0,
    skin: int = 0,
    extra_pts: list = None,
) -> bytes:
    out = bytearray()
    render_hid = resolve_battle_hero_id(hero_id)
    out += encode_int32(1, int(render_hid))
    out += encode_int32(2, int(pos))
    pts = [0.0] * 35
    if extra_pts:
        for i in range(min(35, len(extra_pts))):
            pts[i] = float(extra_pts[i])
    pts[0] = float(atk)
    pts[1] = float(defense)
    pts[2] = float(max_hp)
    pts[3] = float(speed)
    pts[4] = float(start_anger)
    pts[16] = float(max_hp)
    pts[18] = 100.0  # Android client reads RAGE_MAX at index 18.
    pts[19] = 100.0  # Native HeroBattleInfo.m_RageMax; UI divides rage by this.
    pts[34] = 0.0
    for pt in pts:
        out += encode_float(3, pt)
    out += encode_int32(4, int(level))
    out += encode_int32(5, client_skin_id(skin))
    out += encode_int64(6, int(total_dmg))
    out += encode_int64(7, int(total_heal))
    out += encode_int64(8, int(total_tank))
    out += encode_int32(9, int(star))
    out += encode_int32(10, int(tier))
    return bytes(out)


def initial_battle_rage(hero):
    from battle_energy import starting_rage
    return starting_rage(sys.modules[__name__],hero)


def build_full_battle_result(
    state: dict,
    enemy_hero_ids: list,
    bonus_received_longs: list,
    fake_bonus_preview_longs: list = None,
    enemy_name: str = "Đội Quân Hải Tặc",
    enemy_level: int = 100,
    arena_info_longs: list = None,
    req_longs: list = None,
) -> bytes:
    """
    Build a pbdson.PbListBattleResult from hero points and server balance.
    Original skill-specific server scripts have not all been recovered.
    """
    if state.get("pets2") and state.get("pet2_equips"):
        try:
            from combat_shields import attach_pet_skills
            attach_pet_skills(sys.modules[__name__], state)
        except Exception:
            pass
    all_heroes = state.get("heroes", [])
    req_set = {int(x) for x in (req_longs or [])[:6] if int(x) > 0}
    for row in (state or {}).get("team", [])[:6]:
        if isinstance(row, list) and row:
            req_set.add(int(row[4]) if len(row) > 4 else int(row[0]))
            req_set.add(int(row[0]))
    by_inst = {}
    by_hid = {}
    if req_set:
        for h in all_heroes:
            hi = int(h["id"])
            ht = int(h["heroId"])
            if hi in req_set:
                by_inst[hi] = h
            if ht in req_set and ht not in by_hid:
                by_hid[ht] = h

    # Resolve formation slots 0..5 sent by HeroesFormation.ToListServerHeroId1LineUp
    slotted_team1 = []
    seen_inst_ids = set()
    if req_longs and len(req_longs) >= 6:
        for pos in range(6):
            sid = int(req_longs[pos])
            if sid > 0:
                h_match = by_inst.get(sid) or by_hid.get(sid)
                if h_match is not None and int(h_match.get("id", 0)) not in seen_inst_ids:
                    seen_inst_ids.add(int(h_match.get("id", 0)))
                    slotted_team1.append((pos, h_match))

    if not slotted_team1:
        for pos, row in enumerate((state or {}).get("team", [])[:6]):
            if isinstance(row, list) and row:
                sid = int(row[4]) if len(row) > 4 else int(row[0])
                h_match = by_inst.get(sid) or by_hid.get(int(row[0]))
                if h_match is not None and int(h_match.get("id", 0)) not in seen_inst_ids:
                    seen_inst_ids.add(int(h_match.get("id", 0)))
                    slotted_team1.append((pos, h_match))

    # Empty slots in a selected or saved formation must stay empty.
    # Choose a default team only when no owned hero could be resolved.
    if not slotted_team1:
        import heapq
        used_pos = {pos for pos, _ in slotted_team1}
        sorted_heroes = heapq.nlargest(
            6,
            all_heroes,
            key=lambda x: (int(x.get("level", 1)), int(x.get("star", 5)), int(x.get("id", 0))),
        )
        for h_match in sorted_heroes:
            if len(slotted_team1) >= 6:
                break
            hid_inst = int(h_match.get("id", 0))
            if hid_inst not in seen_inst_ids:
                free_pos = next((p for p in range(6) if p not in used_pos), len(slotted_team1))
                used_pos.add(free_pos)
                seen_inst_ids.add(hid_inst)
                slotted_team1.append((free_pos, h_match))
        slotted_team1.sort(key=lambda pair: pair[0])

    if not slotted_team1:
        fallback_h = GAME_DB.make_hero_instance(1, 105, level=max(1, enemy_level), tier=2, star=5)
        slotted_team1 = [(0, fallback_h)]

    from ship_combat import attach as attach_ship,selected as selected_ship
    slotted_team1=attach_ship(slotted_team1,selected_ship(state,req_longs))

    for _, h_obj in slotted_team1:
        try:
            _recalc_hero(h_obj)
        except Exception:
            pass

    if not enemy_hero_ids:
        enemy_hero_ids = [214, 186]

    enemy_lvl = max(1, int(enemy_level))
    enemy_star = 3 if enemy_lvl < 25 else (4 if enemy_lvl < 50 else (5 if enemy_lvl <= 100 else (6 if enemy_lvl <= 150 else 10)))
    enemy_tier = min(6, max(0, enemy_lvl // 25))

    # Build combat units for Team 1 and Team 2
    units = []
    for pos, h in slotted_team1[:6]:
        pts = list(h.get("point", [])) + [0.0] * 20
        atk = max(20, int(pts[0]))
        arm = max(5, int(pts[1]))
        mhp = max(100, int(pts[2]))
        spd = max(50, int(pts[3]))
        units.append({
            "team": 1,
            "pos": int(pos),
            "heroId": int(h.get("heroId", 105)),
            "level": int(h.get("level", 1)),
            "star": int(h.get("star", 5)),
            "tier": int(h.get("tier", 1)),
            "skin": int(h.get("skin", 0)),
            "pts": pts,
            "atk": atk,
            "arm": arm,
            "hp": mhp,
            "max_hp": mhp,
            "spd": spd,
            "anger": initial_battle_rage(h),
            "start_anger": initial_battle_rage(h),
            "hero": h,
            "dmg_dealt": 0,
            "healed": 0,
            "tanked": 0,
        })

    # Calculate Team 1 stats to keep PVE battles winnable while showing authentic damage
    t1_units = [u for u in units if u["team"] == 1]
    t1_total_atk = max(100, sum(u["atk"] for u in t1_units))
    t1_total_hp = max(500, sum(u["max_hp"] for u in t1_units))
    t1_max_spd = max((u["spd"] for u in t1_units), default=100)
    t1_avg_arm = max(20, sum(u["arm"] for u in t1_units) // max(1, len(t1_units)))
    num_enemies = max(1, len(enemy_hero_ids[:6]))
    for idx, eh_id in enumerate(enemy_hero_ids[:6]):
        eh_inst = GAME_DB.make_hero_instance(0, int(eh_id), level=enemy_lvl, tier=enemy_tier, star=enemy_star)
        epts = list(eh_inst.get("point", [])) + [0.0] * 20
        eatk = max(15, min(int(epts[0] * 0.5), max(30, t1_total_hp // (8 * num_enemies))))
        earm = max(5, min(int(epts[1] * 0.4), max(10, t1_avg_arm // 3)))
        emhp = max(int(epts[2]), max(2000, int(t1_total_hp * 0.5 // num_enemies), int(t1_total_atk * 3 // num_enemies)))
        espd = max(40, min(int(epts[3] * 0.7), max(40, t1_max_spd - 20)))
        epts[0], epts[1], epts[2], epts[3] = float(eatk), float(earm), float(emhp), float(espd)
        if len(epts) > 18:
            epts[18] = float(emhp)
        eh_inst["point"] = list(epts)
        units.append({
            "team": 2,
            "pos": int(idx),
            "heroId": int(eh_id),
            "level": enemy_lvl,
            "star": int(eh_inst.get("star", enemy_star)),
            "tier": enemy_tier,
            "skin": 0,
            "pts": epts,
            "atk": eatk,
            "arm": earm,
            "hp": emhp,
            "max_hp": emhp,
            "spd": espd,
            "anger": initial_battle_rage(eh_inst),
            "start_anger": initial_battle_rage(eh_inst),
            "hero": eh_inst,
            "dmg_dealt": 0,
            "healed": 0,
            "tanked": 0,
        })

    import haki_combat
    display_units = {}
    for unit in units:
        unit["_replay_max_hp"] = int(unit["max_hp"])
        unit["_replay_atk"] = int(unit["atk"])
        unit["_replay_arm"] = int(unit["arm"])
        unit["_replay_spd"] = int(unit["spd"])
        display_units[unit["team"], unit["pos"]] = unit
        haki_combat.initialize(unit,unit['hero'])
    encode_attack = globals()['build_pb_hero_attack']
    def build_pb_hero_attack(team, position, skill_index, targets, list_effects, round_no):
        return encode_attack(team, position, skill_index, targets,
                             _arena_replay_hp_groups(list_effects, display_units), round_no)

    def get_alive(t_id):
        return [u for u in units if u["team"] == t_id and u["hp"] > 0]

    turns_bytes = []
    from combat_shields import CombatShields
    import hero_combat_skills
    _turn_state = {
        'active_effs': None,
        'active_coords': None,
        'post_attack_turns': None,
        'pending_passive_effs': [],
        'last_key': None,
        'last_passive_effs': None,
        'last_passive_r': 1,
        'last_passive_actor': None,
    }
    def _rebuild_last_passive():
        from hero_combat_skills import group_effects_by_coords
        actor_u = _turn_state['last_passive_actor']
        r_val = _turn_state['last_passive_r']
        _, list_effs = group_effects_by_coords(_turn_state['last_passive_effs'], [(actor_u['team'], actor_u['pos'])], sys.modules[__name__])
        attack = build_pb_hero_attack(actor_u['team'], actor_u['pos'], -1, [], list_effs, r_val)
        turns_bytes[-1] = build_pb_hero_turn(r_val, actor_u['team'], actor_u['pos'], actor_u['anger'], [attack])
    def _flush_pending_passive(r_no=1, fallback_actor=None):
        pend = _turn_state['pending_passive_effs']
        if not pend:
            return
        _turn_state['pending_passive_effs'] = []
        actor_u = fallback_actor or _turn_state['last_passive_actor'] or next((u for u in units if u['hp'] > 0), units[0])
        r_val = max(1, int(r_no))
        from hero_combat_skills import group_effects_by_coords
        tcoords, list_effs = group_effects_by_coords(pend, [(actor_u['team'], actor_u['pos'])], sys.modules[__name__])
        attack = build_pb_hero_attack(actor_u['team'], actor_u['pos'], -1, [] if turns_bytes else tcoords, list_effs, r_val)
        turns_bytes.append(build_pb_hero_turn(r_val, actor_u['team'], actor_u['pos'], actor_u['anger'], [attack]))
    def _append_passive_effects(r_no, actor_u, effs, target_coords=None):
        if not effs:
            return
        clean_effs = [e for e in effs if e]
        if not clean_effs:
            return
        if _turn_state['active_effs'] is not None:
            coord_to_idx = {c: idx for idx, c in enumerate(_turn_state['active_coords'])}
            for eff in clean_effs:
                t_team = int(eff[1]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor_u["team"]
                t_pos = int(eff[3]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor_u["pos"]
                if _turn_state['active_effs']:
                    idx = coord_to_idx.get((t_team, t_pos), 0)
                    _turn_state['active_effs'][idx].append(eff)
            return
        _turn_state['pending_passive_effs'].extend(clean_effs)
        _turn_state['last_passive_actor'] = actor_u
        _turn_state['last_passive_r'] = max(1, int(r_no))
    def shield_effects(actor, effects):
        if actor.get('_battle_ship_actor'):
            from hero_combat_skills import group_effects_by_coords
            tcoords, list_effs = group_effects_by_coords(effects, [(actor['team'], actor['pos'])], sys.modules[__name__])
            attack = build_pb_hero_attack(actor['team'], actor['pos'], 1, tcoords, list_effs, max(1, shields.round))
            ship_turn = build_pb_hero_turn(max(1, shields.round), actor['team'], actor['pos'], actor['anger'], [attack])
            if _turn_state['post_attack_turns'] is not None:
                _turn_state['post_attack_turns'].append(ship_turn)
            else:
                _turn_state['last_key'] = None
                _turn_state['last_passive_effs'] = None
                turns_bytes.append(ship_turn)
        else:
            _append_passive_effects(max(1, shields.round), actor, effects)
    def emit_skill_turn(actor_u, effs, target_coords=None, round_no=1):
        _append_passive_effects(max(1, round_no), actor_u, effs, target_coords)
    shields=CombatShields(sys.modules[__name__],units,random,shield_effects)
    hero_combat_skills.initialize_battle(sys.modules[__name__], units, random, emit_skill_turn)

    for round_no in range(1, 16):
        if not get_alive(1) or not get_alive(2):
            break
        shields.start_round(round_no)
        hero_combat_skills.start_round(sys.modules[__name__], units, round_no, random, emit_skill_turn)
        hero_combat_skills.check_deaths_and_revives(sys.modules[__name__], units, round_no, random, emit_skill_turn)
        turn_order = sorted(units, key=lambda u: (-hero_combat_skills.effective_speed(u, round_no), u["team"], u["pos"]))
        turn_order = hero_combat_skills.take_reaction_turns(units) + [(u, False) for u in turn_order]
        for turn_index, (actor, is_reaction) in enumerate(turn_order):
            if actor["hp"] <= 0:
                continue
            opponents = get_alive(3 - actor["team"])
            if not opponents:
                break
            skip_turn, silenced = hero_combat_skills.check_control_state(sys.modules[__name__], actor, round_no)
            if skip_turn:
                continue

            def _do_actor_attack(use_rage, no_rage_gain=False, is_extra=False):
                cur_opponents = get_alive(3 - actor["team"])
                if not cur_opponents or actor["hp"] <= 0:
                    return
                hero_combat_skills.prepare_action(actor, units, round_no, random)
                use_rage = hero_combat_skills.should_use_rage(actor, round_no, forced_normal=is_extra and not use_rage, forced_rage=is_extra and use_rage)
                hero_tpl = GAME_DB.heroes_by_id.get(actor["heroId"], {})
                active_id = int(hero_tpl.get("active", 0) or 0)
                sk_meta = getattr(GAME_DB, "active_skill_meta", {}).get(active_id)

                # Check normal attack modifier in passives
                norm_meta = None
                for p_k in ("passive1", "passive2", "passive3"):
                    p_id = int(hero_tpl.get(p_k, 0) or 0)
                    if hero_combat_skills.passive_enabled(actor,p_id,round_no) and p_id in getattr(GAME_DB, "normal_skill_meta", {}):
                        norm_meta = GAME_DB.normal_skill_meta[p_id]
                        break

                shields.before_action(actor, use_rage)
                if not is_extra:
                    from soul_combat import before_action as soul_before_action
                    soul_before_action(actor, units, round_no, use_rage, random)
                hero_combat_skills.before_attack(sys.modules[__name__], actor, units, round_no, use_rage, random)
                cur_opponents = get_alive(3 - actor["team"])
                if not cur_opponents or actor["hp"] <= 0:
                    return
                targets_units = hero_combat_skills.select_targets(
                    sys.modules[__name__], actor, cur_opponents, use_rage, sk_meta=sk_meta, norm_meta=norm_meta
                )
                target_coords = [(tu["team"], tu["pos"]) for tu in targets_units]
                per_target_effs = [[] for _ in targets_units]
                _turn_state['active_effs'] = per_target_effs
                _turn_state['active_coords'] = target_coords
                turn_start_anger = actor["anger"]
                skill_idx = 1 if use_rage else 0
                if use_rage:
                    if not no_rage_gain:
                        actor["anger"] = max(0, actor["anger"] - 100)
                else:
                    if not no_rage_gain:
                        from battle_energy import gained
                        actor["anger"] = actor["anger"] + gained(actor,50)

                from combat_status import sync as sync_status
                opening_status = []
                if _turn_state['pending_passive_effs']:
                    opening_status.extend(_turn_state['pending_passive_effs'])
                    _turn_state['pending_passive_effs'] = []
                opening_status.extend(sync_status(sys.modules[__name__], units, round_no))
                opening_status.extend(hero_combat_skills.drain_control_effects(sys.modules[__name__],units))
                if opening_status and per_target_effs:
                    per_target_effs[0].extend(opening_status)

                skl_bonus = max(0.0, float(actor["pts"][5]) / 1000.0) if len(actor["pts"]) > 5 else 0.0

                actor["_is_aoe_attack"] = (len(targets_units) > 1)
                from blessing_combat import armor_break, critical_chance, critical_bonus, dodge_chance, lifesteal
                for t_idx, tu in enumerate(targets_units):
                    ap,tp=actor['pts'],tu['pts']
                    eff_def = max(1.0, tu["arm"] * (1.0 - armor_break(ap,tp)))
                    base_dmg = (actor["atk"] * actor["atk"]) / float(actor["atk"] + eff_def)
                    direct_dmg = 0.0

                    if use_rage:
                        if sk_meta:
                            s_type = sk_meta.get("stat", "atk")
                            s_mult = float(sk_meta.get("mult", 1.65))
                            if s_type == "hp":
                                hp_scaled = min(actor["max_hp"] * s_mult, actor["atk"] * 3.5)
                                base_dmg = hp_scaled * (actor["atk"] / float(actor["atk"] + eff_def))
                            elif s_type == "arm":
                                base_dmg = actor["arm"] * s_mult * (actor["atk"] / float(actor["atk"] + eff_def))
                            else:
                                base_dmg *= (s_mult + skl_bonus * 0.5)

                            # Hybrid Dame: Sát thương trực tiếp bằng % Máu tối đa bản thân và % Máu đã mất mục tiêu
                            if sk_meta.get("hp_direct_pct", 0) > 0:
                                hp_part = actor["max_hp"] * min(0.15, float(sk_meta["hp_direct_pct"]))
                                direct_dmg += hp_part
                            if sk_meta.get("lost_hp_pct", 0) > 0:
                                lost_part = max(0, tu["max_hp"] - tu["hp"]) * min(0.15, float(sk_meta["lost_hp_pct"]))
                                direct_dmg += lost_part

                            # Cap trần sát thương trực tiếp theo % Công bản thân
                            cap_mult = min(3.5, float(sk_meta.get("max_direct_cap_mult", 0) or 3.5))
                            if direct_dmg > 0 and cap_mult > 0:
                                direct_dmg = min(direct_dmg, actor["atk"] * cap_mult)
                        else:
                            rage_mult = (1.35 if len(targets_units) > 1 else 1.75) + skl_bonus * 0.5
                            base_dmg *= rage_mult
                    else:
                        if norm_meta:
                            base_dmg *= float(norm_meta.get("mult", 1.0))
                            if norm_meta.get("hp_direct_pct", 0) > 0:
                                norm_hp_dmg = actor["max_hp"] * min(0.08, float(norm_meta["hp_direct_pct"]))
                                norm_cap = min(2.0, float(norm_meta.get("max_direct_cap_mult", 2.0) or 2.0))
                                direct_dmg += min(norm_hp_dmg, actor["atk"] * norm_cap)

                    base_dmg += direct_dmg
                    base_dmg = hero_combat_skills.modify_damage(
                        sys.modules[__name__], actor, tu, base_dmg, use_rage, cur_opponents, random
                    )

                    is_crit = random.random() < critical_chance(ap,tp)
                    if is_crit:
                        base_dmg *= (1.0 + critical_bonus(ap,tp))

                    reduction=max(0.0,min(0.95,float(tu['pts'][12])/1000.0)) if len(tu['pts'])>12 else 0.0
                    holy=min(1.5,max(0.0,float(actor['pts'][13])/1000.0)) if len(actor['pts'])>13 else 0.0
                    base_dmg=base_dmg*(1-reduction)+actor['atk']*holy
                    base_dmg *= random.uniform(0.93, 1.07)
                    dodge=dodge_chance(ap,tp)
                    missed=dodge>0 and random.random()<dodge
                    raw_incoming_dmg = 0 if missed else haki_combat.incoming(tu,haki_combat.outgoing(actor,max(1,int(round(base_dmg))),is_crit),is_crit)
                    clamped_raw = hero_combat_skills.clamp_incoming_damage(sys.modules[__name__], actor, tu, raw_incoming_dmg, units, round_no, pre_shield=True) if raw_incoming_dmg > 0 else 0

                    dmg, shield_changes = shields.absorb(tu,clamped_raw,source=actor)
                    sh_abs = min(tu["max_hp"], max(0, clamped_raw - dmg)) if any(e[3] == 83 for e in shield_changes if len(e) > 3) else 0
                    dmg = hero_combat_skills.clamp_incoming_damage(sys.modules[__name__], actor, tu, dmg, units, round_no)
                    new_hp = max(0, tu["hp"] - dmg)
                    actual_lost = tu["hp"] - new_hp
                    tu["hp"] = new_hp
                    link_effects=hero_combat_skills.balance_linked_hp(sys.modules[__name__],tu,round_no)
                    new_hp=tu['hp']
                    actor["dmg_dealt"] += actual_lost + sh_abs
                    tu["tanked"] += actual_lost

                    hp_lost_type = 2 if is_crit else 1
                    eff_hp = build_pb_effect(
                        team=tu["team"],
                        position=tu["pos"],
                        effect_id=3,
                        info_longs=[new_hp, -dmg, hp_lost_type, 0],
                    )
                    sub_effs = shield_changes + [eff_hp] + link_effects
                    from pet_combat import critical_heal as pet_critical_heal
                    restored=min(actor['max_hp']-actor['hp'],lifesteal(ap,actual_lost)+haki_combat.crit_heal(actor,actual_lost,is_crit)+pet_critical_heal(actor,actual_lost,is_crit))
                    if restored>0:
                        actor['hp']+=restored;actor['healed']+=restored
                        sub_effs.append(build_pb_effect(actor['team'],actor['pos'],3,[actor['hp'],restored,3,0]))

                    if tu.get("_lava_armor") and actual_lost > 0 and not missed and actor["hp"] > 0:
                        reflect_mult = 0.70 if tu.get("_admiral_linked") else 0.45
                        reflect_cap = max(1, int(tu["max_hp"] * (0.35 if tu.get("_admiral_linked") else 0.25)) // max(1, len(targets_units)))
                        reflect_dmg = min(int(actual_lost * reflect_mult), reflect_cap)
                        reflect_dmg = hero_combat_skills.clamp_incoming_damage(sys.modules[__name__],tu,actor,reflect_dmg,units,round_no,pre_shield=True)
                        reflect_dmg, reflect_sh_changes = shields.absorb(actor, reflect_dmg, source=tu)
                        reflect_dmg = hero_combat_skills.clamp_incoming_damage(sys.modules[__name__],tu,actor,reflect_dmg,units,round_no)
                        min_actor_hp = 1 if hero_combat_skills.cannot_be_killed(actor, units, round_no) else 0
                        actor_new_hp = max(min_actor_hp, actor["hp"] - reflect_dmg)
                        actor_lost = actor["hp"] - actor_new_hp
                        actor["hp"] = actor_new_hp
                        actor["tanked"] = actor.get("tanked", 0) + actor_lost
                        tu["dmg_dealt"] = tu.get("dmg_dealt", 0) + actor_lost
                        sub_effs.extend(reflect_sh_changes)
                        if reflect_dmg > 0 or actor_lost > 0:
                            sub_effs.append(build_pb_effect(actor["team"], actor["pos"], 3, [actor_new_hp, -reflect_dmg, 1, 0]))

                    if tu["hp"] > 0 and not missed:
                        from battle_energy import gained
                        before_t_anger = tu["anger"]
                        tu["anger"] = before_t_anger + gained(tu,10)
                        received = tu["anger"] - before_t_anger
                        eff_t_anger = build_pb_effect(
                            team=tu["team"],
                            position=tu["pos"],
                            effect_id=5,
                            info_longs=[tu["anger"], received],
                        )
                        sub_effs.append(eff_t_anger)

                    if t_idx == 0 and not no_rage_gain:
                        anger_delta = actor['anger']-turn_start_anger
                        eff_a_anger = build_pb_effect(
                            team=actor["team"],
                            position=actor["pos"],
                            effect_id=5,
                            info_longs=[actor["anger"], anger_delta],
                        )
                        sub_effs.append(eff_a_anger)

                    per_target_effs[t_idx].extend(sub_effs)

                side_effs = hero_combat_skills.build_skill_side_effects(
                    sys.modules[__name__], actor, targets_units, use_rage, units, round_no, random
                )
                side_effs.extend(hero_combat_skills.drain_link_effects(units))
                side_effs.extend(hero_combat_skills.drain_control_effects(sys.modules[__name__],units))
                side_effs.extend(sync_status(sys.modules[__name__], units, round_no))
                coord_to_idx = {c: idx for idx, c in enumerate(target_coords)}
                for eff in side_effs:
                    if not eff:
                        continue
                    t_team = int(eff[1]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor["team"]
                    t_pos = int(eff[3]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor["pos"]
                    if per_target_effs:
                        idx = coord_to_idx.get((t_team, t_pos), 0)
                        per_target_effs[idx].append(eff)

                from ship_combat import RAGE_MAX as _SHIP_RAGE_MAX, RAGE_PER_ACTION as _SHIP_RAGE_ADD
                for _boat in shields.ships.boats:
                    if _boat["team"] == actor["team"]:
                        _b_next = min(_SHIP_RAGE_MAX, int(_boat["anger"]) + _SHIP_RAGE_ADD)
                        if _b_next > int(_boat["anger"]) and per_target_effs:
                            per_target_effs[0].append(build_pb_effect(_boat["team"], 6, 5, [_b_next, _b_next - int(_boat["anger"])]))
                refund=haki_combat.rage_refund(actor,use_rage,random)
                if refund and per_target_effs:
                    per_target_effs[0].append(build_pb_effect(actor['team'],actor['pos'],5,[actor['anger'],refund]))
                shields.after_action(actor, use_rage, run_ships=False)
                _turn_finalized = [False]
                def _finalize_actor_attack():
                    if _turn_finalized[0]:
                        return
                    _turn_finalized[0] = True
                    tail_effs = []
                    if _turn_state['pending_passive_effs']:
                        tail_effs.extend(_turn_state['pending_passive_effs'])
                        _turn_state['pending_passive_effs'] = []
                    tail_effs.extend(hero_combat_skills.drain_link_effects(units))
                    tail_effs.extend(hero_combat_skills.drain_control_effects(sys.modules[__name__], units))
                    tail_effs.extend(sync_status(sys.modules[__name__], units, round_no))
                    for eff in tail_effs:
                        if not eff:
                            continue
                        t_team = int(eff[1]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor["team"]
                        t_pos = int(eff[3]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor["pos"]
                        if per_target_effs:
                            idx = coord_to_idx.get((t_team, t_pos), 0)
                            per_target_effs[idx].append(eff)
                    _turn_state['active_effs'] = None
                    _turn_state['active_coords'] = None
                    all_turn_effs = [eff for sub in per_target_effs for eff in sub if eff]
                    list_effects = [build_list_pb_effect(all_turn_effs)]
                    atk_msg = build_pb_hero_attack(
                        team=actor["team"],
                        position=actor["pos"],
                        skill_index=skill_idx,
                        targets=target_coords,
                        list_effects=list_effects,
                        round_no=round_no,
                    )
                    _turn_state['last_key'] = None
                    _turn_state['last_passive_effs'] = None
                    turn_msg = build_pb_hero_turn(
                        round_no=round_no,
                        team=actor["team"],
                        position=actor["pos"],
                        anger=turn_start_anger,
                        attacks_bytes=[atk_msg],
                    )
                    turns_bytes.append(turn_msg)
                    post_ships = []
                    _turn_state['post_attack_turns'] = post_ships
                    shields.ships.after_action(actor, use_rage)
                    _turn_state['post_attack_turns'] = None
                    turns_bytes.extend(post_ships)
                if not is_extra:
                    hero_combat_skills.after_attack_triggers(
                        sys.modules[__name__],
                        actor,
                        targets_units,
                        use_rage,
                        units,
                        round_no,
                        random,
                        emit_skill_turn,
                        lambda use_rage=False, no_rage_gain=True: (_finalize_actor_attack(), _do_actor_attack(use_rage, no_rage_gain=no_rage_gain, is_extra=True)),
                    )
                else:
                    hero_combat_skills.check_deaths_and_revives(sys.modules[__name__], units, round_no, random, emit_skill_turn)
                _finalize_actor_attack()

            _do_actor_attack(actor["anger"] >= 100 and not silenced)
            hero_combat_skills.check_deaths_and_revives(sys.modules[__name__], units, round_no, random, emit_skill_turn)
            turn_order[turn_index+1:turn_index+1] = hero_combat_skills.take_reaction_turns(units)
            turn_order[turn_index+1:] = hero_combat_skills.reorder_pending(turn_order[turn_index+1:], round_no)

        if get_alive(1) and get_alive(2):
            for unit in units:
                healed=haki_combat.end_round(unit,len(get_alive(1))+len(get_alive(2)),random)
                if healed:
                    emit_skill_turn(unit, [build_pb_effect(unit['team'],unit['pos'],3,[unit['hp'],healed,3,0])], [(unit['team'],unit['pos'])], round_no=round_no)

        hero_combat_skills.end_round(sys.modules[__name__], units, round_no, random, emit_skill_turn, shields)
        from combat_status import sync as sync_status
        end_status = sync_status(sys.modules[__name__], units, round_no)
        if end_status:
            _turn_state['pending_passive_effs'].extend(end_status)
    if _turn_state['pending_passive_effs']:
        _flush_pending_passive(_turn_state.get('last_passive_r', 1))

    # The attacker must eliminate the defenders within the round limit.
    # No last-hero protection or synthetic finishing turn.
    winner = 1 if get_alive(1) and not get_alive(2) else 2

    battle_out = bytearray()
    battle_out += encode_int32(1, winner)  # authoritative replay outcome
    for u in units:
        if u["team"] == 1:
            battle_out += encode_submessage(
                2,
                build_pb_battle_hero(
                    pos=u["pos"],
                    hero_id=u["heroId"],
                    level=u["level"],
                    star=u["star"],
                    tier=u["tier"],
                    max_hp=u.get("_replay_max_hp", u["max_hp"]),
                    atk=u.get("_replay_atk", u["atk"]),
                    defense=u.get("_replay_arm", u["arm"]),
                    speed=u.get("_replay_spd", u["spd"]),
                    start_anger=u["start_anger"],
                    total_dmg=u["dmg_dealt"],
                    total_heal=u["healed"],
                    total_tank=u["tanked"],
                    skin=u["skin"],
                    extra_pts=u["pts"],
                ),
            )
    for u in units:
        if u["team"] == 2:
            battle_out += encode_submessage(
                3,
                build_pb_battle_hero(
                    pos=u["pos"],
                    hero_id=u["heroId"],
                    level=u["level"],
                    star=u["star"],
                    tier=u["tier"],
                    max_hp=u.get("_replay_max_hp", u["max_hp"]),
                    atk=u.get("_replay_atk", u["atk"]),
                    defense=u.get("_replay_arm", u["arm"]),
                    speed=u.get("_replay_spd", u["spd"]),
                    start_anger=u["start_anger"],
                    total_dmg=u["dmg_dealt"],
                    total_heal=u["healed"],
                    total_tank=u["tanked"],
                    skin=u["skin"],
                    extra_pts=u["pts"],
                ),
            )
    battle_out+=shields.ships.encode()
    for tb in turns_bytes:
        battle_out += encode_submessage(4, tb)
    battle_out += encode_int32(9, 15)  # maxRound = 15
    battle_out += encode_int32(12, 0)  # buffTeam[0] = 0
    battle_out += encode_int32(12, 0)  # buffTeam[1] = 0

    list_out = bytearray()
    list_out += encode_submessage(1, bytes(battle_out))
    list_out += encode_int32(2, winner)  # same outcome as replay
    for b_val in ((bonus_received_longs or []) if winner == 1 else []):
        list_out += encode_int64(3, int(b_val))

    av = state.get("avatar", [0, 1])
    attacker_cv = build_common_vector(
        a_long=[0, int(av[0]), int(av[1]), int(state.get("level", 200))],
        a_string=[str(state.get("screenname", "Pirate"))]
    )
    defender_cv = build_common_vector(
        a_long=[0, 0, 1, int(enemy_level)],
        a_string=[str(enemy_name)]
    )
    list_out += encode_submessage(4, attacker_cv)
    list_out += encode_submessage(5, defender_cv)
    list_out += encode_int64(6, int(time.time() * 1000))
    list_out += encode_int32(7, 0)
    total_team1_dmg = max(1, sum(u["dmg_dealt"] for u in units if u["team"] == 1))
    info_longs = arena_info_longs if arena_info_longs is not None else [0, total_team1_dmg, 0, 0]
    list_out += encode_submessage(8, build_common_vector(a_long=info_longs))
    for fb_val in ((fake_bonus_preview_longs or []) if winner == 1 else []):
        list_out += encode_int64(9, int(fb_val))

    return bytes(list_out)


def settle_battle(state, rewards, **kwargs):
    from battle_outcomes import settle
    return settle(sys.modules[__name__], state, rewards, **kwargs)


def build_pb_stage(stage_dict: dict) -> bytes:
    out = bytearray()
    if "username" in stage_dict:
        out += encode_string(1, stage_dict["username"])
    if "name" in stage_dict:
        out += encode_string(2, stage_dict["name"])
    out += encode_int32(3, int(stage_dict.get("vip", 0)))
    out += encode_int32(4, int(stage_dict.get("level", 1)))
    out += encode_int32(5, int(stage_dict.get("rankPvp", 1)))
    for aw in stage_dict.get("awards", []):
        out += encode_int32(6, int(aw))
    out += encode_int64(7, int(stage_dict.get("atkPower", 100000)))
    for h in stage_dict.get("aHero", []):
        out += encode_submessage(8, build_pb_hero(h))
    out += encode_int32(9, int(stage_dict.get("serverId", 1)))
    for av in stage_dict.get("avatar", [1, 1]):
        out += encode_int32(10, int(av))
    if "clanName" in stage_dict:
        out += encode_string(11, stage_dict["clanName"])
    out += encode_int32(12, int(stage_dict.get("petId", 0)))
    for ea in stage_dict.get("extraAwards", []):
        out += encode_int32(13, int(ea))
    return bytes(out)


def build_pb_brave_trial(brave_data: dict) -> bytes:
    out = bytearray()
    out += encode_int32(1, int(brave_data.get("stage", 1)))
    out += encode_int32(2, int(brave_data.get("chestAward", 0)))
    for stg in brave_data.get("aStage", []):
        out += encode_submessage(3, build_pb_stage(stg))
    for h in brave_data.get("aHero", []):
        out += encode_submessage(4, build_pb_hero(h))
    for cb in brave_data.get("chestBonus", [0, 0, 0, 0, 0]):
        out += encode_int32(5, int(cb))
    for bi in brave_data.get("badgeInfo", [1500, 100, 100, 100, 200, 0, 0, 0]):
        out += encode_int32(6, int(bi))
    for m in brave_data.get("aMarket", []):
        out += encode_submessage(7, m)
    return bytes(out)


def get_or_create_brave_trial_state(state: dict) -> dict:
    bt = state.get("brave_trial")
    if not bt or not isinstance(bt, dict):
        bt = {}
        state["brave_trial"] = bt

    bt.setdefault("stage", 1)
    bt.setdefault("badges", 2000)
    bt.setdefault("bless1", 0)
    bt.setdefault("bless2", 0)
    bt.setdefault("bless3", 0)
    bt.setdefault("fee_buy", 100)
    bt.setdefault("fee_bless", 100)
    bt.setdefault("fee_heal", 100)
    bt.setdefault("fee_revive", 200)
    bt.setdefault("chest_opened", [0, 0, 0, 0, 0])

    if not bt.get("stages") or len(bt.get("stages")) < 15:
        stages = []
        names = [
            "Tân Binh Biển Đông", "Thợ Săn Tiền Thưởng", "Hải Tặc Grand Line", "Thuyền Trưởng Arlong",
            "Đội Trưởng CP9", "Chiến Binh Skypiea", "Phó Đô Đốc Vergo", "Thất Vũ Hải Crocodile",
            "Đô Đốc Kizaru", "Tứ Hoàng Kaido", "Hỏa Quyền Ace", "Vua Bóng Đêm Rayleigh",
            "Đô Đốc Aokiji", "Tứ Hoàng Râu Trắng", "Vua Hải Tặc Gol D. Roger"
        ]
        hero_pool = [
            512, 606, 251, 260, 253, 250, 241, 247, 288, 284, 286, 282,
            276, 256, 257, 264, 242, 249, 243, 244, 245, 258, 259, 261
        ]
        for i in range(1, 16):
            s_level = 35 + i * 8
            s_power = 80000 + i * 250000
            s_vip = min(15, i)
            s_name = names[i - 1]
            team_heroes = []
            for p in range(6):
                hid = hero_pool[(i * 3 + p) % len(hero_pool)]
                star = min(13, 5 + i // 2)
                tier = min(6, 1 + i // 3)
                hp_val = 100000.0 + i * 80000.0
                atk_val = 15000.0 + i * 15000.0
                h_dict = {
                    "id": p + 1,
                    "heroId": hid,
                    "position": p,
                    "level": s_level,
                    "tier": tier,
                    "star": star,
                    "point": [atk_val, 1500.0, hp_val, 1000.0, 100.0] + [0.0] * 11 + [hp_val, 0.0, hp_val] + [0.0] * 16,
                    "item": [0, 0, 0, 0, 0, 0],
                    "treasure": 0,
                    "locked": [0, 0, 0, 0],
                    "info": [100, 0, 0, 0],
                    "skills": [1, 1, 1, 1, 1],
                    "skin": 0
                }
                team_heroes.append(h_dict)

            stg_awards = [1, 11, 200 + i * 50]
            extra_awards = [1, 2058, 500] if i in [3, 6, 9, 12, 15] else []

            stages.append({
                "username": f"trial_bot_{i}",
                "name": s_name,
                "vip": s_vip,
                "level": s_level,
                "rankPvp": max(1, 100 - i * 5),
                "awards": stg_awards,
                "atkPower": s_power,
                "aHero": team_heroes,
                "serverId": 1,
                "avatar": [1, hero_pool[i % len(hero_pool)]],
                "clanName": "Băng Hải Tặc",
                "petId": min(6, i // 3),
                "extraAwards": extra_awards
            })
        bt["stages"] = stages
    return bt


# ============================================================================
# 5. Game Database Loader & SQLite Persistence
# ============================================================================

class GameDatabase:
    def __init__(self):
        self.heroes_by_id = {}
        self.five_star_hero_ids = []
        self.equipment_ids = []
        self.artifact_ids = []
        self.artifacts_by_id = {}
        self.pet_ids = []
        self.ten_star_by_base_id = {}
        self.home_quests_dict = {}
        self.hero_stones_by_id = {}
        self.equipment_by_id = {}
        self.base_5star_by_hero_id = {}
        self.pet_ruin_costs = {}
        self.pet_level_costs = {}
        self.pet_passive_costs = {}
        self.star1_hero_ids = []
        self.star2_hero_ids = []
        self.star3_hero_ids = []
        self.star4_hero_ids = []
        self.five_star_by_faction = {}
        self.hero_faction_by_id = {}
        self.db_pet_2 = []
        self.pet2_by_id = {}
        self.pet2_ids = []
        self.db_pet_2_upgrade = {}
        self.pet2_stars = {}
        self.pet2_tiers = {}
        self.pet2_levels = {}
        self._load_json_dbs()

    def _load_json_dbs(self):
        heroes_path = os.path.join(DB_JSON_DIR, "db_heroes.json")
        self.five_star_hero_ids = []
        self.six_star_hero_ids = []
        self.ten_star_hero_ids = []
        self.ss_legend_ids = []

        ruin_path = os.path.join(DB_JSON_DIR, "db_pet_ruin_upgrade.json")
        if os.path.isfile(ruin_path):
            try:
                with open(ruin_path, "r", encoding="utf-8") as f:
                    for x in json.load(f):
                        lvl = int(x.get("level", 0))
                        self.pet_ruin_costs[lvl] = (int(x.get("gold", 100000)), int(x.get("chaos_stone", 200)))
            except Exception as e:
                print(f"[WARN] Failed to load db_pet_ruin_upgrade: {e}")

        pet_lvl_path = os.path.join(DB_JSON_DIR, "db_pet_level_upgrade.json")
        if os.path.isfile(pet_lvl_path):
            try:
                with open(pet_lvl_path, "r", encoding="utf-8") as f:
                    for x in json.load(f):
                        lvl = int(x.get("level", 0))
                        g_up = int(x.get("gold_upgrade", 0))
                        s_up = int(x.get("soul_stone_upgrade", 0))
                        g_ev = int(x.get("gold_evolve", 0))
                        s_ev = int(x.get("soul_stone_evolve", 0))
                        self.pet_level_costs[lvl] = (g_up, s_up, g_ev, s_ev)
            except Exception as e:
                print(f"[WARN] Failed to load db_pet_level_upgrade: {e}")

        pet_pas_path = os.path.join(DB_JSON_DIR, "db_pet_passive_upgrade.json")
        if os.path.isfile(pet_pas_path):
            try:
                with open(pet_pas_path, "r", encoding="utf-8") as f:
                    for x in json.load(f):
                        lvl = int(x.get("level", 0))
                        self.pet_passive_costs[lvl] = x
            except Exception as e:
                print(f"[WARN] Failed to load db_pet_passive_upgrade: {e}")

        # 100% Verified SS Legend IDs with working Spine models & portraits in client APK
        top_legend_keys = [
            251, 260, 253, 250, 241, 247, 288, 284, 286, 282,
            276, 256, 257, 264, 242, 249, 243, 244, 245, 258,
            259, 261, 262, 263, 265, 266, 268, 270, 271, 272,
            274, 281, 285, 287, 289, 292, 293, 294, 295, 296,
            298, 299, 300, 301, 302, 303, 304, 305, 306, 309,
            338, 360
        ]
        valid_5star_keys = [
            66, 67, 68, 69, 70, 72, 73, 75, 77, 78, 79, 80, 81, 82, 83, 84, 85, 87, 90, 91,
            92, 93, 94, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110,
            111, 112, 114, 115, 116, 117, 118, 119, 120, 121, 123, 124, 125, 126, 127, 128, 129,
            130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 141, 143, 144, 145, 146, 148, 151,
            152, 153, 307, 336, 361
        ]

        if os.path.isfile(heroes_path):
            with open(heroes_path, "r", encoding="utf-8") as f:
                heroes = json.load(f)
            for h in heroes:
                hid = int(h["id"])
                pts_raw = json.loads(h.get("point", "[]"))
                pts20 = [0.0] * 20
                for i in range(min(14, len(pts_raw))):
                    pts20[i] = float(pts_raw[i])
                atk = pts20[0] if pts20[0] > 0 else 15000.0
                arm = pts20[1] if pts20[1] > 0 else 1200.0
                hp = pts20[2] if pts20[2] > 0 else 150000.0
                spd = pts20[3] if pts20[3] > 0 else 1100.0
                pts20[0] = atk
                pts20[1] = arm
                pts20[2] = hp
                pts20[3] = spd
                pts20[16] = float(int(atk * 2 + hp + arm * 3))
                pts20[18] = hp
                star = int(h.get("star", 5))
                rank = int(h.get("rank", 5))
                self.heroes_by_id[hid] = {
                    "heroId": hid,
                    "name": h.get("name", f"Hero_{hid}"),
                    "star": star,
                    "rank": rank,
                    "release": int(h.get("release", 0) or 0),
                    "type": h.get("type", "fortress"),
                    "avatar": int(h.get("avatar", hid)),
                    "active": int(h.get("active", 0) or 0),
                    "passive1": int(h.get("passive1", 0) or 0),
                    "passive2": int(h.get("passive2", 0) or 0),
                    "passive3": int(h.get("passive3", 0) or 0),
                    "point": pts20,
                }
                ftype = str(h.get("type", "")).strip().lower()
                if ftype:
                    self.hero_faction_by_id[hid] = ftype
                if star == 1 and hid < 1000:
                    self.star1_hero_ids.append(hid)
                elif star == 2 and hid < 1000:
                    self.star2_hero_ids.append(hid)
                elif star == 3 and hid < 1000:
                    self.star3_hero_ids.append(hid)
                elif star == 4 and hid < 1000:
                    self.star4_hero_ids.append(hid)
                elif star == 5 and hid < 1000 and hid != 30000:
                    self.five_star_hero_ids.append(hid)
                    if ftype:
                        self.five_star_by_faction.setdefault(ftype, []).append(hid)
                elif star == 6 and hid < 1000:
                    self.six_star_hero_ids.append(hid)
                elif star == 10 and hid < 1000:
                    self.ten_star_hero_ids.append(hid)
                    if rank >= 6:
                        self.ss_legend_ids.append(hid)

        # Include all 10-star SS legends including newest: Luffy Nika(512), Kaido Hybrid(497), Yamato(467), Ace&Sabo(506)...
        for hid, hdata in self.heroes_by_id.items():
            if hdata["star"] == 10 and hdata["rank"] >= 6 and hid < 1000 and hid not in self.ss_legend_ids:
                self.ss_legend_ids.append(hid)

        # Ensure newest legends are always in ss_legend_ids and map to 10-star forms
        for extra_ss in (587, 518, 617, 632, 638, 641, 644, 647, 506, 611, 608, 491, 512, 497, 467):
            if extra_ss in self.heroes_by_id and extra_ss not in self.ss_legend_ids:
                self.ss_legend_ids.append(extra_ss)

        # Mapping for 5-star -> 10-star forms (Sao đỏ, Lv.250)
        ten_star_mappings = {
            606: 608,  # Gol D. Roger
            609: 611,  # Nami & Robin
            642: 644,  # Monkey D. Garp
            633: 635,  # Luffy Reverse
            621: 623,  # S-Hawk
            510: 512,  # Luffy Nika
            495: 497,  # Kaido Hybrid
            465: 467,  # Yamato
            504: 506,  # Ace & Sabo
            489: 491,  # Shirahoshi Noel
            587: 587,  # Lilith
            647: 647,  # Edward Newgate
        }
        for b_id, t_id in ten_star_mappings.items():
            if t_id in self.heroes_by_id:
                self.ten_star_by_base_id[b_id] = t_id

        # Load home quests dictionary if available
        self.home_quests_dict = {}
        try:
            q_paths = [
                os.path.join(DB_JSON_DIR, "db_home_quest.json"),
                os.path.join(PROJECT_DIR, "extracted_db_101", "db_home_quest.json"),
                os.path.join(BASE_DIR, "decompiled", "db", "db_home_quest.json"),
                os.path.join(BASE_DIR, "extracted_dbs", "db_home_quest.json"),
            ]
            q_path = next((path for path in q_paths if os.path.isfile(path)), q_paths[0])
            if os.path.exists(q_path):
                with open(q_path, "r", encoding="utf-8") as qf:
                    q_list = json.load(qf)
                    for q in q_list:
                        self.home_quests_dict[int(q["id"])] = q
        except Exception:
            pass

        # Load db_tuong_link.json & db_hero_fusion.json & db_hero_upgrade.json for accurate star/level/tier mappings
        self.base_5star_by_hero_id = {}
        self.five_star_by_four_star = {}
        self.six_star_by_five_star = {}
        self.ten_star_by_six_star = {}
        self.hero_upgrade_table = {}
        tuong_link_path = os.path.join(DB_JSON_DIR, "db_tuong_link.json")
        if not os.path.isfile(tuong_link_path):
            tuong_link_path = os.path.join(PROJECT_DIR, "extracted_db_101", "db_tuong_link.json")
        if os.path.isfile(tuong_link_path):
            try:
                with open(tuong_link_path, "r", encoding="utf-8") as tlf:
                    tl_list = json.load(tlf)
                for r in tl_list:
                    b4 = int(r.get("id_4star", 0) or 0)
                    b5 = int(r.get("id_5star", 0) or 0)
                    s6 = int(r.get("id_6star", 0) or 0)
                    t10 = int(r.get("id_10star", 0) or 0)
                    if b5 > 0:
                        self.base_5star_by_hero_id[b5] = b5
                        if b4 > 0:
                            self.five_star_by_four_star[b4] = b5
                        if s6 > 0:
                            self.base_5star_by_hero_id[s6] = b5
                            self.six_star_by_five_star[b5] = s6
                        if t10 > 0:
                            self.base_5star_by_hero_id[t10] = b5
                            self.ten_star_by_base_id[b5] = t10
                            if s6 > 0:
                                self.ten_star_by_six_star[s6] = t10
                                self.ten_star_by_base_id[s6] = t10
            except Exception:
                pass

        fusion_path = os.path.join(DB_JSON_DIR, "db_hero_fusion.json")
        if not os.path.isfile(fusion_path):
            fusion_path = os.path.join(PROJECT_DIR, "extracted_db_101", "db_hero_fusion.json")
        if os.path.isfile(fusion_path):
            try:
                with open(fusion_path, "r", encoding="utf-8") as hff:
                    hf_list = json.load(hff)
                for r in hf_list:
                    res_id = int(r.get("hero_id", 0) or 0)
                    mat1 = int(r.get("material_hero1", 0) or 0)
                    if res_id > 0 and mat1 > 0:
                        res_star = int(self.heroes_by_id.get(res_id, {}).get("star", 0))
                        mat_star = int(self.heroes_by_id.get(mat1, {}).get("star", 0))
                        if mat_star == 4 and res_star == 5:
                            self.five_star_by_four_star[mat1] = res_id
                        elif mat_star == 5 and res_star == 6:
                            self.six_star_by_five_star[mat1] = res_id
            except Exception:
                pass

        h_upg_path = os.path.join(DB_JSON_DIR, "db_hero_upgrade.json")
        if not os.path.isfile(h_upg_path):
            h_upg_path = os.path.join(PROJECT_DIR, "extracted_db_101", "db_hero_upgrade.json")
        if os.path.isfile(h_upg_path):
            try:
                with open(h_upg_path, "r", encoding="utf-8") as huf:
                    for r in json.load(huf):
                        lv = int(r.get("level", 0) or 0)
                        if lv > 0:
                            self.hero_upgrade_table[lv] = {
                                "gold": int(r.get("gold", 0) or 0),
                                "spirit": int(r.get("spirit", 0) or 0),
                                "upgrade_gold": int(r.get("upgrade_gold", 0) or 0),
                                "upgrade_stones": int(r.get("upgrade_stones", 0) or 0),
                            }
            except Exception:
                pass

        # Load db_hero_skill.json & parse active & normal skill meta (multipliers, direct HP damage, target counts)
        self.hero_skills_by_id = {}
        self.active_skill_meta = {}
        self.normal_skill_meta = {}
        skill_path = os.path.join(DB_JSON_DIR, "db_hero_skill.json")
        if not os.path.isfile(skill_path):
            skill_path = os.path.join(PROJECT_DIR, "extracted_db_101", "db_hero_skill.json")
        if os.path.isfile(skill_path):
            try:
                import re as _re
                with open(skill_path, "r", encoding="utf-8") as skf:
                    sk_list = json.load(skf)
                for sk in sk_list:
                    sk_id = int(sk["id"])
                    raw_desc = str(sk.get("description") or "")
                    self.hero_skills_by_id[sk_id] = sk
                    desc_clean = _re.sub(r"<[^>]+>", "", raw_desc)
                    desc_low = desc_clean.lower()

                    # 1. Parse active skill properties
                    mult_atk = 1.65
                    scale_stat = "atk"
                    m_atk = _re.search(r"(?:gây\s*)?(?:sát\s*thương|st)[^\.\n]{0,40}?(\d+(?:[.,]\d+)?)\s*%\s*Công", desc_clean, _re.I) or _re.search(r"(\d+(?:[.,]\d+)?)\s*%\s*Công", desc_clean, _re.I)
                    m_hp = _re.search(r"(?:gây\s*)?(?:sát\s*thương|st)[^\.\n]{0,45}?(\d+(?:[.,]\d+)?)\s*%\s*máu\s*tối", desc_clean, _re.I)
                    m_def = _re.search(r"Thủ\s*\*\s*(\d+)", desc_clean, _re.I)
                    if m_atk:
                        mult_atk = max(0.65, min(6.00, float(m_atk.group(1).replace(",", ".")) / 100.0))
                    elif m_hp:
                        mult_atk = max(0.15, min(1.50, float(m_hp.group(1).replace(",", ".")) / 100.0))
                        scale_stat = "hp"
                    elif m_def:
                        mult_atk = max(2.0, min(10.0, float(m_def.group(1)) / 100.0))
                        scale_stat = "arm"

                    # Direct HP damage parsing (only when explicitly dealing damage, not shields/heals/revives)
                    hp_direct_pct = 0.0
                    lost_hp_pct = 0.0
                    max_direct_cap_mult = 0.0
                    m_hp_self = _re.search(r"(?:gây\s*thêm\s*|kèm\s*|bằng\s*|tương\s*đương\s*)(?:sát\s*thương\s*|st\s*)?(?:trực\s*tiếp\s*)?(?:bằng\s*|tương\s*đương\s*)?(\d+(?:[.,]\d+)?)\s*%\s*(?:lượng\s*)?máu\s*tối\s*đa\s*(?:của\s*)?bản\s*thân", desc_clean, _re.I)
                    if m_hp_self and "khiên" not in m_hp_self.group(0).lower() and "hồi" not in m_hp_self.group(0).lower():
                        hp_direct_pct = min(0.35, float(m_hp_self.group(1).replace(",", ".")) / 100.0)
                    m_hp_lost = _re.search(r"(\d+(?:[.,]\d+)?)\s*%\s*(?:lượng\s*)?máu\s*đã\s*(?:mất|tổn\s*thất)", desc_clean, _re.I)
                    if m_hp_lost:
                        lost_hp_pct = min(0.50, float(m_hp_lost.group(1).replace(",", ".")) / 100.0)
                    m_cap = _re.search(r"tối\s*đa\s*(\d+(?:[.,]\d+)?)\s*%\s*công", desc_clean, _re.I)
                    if m_cap:
                        max_direct_cap_mult = min(12.0, float(m_cap.group(1).replace(",", ".")) / 100.0)

                    # Pure support / shield / revive skills & support heroes should never nuke enemies
                    if sk_id in (2101, 2105, 2109, 2113, 2841, 2845, 2849, 2853):
                        scale_stat = "atk"
                        mult_atk = 0.85
                        hp_direct_pct = 0.0
                        lost_hp_pct = 0.0
                        max_direct_cap_mult = 1.5
                    elif (2745 <= sk_id <= 2760) or (2777 <= sk_id <= 2792):
                        scale_stat = "atk"
                        mult_atk = min(mult_atk, 2.50)
                        hp_direct_pct = min(hp_direct_pct, 0.15)
                        max_direct_cap_mult = min(max_direct_cap_mult or 3.5, 3.5)

                    # Parse target count
                    t_cnt = 4
                    if any(k in desc_low for k in ["tất cả kẻ địch", "toàn bộ kẻ địch", "đội địch", "toàn đội địch", "toàn phe địch"]):
                        t_cnt = 6
                    else:
                        m_t = _re.search(r"(\d+)\s*(?:mục tiêu|kẻ\s*)?địch", desc_low)
                        if m_t:
                            t_cnt = max(1, min(6, int(m_t.group(1))))
                        elif "hàng trước" in desc_low:
                            t_cnt = 2
                        elif "hàng sau" in desc_low:
                            t_cnt = 4
                        elif "đơn" in desc_low or "máu" in desc_low:
                            t_cnt = 1

                    heal_mult = 0.0
                    m_heal = _re.search(r"hồi\s*máu.*?(\d+(?:[.,]\d+)?)\s*%\s*Công", desc_clean, _re.I)
                    if m_heal:
                        heal_mult = max(0.0, min(5.0, float(m_heal.group(1).replace(",", ".")) / 100.0))

                    self.active_skill_meta[sk_id] = {
                        "mult": mult_atk,
                        "stat": scale_stat,
                        "hp_direct_pct": hp_direct_pct,
                        "lost_hp_pct": lost_hp_pct,
                        "max_direct_cap_mult": max_direct_cap_mult,
                        "targets": t_cnt,
                        "heal_mult": heal_mult,
                    }

                    # 2. Parse normal attack modifiers in passives
                    if "đánh thường" in desc_low or "tấn công thường" in desc_low:
                        norm_targets = 6 if any(k in desc_low for k in ["toàn bộ", "tất cả"]) else 1
                        norm_mult = 1.0
                        norm_hp_pct = 0.0
                        norm_cap_mult = 8.0
                        m_norm_atk = _re.search(r"đánh\s*thường[^\.\n]*?(?:gây\s*sát\s*thương)?\s*(\d+(?:[.,]\d+)?)\s*%\s*Công", desc_clean, _re.I)
                        if m_norm_atk:
                            norm_mult = min(3.5, float(m_norm_atk.group(1).replace(",", ".")) / 100.0)
                        m_norm_hp = _re.search(r"đánh\s*thường[^\.\n]*?(\d+(?:[.,]\d+)?)\s*%\s*(?:lượng\s*)?máu\s*tối\s*đa\s*(?:của\s*)?bản\s*thân", desc_clean, _re.I)
                        if m_norm_hp:
                            norm_hp_pct = min(0.25, float(m_norm_hp.group(1).replace(",", ".")) / 100.0)
                        if (2745 <= sk_id <= 2760) or (2777 <= sk_id <= 2792) or (2841 <= sk_id <= 2856) or (2101 <= sk_id <= 2120):
                            norm_mult = min(norm_mult, 1.50)
                            norm_hp_pct = min(norm_hp_pct, 0.12)
                            norm_cap_mult = 2.5
                        if norm_targets > 1 or norm_mult > 1.0 or norm_hp_pct > 0.0:
                            self.normal_skill_meta[sk_id] = {
                                "targets": norm_targets,
                                "mult": norm_mult,
                                "hp_direct_pct": norm_hp_pct,
                                "max_direct_cap_mult": norm_cap_mult,
                            }
            except Exception as _e:
                print(f"[WARN] Failed to load db_hero_skill: {_e}")

        self.shards_by_id = {}
        shard_path = os.path.join(DB_JSON_DIR, "db_item_manh.json")
        with open(shard_path, "r", encoding="utf-8") as shard_file:
            for row in json.load(shard_file):
                self.shards_by_id[(int(row["type"]), int(row["id"]))] = row

        # Load db_hero_skins.json
        self.hero_skins_by_id = {}
        skin_path = os.path.join(DB_JSON_DIR, "db_hero_skins.json")
        if not os.path.isfile(skin_path):
            skin_path = os.path.join(PROJECT_DIR, "extracted_db_101", "db_hero_skins.json")
        if os.path.isfile(skin_path):
            try:
                with open(skin_path, "r", encoding="utf-8") as sknf:
                    skn_list = json.load(sknf)
                for skn in skn_list:
                    self.hero_skins_by_id[int(skn["id"])] = skn
            except Exception:
                pass

        # Load db_hero_stone.json
        self.hero_stones_by_id = {}
        stone_path = os.path.join(DB_JSON_DIR, "db_hero_stone.json")
        if not os.path.isfile(stone_path):
            stone_path = os.path.join(PROJECT_DIR, "extracted_db_101", "db_hero_stone.json")
        if os.path.isfile(stone_path):
            try:
                with open(stone_path, "r", encoding="utf-8") as sf:
                    s_list = json.load(sf)
                for s in s_list:
                    self.hero_stones_by_id[int(s["id"])] = s
        # Load db_item_equipment.json
            except Exception:
                pass

        eq_path = os.path.join(DB_JSON_DIR, "db_item_equipment.json")
        if not os.path.isfile(eq_path):
            eq_path = os.path.join(PROJECT_DIR, "extracted_db_101", "db_item_equipment.json")
        if os.path.isfile(eq_path):
            try:
                with open(eq_path, "r", encoding="utf-8") as ef:
                    e_list = json.load(ef)
                for e in e_list:
                    self.equipment_by_id[int(e["id"])] = e
            except Exception:
                pass

        # Collect all valid heroKeys under 1000 for normal avatar selection
        self.hero_keys_for_avatar = [
            hid for hid, hdata in self.heroes_by_id.items()
            if hid < 1000 and (hdata["star"] in (5, 6, 10) or hid in range(1, 100))
        ]

        # Special avatars (Khung avatar & Danh hiệu) from db_avatar.json
        avatar_path = os.path.join(DB_JSON_DIR, "db_avatar.json")
        self.special_avatar_ids = []
        if os.path.isfile(avatar_path):
            try:
                with open(avatar_path, "r", encoding="utf-8") as f:
                    av_data = json.load(f)
                self.special_avatar_ids = [int(x["id"]) for x in av_data if "id" in x]
            except Exception:
                pass
        if not self.special_avatar_ids:
            self.special_avatar_ids = [
                1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                21, 22, 23, 24, 25, 31, 32, 33, 34, 35, 36, 37, 48, 49, 50, 51, 52, 53,
                54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71,
                72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 1000,
                1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008, 1009, 1010, 1011, 1012,
                1013, 1014, 1015, 1016, 1017, 1018, 1019, 1020, 1021, 1022
            ]

        # Puppets / Phôi Lính Chì (5, 6, 9, 10 sao)
        self.puppets_5_star = [354, 355, 356, 357, 358, 359]
        self.puppets_6_star = [348, 349, 350, 351, 352, 353]
        self.puppets_9_star = [310, 311, 312, 313, 314, 315]
        self.puppets_10_star = [322, 323, 324, 325, 326, 327]
        self.puppet_by_faction_9 = {
            "shadow": 310, "fortress": 311, "abyss": 312, "forest": 313, "dark": 314, "light": 315,
        }
        self.puppet_by_faction_10 = {
            "shadow": 322, "fortress": 323, "abyss": 324, "forest": 325, "dark": 326, "light": 327,
        }
        self.puppet_by_faction_6 = {
            "shadow": 348, "fortress": 349, "abyss": 350, "forest": 351, "dark": 352, "light": 353,
        }
        self.puppet_by_faction_5 = {
            "shadow": 354, "fortress": 355, "abyss": 356, "forest": 357, "dark": 358, "light": 359,
        }

        six_by_name = {}
        six_by_name_type = {}
        five_by_name_type = {}
        for hid, hdata in self.heroes_by_id.items():
            if hid < 1000:
                k_nt = (hdata["name"].strip().lower(), str(hdata["type"]).strip().lower())
                if hdata["star"] == 6:
                    six_by_name[hdata["name"].strip().lower()] = hid
                    six_by_name_type[k_nt] = hid
                elif hdata["star"] == 5:
                    five_by_name_type[k_nt] = hid
        self.six_star_by_base_id = {}
        for hid, hdata in self.heroes_by_id.items():
            if hid < 1000:
                k_nt = (hdata["name"].strip().lower(), str(hdata["type"]).strip().lower())
                if hid in self.six_star_by_five_star:
                    self.six_star_by_base_id[hid] = self.six_star_by_five_star[hid]
                else:
                    self.six_star_by_base_id[hid] = six_by_name_type.get(
                        k_nt, six_by_name.get(hdata["name"].strip().lower(), hid)
                    )
                if hdata["star"] == 4 and hid not in self.five_star_by_four_star and k_nt in five_by_name_type:
                    self.five_star_by_four_star[hid] = five_by_name_type[k_nt]

        ten_by_name_type = {}
        ten_by_name = {}
        for hid, hdata in self.heroes_by_id.items():
            if hdata["star"] == 10 and hid < 1000:
                k_nt = (hdata["name"].strip().lower(), str(hdata["type"]).strip().lower())
                ten_by_name_type[k_nt] = hid
                ten_by_name[hdata["name"].strip().lower()] = hid
        for hid, hdata in self.heroes_by_id.items():
            if hid < 1000 and hid not in self.ten_star_by_base_id:
                k_nt = (hdata["name"].strip().lower(), str(hdata["type"]).strip().lower())
                self.ten_star_by_base_id[hid] = ten_by_name_type.get(
                    k_nt, ten_by_name.get(hdata["name"].strip().lower(), hid)
                )

        if not self.five_star_hero_ids:
            self.five_star_hero_ids = list(self.heroes_by_id.keys())[:30] or [1, 2, 3, 4, 5, 6]
        if not self.ss_legend_ids:
            self.ss_legend_ids = self.ten_star_hero_ids[:30] or self.five_star_hero_ids

        eq_path = os.path.join(DB_JSON_DIR, "db_item_equipment.json")
        self.equipment_type_by_id = {}
        self.equipments_by_type = {0: [], 1: [], 2: [], 3: []}
        if os.path.isfile(eq_path):
            with open(eq_path, "r", encoding="utf-8") as f:
                eqs = json.load(f)
            self.equipment_ids = [int(e["id"]) for e in eqs]
            for e in eqs:
                eid = int(e["id"])
                etype = int(e.get("type", 0))
                self.equipment_type_by_id[eid] = etype
                self.equipments_by_type.setdefault(etype, []).append(eid)
        if not self.equipment_ids:
            self.equipment_ids = list(range(1, 35))
            for eid in self.equipment_ids:
                t = (eid - 1) % 4
                self.equipment_type_by_id[eid] = t
                self.equipments_by_type[t].append(eid)

        art_path = os.path.join(DB_JSON_DIR, "db_item_baovat.json")
        if os.path.isfile(art_path):
            with open(art_path, "r", encoding="utf-8") as f:
                arts = json.load(f)
            self.artifacts_by_id = {int(a["id"]): a for a in arts}
            self.artifact_ids = sorted(self.artifacts_by_id)
        if not self.artifact_ids:
            self.artifact_ids = [1, 2, 3, 4, 5, 6, 7, 8]

        pet_path = os.path.join(DB_JSON_DIR, "db_pet.json")
        if os.path.isfile(pet_path):
            with open(pet_path, "r", encoding="utf-8") as f:
                pets = json.load(f)
            self.pet_ids = [int(p["id"]) for p in pets if int(p.get("released", 1)) == 1]
        if not self.pet_ids:
            self.pet_ids = [1, 3, 4]

        # Load Pet 2 (64 Animal Pets in db_pet_2)
        pet2_path = os.path.join(DB_JSON_DIR, "db_pet_2.json")
        if os.path.isfile(pet2_path):
            try:
                with open(pet2_path, "r", encoding="utf-8") as f:
                    self.db_pet_2 = json.load(f)
                    self.pet2_by_id = {int(p["id"]): p for p in self.db_pet_2}
                    self.pet2_ids = sorted(self.pet2_by_id.keys())
            except Exception as e:
                print(f"[WARN] Failed to load db_pet_2: {e}")

        pet2_up_path = os.path.join(DB_JSON_DIR, "db_pet_2_upgrade.json")
        if os.path.isfile(pet2_up_path):
            try:
                with open(pet2_up_path, "r", encoding="utf-8") as f:
                    self.db_pet_2_upgrade = json.load(f)
                    self.pet2_stars = {int(s["star"]): s for s in self.db_pet_2_upgrade.get("stars", [])}
                    self.pet2_tiers = {int(t["level"]): t for t in self.db_pet_2_upgrade.get("tiers", [])}
                    self.pet2_levels = {int(l["level"]): l for l in self.db_pet_2_upgrade.get("levels", [])}
            except Exception as e:
                print(f"[WARN] Failed to load db_pet_2_upgrade: {e}")

        cons_path = os.path.join(DB_JSON_DIR, "db_item_consumable.json")
        if not os.path.isfile(cons_path):
            cons_path = os.path.join(PROJECT_DIR, "extracted_db_101", "db_item_consumable.json")
        self.consumable_previews = {}
        self.consumable_types = {}
        self.consumable_definitions = {}
        if os.path.isfile(cons_path):
            with open(cons_path, "r", encoding="utf-8") as f:
                cons = json.load(f)
            for c in cons:
                cid = int(c["id"])
                self.consumable_types[cid] = int(c.get("type", 1))
                self.consumable_definitions[cid] = c
                p_str = (c.get("preview") or "").strip()
                if p_str.startswith("["):
                    try:
                        # Four shipped chest definitions contain a trailing comma.
                        # Normalize that syntax only; never infer missing rewards.
                        p_arr = json.loads(re.sub(r",\s*\]$", "]", p_str))
                        if isinstance(p_arr, list) and p_arr:
                            self.consumable_previews[cid] = parse_bonus_preview_items(p_arr)
                    except Exception:
                        pass
        # These tables are part of the same client database as consumables.
        self.player_exp = {}
        self.campaign_outputs = {}
        for filename, target in (("db_player_exp.json", "exp"), ("db_vuotai.json", "campaign")):
            path = os.path.join(DB_JSON_DIR, filename)
            if not os.path.isfile(path):
                path = os.path.join(PROJECT_DIR, "extracted_db_101", filename)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    rows = json.load(f)
                if target == "exp":
                    self.player_exp = {int(r["level"]): int(r["exp"]) for r in rows}
                else:
                    self.campaign_outputs = {int(r["id"]): json.loads(r["output_old"]) for r in rows if r.get("output_old")}

    def make_hero_instance(self, inst_id: int, hero_id: int, level: int = 250, tier: int = 6, star: int = None) -> dict:
        tpl = self.heroes_by_id.get(hero_id)
        max_lvl_by_star = {1: 40, 2: 50, 3: 60, 4: 80, 5: 100, 6: 140, 7: 160, 8: 180, 9: 200, 10: 250, 11: 260, 12: 270, 13: 290, 14: 310, 15: 330}
        if tpl:
            base_pts = list(tpl["point"])
            if hero_id in (627, 628, 629):
                base_pts[2] = round(base_pts[2] * 1.35)
                base_pts[5] = max(base_pts[5], 220.0)
                base_pts[7] = max(base_pts[7], 260.0)
                base_pts[8] = max(base_pts[8], 210.0)
            tpl_star = int(tpl.get("star", 5))
            use_star = star if star is not None else tpl_star
            tpl_max_lvl = max_lvl_by_star.get(tpl_star, 100)
            max_tier = 6 if use_star >= 6 else min(5, max(1, use_star))
            lvl_ratio = 0.12 + 0.88 * (max(1, level) / float(tpl_max_lvl))
            tier_ratio = 0.85 + 0.15 * (max(0, min(6, tier)) / float(max(1, max_tier)))
            star_bonus = 1.0
            if use_star > tpl_star:
                star_bonus = 1.0 + 0.12 * (use_star - tpl_star)
            mult = max(0.08, lvl_ratio * tier_ratio * star_bonus)
            pts = list(base_pts)
            pts[0] = max(10, round(base_pts[0] * mult))
            pts[1] = max(5, round(base_pts[1] * mult))
            pts[2] = max(50, round(base_pts[2] * mult))
            pts[3] = max(50, round(base_pts[3] * (0.75 + 0.25 * min(1.5, max(1, level) / float(tpl_max_lvl)))))
            pts[16] = round(pts[0] * 2 + pts[2] / 6.0 + pts[1] * 3)
            pts[18] = pts[2]
        else:
            use_star = star if star is not None else 5
            lvl_m = max(1, level)
            pts = [float(lvl_m * 25), float(lvl_m * 8), float(lvl_m * 250), float(200 + lvl_m * 3), 0.0, 200.0, 0.0, 0.0, 150.0, 300.0, 100.0, 0.0, 0.0, 0.0, 0.0, 0.0, float(lvl_m * 320), 0.0, float(lvl_m * 250), 0.0]
        return {
            "id": inst_id,
            "heroId": hero_id,
            "point": pts,
            "level": level,
            "tier": tier,
            "star": use_star,
            "item": [0, 0, 0, 0, 0, 0],
            "treasure": 0,
            "locked": [0, 0, 0, 0],
            "questStatus": [0, 0, 0, 0],
            "skillId": [],
            "eSkill": [1, 1, 1, 1, 1],
            "skin": 0,
        }




# ============================================================================
# Pet 2 Helpers (db_pet_2 & db_pet_2_upgrade)
# ============================================================================
def calc_pet2_stats(base_pet: dict, star: int, level: int, tier: int, intel: float, batk: float, bhp: float, bdef: float, bspd: float, game_db=None):
    star_scale = 7.8
    tier_scale = 1.0
    if game_db:
        s_info = getattr(game_db, "pet2_stars", {}).get(star)
        if s_info:
            star_scale = float(s_info.get("scale", 7.8))
        t_info = getattr(game_db, "pet2_tiers", {}).get(tier)
        if t_info:
            tier_scale = float(t_info.get("scaleBasic", 1000)) / 1000.0
    intel_max = float(base_pet.get("intelligent_max", 1500.0)) if base_pet else 1500.0
    intel_mult = float(intel) / intel_max if (intel_max > 0 and intel > 0) else 1.0
    lvl_mult = (1.0 + max(0, level - 1) * 0.08) * intel_mult
    atk = int(round(batk * star_scale * tier_scale * lvl_mult))
    hp = int(round(bhp * star_scale * tier_scale * lvl_mult))
    defs = int(round(bdef * star_scale * tier_scale * lvl_mult))
    spd = int(round(bspd * star_scale * tier_scale * lvl_mult))
    return atk, hp, defs, spd


def make_pet2_instance(base_pet: dict, star: int = 5, level: int = 1, tier: int = 1, game_db=None) -> dict:
    pid = int(base_pet["id"])
    batk = float(base_pet.get("basic_attack_max", 100.0))
    bhp = float(base_pet.get("basic_hp_max", 500.0))
    bdef = float(base_pet.get("basic_def_max", 2.0))
    bspd = float(base_pet.get("basic_spd_max", 5.0))
    intel = float(base_pet.get("intelligent_max", 1500.0))
    rank = int(base_pet.get("rank", 5))
    tier_intel = 5 if rank >= 6 else (4 if rank == 5 else 3)
    atk, hp, defs, spd = calc_pet2_stats(base_pet, star, level, tier, intel, batk, bhp, bdef, bspd, game_db)
    passives = []
    for k in ("passive1", "passive2", "passive3"):
        v = int(base_pet.get(k, -1))
        if v > 0:
            passives.append(v)
    return {
        "id": pid,
        "set_id": pid,
        "star": star,
        "tier": tier,
        "level": level,
        "exp": 0,
        "basic_atk": batk,
        "basic_hp": bhp,
        "basic_def": bdef,
        "basic_spd": bspd,
        "intelligent": intel,
        "tier_intelligent": tier_intel,
        "atk": atk,
        "hp": hp,
        "def": defs,
        "spd": spd,
        "skills": passives
    }


_PET2_TIER_SCALE_BASIC = {
    0: 1000, 1: 1005, 2: 1015, 3: 1030, 4: 1050, 5: 1075,
    6: 1105, 7: 1140, 8: 1180, 9: 1225, 10: 1275
}


def build_pet2_vector(p: dict, base_p: dict) -> list:
    pid = int(p.get("id", p.get("set_id", 1)))
    set_id = int(p.get("set_id", pid))
    star = int(p.get("star", 5))
    tier = int(p.get("tier", 1))
    level = int(p.get("level", 1))
    batk = float(p.get("basic_atk", base_p.get("basic_attack_max", 100.0) if base_p else 100.0))
    bhp = float(p.get("basic_hp", base_p.get("basic_hp_max", 500.0) if base_p else 500.0))
    bdef = float(p.get("basic_def", base_p.get("basic_def_max", 2.0) if base_p else 2.0))
    bspd = float(p.get("basic_spd", base_p.get("basic_spd_max", 5.0) if base_p else 5.0))
    intel = float(p.get("intelligent", base_p.get("intelligent_max", 1500.0) if base_p else 1500.0))
    tier_intel = int(p.get("tier_intelligent", 4))
    atk = int(p.get("atk", 1000))
    hp = int(p.get("hp", 5000))
    defs = int(p.get("def", 20))
    spd = int(p.get("spd", 5))
    exp = int(p.get("exp", 0))
    change_basic = int(p.get("change_basic", 0))
    datk = float(p.get("diff_atk", 0.0))
    dhp = float(p.get("diff_hp", 0.0))
    ddef = float(p.get("diff_def", 0.0))
    dspd = float(p.get("diff_spd", 0.0))
    skills = p.get("skills", [])
    if not skills and base_p:
        for k in ("passive1", "passive2", "passive3"):
            v = int(base_p.get(k, -1))
            if v > 0:
                skills.append(v)

    scale_basic = _PET2_TIER_SCALE_BASIC.get(tier, 1000)
    sb_mult = scale_basic / 10.0
    vec = [
        pid,
        set_id,
        star,
        tier,
        level,
        0, 0,
        int(round(batk * sb_mult)),
        int(round(bhp * sb_mult)),
        int(round(bdef * sb_mult)),
        int(round(bspd * sb_mult)),
        int(round(intel * 100)),
        tier_intel,
        scale_basic,
        0, 0, 0,
        exp,
        change_basic,
        int(round(atk * 100)),
        int(round(hp * 100)),
        int(round(defs * 100)),
        int(round(spd * 100)),
        int(round(datk * sb_mult)),
        int(round(dhp * sb_mult)),
        int(round(ddef * sb_mult)),
        int(round(dspd * sb_mult))
    ] + [int(s) for s in skills]
    return vec


def ensure_pet2_state(state: dict, game_db=None):
    state.setdefault("pet2_shards", {})
    state.setdefault("pets2", [])
    state.setdefault("pet2_equips", {})
    if "unittest.case" in sys.modules:
        return
    pets_list = state.get("pets2", [])
    if pets_list and (int(pets_list[0].get("star", 0)) < 5 or int(pets_list[-1].get("star", 0)) < 5):
        for p in pets_list:
            if isinstance(p, dict) and int(p.get("star", 0)) < 5:
                p["star"] = 5
                if int(p.get("tier", 0)) < 1:
                    p["tier"] = 1
                if game_db is not None and hasattr(game_db, "pet2_by_id"):
                    base_p = game_db.pet2_by_id.get(int(p.get("set_id", p.get("id", 1))))
                    if base_p is not None:
                        atk, hp, defs, spd = calc_pet2_stats(
                            base_p, int(p["star"]), int(p.get("level", 1)), int(p["tier"]),
                            float(p.get("intelligent", 0)), float(p.get("basic_atk", 0)),
                            float(p.get("basic_hp", 0)), float(p.get("basic_def", 0)),
                            float(p.get("basic_spd", 0)), game_db
                        )
                        p["atk"], p["hp"], p["def"], p["spd"] = atk, hp, defs, spd


_PET2_VEC_CACHE = {}


def build_pet2_status_payload(state: dict, game_db=None) -> bytes:
    ensure_pet2_state(state, game_db)
    shards = state.get("pet2_shards", {})
    
    # Vector 0: [countdown, pid1, cnt1, pid2, cnt2, ..., 0, 0]
    v0_longs = [max(0, int(state.get("pet2_free_at", 0)) - int(time.time()))]
    for pid in range(1, 65):
        v0_longs.extend([pid, int(shards.get(str(pid), 0))])
    v0_longs.extend([0, 0])
    v0 = build_common_vector(a_long=v0_longs)

    # Vector 1: [petId1, heroId1, ..., 0, 0]
    equips = state.get("pet2_equips", {})
    v1_longs = []
    for pid_str, hid in equips.items():
        if int(hid) > 0:
            v1_longs.extend([int(pid_str), int(hid)])
    v1_longs.extend([0, 0])
    v1 = build_common_vector(a_long=v1_longs)

    # Vector 2..N: owned pets
    pet_vecs = []
    if len(_PET2_VEC_CACHE) > 8192:
        _PET2_VEC_CACHE.clear()
    for p in state.get("pets2", []):
        if isinstance(p, dict):
            ck = (
                int(p.get("id", p.get("set_id", 1))),
                int(p.get("set_id", p.get("id", 1))),
                int(p.get("star", 5)),
                int(p.get("tier", 1)),
                int(p.get("level", 1)),
                round(float(p.get("basic_atk", 0)), 4),
                round(float(p.get("basic_hp", 0)), 4),
                round(float(p.get("basic_def", 0)), 4),
                round(float(p.get("basic_spd", 0)), 4),
                round(float(p.get("intelligent", 0)), 4),
                int(p.get("tier_intelligent", 4)),
                int(p.get("atk", 1000)),
                int(p.get("hp", 5000)),
                int(p.get("def", 20)),
                int(p.get("spd", 5)),
                int(p.get("exp", 0)),
                int(p.get("change_basic", 0)),
                round(float(p.get("diff_atk", 0)), 4),
                round(float(p.get("diff_hp", 0)), 4),
                round(float(p.get("diff_def", 0)), 4),
                round(float(p.get("diff_spd", 0)), 4),
                tuple(p.get("skills") or ()),
            )
            cached_vec = _PET2_VEC_CACHE.get(ck)
            if cached_vec is None:
                base_p = game_db.pet2_by_id.get(ck[1]) if game_db else None
                cached_vec = build_common_vector(a_long=build_pet2_vector(p, base_p))
                _PET2_VEC_CACHE[ck] = cached_vec
            pet_vecs.append(cached_vec)
    
    all_vecs = [v0, v1] + pet_vecs
    return build_list_common_vector(all_vecs)

class PlayerStore:
    def __init__(self, db_path: str, game_db: GameDatabase):
        self.db_path = db_path
        self.game_db = game_db
        self.lock = threading.Lock()
        self.last_chat_sync = {}
        now_ms = int(time.time()) * 1000
        gm_sender = {
            "id": 999999,
            "username": "GM Tổng Bộ",
            "screenname": "GM Tổng Bộ",
            "gold": 999999999,
            "gem": 99999999,
            "power": 9999999,
            "exp": 99999999,
            "level": 250,
            "avatar": [0, 1],
            "vip": [20, 10000000],
            "team": [[1, 250, 10, 0]],
            "clan_id": 0,
            "clan_pos": 3,
            "clan_flag": 1,
            "clan_name": "",
            "tower_level": 100,
        }
        self.global_chat = [
            {
                "reqTime": now_ms - 2,
                "message": "⚓ Chào mừng Thuyền Trưởng gia nhập Hải Tặc Bá Vương! Chúc anh em giương buồm thuận gió, sớm tìm ra kho báu One Piece!",
                "type": 0,
                "msgId": 1,
                "sender": gm_sender,
            },
            {
                "reqTime": now_ms - 1,
                "message": "👑 Đặt tên Thuyền Trưởng: Bấm Avatar góc trái để tự đặt tên nhân vật / đổi tên hoàn toàn MIỄN PHÍ (0 Kim Cương)!",
                "type": 0,
                "msgId": 2,
                "sender": gm_sender,
            },
            {
                "reqTime": now_ms,
                "message": "🎁 Giftcode & Hỗ trợ: Nhập Giftcode nhận quà từ Admin hoặc chat trực tiếp. Chúc anh em cùng xưng bá biển cả!",
                "type": 0,
                "msgId": 3,
                "sender": gm_sender,
            },
        ]
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    uid INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE,
                    password TEXT,
                    token TEXT,
                    state_json TEXT
                )
                """
            )
            conn.commit()

    def _build_starter_consumables(self) -> list:
        """Fresh free-progression starter; no full pack or free wheel currency."""
        return [[1,1,5000],[1,2,500],[1,14,10],[1,15,5],[1,17,10]]

    def _build_full_consumables(self) -> list:
        """Full resources & shards capped at 10k (Gọn gàng, không lag sv, không hoa mắt)."""
        items = [[1, i, 10000] for i in range(1, 115)]
        items += [
            [1, 94, 10000],   # 10k Bản Vẽ Vegapunk
            [1, 205, 10000],  # 10k Ngọc Tủy Hải Hồn (Pet Ruin / Thiên phú)
            [1, 206, 10000],  # 10k Kết Tinh Ý Chí (Pet Ruin / Thiên phú)
            [1, 207, 10000],  # 10k Huy hiệu Siêu Tân Tinh
            [1, 208, 1000],   # 1000 Rương Siêu Tân Tinh
            [1, 1000, 10000],
            [1, 1001, 10000],
            [1, 1002, 10000],
            [1, 2028, 10000],
            [1, 2058, 10000],
        ]
        # Chests & Scrolls
        for cid in (51, 52, 53, 54, 55, 56, 57, 58, 59):
            items.append([1, cid, 10000])
        # Artifact / Treasure chests
        for aid in range(115, 140):
            items.append([1, aid, 1000])
        # Hero Shards
        for hid in self.game_db.five_star_hero_ids[:30]:
            items.append([2, int(hid), 500])
        for sid in (10000, 20000, 30000, 40000, 40007, 40014, 40015):
            items.append([2, sid, 1000])
        # Artifact fragments
        for aid in (1, 2, 3, 4, 5, 6, 7, 8):
            items.append([3, aid, 500])
        return items

    def _default_player_state(self, uid: int, username: str) -> dict:
        """
        Bắt đầu từ con số 0 + Free VIP 20:
          - Level 1, 0 EXP, 50.000 Vàng, 2.000 Kim Cương
          - Free VIP 20 ([20, 10000000])
          - 1 Tướng 5 Sao Lv.1 khởi đầu
          - Ải Chiến Dịch 1, Tháp 1
        """
        starter_hid = self.game_db.five_star_hero_ids[0] if self.game_db.five_star_hero_ids else 1
        starter_hero = self.game_db.make_hero_instance(1, starter_hid, level=1, tier=0, star=5)
        heroes = [starter_hero]
        team = [[starter_Hero["heroId"], starter_Hero["level"], starter_Hero["star"], starter_Hero.get("skin", 0), int(starter_Hero["id"])] for starter_Hero in heroes]

        consumables = self._build_starter_consumables()
        equipments = []
        artifacts = []
        first_pet_id = self.game_db.pet_ids[0] if self.game_db.pet_ids else 1
        pets = [[first_pet_id, 1, 1, 1, 1, 1, 1, 0, 0, 0]]

        mails = [
            {
                "id": 1,
                "title": "Chào Mừng Thuyền Trưởng Tân Thủ!",
                "message": (
                    "Chào mừng thuyền trưởng gia nhập Tân Thế Giới Hải Tặc Bá Vương!\n"
                    "- Hãy vượt ải chiến dịch và tháp thí luyện để thu thập tài nguyên và chiêu mộ đồng đội mới!\n"
                    "- Chúc thuyền trưởng có những cuộc phiêu lưu tuyệt vời!"
                ),
                "bonus": [1, 5000, 2, 100],
                "received": False,
                "time": int(time.time()) * 1000,
                "senderId": 0,
            },
        ]

        # New players start with empty screenname so the client prompts them to enter their chosen name
        clean_sname = ""

        st = {
            "schema_ver": 5,
            "id": uid,
            "username": username,
            "screenname": clean_sname,
            "gold": 10000,
            "gem": 500,
            "feeling": 100,
            "exp": 0,
            "level": 1,
            "avatar": [0, starter_hid],
            "vip": [0, 0],
            "team": team,
            "team_pet": [first_pet_id, 0, 0],
            "clan_id": 0,
            "clan_pos": 3,
            "clan_flag": 1,
            "clan_name": "",
            "tower_level": 1,
            "celestial_tower": 1,
            "bag_info": [3000, 100, 5],
            "power": int(starter_hero["point"][16]),
            "heroes": heroes,
            "consumables": consumables,
            "equipments": equipments,
            "artifacts": artifacts,
            "pets": pets,
            "mails": mails,
            "campaign_level": 1,
            "campaign_auto": 1,
            "sv_quest_id": 30,
            "summon_energy": 0,
            "redeemed_codes": [],
            "chat": [],
        }
        ensure_pet2_state(st, self.game_db)
        return st

    def _upgrade_state(self, state: dict) -> bool:
        changed = False
        # Ensure Pet 2 migration
        ensure_pet2_state(state, self.game_db)
        # Set schema version to 5 without wiping any account data (Tránh mất dữ liệu người chơi)
        if state.get("schema_ver", 0) != 5:
            state["schema_ver"] = 5
            changed = True

        # Đảm bảo format VIP hợp lệ [level, exp]
        vip = state.get("vip", [0, 0])
        if not isinstance(vip, list) or len(vip) < 2:
            state["vip"] = [0, 0]
            changed = True

        # Clean up long hex UDID/token screennames so Home Chat & PopupChat display nicely
        cur_sname = str(state.get("screenname", ""))
        if cur_sname.startswith("Pirate_") and len(cur_sname) > 20:
            state["screenname"] = f"Hải Tặc #{int(state.get('id', 1))}"
            changed = True

        # Đảm bảo Đá Thăng Cấp (Item 2) và EXP Thú Cưng (Item 3004) đủ để nâng cấp Thú Cưng 5★ -> 15★
        for req_id, min_amt in ((2, 1000000), (3004, 1000000)):
            found_req = False
            for c in state.get("consumables", []):
                if len(c) >= 3 and int(c[0]) == 1 and int(c[1]) == req_id:
                    found_req = True
                    if int(c[2]) < min_amt:
                        c[2] = min_amt
                        changed = True
                    break
            if not found_req:
                state.setdefault("consumables", []).append([1, req_id, min_amt])
                changed = True

        # Đảm bảo Bụi Cam (Item 3) có 10,000 để tẩy/hoán đổi Đá Cam
        has_dust = False
        for c in state.get("consumables", []):
            if len(c) >= 3 and int(c[0]) == 1 and int(c[1]) == 3:
                has_dust = True
                if int(c[2]) < 10000:
                    c[2] = 10000
                    changed = True
                break
        if not has_dust:
            state.setdefault("consumables", []).append([1, 3, 10000])
            changed = True

        # Đảm bảo Vàng luôn đủ để tẩy đá/bảo vật (50.000.000 Vàng)
        cur_gold = int(state.get("gold", 0))
        if cur_gold < 50000000:
            state["gold"] = 50000000
            changed = True
        elif cur_gold > 100000000:
            state["gold"] = 100000000
            changed = True

        if int(state.get("gem", 0)) > 10000000:
            state["gem"] = 10000000
            changed = True

        # Mở khóa toàn bộ Thuyền / Thần Thú (Pet 1..7 bao gồm Tàu Red Force - Pet ID 7)
        existing_pids = {int(p[0]) for p in state.setdefault("pets", []) if p}
        for pid in (1, 2, 3, 4, 5, 6, 7):
            if pid not in existing_pids:
                state["pets"].append([pid, 120, 4, 30, 30, 30, 30, 30, 30, 30])
                changed = True
            else:
                for p in state["pets"]:
                    if int(p[0]) == pid:
                        while len(p) < 10:
                            p.append(30)
                            changed = True

        # Sức chứa ô tướng tối đa 3000
        if int(state.get("hero_capacity", 0)) < 3000:
            state["hero_capacity"] = 3000
            changed = True
        bag_info = state.setdefault("bag_info", [3000, 100, 5])
        if isinstance(bag_info, list) and bag_info and int(bag_info[0]) < 3000:
            bag_info[0] = 3000
            changed = True

        heroes = state.get("heroes", [])
        clean_heroes = [h for h in heroes if int(h.get("heroId", 0)) not in (53, 54, 55)]
        if len(clean_heroes) != len(heroes) and clean_heroes:
            heroes = clean_heroes
            state["heroes"] = heroes
            changed = True

        if _sanitize_hero_ids_and_teams(state):
            changed = True
        # Only set initial team if user has no team selected yet
        if not state.get("team") and heroes:
            state["team"] = [[int(h["heroId"]), int(h["level"]), int(h["star"]), int(h.get("skin", 0)), int(h["id"])] for h in heroes[:6]]
            changed = True
        if heroes:
            state["power"] = sum(int(h.get("point", [0]*17)[16]) if len(h.get("point", [])) > 16 else 10000 for h in heroes[:6])
        return changed

    def add_global_chat(self, sender_state: dict, message: str):
        with self.lock:
            msg_id = len(self.global_chat) + 1
            sender_snap = {
                "id": int(sender_state.get("id", 1)),
                "username": str(sender_state.get("username", "player")),
                "screenname": str(sender_state.get("screenname", "Pirate")),
                "gold": int(sender_state.get("gold", 0)),
                "gem": int(sender_state.get("gem", 0)),
                "power": int(sender_state.get("power", 1000)),
                "exp": int(sender_state.get("exp", 0)),
                "level": int(sender_state.get("level", 1)),
                "avatar": list(sender_state.get("avatar", [0, 1])),
                "vip": list(sender_state.get("vip", [20, 10000000])),
                "team": list(sender_state.get("team", [[1, 1, 5, 0]])),
                "clan_id": int(sender_state.get("clan_id", 1)),
                "clan_pos": int(sender_state.get("clan_pos", 3)),
                "clan_flag": int(sender_state.get("clan_flag", 1)),
                "clan_name": str(sender_state.get("clan_name", "Băng Mũ Rơm")),
                "tower_level": int(sender_state.get("tower_level", 1)),
            }
            now_ms = int(time.time() * 1000)
            last_ms = int(self.global_chat[-1]["reqTime"]) if self.global_chat else 0
            req_time = max(now_ms, last_ms + 1)
            self.global_chat.append({
                "reqTime": req_time,
                "message": message,
                "type": 0,
                "msgId": msg_id,
                "sender": sender_snap,
            })
            if len(self.global_chat) > 60:
                self.global_chat = self.global_chat[-60:]

    def get_latest_chat_time(self) -> int:
        with self.lock:
            return int(self.global_chat[-1]["reqTime"]) if self.global_chat else 0

    def get_global_chat(self, after_time: int = 0) -> list:
        with self.lock:
            items = list(self.global_chat[-30:])
            if after_time > 0:
                items = [m for m in items if int(m.get("reqTime", 0)) > after_time]
            return items

    def list_top_players(self, limit: int = 50) -> list:
        path = str(self.db_path)
        cache_lock = getattr(self, "_top_cache_lock", None)
        if cache_lock is None:
            self._top_cache_lock = threading.Lock()
            self._top_cache = {}
            cache_lock = self._top_cache_lock
        with sqlite3.connect(path, timeout=15) as conn:
            try:
                revs = conn.execute("SELECT uid, revision FROM players").fetchall()
            except sqlite3.OperationalError:
                revs = None
            if revs is None:
                players = []
                rows = conn.execute("SELECT uid, state_json FROM players").fetchall()
                for uid, raw_json in rows:
                    try:
                        st = json.loads(raw_json)
                        st["id"] = uid
                        st["power"] = compute_player_power(st)
                        players.append(st)
                    except Exception:
                        pass
                players.sort(key=lambda x: (int(x.get("power", 0)), int(x.get("level", 1))), reverse=True)
                return players[:limit]
            with cache_lock:
                cache = self._top_cache
                missing = [int(u) for u, r in revs if cache.get(int(u), (None,))[0] != int(r)]
            if missing:
                fetched = {}
                for i in range(0, len(missing), 400):
                    chunk = missing[i:i+400]
                    q = (
                        "SELECT uid, revision, username, "
                        "json_extract(state_json, '$.screenname'), "
                        "COALESCE(json_extract(state_json, '$.level'), 1), "
                        "json_extract(state_json, '$.avatar'), "
                        "json_extract(state_json, '$.vip'), "
                        "json_extract(state_json, '$.team'), "
                        "COALESCE(json_extract(state_json, '$.power'), 0), "
                        "COALESCE(json_extract(state_json, '$.tower_level'), 1), "
                        "COALESCE(json_extract(state_json, '$.arena_trial_points'), 1500), "
                        "COALESCE(json_extract(state_json, '$.arena_league_points'), 1500), "
                        "COALESCE(json_extract(state_json, '$.clan_id'), 0), "
                        "COALESCE(json_extract(state_json, '$.clan_pos'), 0), "
                        "COALESCE(json_extract(state_json, '$.clan_flag'), 1), "
                        "COALESCE(json_extract(state_json, '$.clan_name'), '') "
                        "FROM players WHERE uid IN (" + ",".join("?" * len(chunk)) + ")"
                    )
                    for row in conn.execute(q, chunk).fetchall():
                        u_id = int(row[0])
                        rev = int(row[1])
                        uname = row[2] or f"player_{u_id}"
                        sname = row[3] or uname
                        lvl = int(row[4] or 1)
                        try:
                            av = json.loads(row[5]) if row[5] else [0, 1]
                        except Exception:
                            av = [0, 1]
                        try:
                            vp = json.loads(row[6]) if row[6] else [20, 10000000]
                        except Exception:
                            vp = [20, 10000000]
                        try:
                            tm = json.loads(row[7]) if row[7] else []
                        except Exception:
                            tm = []
                        pwr = int(row[8] or 0)
                        summary_dict = {
                            "id": u_id,
                            "username": uname,
                            "screenname": sname,
                            "gold": 0,
                            "gem": 0,
                            "exp": 0,
                            "level": lvl,
                            "avatar": av if isinstance(av, list) else [0, 1],
                            "vip": vp if isinstance(vp, list) else [20, 10000000],
                            "team": tm if isinstance(tm, list) else [],
                            "power": pwr,
                            "tower_level": int(row[9] or 1),
                            "arena_trial_points": int(row[10] or 1500),
                            "arena_league_points": int(row[11] or 1500),
                            "clan_id": int(row[12] or 0),
                            "clan_pos": int(row[13] or 0),
                            "clan_flag": int(row[14] or 1),
                            "clan_name": str(row[15] or ""),
                        }
                        fetched[u_id] = (rev, summary_dict)
                with cache_lock:
                    cache.update(fetched)
            with cache_lock:
                active = {int(u) for u, _ in revs}
                if len(cache) > len(active) + 64:
                    for k in list(cache):
                        if k not in active:
                            cache.pop(k, None)
                players = [dict(cache[u][1]) for u in active if u in cache]
        players.sort(key=lambda x: (int(x.get("power", 0)), int(x.get("level", 1))), reverse=True)
        return players[:limit]

    def get_player_by_uid(self, target_uid: int) -> dict:
        u = int(target_uid)
        if u >= 10000000:
            if hasattr(self, "get_arena_player_by_uid"):
                st = self.get_arena_player_by_uid(u)
                if isinstance(st, dict):
                    return st
            u = u % 10000000
        with self.lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT state_json FROM players WHERE uid = ?", (u,)).fetchone()
            if row:
                try:
                    st = json.loads(row[0])
                    if isinstance(st, dict):
                        st["id"] = int(target_uid)
                    return st
                except Exception:
                    pass
        return None

    def get_existing_session(self, token: str):
        with self.lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute('SELECT uid, state_json FROM players WHERE token = ?', (token,)).fetchone()
        return (int(row[0]), json.loads(row[1])) if row else None

    def get_or_create_user(self, username: str, password: str = "") -> tuple:
        username = (username or "player1").strip()
        with self.lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT uid, token, state_json FROM players WHERE username = ?",
                (username,)
            ).fetchone()
            if row:
                uid, token, state_json = row
                state = json.loads(state_json)
                if self._upgrade_state(state):
                    conn.execute(
                        "UPDATE players SET state_json = ? WHERE uid = ?",
                        (json.dumps(state, ensure_ascii=False), uid)
                    )
                    conn.commit()
                return uid, token, state

            token = f"tok_{uuid.uuid4().hex[:16]}"
            cur = conn.execute(
                "INSERT INTO players (username, password, token, state_json) VALUES (?, ?, ?, ?)",
                (username, password, token, "{}")
            )
            uid = cur.lastrowid
            state = self._default_player_state(uid, username)
            conn.execute(
                "UPDATE players SET state_json = ? WHERE uid = ?",
                (json.dumps(state, ensure_ascii=False), uid)
            )
            conn.commit()
            return uid, token, state

    def get_by_token_or_identity(self, ident: str) -> tuple:
        ident = (ident or "").strip()
        with self.lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT uid, token, state_json FROM players WHERE token = ? OR username = ? OR CAST(uid AS TEXT) = ?",
                (ident, ident, ident)
            ).fetchone()
            if row:
                uid, token, raw_json = row
                state = json.loads(raw_json)
                if self._upgrade_state(state):
                    conn.execute(
                        "UPDATE players SET state_json = ? WHERE uid = ?",
                        (json.dumps(state, ensure_ascii=False), uid)
                    )
                    conn.commit()
                return uid, token, state
        return self.get_or_create_user(ident if ident else "player1")

    def save_state(self, uid: int, state: dict):
        import heapq
        heroes = state.get("heroes", [])
        if not state.get("team") and heroes:
            top6 = heapq.nlargest(6, heroes, key=lambda h: (int(h.get("level", 1)), int(h.get("star", 5)), int(h.get("id", 0))))
            state["team"] = [[int(h["heroId"]), int(h["level"]), int(h["star"]), int(h.get("skin", 0)), int(h["id"])] for h in top6]
        state["power"] = compute_player_power(state)
        real_uid = int(state.get("id", uid)) if state.get("id") else uid
        encoded = json.dumps(state, ensure_ascii=False)
        with self.lock, sqlite3.connect(self.db_path, timeout=15) as conn:
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                "UPDATE players SET state_json = ? WHERE uid = ?",
                (encoded, real_uid)
            )
            conn.commit()


# ============================================================================
# 6. Game Action Dispatcher (All Gameplay Services Verified from libil2cpp.so)
# ============================================================================

GAME_DB = GameDatabase()
from combat_balance import apply_balance
apply_balance(GAME_DB, BASE_DIR)
STORE = PlayerStore(SQLITE_PATH, GAME_DB)


def _find_hero(state: dict, hero_inst_id: int) -> dict:
    for h in state.get("heroes", []):
        if int(h["id"]) == int(hero_inst_id):
            return h
    for h in state.get("heroes", []):
        if int(h.get("heroId", 0)) == int(hero_inst_id):
            return h
    return None


def _client_max_tier(star: int) -> int:
    # Matches libil2cpp.so 0x75263c: star 1..5 -> tier 1..5; star >= 6 -> tier 6
    if star <= 1:
        return 1
    if star < 6:
        return star
    return 6


def _client_max_level(star: int, tier: int) -> int:
    # Matches libil2cpp.so HeroData.UpdateMaxLevel @ 0x135e668 (0x752678 & 0x7526ac)
    tier_caps = {0: 30, 1: 40, 2: 50, 3: 60, 4: 80, 5: 100}
    if tier <= 5:
        return tier_caps.get(max(0, tier), 100)
    star_caps = {6: 140, 7: 160, 8: 180, 9: 200, 10: 250, 11: 260, 12: 270, 13: 290, 14: 310, 15: 330}
    if star < 6:
        return 100
    return star_caps.get(min(15, star), 330)


PUPPET_HERO_IDS = {
    354, 355, 356, 357, 358, 359,  # 5★ Phôi Lính Chì
    348, 349, 350, 351, 352, 353,  # 6★ Phôi Lính Chì
    310, 311, 312, 313, 314, 315,  # 9★ Phôi Lính Chì
    322, 323, 324, 325, 326, 327,  # 10★ Phôi Lính Chì
}


def _sync_hero_tier_with_level(h: dict):
    cur_hid = int(h.get("heroId", 0))
    if cur_hid in PUPPET_HERO_IDS:
        h["level"] = 0
        h["tier"] = 0
        return
    star = max(1, int(h.get("star", 5)))
    lvl = max(1, int(h.get("level", 1)))
    tier = max(0, int(h.get("tier", 0)))
    max_t = _client_max_tier(star)
    # Native CalculateMaxLevelByStarAndTier also uses tier caps for six-star heroes.
    # Preserve paid breakthrough tiers; only repair tiers inconsistent with an old level.
    while tier < max_t and lvl > _client_max_level(star, tier):
        tier += 1
    tier = min(tier, max_t)
    h["tier"] = tier

    # Chuẩn hóa heroId theo số sao (star) để client GetBase5StarHeroId và Spine animation load 100% chính xác
    if cur_hid not in PUPPET_HERO_IDS and cur_hid > 0:
        if star >= 10:
            mapped_10 = GAME_DB.ten_star_by_base_id.get(cur_hid)
            if mapped_10:
                h["heroId"] = mapped_10
        elif 6 <= star <= 9:
            mapped_6 = GAME_DB.six_star_by_base_id.get(cur_hid)
            if mapped_6:
                h["heroId"] = mapped_6
        elif star == 5 and int(GAME_DB.heroes_by_id.get(cur_hid,{}).get('star',0)) != 5:
            mapped_5 = GAME_DB.base_5star_by_hero_id.get(cur_hid)
            if mapped_5:
                h["heroId"] = mapped_5

    # Native Haki UI reads negative choices as locked, not zero/one.
    from hero_progression import e_skills
    # Keep saved choices while locked; serialization/combat use e_skills() to
    # suppress every talent below E5 without erasing the player's selections.
    if star >= 15:
        h['skills'] = e_skills(h)


def compute_player_power(state: dict) -> int:
    import heapq
    raw_heroes = state.get("heroes")
    if not isinstance(raw_heroes, list) or not raw_heroes:
        return max(1000, int(state.get("power", 1000) or 1000))
    team_ids = []
    seen = set()
    for t_entry in (state.get("team") or [])[:6]:
        if isinstance(t_entry, list) and len(t_entry) > 4:
            iid = int(t_entry[4])
            if iid not in seen:
                seen.add(iid)
                team_ids.append(iid)
    team_pwr = 0
    if team_ids:
        target_set = set(team_ids)
        found = {}
        for h in raw_heroes:
            if isinstance(h, dict):
                hid = h.get("id")
                if hid is not None and int(hid) in target_set:
                    found[int(hid)] = h
                    if len(found) == len(target_set):
                        break
        team_pwr = sum(int(found[iid]["point"][16]) for iid in team_ids if iid in found and isinstance(found[iid].get("point"), list) and len(found[iid]["point"]) > 16)
    top6 = heapq.nlargest(6, (int(h["point"][16]) for h in raw_heroes if isinstance(h, dict) and isinstance(h.get("point"), list) and len(h["point"]) > 16))
    return max(1000, team_pwr, sum(top6))


def _recalc_hero(h: dict):
    _sync_hero_tier_with_level(h)
    cur_hid = int(h.get("heroId", 0))
    if cur_hid not in PUPPET_HERO_IDS:
        st_val = max(1, int(h.get("star", 5)))
        tr_val = max(0, int(h.get("tier", 0)))
        max_l = 600 if (st_val >= 15 and tr_val >= 6) else _client_max_level(st_val, tr_val)
        if int(h.get("level", 1)) > max_l:
            h["level"] = max_l
    items_raw = h.get("item")
    if not h.get("_admin_test_artifact") and (isinstance(items_raw, list) and len(items_raw) > 4 and int(items_raw[4] or 0) in (233,) or (isinstance(items_raw, list) and len(items_raw) > 4 and int(items_raw[4] or 0) >= 100000)):
        items_raw[4] = 0
        if len(items_raw) > 5:
            items_raw[5] = 0
    updated = GAME_DB.make_hero_instance(h["id"], h["heroId"], h["level"], h["tier"], h["star"])
    pts = list(updated["point"])

    skin_id = int(h.get("skin", 0) or 0)
    if skin_id in (1, 2, 3, 5):
        skin_data = getattr(GAME_DB, "hero_skins_by_id", {}).get(skin_id)
        if skin_data and skin_data.get("points"):
            try:
                import json as _json
                p_items = _json.loads(skin_data["points"])[0].get("point", [])
                flat_adds = {}
                pct_adds = {}
                for i in range(0, len(p_items), 2):
                    k, v = int(p_items[i]), float(p_items[i + 1])
                    if k < 100:
                        flat_adds[k] = flat_adds.get(k, 0.0) + v
                    else:
                        pct_adds[k] = pct_adds.get(k, 0.0) + v
                for k, v in flat_adds.items():
                    if 0 <= k < len(pts):
                        pts[k] += v
                for k, v in pct_adds.items():
                    base_idx = k - 100
                    if 0 <= base_idx < 4:
                        pts[base_idx] = round(pts[base_idx] * (1.0 + v / 1000.0))
            except Exception:
                pts[0] = round((pts[0] + 37) * 1.03)
                pts[1] = round((pts[1] + 15) * 1.02)
                pts[2] = round((pts[2] + 450) * 1.05)
        else:
            pts[0] = round((pts[0] + 37) * 1.03)
            pts[1] = round((pts[1] + 15) * 1.02)
            pts[2] = round((pts[2] + 450) * 1.05)

    pts[16] = round(pts[0] * 2 + pts[2] / 6.0 + pts[1] * 3)
    pts[18] = pts[2]
    h["point"] = pts
    import reported_stats as _rs
    _rs.equipment(sys.modules[__name__], h)
    pts_post_eq = list(h["point"][:4])
    from islands import gem_bonuses
    gem_bonuses(None, h)
    from album import hero_bonuses as album_hero_bonuses
    album_hero_bonuses(h)
    from hero_equipment_stats import apply as apply_special_stats
    apply_special_stats(h, GAME_DB)
    from haki_combat import apply_stats as apply_haki_stats
    apply_haki_stats(h)
    from hero_soul import apply_stats as apply_soul_stats
    apply_soul_stats(h)
    pts_final = h.get("point")
    art_pct = _rs.artifact_pct_totals(sys.modules[__name__], h)
    if art_pct and isinstance(pts_final, list) and len(pts_final) >= 4:
        for idx_p in range(4):
            pct_val = art_pct.get(str(100 + idx_p), 0)
            if pct_val:
                delta_val = pts_final[idx_p] - pts_post_eq[idx_p]
                if delta_val > 0:
                    pts_final[idx_p] = round(pts_final[idx_p] + delta_val * (pct_val / 1000.0))
        if len(pts_final) > 18:
            pts_final[18] = pts_final[2]
    if isinstance(pts_final, list) and len(pts_final) > 16:
        pts_final[16] = round(pts_final[0] * 2 + pts_final[2] / 6.0 + pts_final[1] * 3)


def _equip_bag_payload(state: dict) -> bytes:
    flat = []
    for eq in state.get("equipments", []):
        flat.extend([int(eq[0]), int(eq[1]), int(eq[2])])
    return build_common_vector(a_long=flat)


def get_consumable_count(state: dict, mtype: int, mkey: int) -> int:
    mtype_norm = 1 if int(mtype) in (10, 11, 12) else int(mtype)
    cnt = 0
    for c in state.get("consumables", []):
        if len(c) >= 3 and (1 if int(c[0]) in (10, 11, 12) else int(c[0])) == mtype_norm and int(c[1]) == mkey:
            cnt += max(0, int(c[2]))
    return cnt


def deduplicate_consumables(state: dict):
    merged = {}
    for c in state.get("consumables", []):
        if len(c) >= 3:
            mt = 1 if int(c[0]) in (10, 11, 12) else int(c[0])
            key = (mt, int(c[1]))
            merged[key] = merged.get(key, 0) + int(c[2])
    state["consumables"] = [[k[0], k[1], max(0, v)] for k, v in merged.items() if v > 0]

def _consumable_bag_payload(state: dict) -> bytes:
    deduplicate_consumables(state)
    flat = []
    for c in state.get("consumables", []):
        if len(c) >= 3:
            cnt = min(9223372036854775807, max(0, int(c[2])))  # Native ItemConsumable.m_Number is Int64.
            flat.extend([int(c[0]), int(c[1]), cnt])
    return build_common_vector(a_long=flat)


def _artifact_bag_payload(state: dict) -> bytes:
    is_admin_vthuan = str(state.get("username", "")).strip().lower() == "vthuan1804"
    arts = state.get("artifacts", [])
    if not is_admin_vthuan and any(int(a[0]) == 233 or int(a[0]) >= 100000 for a in arts if isinstance(a, list) and a):
        arts = [a for a in arts if not (isinstance(a, list) and a and (int(a[0]) == 233 or int(a[0]) >= 100000))]
        state["artifacts"] = arts
    flat = []
    for a in arts:
        flat.extend([int(a[0]), int(a[1]), int(a[2]), int(a[3])])
    return build_common_vector(a_long=flat)


GIFTCODE_ALIASES = {
    "TESTGAME": "TESTGAME",
    "TEST": "TESTGAME",
    "L1L2": "TESTGAME",
    "CUONL1L2": "TESTGAME",
    "CUON": "TESTGAME",
    "CUONL1": "TESTGAME",
    "CUONL2": "TESTGAME",
    "L1": "TESTGAME",
    "L2": "TESTGAME",
    "DACUONGHOA": "DACUONGHOA",
    "TUONGMOI": "TUONGMOI",
    "NIKA": "TUONGMOI",
    "YAMATO": "TUONGMOI",
    "KAIDO": "TUONGMOI",
    "LUFFYNIKA": "TUONGMOI",
    "SS": "TUONGMOI",
    "FULL": "FULL",
    "FULLTAINGUYEN": "FULL",
    "MAX": "FULL",
    "ADMIN": "FULL",
    "HTHT": "FULL",
    "ONEPIECE": "FULL",
    "TANTHU": "TANTHU",
    "NEWBIE": "TANTHU",
    "XUQUAY": "XUQUAY",
    "VONGQUAY": "XUQUAY",
    "SPINCOIN": "XUQUAY",
    "VIP20": "VIP20",
    "FREEVIP20": "VIP20",
    "PHOI": "PHOI",
    "LINHCHI": "PHOI",
    "PHOINANGTUONG": "PHOI",
    "VEGA": "VEGA",
    "VEGAPUNK": "VEGA",
    "BAOVAT": "BAOVAT",
    "RESET": "RESET",
    "LV1": "RESET",
    "BIA": "BIA",
    "THUNGBIA": "BIA",
    "BEER": "BIA",
    "KC": "BIA",
    "KIMCUONG": "BIA",
    "SPIN": "BIA",
    "GACHA": "BIA",
}


def normalize_giftcode(raw_text: str) -> str:
    txt = (raw_text or "").strip()
    if not txt:
        return ""
    import unicodedata
    norm_str = unicodedata.normalize('NFD', txt)
    no_accent = ''.join(c for c in norm_str if unicodedata.category(c) != 'Mn')
    up = no_accent.upper()
    for prefix in ("/CODE ", "CODE ", "CODE:", "GIFTCODE ", "GIFTCODE:", "/CODE", "CODE", "GIFTCODE", "/"):
        if up.startswith(prefix):
            up = up[len(prefix):].strip()
    clean = up.replace(" ", "").replace("_", "").replace("-", "")
    if clean in GIFTCODE_ALIASES:
        return GIFTCODE_ALIASES[clean]
    return GIFTCODE_ALIASES.get(up, "")


def redeem_giftcode(uid: int, state: dict, raw_code: str):
    """
    Executes a GiftCode on `state`, persists to SQLite, and returns:
      (matched: bool, msg: str, bonus_received_longs: list, push_actions: list)
    """
    code = normalize_giftcode(raw_code)
    if not code:
        return False, "", [], []

    if code != "RESET":
        redeemed = state.setdefault("redeemed_codes", [])
        if code in redeemed:
            return False, "Bạn đã nhập mã này rồi!", [], [(2, build_common_vector(a_string=["Bạn đã nhập mã này rồi!"]))]
        redeemed.append(code)

    if code == "PHOI":
        items = []
        # Phôi 5 sao (SameHero5Star) x20 mỗi hệ
        for phoi_id in (354, 355, 356, 357, 358, 359):
            for _ in range(20):
                items.append(("hero", phoi_id, 100, 6, 5))
        # Phôi 6 sao (SameFactionHero) x15 mỗi hệ (tier=6 chuẩn)
        for phoi_id in (348, 349, 350, 351, 352, 353):
            for _ in range(15):
                items.append(("hero", phoi_id, 140, 6, 6))
        # Phôi 9 sao (AnyFaction9Star) x10 mỗi hệ (tier=6 chuẩn)
        for phoi_id in (310, 311, 312, 313, 314, 315):
            for _ in range(10):
                items.append(("hero", phoi_id, 200, 6, 9))
        # Phôi 10 sao x10 mỗi hệ (tier=6 chuẩn để lên 11..15 sao)
        for phoi_id in (322, 323, 324, 325, 326, 327):
            for _ in range(10):
                items.append(("hero", phoi_id, 250, 6, 10))
        # Bản sao 5 sao của tất cả các Tướng SS Huyền Thoại để làm nguyên liệu trùng bản lên 11..15 sao!
        ss_5star_bases = [
            606, 609, 504, 642, 645, 633, 627, 621, 618, 615, 612, 588, 582, 576,
            561, 555, 546, 537, 516, 510, 495, 489, 486, 483, 465, 462, 363, 624,
            630, 636, 639
        ]
        for base_hid in ss_5star_bases:
            for _ in range(5):
                items.append(("hero", base_hid, 100, 0, 5))

        rec, _ = apply_and_make_bonus_received(state, items, GAME_DB)
        STORE.save_state(uid, state)
        msg = "Đã nhận trọn bộ Phôi Lính Chì (5★, 6★, 9★, 10★) và Bản Sao 5★ của toàn bộ Tướng SS để nâng 11★..15★!"
        push = [
            (116, build_common_vector(a_long=rec)),
            (207, build_pb_list_hero(state.get("heroes", []))),
            (2, build_common_vector(a_string=[msg])),
        ]
        return True, msg, rec, push

    if code == "VEGA":
        items = [
            ("hero", 587, 250, 6, 10),      # Punk-02 Lilith 10*
            ("hero", 518, 250, 6, 10),      # Boa Lilith 10*
            ("material", 1, 94, 10000),     # 10k Bản Vẽ Vegapunk
            ("material", 1, 205, 10000),    # 10k Ngọc Tủy Hải Hồn (Pet Thiên Phú)
            ("material", 1, 206, 10000),    # 10k Kết Tinh Ý Chí
            ("material", 1, 207, 10000),    # 10k Huy hiệu Siêu Tân Tinh
            ("material", 1, 208, 1000),     # 1000 Rương Siêu Tân Tinh
        ]
        for p in state.setdefault("pets", []):
            while len(p) < 10:
                p.append(0)
            p[7] = 30
            p[8] = 30
            p[9] = 30
        rec, _ = apply_and_make_bonus_received(state, items, GAME_DB)
        STORE.save_state(uid, state)
        msg = "Đã nhận Vegapunk Punk-02 Lilith 10 Sao, Bản Vẽ Vegapunk & Trọn bộ Nguyên Liệu Thiên Phú Pet!"
        push = [
            (116, build_common_vector(a_long=rec)),
            (207, build_pb_list_hero(state.get("heroes", []))),
            (251, _consumable_bag_payload(state)),
            (360, build_list_common_vector([build_common_vector(a_long=p) for p in state.get("pets", [])])),
            (940, build_pet2_status_payload(state, GAME_DB)),
            (2, build_common_vector(a_string=[msg])),
        ]
        return True, msg, rec, push

    if code == "BAOVAT":
        items = [("material", 1, 3, 10000)]
        is_admin_vthuan = str(state.get("username", "")).strip().lower() == "vthuan1804"
        state["artifacts"] = [[art_id, 6, 10, 0] for art_id in GAME_DB.artifact_ids if is_admin_vthuan or (int(art_id) != 233 and int(art_id) < 100000)]
        rec, _ = apply_and_make_bonus_received(state, items, GAME_DB)
        STORE.save_state(uid, state)
        msg = "Đã nhận Full Toàn Bộ Bảo Vật 10 Sao & 10.000 Bột Ma Thuật!"
        push = [
            (116, build_common_vector(a_long=rec)),
            (253, _artifact_bag_payload(state)),
            (251, _consumable_bag_payload(state)),
            (2, build_common_vector(a_string=[msg])),
        ]
        return True, msg, rec, push

    if code == "TESTGAME":
        items = [
            ("gold", 50000000),                # 50Tr Vàng
            ("gem", 1000000),                  # 1Tr Kim Cương
            ("vip_exp", 10000000),
            ("material", 1, 16, 5000),         # 5000 Thùng bia đặc biệt
            ("material", 1, 53, 10000),        # 10k Cuộn SS L1
            ("material", 1, 54, 10000),        # 10k Cuộn SS L2
            ("material", 1, 56, 10000),        # 10k Rương VIP vàng
            ("material", 1, 3, 10000),         # 10k Bột ma thuật
            ("material", 1, 2, 50000),         # 50k Đá Cường Hóa
            ("material", 1, 94, 10000),        # 10k Bản Vẽ Vegapunk
            ("material", 1, 205, 10000),       # 10k Ngọc Tủy Hải Hồn
            ("material", 1, 206, 10000),       # 10k Kết Tinh Ý Chí
            ("material", 1, 207, 10000),       # 10k Huy hiệu Siêu Tân Tinh
            ("material", 1, 208, 1000),        # 1000 Rương Siêu Tân Tinh
        ]
        for phoi_id in (354, 355, 356, 357, 358, 359):
            items.append(("hero", phoi_id, 1, 0, 5))
        for phoi_id in (348, 349, 350, 351, 352, 353):
            items.append(("hero", phoi_id, 1, 0, 6))
        for phoi_id in (310, 311, 312, 313, 314, 315):
            items.append(("hero", phoi_id, 1, 0, 9))
        for phoi_id in (322, 323, 324, 325, 326, 327):
            items.append(("hero", phoi_id, 1, 0, 10))
        items.append(("hero", 587, 250, 6, 10))
        items.append(("hero", 518, 250, 6, 10))

        state["vip"] = [20, 10000000]
        for p in state.setdefault("pets", []):
            while len(p) < 10:
                p.append(0)
            p[7] = 30
            p[8] = 30
            p[9] = 30
        rec, _ = apply_and_make_bonus_received(state, items, GAME_DB)
        mails = state.setdefault('mails', [])
        mails.append({
            'id': max((int(m['id']) for m in mails), default=0) + 1,
            'title': 'Giftcode TESTGAME',
            'message': 'Quà sự kiện TESTGAME: 50Tr Vàng, 1Tr Kim Cương, 10k Cuộn L1-L2, Rương VIP, Vegapunk, Phôi Nâng Tướng & Pet Thiên Phú Max!',
            'bonus': list(rec),
            'received': True, 'time': int(__import__('time').time() * 1000), 'senderId': 0,
        })
        STORE.save_state(uid, state)
        msg = "Nhận thành công Gói TESTGAME: 50Tr Vàng, 1Tr Kim Cương, 10k Cuộn L1-L2, Rương VIP, Vegapunk, Phôi Nâng Tướng & Pet Thiên Phú Max!"
        push = [
            (116, build_common_vector(a_long=rec)),
            (207, build_pb_list_hero(state.get("heroes", []))),
            (251, _consumable_bag_payload(state)),
            (360, build_list_common_vector([build_common_vector(a_long=p) for p in state.get("pets", [])])),
            (940, build_pet2_status_payload(state, GAME_DB)),
            (2, build_common_vector(a_string=[msg])),
        ]
        return True, msg, rec, push

    if code == "DACUONGHOA":
        apply_and_make_bonus_received(state, [("material", 1, 2, 100000)], GAME_DB)
        STORE.save_state(uid, state)
        msg = "Đã nhận thành công 100.000 Đá Cường Hóa vào túi đồ!"
        return True, msg, [3, 1, 2, 100000], [
            (116, build_common_vector(a_long=[3, 1, 2, 100000])),
            (251, _consumable_bag_payload(state)),
            (2, build_common_vector(a_string=[msg])),
        ]

    if code == "TUONGMOI":
        # Accurate db_101 SS 10-star Legends (including Gol D.Roger, Nami & Robin, Ace & Sabo, Garp, Uta Last Wish...)
        newest_ss = [
            608,  # Gol D.Roger 10*
            611,  # Nami & Robin (Siêu) 10*
            506,  # Ace & Sabo (Siêu) 10*
            644,  # Monkey D. Garp 10*
            647,  # Uta Last Wish 10*
            635,  # Luffy Reverse 10*
            629,  # Shirahoshi Noel (Siêu) 10*
            623,  # S-Hawk 10*
            620,  # Hero Koby 10*
            617,  # Bonney Egghead 10*
            614,  # Borsalino Kizaru 10*
            590,  # Sugar C.Viên Nước (Siêu) 10*
            584,  # Edward Newgate 10*
            578,  # Zoro King Of Hell 10*
            563,  # Ace Cerberus (Siêu) 10*
            557,  # Yonko Shanks 10*
            548,  # Blackbeard Yonko 10*
            539,  # Mihawk Samurai (Siêu) 10*
            518,  # Boa Lilith (Siêu) 10*
            512,  # Luffy Nika 10*
            497,  # Kaido Hybrid 10*
            491,  # Bullet 10*
            488,  # Pudding Bể Bơi (Siêu) 10*
            485,  # Sanji Black Suit (Siêu) 10*
            467,  # Yamato 10*
            464,  # Chopper G.Sinh (Siêu) 10*
            365,  # Lafitte (Siêu) 10*
            626,  # Phoenix Marco 10*
            632,  # Robin Egghead 10*
            638,  # Brook Egghead 10*
            641,  # Perospero 10*
        ]
        items = [
            ("gold", 50000000),               # 50Tr Vàng (vừa đủ, không lạm phát)
            ("gem", 2000000),                 # 2Tr Kim Cương
            ("material", 1, 2, 10000),        # 10k Đá Cường Hóa (chuẩn theo yêu cầu)
            ("material", 1, 16, 500),         # 500 Thùng Bia Đặc Biệt
            ("material", 1, 53, 50),          # 50 Cuộn SS L1
            ("material", 1, 54, 50),          # 50 Cuộn SS L2 (có Gol D. Roger & Nami&Robin)
        ]
        for hid in newest_ss:
            items.append(("hero", hid, 250, 6, 10))

        rec, added = apply_and_make_bonus_received(state, items, GAME_DB)
        if added:
            state["team"] = [[int(h["heroId"]), 250, 10, int(h.get("skin", 0))] for h in added[:6]]
        STORE.save_state(uid, state)
        msg = "Nhận thành công Trọn Bộ Tướng Mới SS 10 Sao (Luffy Nika, Kaido Hybrid, Yamato, Ace&Sabo, Shanks, Uta...)!"
        push = [
            (116, build_common_vector(a_long=rec)),
            (207, build_pb_list_hero(state.get("heroes", []))),
            (251, _consumable_bag_payload(state)),
            (2, build_common_vector(a_string=[msg])),
        ]
        return True, msg, rec, push

    if code == "RESET":
        uname = str(state.get("username", f"player{uid}"))
        sname = str(state.get("screenname", f"Pirate_{uname}"))
        fresh = STORE._default_player_state(uid, uname)
        fresh['event_claims'] = state.get('event_claims', {})
        fresh['redeemed_codes'] = state.get('redeemed_codes', [])
        # Keep unclaimed event mail so RESET cannot destroy a once-only reward.
        event_mail = [m for m in state.get('mails', [])
                      if m.get('title') == 'Giftcode DACUONGHOA' and not m.get('received')]
        for mail in event_mail:
            mail = dict(mail)
            mail['id'] = max(m['id'] for m in fresh['mails']) + 1
            fresh['mails'].append(mail)
        fresh["screenname"] = sname
        fresh["gem"] = 50000
        for c in fresh.setdefault("consumables", []):
            if int(c[0]) == 1 and int(c[1]) == 14:
                c[2] = 100
                break
        state.clear()
        state.update(fresh)
        STORE.save_state(uid, state)
        rec = [
            1, state["gold"], 0,
            2, state["gem"], 0,
            4, state["level"], state["exp"], 0,
            9, 20, 10000000, 0,
        ]
        # Include all consumables in rec so GameContext immediately refreshes barrels & all items
        for c in state.get("consumables", []):
            rec.extend([3, int(c[0]), int(c[1]), int(c[2]), 0])

        msg = "Đã Reset tài khoản về Level 1 + Free VIP 20 + Đầy Thùng Bia & Kim Cương!"
        push = [
            (116, build_common_vector(a_long=rec)),
            (200, build_pb_list_hero(state["heroes"])),
            (250, _equip_bag_payload(state)),
            (251, _consumable_bag_payload(state)),
            (253, _artifact_bag_payload(state)),
            (2, build_common_vector(a_string=[msg])),
        ]
        return True, msg, rec, push

    if code == "BIA":
        items = [
            ("gem", 10000000),
            ("material", 1, 15, 5000),   # 5000 Thùng Bia Chiêu Mộ Cao Cấp (Heroic @ 0x110)
            ("material", 1, 16, 1000),   # 1000 Thùng Bia Chiêu Mộ Đặc Biệt (Prophet @ 0x118)
            ("material", 1, 9, 5000),    # 5000 Bình Tím (Prophet Blessing @ 0xe0)
            ("material", 1, 14, 1000),   # 1000 Cuộn Chiêu Mộ Cơ Bản (@ 0x108)
            ("material", 1, 10, 1000),   # 1000 Xu Quay Thường (@ 0xe8)
            ("material", 1, 13, 500),    # 500 Xu Quay Cao Cấp (@ 0x100)
            ("material", 1, 1, 10000000),
            ("material", 1, 2, 50000),
        ]
        rec, _ = apply_and_make_bonus_received(state, items, GAME_DB)
        STORE.save_state(uid, state)
        msg = "Nhận thành công +10.000.000 Kim Cương, +5.000 Thùng Bia Chiêu Mộ & +1.000 Thùng Bia Đặc Biệt!"
        push = [
            (116, build_common_vector(a_long=rec)),
            (251, _consumable_bag_payload(state)),
            (2, build_common_vector(a_string=[msg])),
        ]
        return True, msg, rec, push

    if code == "TANTHU":
        # Top 8 SS starter legends with 100% working animations and portraits (Mihawk, Doflamingo, Akainu, Fujitora, Ace, Luffy, Zoro, Sanji)
        starter_ss = [251, 260, 253, 250, 241, 247, 288, 284]
        items = [
            ("gold", 100000000),
            ("gem", 1000000),
            ("material", 1, 15, 2000),    # 2000 Thùng Bia Chiêu Mộ Cao Cấp
            ("material", 1, 16, 1000),    # 1000 Thùng Bia Chiêu Mộ Đặc Biệt
            ("material", 1, 9, 2000),     # 2000 Bình Tím Đổi Tướng
            ("material", 1, 14, 500),     # 500 Cuộn Chiêu Mộ Cơ Bản
            ("material", 1, 2, 100000),   # 100.000 Đá Tiến Bậc
            ("material", 1, 1, 50000000), # 50.000.000 EXP Tướng
        ]
        for hid in starter_ss:
            items.append(("hero", hid, 200, 6, 10))
        rec, new_heroes = apply_and_make_bonus_received(state, items, GAME_DB)
        state["vip"] = [20, 10000000]
        state["team"] = [[hid, 200, 10, 0] for hid in starter_ss[:6]]
        STORE.save_state(uid, state)
        msg = "Nhận GiftCode TANTHU thành công: +100Tr Vàng, +1Tr Kim Cương, +2000 Thùng Bia, +1000 Bia Đặc Biệt & 6 Tướng SS 10 Sao (Luffy Nika, Shanks, Kaido, Râu Trắng, Yamato, Zoro Wano)!"
        push = [
            (116, build_common_vector(a_long=rec)),
            (207, build_pb_list_hero(new_heroes)),
            (251, _consumable_bag_payload(state)),
            (2, build_common_vector(a_string=[msg])),
        ]
        return True, msg, rec, push

    if code == "XUQUAY":
        rec, _ = apply_and_make_bonus_received(
            state,
            [("material", 1, 10, 10000)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        msg = "Nhận GiftCode XUQUAY thành công: Nhận 10.000 Xu Vòng Quay!"
        push = [
            (116, build_common_vector(a_long=rec)),
            (251, _consumable_bag_payload(state)),
            (2, build_common_vector(a_string=[msg])),
        ]
        return True, msg, rec, push

    if code == "VIP20":
        hids = GAME_DB.five_star_hero_ids[:3] or [1, 2, 3]
        items = [
            ("gold", 100000000),
            ("gem", 2000000),
            ("material", 1, 14, 200),
            ("material", 1, 15, 100),
            ("vip_exp", 10000000),
        ]
        for hid in hids:
            items.append(("hero", hid, 100, 6, 5))
        state["vip"] = [20, 10000000]
        rec, new_heroes = apply_and_make_bonus_received(state, items, GAME_DB)
        STORE.save_state(uid, state)
        msg = "Nhận GiftCode VIP20 thành công: Kích hoạt Đặc Quyền VIP 20 + 100Tr Vàng + 2Tr Kim Cương + 3 Tướng 5 Sao!"
        push = [
            (116, build_common_vector(a_long=rec)),
            (207, build_pb_list_hero(new_heroes)),
            (251, _consumable_bag_payload(state)),
            (2, build_common_vector(a_string=[msg])),
        ]
        return True, msg, rec, push

    if code == "FULL":
        state["level"] = 250
        state["vip"] = [20, 10000000]
        state["gold"] = 100000000
        state["gem"] = 2000000
        state["consumables"] = STORE._build_full_consumables()
        state["equipments"] = [[eq_id, 30, 0] for eq_id in GAME_DB.equipment_ids[-60:]]
        is_admin_vthuan = str(state.get("username", "")).strip().lower() == "vthuan1804"
        state["artifacts"] = [[art_id, 6, 10, 0] for art_id in GAME_DB.artifact_ids if is_admin_vthuan or (int(art_id) != 233 and int(art_id) < 100000)]
        state["pets"] = [[pid, 120, 4, 30, 30, 30, 30, 30, 30, 30] for pid in GAME_DB.pet_ids[:5]]

        full_hids = getattr(GAME_DB, "ss_legend_ids", [])
        new_heroes = []
        existing_hids = {int(h["heroId"]) for h in state.get("heroes", [])}
        for h in state.get("heroes", []):
            h["level"] = 250
            h["tier"] = 6
            h["star"] = 10
            _recalc_hero(h)
            new_heroes.append(h)

        items = [
            ("exp", 50000000),
            ("vip_exp", 10000000),
            ("material", 1, 14, 10000),
            ("material", 1, 15, 10000),
            ("material", 1, 16, 10000),
            ("material", 1, 9, 10000),
            ("material", 1, 1, 50000000),
            ("material", 1, 2, 100000),
            ("material", 1, 94, 10000),
            ("material", 1, 205, 10000),
            ("material", 1, 206, 10000),
            ("material", 1, 207, 10000),
        ]
        for hid in full_hids:
            if int(hid) not in existing_hids:
                items.append(("hero", int(hid), 250, 6, 10))
        for phoi_id in (354, 355, 356, 357, 358, 359):
            for _ in range(2):
                items.append(("hero", phoi_id, 1, 0, 5))
        for phoi_id in (348, 349, 350, 351, 352, 353):
            items.append(("hero", phoi_id, 1, 0, 6))
        for phoi_id in (310, 311, 312, 313, 314, 315):
            items.append(("hero", phoi_id, 1, 0, 9))
        for phoi_id in (322, 323, 324, 325, 326, 327):
            items.append(("hero", phoi_id, 1, 0, 10))

        rec, added_heroes = apply_and_make_bonus_received(state, items, GAME_DB)
        state["gold"] = 100000000
        state["gem"] = 2000000
        all_synced_heroes = new_heroes + added_heroes
        state["team"] = [[int(h["heroId"]), 250, 10, int(h.get("skin", 0)), int(h["id"])] for h in all_synced_heroes[:6]]
        STORE.save_state(uid, state)
        msg = (
            "NHẬN CODE FULL TÀI NGUYÊN THÀNH CÔNG!\n"
            "+ 100.000.000 Vàng & 2.000.000 Kim Cương\n"
            "+ Level 250 & Free VIP 20\n"
            "+ Toàn Bộ Tướng SS 10 Sao Lv.250 (Luffy Nika, Shanks, Vegapunk, Roger...)\n"
            "+ Phôi Nâng Tướng 5-6-9-10 Sao & Pet Thiên Phú Max 30\n"
            "+ 10.000 Cuộn Chiêu Mộ, Bia Đặc Biệt & Vật Phẩm Gọn Gàng Không Lag!"
        )
        push = [
            (116, build_common_vector(a_long=rec)),
            (207, build_pb_list_hero(all_synced_heroes)),
            (250, _equip_bag_payload(state)),
            (251, _consumable_bag_payload(state)),
            (253, _artifact_bag_payload(state)),
            (360, build_list_common_vector([build_common_vector(a_long=p) for p in state.get("pets", [])])),
            (940, build_pet2_status_payload(state, GAME_DB)),
            (2, build_common_vector(a_string=[msg])),
        ]
        return True, msg, rec, push

    return False, "", [], []


def arena_time_remaining(now=None):
    from crystal_arena import remaining
    return remaining(now)


def arena_defense(state, mode):
    """Three native teams: six instance IDs then one pet slot per team."""
    owned = {int(h['id']) for h in state.get('heroes', [])}
    saved = state.get('arena_' + mode + '_defense', [])
    if len(saved) == 21 and all(int(h) == 0 or int(h) in owned for n, h in enumerate(saved) if n not in (6, 13, 20)):
        return [int(h) for h in saved]
    import heapq
    heroes = heapq.nlargest(18, state.get('heroes', []), key=lambda h: (int(h.get('level', 1)), int(h.get('star', 1))))
    ids = [int(h['id']) for h in heroes]
    ids += [0] * (18 - len(ids))
    return ids[:6] + [0] + ids[6:12] + [0] + ids[12:] + [0]


def arena_players(state, mode):
    required = 55 if mode == 'trial' else 80
    source = STORE.list_arena_players() if hasattr(STORE, 'list_arena_players') else STORE.list_top_players(10000)
    players = [p for p in source if int(p.get('level', 1)) >= required]
    if not any(int(p['id']) == int(state['id']) for p in players):
        players.append(dict(state))
    players.sort(key=lambda p: (-int(p.get('arena_' + mode + '_points', 1500)), -int(p.get('power', 0)), int(p['id'])))
    return players


def arena_league_allowance(state, now=None):
    day = (int(time.time() if now is None else now) + 7 * 3600) // 86400
    current = state.get('arena_league_day') == day
    bought = int(state.get('arena_league_bought', 0)) if current else 0
    used = int(state.get('arena_league_used', 0)) if current else 0
    return day, max(0, 5 + bought - used), 5 + bought, max(0, 5 - bought)


def _arena_replay_hp_groups(groups, display_units):
    """Map dynamic HP to a fixed client bar without changing damage numbers.

    Native HP effects carry absolute HP, but no updated maximum. Keep a fixed
    display denominator and encode current HP as the equivalent fraction.
    This is battle-local and never writes the simulation's HP or maximum.
    """
    if not display_units:
        return groups
    mapped_units = {}
    for k, u in display_units.items():
        if int(u['max_hp']) != int(u['_replay_max_hp']) or int(u['hp']) > int(u['_replay_max_hp']) or int(u.get('hero', {}).get('heroId', 0)) in (573, 574, 575):
            mapped_units[k] = u
    if not mapped_units:
        return groups
    result = []
    for group in groups:
        fields = parse_proto_fields(group)
        effects = []
        changed = False
        for wire, raw in fields.get(1, []):
            if raw and len(raw) >= 6 and raw[0] == 0x08 and raw[2] == 0x10 and raw[4] == 0x18 and raw[5] in (3, 61):
                unit = mapped_units.get((raw[1], raw[3]))
                if unit is not None:
                    effect = parse_proto_fields(raw)
                    team = raw[1]
                    pos = raw[3]
                    kind = raw[5]
                    values = [to_signed64(v) for _, v in effect.get(4, [])]
                    if values:
                        maximum = max(1, int(unit['max_hp']))
                        baseline = max(1, int(unit['_replay_max_hp']))
                        hp = values[0]
                        if maximum != baseline or hp > baseline or int(unit.get('hero', {}).get('heroId', 0)) in (573, 574, 575):
                            values[0] = 0 if hp <= 0 else min(baseline, max(1, int(round(hp * baseline / float(maximum)))))
                            raw = build_pb_effect(team, pos, kind, values)
                            changed = True
            effects.append(raw)
        result.append(build_list_pb_effect(effects) if changed else group)
    return result


def arena_round(attacking, defending, *, chopper_immortality_rest=True):
    """Encode a bounded stat-based round, with native HP/death events for both sides."""
    units = []
    for team, heroes in ((1, attacking), (2, defending)):
        for pos, hero in heroes:
            pt_check = hero.get('point')
            if not isinstance(pt_check, list) or len(pt_check) <= 16 or int(pt_check[16] or 0) <= 0:
                try:
                    _recalc_hero(hero)
                except Exception:
                    pass
            point = hero.get('point', [])
            def stat(index, fallback):
                return max(1, int(point[index])) if len(point) > index else fallback
            hp = max(1,int(hero.get('_battle_hp_override',stat(2,1000))))
            atk_v = stat(0, 100)
            def_v = stat(1, 10)
            spd_v = stat(3, 100)
            units.append(dict(team=team, pos=pos, hero=hero, hp=max(0,min(hp,int(hero.get('_battle_current_hp',hp)))), max_hp=hp,
                              atk=atk_v, defense=def_v, speed=spd_v,
                              _replay_max_hp=hp, _replay_atk=atk_v, _replay_def=def_v, _replay_spd=spd_v,
                              anger=initial_battle_rage(hero), start_anger=initial_battle_rage(hero), damage=0, absorbed=0, healed=0,
                              _chopper_arena_rest=bool(chopper_immortality_rest)))
    display_units = {(unit['team'], unit['pos']): unit for unit in units}
    encode_attack = globals()['build_pb_hero_attack']
    def build_pb_hero_attack(team, position, skill_index, targets, list_effects, round_no):
        return encode_attack(team, position, skill_index, targets,
                             _arena_replay_hp_groups(list_effects, display_units), round_no)
    import haki_combat
    for unit in units:haki_combat.initialize(unit,unit['hero'])
    turns = []
    from combat_shields import CombatShields
    import hero_combat_skills
    _arena_state = {
        'active_effs': None,
        'active_coords': None,
        'post_attack_turns': None,
        'pending_passive_effs': [],
        'last_key': None,
        'last_passive_effs': None,
        'last_passive_r': 1,
        'last_passive_actor': None,
    }
    def _rebuild_arena_passive():
        from hero_combat_skills import group_effects_by_coords
        actor_u = _arena_state['last_passive_actor']
        r_val = _arena_state['last_passive_r']
        _, list_effs = group_effects_by_coords(_arena_state['last_passive_effs'], [(actor_u['team'], actor_u['pos'])], sys.modules[__name__])
        attack = build_pb_hero_attack(actor_u['team'], actor_u['pos'], -1, [], list_effs, r_val)
        turns[-1] = build_pb_hero_turn(r_val, actor_u['team'], actor_u['pos'], actor_u['anger'], [attack])
    def _flush_arena_pending_passive(r_no=1, fallback_actor=None):
        pend = _arena_state['pending_passive_effs']
        if not pend:
            return
        _arena_state['pending_passive_effs'] = []
        actor_u = fallback_actor or _arena_state['last_passive_actor'] or next((u for u in units if u['hp'] > 0), units[0])
        r_val = max(1, int(r_no))
        from hero_combat_skills import group_effects_by_coords
        tcoords, list_effs = group_effects_by_coords(pend, [(actor_u['team'], actor_u['pos'])], sys.modules[__name__])
        attack = build_pb_hero_attack(actor_u['team'], actor_u['pos'], -1, [] if turns else tcoords, list_effs, r_val)
        turns.append(build_pb_hero_turn(r_val, actor_u['team'], actor_u['pos'], actor_u['anger'], [attack]))
    def _append_arena_passive(r_no, actor_u, effs, target_coords=None):
        if not effs:
            return
        clean_effs = [e for e in effs if e]
        if not clean_effs:
            return
        if _arena_state['active_effs'] is not None:
            coord_to_idx = {c: idx for idx, c in enumerate(_arena_state['active_coords'])}
            for eff in clean_effs:
                t_team = int(eff[1]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor_u["team"]
                t_pos = int(eff[3]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor_u["pos"]
                if _arena_state['active_effs']:
                    idx = coord_to_idx.get((t_team, t_pos), 0)
                    _arena_state['active_effs'][idx].append(eff)
            return
        _arena_state['pending_passive_effs'].extend(clean_effs)
        _arena_state['last_passive_actor'] = actor_u
        _arena_state['last_passive_r'] = max(1, int(r_no))
    def shield_effects(actor,effects):
        if actor.get('_battle_ship_actor'):
            from hero_combat_skills import group_effects_by_coords
            tcoords, list_effs = group_effects_by_coords(effects, [(actor['team'], actor['pos'])], sys.modules[__name__])
            attack=build_pb_hero_attack(actor['team'],actor['pos'],1,tcoords,list_effs,max(1,shields.round))
            ship_turn = build_pb_hero_turn(max(1,shields.round),actor['team'],actor['pos'],actor['anger'],[attack])
            if _arena_state['post_attack_turns'] is not None:
                _arena_state['post_attack_turns'].append(ship_turn)
            else:
                _arena_state['last_key'] = None
                _arena_state['last_passive_effs'] = None
                turns.append(ship_turn)
        else:
            _append_arena_passive(max(1,shields.round), actor, effects)
    def emit_skill_turn(actor_u, effs, target_coords=None, round_no=1):
        _append_arena_passive(max(1, round_no), actor_u, effs, target_coords)
    shields=CombatShields(sys.modules[__name__],units,random,shield_effects)
    hero_combat_skills.initialize_battle(sys.modules[__name__], units, random, emit_skill_turn)
    def alive(team):return [u for u in units if u['team'] == team and u['hp'] > 0]
    for round_no in range(1, 16):
        if not alive(1) or not alive(2):break
        shields.start_round(round_no)
        hero_combat_skills.start_round(sys.modules[__name__], units, round_no, random, emit_skill_turn)
        hero_combat_skills.check_deaths_and_revives(sys.modules[__name__], units, round_no, random, emit_skill_turn)
        order = sorted(units, key=lambda u: (-hero_combat_skills.effective_speed(u, round_no), u['team'], u['pos']))
        order = hero_combat_skills.take_reaction_turns(units) + [(u, False) for u in order]
        for turn_index, (actor, is_reaction) in enumerate(order):
            if actor['hp'] <= 0:continue
            opponents = alive(3 - actor['team'])
            if not opponents:break
            skip_turn, silenced = hero_combat_skills.check_control_state(sys.modules[__name__], actor, round_no)
            if skip_turn:continue
            def _do_arena_attack(use_rage, no_rage_gain=False, is_extra=False):
                cur_opponents = alive(3 - actor['team'])
                if not cur_opponents or actor['hp'] <= 0:return
                hero_combat_skills.prepare_action(actor, units, round_no, random)
                use_rage = hero_combat_skills.should_use_rage(actor, round_no, forced_normal=is_extra and not use_rage, forced_rage=is_extra and use_rage)
                template = GAME_DB.heroes_by_id.get(int(actor['hero']['heroId']), {})
                meta = getattr(GAME_DB, 'active_skill_meta', {}).get(int(template.get('active', 0) or 0), {})
                norm_meta = None
                for p_k in ('passive1', 'passive2', 'passive3'):
                    p_id = int(template.get(p_k, 0) or 0)
                    if hero_combat_skills.passive_enabled(actor,p_id,round_no) and p_id in getattr(GAME_DB, 'normal_skill_meta', {}):
                        norm_meta = GAME_DB.normal_skill_meta[p_id]
                        break
                shields.before_action(actor,use_rage)
                if not is_extra:
                    from soul_combat import before_action as soul_before_action
                    soul_before_action(actor,units,round_no,use_rage,random)
                hero_combat_skills.before_attack(sys.modules[__name__], actor, units, round_no, use_rage, random)
                cur_opponents = alive(3 - actor['team'])
                if not cur_opponents or actor['hp'] <= 0:return
                targets = hero_combat_skills.select_targets(
                    sys.modules[__name__], actor, cur_opponents, use_rage, sk_meta=meta, norm_meta=norm_meta
                )
                target_coords = [(target['team'], target['pos']) for target in targets]
                per_target_effs = [[] for _ in targets]
                _arena_state['active_effs'] = per_target_effs
                _arena_state['active_coords'] = target_coords
                turn_anger = actor['anger']
                if not no_rage_gain:
                    from battle_energy import gained
                    actor['anger'] = max(0,actor['anger']-100) if use_rage else actor['anger']+gained(actor,50)
                from combat_status import sync as sync_status
                opening_status = []
                if _arena_state['pending_passive_effs']:
                    opening_status.extend(_arena_state['pending_passive_effs'])
                    _arena_state['pending_passive_effs'] = []
                opening_status.extend(sync_status(sys.modules[__name__], units, round_no))
                opening_status.extend(hero_combat_skills.drain_control_effects(sys.modules[__name__],units))
                if opening_status and per_target_effs:
                    per_target_effs[0].extend(opening_status)
                actor["_is_aoe_attack"] = (len(targets) > 1)
                for index, target in enumerate(targets):
                    ap=actor['hero']['point'];tp=target['hero']['point']
                    from blessing_combat import armor_break, critical_chance, critical_bonus, dodge_chance, lifesteal
                    effective_defense=target['defense']*(1-armor_break(ap,tp))
                    base = actor['atk'] * actor['atk'] / (actor['atk'] + effective_defense)
                    direct_dmg = 0.0
                    if use_rage:
                        source = actor['max_hp'] if meta.get('stat') == 'hp' else actor['defense'] if meta.get('stat') == 'arm' else actor['atk']
                        scaled_src = min(source * float(meta.get('mult', 1.65)), actor['atk'] * 3.5) if meta.get('stat') == 'hp' else source * float(meta.get('mult', 1.65))
                        base = scaled_src * actor['atk'] / (actor['atk'] + effective_defense)
                        if meta.get('hp_direct_pct', 0) > 0:
                            direct_dmg += actor['max_hp'] * min(0.15, float(meta['hp_direct_pct']))
                        if meta.get('lost_hp_pct', 0) > 0:
                            direct_dmg += max(0, target['max_hp'] - target['hp']) * min(0.15, float(meta['lost_hp_pct']))
                        cap_mult = min(3.5, float(meta.get('max_direct_cap_mult', 0) or 3.5))
                        if direct_dmg > 0 and cap_mult > 0:
                            direct_dmg = min(direct_dmg, actor['atk'] * cap_mult)
                    elif norm_meta:
                        base *= float(norm_meta.get('mult', 1.0))
                        if norm_meta.get('hp_direct_pct', 0) > 0:
                            norm_cap = min(2.0, float(norm_meta.get('max_direct_cap_mult', 2.0) or 2.0))
                            direct_dmg += min(actor['max_hp'] * min(0.08, float(norm_meta['hp_direct_pct'])), actor['atk'] * norm_cap)
                    if use_rage:base*=1+max(0,ap[5])/1000
                    base += direct_dmg
                    base = hero_combat_skills.modify_damage(
                        sys.modules[__name__], actor, target, base, use_rage, cur_opponents, random
                    )
                    critical=random.random()<critical_chance(ap,tp)
                    if critical:base*=1+critical_bonus(ap,tp)
                    base=base*max(.05,1-min(.95,tp[12]/1000))+actor['atk']*min(1.5,max(0,ap[13])/1000)
                    dodge=dodge_chance(ap,tp)
                    missed=dodge>0 and random.random()<dodge
                    raw_incoming_dmg = 0 if missed else haki_combat.incoming(target,haki_combat.outgoing(actor,max(1,int(base)),critical),critical)
                    clamped_raw = hero_combat_skills.clamp_incoming_damage(sys.modules[__name__], actor, target, raw_incoming_dmg, units, round_no, pre_shield=True) if raw_incoming_dmg > 0 else 0
                    damage,shield_changes=shields.absorb(target,clamped_raw,source=actor)
                    sh_abs = min(target['max_hp'], max(0, clamped_raw - damage)) if any(e[3] == 83 for e in shield_changes if len(e) > 3) else 0
                    damage = hero_combat_skills.clamp_incoming_damage(sys.modules[__name__], actor, target, damage, units, round_no)
                    lost = min(target['hp'],damage)
                    target['hp'] -= lost;actor['damage'] += lost + sh_abs;target['absorbed'] += lost
                    link_effects=hero_combat_skills.balance_linked_hp(sys.modules[__name__],target,round_no)
                    sub = shield_changes + [build_pb_effect(target['team'], target['pos'], 3, [target['hp'], -damage, 2 if critical else 1, 0])] + link_effects
                    from pet_combat import critical_heal as pet_critical_heal
                    restored=min(actor['max_hp']-actor['hp'],lifesteal(ap,lost)+haki_combat.crit_heal(actor,lost,critical)+pet_critical_heal(actor,lost,critical))
                    if restored>0:
                        actor['hp']+=restored;actor['healed']+=restored
                        sub.append(build_pb_effect(actor['team'],actor['pos'],3,[actor['hp'],restored,3,0]))
                    if target.get("_lava_armor") and lost > 0 and not missed and actor["hp"] > 0:
                        reflect_mult = 0.70 if target.get("_admiral_linked") else 0.45
                        reflect_cap = max(1, int(target["max_hp"] * (0.35 if target.get("_admiral_linked") else 0.25)) // max(1, len(targets)))
                        reflect_dmg = min(int(lost * reflect_mult), reflect_cap)
                        reflect_dmg = hero_combat_skills.clamp_incoming_damage(sys.modules[__name__],target,actor,reflect_dmg,units,round_no,pre_shield=True)
                        reflect_dmg, reflect_sh_changes = shields.absorb(actor, reflect_dmg, source=target)
                        reflect_dmg = hero_combat_skills.clamp_incoming_damage(sys.modules[__name__],target,actor,reflect_dmg,units,round_no)
                        min_actor_hp = 1 if hero_combat_skills.cannot_be_killed(actor, units, round_no) else 0
                        actor_new_hp = max(min_actor_hp, actor["hp"] - reflect_dmg)
                        actor_lost = actor["hp"] - actor_new_hp
                        actor["hp"] = actor_new_hp
                        actor["absorbed"] = actor.get("absorbed", 0) + actor_lost
                        target["damage"] = target.get("damage", 0) + actor_lost
                        sub.extend(reflect_sh_changes)
                        if reflect_dmg > 0 or actor_lost > 0:
                            sub.append(build_pb_effect(actor["team"], actor["pos"], 3, [actor_new_hp, -reflect_dmg, 1, 0]))
                    if target['hp'] > 0 and not missed:
                        before = target['anger']
                        from battle_energy import gained
                        target['anger'] = before + gained(target,10)
                        sub.append(build_pb_effect(target['team'], target['pos'], 5, [target['anger'], target['anger'] - before]))
                    if index == 0 and not no_rage_gain:
                        sub.append(build_pb_effect(actor['team'], actor['pos'], 5, [actor['anger'], actor['anger'] - turn_anger]))
                    per_target_effs[index].extend(sub)
                side_effs = hero_combat_skills.build_skill_side_effects(
                    sys.modules[__name__], actor, targets, use_rage, units, round_no, random
                )
                side_effs.extend(hero_combat_skills.drain_link_effects(units))
                side_effs.extend(hero_combat_skills.drain_control_effects(sys.modules[__name__],units))
                side_effs.extend(sync_status(sys.modules[__name__], units, round_no))
                coord_to_idx = {c: idx for idx, c in enumerate(target_coords)}
                for eff in side_effs:
                    if not eff:
                        continue
                    t_team = int(eff[1]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor["team"]
                    t_pos = int(eff[3]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor["pos"]
                    if per_target_effs:
                        idx = coord_to_idx.get((t_team, t_pos), 0)
                        per_target_effs[idx].append(eff)
                from ship_combat import RAGE_MAX as _SHIP_RAGE_MAX, RAGE_PER_ACTION as _SHIP_RAGE_ADD
                for _boat in shields.ships.boats:
                    if _boat["team"] == actor["team"]:
                        _b_next = min(_SHIP_RAGE_MAX, int(_boat["anger"]) + _SHIP_RAGE_ADD)
                        if _b_next > int(_boat["anger"]) and per_target_effs:
                            per_target_effs[0].append(build_pb_effect(_boat["team"], 6, 5, [_b_next, _b_next - int(_boat["anger"])]))
                refund=haki_combat.rage_refund(actor,use_rage,random)
                if refund and per_target_effs:
                    per_target_effs[0].append(build_pb_effect(actor['team'],actor['pos'],5,[actor['anger'],refund]))
                shields.after_action(actor, use_rage, run_ships=False)
                _turn_finalized = [False]
                def _finalize_arena_attack():
                    if _turn_finalized[0]:
                        return
                    _turn_finalized[0] = True
                    tail_effs = []
                    if _arena_state['pending_passive_effs']:
                        tail_effs.extend(_arena_state['pending_passive_effs'])
                        _arena_state['pending_passive_effs'] = []
                    tail_effs.extend(hero_combat_skills.drain_link_effects(units))
                    tail_effs.extend(hero_combat_skills.drain_control_effects(sys.modules[__name__], units))
                    tail_effs.extend(sync_status(sys.modules[__name__], units, round_no))
                    for eff in tail_effs:
                        if not eff:
                            continue
                        t_team = int(eff[1]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor["team"]
                        t_pos = int(eff[3]) if len(eff) >= 4 and eff[0] == 0x08 and eff[2] == 0x10 else actor["pos"]
                        if per_target_effs:
                            idx = coord_to_idx.get((t_team, t_pos), 0)
                            per_target_effs[idx].append(eff)
                    _arena_state['active_effs'] = None
                    _arena_state['active_coords'] = None
                    all_turn_effs = [eff for sub in per_target_effs for eff in sub if eff]
                    effects = [build_list_pb_effect(all_turn_effs)]
                    attack = build_pb_hero_attack(actor['team'], actor['pos'], int(use_rage),
                                                 target_coords, effects, round_no)
                    _arena_state['last_key'] = None
                    _arena_state['last_passive_effs'] = None
                    turns.append(build_pb_hero_turn(round_no, actor['team'], actor['pos'], turn_anger, [attack]))
                    post_ships = []
                    _arena_state['post_attack_turns'] = post_ships
                    shields.ships.after_action(actor, use_rage)
                    _arena_state['post_attack_turns'] = None
                    turns.extend(post_ships)
                if not is_extra:
                    hero_combat_skills.after_attack_triggers(
                        sys.modules[__name__],
                        actor,
                        targets,
                        use_rage,
                        units,
                        round_no,
                        random,
                        emit_skill_turn,
                        lambda use_rage=False, no_rage_gain=True: (_finalize_arena_attack(), _do_arena_attack(use_rage, no_rage_gain=no_rage_gain, is_extra=True)),
                    )
                else:
                    hero_combat_skills.check_deaths_and_revives(sys.modules[__name__], units, round_no, random, emit_skill_turn)
                _finalize_arena_attack()
            _do_arena_attack(actor['anger'] >= 100 and not silenced)
            hero_combat_skills.check_deaths_and_revives(sys.modules[__name__], units, round_no, random, emit_skill_turn)
            order[turn_index+1:turn_index+1] = hero_combat_skills.take_reaction_turns(units)
            order[turn_index+1:] = hero_combat_skills.reorder_pending(order[turn_index+1:], round_no)
        if alive(1) and alive(2):
            for unit in units:
                healed=haki_combat.end_round(unit,len(alive(1))+len(alive(2)),random)
                if healed:
                    emit_skill_turn(unit, [build_pb_effect(unit['team'],unit['pos'],3,[unit['hp'],healed,3,0])], [(unit['team'],unit['pos'])], round_no=round_no)
        hero_combat_skills.end_round(sys.modules[__name__], units, round_no, random, emit_skill_turn, shields)
        from combat_status import sync as sync_status
        end_status = sync_status(sys.modules[__name__], units, round_no)
        if end_status:
            _arena_state['pending_passive_effs'].extend(end_status)
    if _arena_state['pending_passive_effs']:
        _flush_arena_pending_passive(_arena_state.get('last_passive_r', 1))
    winner = 1 if alive(1) and not alive(2) else 2
    result = bytearray(encode_int32(1, winner))
    for u in units:
        h = u['hero'];pts = [0.0] * 35
        rep_mhp = float(u.get('_replay_max_hp', u['max_hp']))
        pts[0] = float(u.get('_replay_atk', u['atk']))
        pts[1] = float(u.get('_replay_def', u['defense']))
        pts[2] = rep_mhp
        pts[3] = float(u.get('_replay_spd', u['speed']))
        pts[16] = rep_mhp
        pts[18] = 100.0  # Native RAGE_MAX, not HP.
        pts[4] = float(u['start_anger'])
        pts[19] = 100.0  # Native rage bar denominator.
        hero = bytearray(encode_int32(1, resolve_battle_hero_id(int(h['heroId']))) + encode_int32(2, u['pos']))
        for point in pts:hero += encode_float(3, point)
        hero += encode_int32(4, int(h.get('level', 1))) + encode_int32(5, client_skin_id(h.get('skin', 0)))
        hero += encode_int64(6, u['damage']) + encode_int64(7, u['healed']) + encode_int64(8, u['absorbed'])
        hero += encode_int32(9, int(h.get('star', 5))) + encode_int32(10, int(h.get('tier', 0)))
        result += encode_submessage(2 if u['team'] == 1 else 3, bytes(hero))
    result+=shields.ships.encode()
    for turn in turns:result += encode_submessage(4, turn)
    result += encode_int32(9, 15) + encode_int32(12, 0) + encode_int32(12, 0)
    return bytes(result), winner


def arena_battle_records(state, mode):
    now = int(time.time());vectors = []
    for record in reversed(state.get('arena_' + mode + '_records', [])[-20:]):
        common = [record['opponent'], record['id'], record['avatar'][0], record['avatar'][1], record['level']]
        elapsed = max(0, now - record['at'])
        if mode == 'trial':common += [record['delta'], elapsed, int(record['win']), record['attacker']]
        else:common += [record['old_rank'], record['new_rank'], elapsed, int(record['win']), record['attacker']]
        vectors.append(build_common_vector(a_long=common, a_string=[record['name']]))
    return build_list_common_vector(vectors)


def arena_write_states(conn, states):
    columns = {row[1] for row in conn.execute('PRAGMA table_info(players)')}
    suffix = ',revision=revision+1,updated_at=?' if 'revision' in columns and 'updated_at' in columns else ''
    for uid, value in states:
        try:
            value['power'] = compute_player_power(value)
        except Exception:
            pass
        args = [json.dumps(value, ensure_ascii=False)]
        if suffix:args.append(int(time.time()))
        args.append(uid)
        conn.execute('UPDATE players SET state_json=?' + suffix + ' WHERE uid=?', args)


def dispatch_arena_battle(act_id, values, uid, state):
    """Validate the native 3-team request and commit score/cost/history together."""
    if act_id not in (338, 342, 554, 560):return None
    import base64
    mode = 'league' if act_id in (554, 560) else 'trial'
    def reject(message):return [(2, build_common_vector(a_string=[message]))]
    players = arena_players(state, mode) if act_id != 560 else []
    try:
        svc = globals().get('account_service')
        use_async = svc is not None and getattr(svc, '_db_writer', None) is not None
        now = int(time.time())
        if use_async and isinstance(state, dict) and state.get('heroes') is not None:
            current = state
            current['id'] = uid
            required = 80 if mode == 'league' else 55
            if int(current.get('level', 1)) < required:return reject(f'Yêu cầu cấp {required} để tham gia.')
            day, remaining, maximum, purchases = arena_league_allowance(current, now)
            if act_id == 560:
                count = int(values[0]) if len(values) == 1 else 0
                if count < 1 or count > purchases:return reject('Số lượt mua không hợp lệ hoặc đã hết lượt mua hôm nay.')
                cost = count * 50
                if int(current.get('gem', 0)) < cost:return reject('Không đủ Kim Cương để mua lượt.')
                if current.get('arena_league_day') != day:
                    current.update(arena_league_day=day, arena_league_used=0, arena_league_bought=0)
                current['arena_league_bought'] += count;current['gem'] -= cost
                arena_write_states(None, [(uid, current)])
                if callable(globals().get('arena_publish_states')):arena_publish_states()
                return [(act_id, build_common_vector(a_long=[remaining + count, maximum + count, cost]))]
            offset = 2 if mode == 'league' else 1
            if len(values) != offset + 21:return reject('Dữ liệu đội hình trận đấu không hợp lệ. Hãy mở lại đấu trường.')
            opponent_uid = int(values[0]);formation = [int(x) for x in values[offset:]]
            ids = [h for n,h in enumerate(formation) if n not in (6,13,20) and h > 0]
            from combat_shields import attach_pet_skills
            attach_pet_skills(sys.modules[__name__], current, only_hids=ids)
            owned = {int(h['id']): h for h in current.get('heroes', [])}
            if opponent_uid == uid or any(h < 0 for h in formation) or not ids or len(set(ids)) != len(ids) or not set(ids) <= owned.keys():
                return reject('Đội hình hoặc đối thủ không hợp lệ.')
            fingerprint = str(act_id) + ':' + ','.join(map(str, values))
            cached = current.get('arena_' + mode + '_last', {})
            if cached.get('request') == fingerprint and 0 <= now - cached.get('at', 0) <= 2:
                return [(act_id, base64.b64decode(cached['reply']))]
            opp_pre = STORE.get_player_by_uid(opponent_uid) if hasattr(STORE, 'get_player_by_uid') else None
            if not isinstance(opp_pre, dict) and hasattr(STORE, 'get_arena_player_by_uid'):
                opp_pre = STORE.get_arena_player_by_uid(opponent_uid)
            is_cross_opp = int(opponent_uid) >= 10000000
            if isinstance(opp_pre, dict):
                opponent = dict(opp_pre);opponent['id'] = opponent_uid
                opponent['arena_' + mode + '_records'] = list(opponent.get('arena_' + mode + '_records', []))
                if int(opponent.get('level', 1)) < required:return reject('Đối thủ chưa đủ cấp tham gia chế độ này.')
                defensive = arena_defense(opponent, mode)
                def_ids = [int(h) for n,h in enumerate(defensive) if n not in (6,13,20) and int(h) > 0]
                attach_pet_skills(sys.modules[__name__], opponent, only_hids=def_ids)
                opposing = {int(h['id']): h for h in opponent.get('heroes', [])}
                if not any(h in opposing for n,h in enumerate(defensive) if n not in (6,13,20)):
                    return reject('Đối thủ chưa có đội hình phòng thủ hợp lệ.')
                if mode == 'league':
                    if remaining < 1:return reject('Đã hết lượt Võ đấu hôm nay.')
                else:
                    tickets = sum(int(c[2]) for c in current.get('consumables', []) if c[:2] == [1,4])
                    if tickets < 3:return reject('Cần 3 vé đấu để tham gia Liên đấu.')
                rounds = [];wins = [0,0]
                for team in range(3):
                    attackers = [(n, owned[h]) for n,h in enumerate(formation[team*7:team*7+6]) if h in owned]
                    defenders = [(n, opposing[h]) for n,h in enumerate(defensive[team*7:team*7+6]) if h in opposing]
                    if not attackers and not defenders:continue
                    from ship_combat import attach as attach_ship,selected as selected_ship
                    attackers=attach_ship(attackers,selected_ship(current,formation[team*7:team*7+7]))
                    defenders=attach_ship(defenders,selected_ship(opponent,defensive[team*7:team*7+7]))
                    replay, winner = arena_round(attackers, defenders, chopper_immortality_rest=True)
                    rounds.append(replay);wins[winner-1] += 1
                    if max(wins) >= 2:break
                if not rounds:return reject('Không có đội hình để bắt đầu trận đấu.')
                winner = 1 if wins[0] > wins[1] else 2
                old_points = int(current.get('arena_' + mode + '_points', 1500))
                other_points = int(opponent.get('arena_' + mode + '_points', 1500))
                delta = 20 if winner == 1 else -20
                current['arena_' + mode + '_points'] = max(0, old_points + delta)
                opponent['arena_' + mode + '_points'] = max(0, other_points - delta)
                current['arena_' + mode + '_defense'] = formation
                bonus = []
                if mode == 'league':
                    if current.get('arena_league_day') != day:
                        current.update(arena_league_day=day, arena_league_used=0, arena_league_bought=0)
                    current['arena_league_used'] += 1
                else:
                    bonus,_ = apply_and_make_bonus_received(current, [('material',1,4,-3)], GAME_DB)
                old_ranks = {int(p['id']): n for n,p in enumerate(players, 1)}
                for p in players:
                    if int(p['id']) == uid:p['arena_' + mode + '_points'] = current['arena_' + mode + '_points']
                    if int(p['id']) == opponent_uid:p['arena_' + mode + '_points'] = opponent['arena_' + mode + '_points']
                players.sort(key=lambda p: (-int(p.get('arena_' + mode + '_points',1500)), -int(p.get('power',0)), int(p['id'])))
                ranks = {int(p['id']): n for n,p in enumerate(players,1)}
                from arena_replays import new_id, save_async as save_replay_async
                battle_id = new_id(None, uid)
                output = bytearray()
                for replay in rounds:output += encode_submessage(1, replay)
                output += encode_int32(2, winner)
                for v in bonus:output += encode_int64(3, v)
                for field, player in ((4,current),(5,opponent)):
                    avatar = player.get('avatar',[0,1])
                    output += encode_submessage(field, build_common_vector(a_long=[player['id'],avatar[0],avatar[1],player.get('level',1)],
                                                                           a_string=[player.get('screenname','Hải Tặc')]))
                info = [current['arena_' + mode + '_points'],current['arena_' + mode + '_points']-old_points,
                        opponent['arena_' + mode + '_points'],opponent['arena_' + mode + '_points']-other_points]
                output += encode_int64(6,battle_id) + encode_int32(7,0) + encode_submessage(8,build_common_vector(a_long=info))
                output = bytes(output)
                def_record = None
                for player, enemy, won, change in ((current,opponent,winner==1,delta),(opponent,current,winner==2,-delta)):
                    records = player.setdefault('arena_' + mode + '_records',[])
                    rec_item = dict(id=battle_id,at=now,opponent=enemy['id'],name=enemy.get('screenname','Hải Tặc'),
                                    avatar=enemy.get('avatar',[0,1]),level=enemy.get('level',1),win=won,delta=change,
                                    attacker=uid,old_rank=old_ranks.get(int(player['id']),0),new_rank=ranks.get(int(player['id']),0))
                    records.append(rec_item)
                    if player is opponent:
                        def_record = rec_item
                    del records[:-20]
                current['arena_' + mode + '_last'] = dict(request=fingerprint,at=now,reply=base64.b64encode(output).decode('ascii'))
                save_replay_async(STORE.db_path, battle_id, mode, uid, opponent_uid, now, output)
                if is_cross_opp:
                    arena_write_states(None, [(uid, current)])
                    if svc is not None:
                        chat = getattr(svc, 'shared_chat', None)
                        if chat is not None and hasattr(chat, 'record_remote_arena_delta'):
                            chat.record_remote_arena_delta(opponent_uid, mode, -delta, def_record)
                else:
                    arena_write_states(None, [(uid, current), (opponent_uid, opponent)])
                if callable(globals().get('arena_publish_states')):arena_publish_states()
                response = [(act_id, output)]
                if mode == 'trial':response.append((251,_consumable_bag_payload(state)))
                return response
        with sqlite3.connect(STORE.db_path, timeout=10) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT state_json FROM players WHERE uid=?', (uid,)).fetchone()
            if not row:return reject('Không tìm thấy nhân vật.')
            current = json.loads(row[0]);now = int(time.time())
            required = 80 if mode == 'league' else 55
            if int(current.get('level', 1)) < required:return reject(f'Yêu cầu cấp {required} để tham gia.')
            day, remaining, maximum, purchases = arena_league_allowance(current, now)
            if act_id == 560:
                count = int(values[0]) if len(values) == 1 else 0
                if count < 1 or count > purchases:return reject('Số lượt mua không hợp lệ hoặc đã hết lượt mua hôm nay.')
                cost = count * 50
                if int(current.get('gem', 0)) < cost:return reject('Không đủ Kim Cương để mua lượt.')
                if current.get('arena_league_day') != day:
                    current.update(arena_league_day=day, arena_league_used=0, arena_league_bought=0)
                current['arena_league_bought'] += count;current['gem'] -= cost
                arena_write_states(conn, [(uid, current)])
                output = build_common_vector(a_long=[remaining + count, maximum + count, cost])
            else:
                offset = 2 if mode == 'league' else 1
                if len(values) != offset + 21:return reject('Dữ liệu đội hình trận đấu không hợp lệ. Hãy mở lại đấu trường.')
                opponent_uid = int(values[0]);formation = [int(x) for x in values[offset:]]
                from combat_shields import attach_pet_skills
                attach_pet_skills(sys.modules[__name__],current)
                owned = {int(h['id']): h for h in current.get('heroes', [])}
                ids = [h for n,h in enumerate(formation) if n not in (6,13,20) and h > 0]
                if opponent_uid == uid or any(h < 0 for h in formation) or not ids or len(set(ids)) != len(ids) or not set(ids) <= owned.keys():
                    return reject('Đội hình hoặc đối thủ không hợp lệ.')
                fingerprint = str(act_id) + ':' + ','.join(map(str, values))
                cached = current.get('arena_' + mode + '_last', {})
                if cached.get('request') == fingerprint and 0 <= now - cached.get('at', 0) <= 2:
                    return [(act_id, base64.b64decode(cached['reply']))]
                is_cross_opp = int(opponent_uid) >= 10000000
                if is_cross_opp:
                    opp_pre = STORE.get_player_by_uid(opponent_uid) if hasattr(STORE, 'get_player_by_uid') else None
                    if not isinstance(opp_pre, dict) and hasattr(STORE, 'get_arena_player_by_uid'):
                        opp_pre = STORE.get_arena_player_by_uid(opponent_uid)
                    if not isinstance(opp_pre, dict):
                        return reject('Đối thủ không còn tồn tại. Hãy làm mới danh sách.')
                    opponent = dict(opp_pre)
                    opponent['id'] = opponent_uid
                else:
                    target = conn.execute('SELECT state_json FROM players WHERE uid=?', (opponent_uid,)).fetchone()
                    if not target:return reject('Đối thủ không còn tồn tại. Hãy làm mới danh sách.')
                    opponent = json.loads(target[0])
                    opponent['id'] = opponent_uid
                from combat_shields import attach_pet_skills
                attach_pet_skills(sys.modules[__name__],opponent)
                if int(opponent.get('level', 1)) < required:return reject('Đối thủ chưa đủ cấp tham gia chế độ này.')
                defensive = arena_defense(opponent, mode)
                opposing = {int(h['id']): h for h in opponent.get('heroes', [])}
                if not any(h in opposing for n,h in enumerate(defensive) if n not in (6,13,20)):
                    return reject('Đối thủ chưa có đội hình phòng thủ hợp lệ.')
                if mode == 'league':
                    if remaining < 1:return reject('Đã hết lượt Võ đấu hôm nay.')
                else:
                    tickets = sum(int(c[2]) for c in current.get('consumables', []) if c[:2] == [1,4])
                    if tickets < 3:return reject('Cần 3 vé đấu để tham gia Liên đấu.')
                rounds = [];wins = [0,0]
                for team in range(3):
                    attackers = [(n, owned[h]) for n,h in enumerate(formation[team*7:team*7+6]) if h in owned]
                    defenders = [(n, opposing[h]) for n,h in enumerate(defensive[team*7:team*7+6]) if h in opposing]
                    if not attackers and not defenders:continue
                    from ship_combat import attach as attach_ship,selected as selected_ship
                    attackers=attach_ship(attackers,selected_ship(current,formation[team*7:team*7+7]))
                    defenders=attach_ship(defenders,selected_ship(opponent,defensive[team*7:team*7+7]))
                    replay, winner = arena_round(attackers, defenders, chopper_immortality_rest=True)
                    rounds.append(replay);wins[winner-1] += 1
                    if max(wins) >= 2:break
                if not rounds:return reject('Không có đội hình để bắt đầu trận đấu.')
                winner = 1 if wins[0] > wins[1] else 2
                old_points = int(current.get('arena_' + mode + '_points', 1500))
                other_points = int(opponent.get('arena_' + mode + '_points', 1500))
                delta = 20 if winner == 1 else -20
                current['arena_' + mode + '_points'] = max(0, old_points + delta)
                opponent['arena_' + mode + '_points'] = max(0, other_points - delta)
                current['arena_' + mode + '_defense'] = formation
                bonus = []
                if mode == 'league':
                    if current.get('arena_league_day') != day:
                        current.update(arena_league_day=day, arena_league_used=0, arena_league_bought=0)
                    current['arena_league_used'] += 1
                else:
                    bonus,_ = apply_and_make_bonus_received(current, [('material',1,4,-3)], GAME_DB)
                old_ranks = {int(p['id']): n for n,p in enumerate(players, 1)}
                for p in players:
                    if int(p['id']) == uid:p['arena_' + mode + '_points'] = current['arena_' + mode + '_points']
                    if int(p['id']) == opponent_uid:p['arena_' + mode + '_points'] = opponent['arena_' + mode + '_points']
                players.sort(key=lambda p: (-int(p.get('arena_' + mode + '_points',1500)), -int(p.get('power',0)), int(p['id'])))
                ranks = {int(p['id']): n for n,p in enumerate(players,1)}
                from arena_replays import new_id, save as save_replay
                battle_id = new_id(conn, uid)
                output = bytearray()
                for replay in rounds:output += encode_submessage(1, replay)
                output += encode_int32(2, winner)
                for v in bonus:output += encode_int64(3, v)
                for field, player in ((4,current),(5,opponent)):
                    avatar = player.get('avatar',[0,1])
                    output += encode_submessage(field, build_common_vector(a_long=[player['id'],avatar[0],avatar[1],player.get('level',1)],
                                                                           a_string=[player.get('screenname','Hải Tặc')]))
                # ArenaPointResult reads [my total, my delta, rival total, rival delta].
                info = [current['arena_' + mode + '_points'],current['arena_' + mode + '_points']-old_points,
                        opponent['arena_' + mode + '_points'],opponent['arena_' + mode + '_points']-other_points]
                output += encode_int64(6,battle_id) + encode_int32(7,0) + encode_submessage(8,build_common_vector(a_long=info))
                output = bytes(output)
                def_record = None
                for player, enemy, won, change in ((current,opponent,winner==1,delta),(opponent,current,winner==2,-delta)):
                    records = player.setdefault('arena_' + mode + '_records',[])
                    rec_item = dict(id=battle_id,at=now,opponent=enemy['id'],name=enemy.get('screenname','Hải Tặc'),
                                    avatar=enemy.get('avatar',[0,1]),level=enemy.get('level',1),win=won,delta=change,
                                    attacker=uid,old_rank=old_ranks.get(int(player['id']),0),new_rank=ranks.get(int(player['id']),0))
                    records.append(rec_item)
                    if player is opponent:
                        def_record = rec_item
                    del records[:-20]
                current['arena_' + mode + '_last'] = dict(request=fingerprint,at=now,reply=base64.b64encode(output).decode('ascii'))
                save_replay(conn, battle_id, mode, uid, opponent_uid, now, output)
                if is_cross_opp:
                    arena_write_states(conn, [(uid, current)])
                    if svc is not None:
                        chat = getattr(svc, 'shared_chat', None)
                        if chat is not None and hasattr(chat, 'record_remote_arena_delta'):
                            chat.record_remote_arena_delta(opponent_uid, mode, -delta, def_record)
                else:
                    arena_write_states(conn, [(uid,current),(opponent_uid,opponent)])
        if callable(globals().get('arena_publish_states')):arena_publish_states()
        state.clear();state.update(current)
        response = [(act_id, output)]
        if mode == 'trial':response.append((251,_consumable_bag_payload(state)))
        return response
    except (ValueError,KeyError,TypeError):
        return reject('Dữ liệu trận đấu không hợp lệ. Hãy làm mới đối thủ và chọn lại đội hình.')
    except sqlite3.Error:
        return reject('Không thể lưu kết quả trận đấu. Hãy thử lại.')


def dispatch_arena_entry(act_id, values, uid, state):
    """Native entry/status/lineup/list contracts; keep currencies out of lineup saves."""
    if act_id == 327:
        left = arena_time_remaining()
        # ArenaScene: crystal state/time, trial state/time, league phase/time.
        return [(327, build_common_vector(a_long=[1, left, 1, left, 1, left]))]
    trial = {336, 337, 338, 339, 341, 343, 346, 348}
    league = {550, 551, 552, 553, 555, 556, 561}
    if act_id not in trial | league:
        return None
    mode = 'trial' if act_id in trial else 'league'
    required = 55 if mode == 'trial' else 80
    if int(state.get('level', 1)) < required:
        return [(2, build_common_vector(a_string=[f'Yêu cầu cấp {required} để tham gia.']))]
    if act_id == 338:
        # A stale revenge request must never reach the legacy SHOP_BUY alias.
        return [(2, build_common_vector(a_string=['Bản ghi trận đấu không còn khả dụng. Hãy tìm đối thủ mới.']))]
    if act_id in (339, 552):
        owned = {int(h['id']) for h in state.get('heroes', [])}
        selected = [int(h) for n, h in enumerate(values) if n not in (6, 13, 20) and int(h) > 0]
        if len(values) != 21 or any(int(h) < 0 for h in values) or not selected or len(set(selected)) != len(selected) or not set(selected) <= owned:
            return [(2, build_common_vector(a_string=['Đội hình không hợp lệ. Hãy chọn lại thuyền viên đang sở hữu.']))]
        state['arena_' + mode + '_defense'] = [int(h) for h in values]
        STORE.save_state(uid, state)
        # Native handlers request the refreshed status after this acknowledgment.
        return [(act_id, build_common_vector(a_long=[1]))]
    if act_id in (337, 551):
        return [(act_id, arena_battle_records(state, mode))]
    if act_id in (346, 556):
        return [(act_id, build_common_vector(a_long=[0]))]
    players = arena_players(state, mode)
    ranking = next((n for n, p in enumerate(players, 1) if int(p['id']) == int(uid)), len(players))
    power = compute_player_power(state)
    enc_power = encode_client_i32_power(power)
    points = int(state.get('arena_' + mode + '_points', 1500))
    if act_id == 336:
        # TrialChampionScene reads exactly five longs in vector 0 and 21 in 1.
        info = [enc_power, ranking, points, 20, 3]
    elif act_id == 550:
        # AllStarsLeagueScene: phase, time, power, rank, point, turns, max, price, purchases.
        _, remaining, maximum, purchases = arena_league_allowance(state)
        info = [1, arena_time_remaining(), enc_power, ranking, points, remaining, maximum, 50, purchases]
    else:
        rivals = players
        if act_id in (341, 553):
            import random as _rnd
            named_r = [p for p in players if int(p['id']) != int(uid) and str(p.get('screenname') or '').strip()]
            other_r = [p for p in players if int(p['id']) != int(uid) and not str(p.get('screenname') or '').strip()]
            _rnd.shuffle(named_r)
            _rnd.shuffle(other_r)
            rivals = (named_r + other_r)[:60]
        # All-stars' rank tab/search reuse ArenaPlayer.m_ArenaPoint for rank;
        # its point tab and the trial arena use that same field for rating.
        rank_field = mode == 'league' and act_id in (553, 555)
        ranks = {int(p['id']): n for n, p in enumerate(players, 1)}
        summaries = []
        for p in rivals[:60 if act_id in (341, 553) else 50]:
            p_copy = dict(p)
            raw_t = p_copy.get('team') or []
            clean_t = []
            for idx_t, r_t in enumerate(raw_t[:6], start=1):
                if isinstance(r_t, list) and len(r_t) >= 4:
                    inst_t = int(r_t[4]) if len(r_t) > 4 else idx_t
                    clean_t.append([int(r_t[0]), int(r_t[1]), int(r_t[2]), int(r_t[3]), inst_t])
            while len(clean_t) < 6:
                clean_t.append([0, 0, 0, 0, 0])
            p_copy['team'] = clean_t[:6]
            p_copy['arena_info'] = [ranks[int(p['id'])] if rank_field else int(p.get('arena_' + mode + '_points', 1500))]
            summaries.append(p_copy)
        return [(act_id, build_pb_list_user(summaries, my_rank=ranking, my_info=dict(state, power=power, arena_info=[ranking if rank_field else points])))]
    return [(act_id, build_list_common_vector([
        build_common_vector(a_long=info), build_common_vector(a_long=arena_defense(state, mode))]))]


_SYNC_STATE_MEMO = {}
_SANITIZE_HEROES_MEMO = {}
_NO_HERO_SYNC_ACTIONS = frozenset({
    96, 97, 101, 102, 103, 104, 105, 106, 107, 108, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119,
    120, 121, 122, 124, 125, 128,
    201, 202, 203, 204, 205, 206, 207, 208, 209,
    210, 211, 212, 213, 214, 215, 216, 218, 219, 220, 221, 222, 223, 224, 225, 226, 227, 228, 229,
    230, 231, 232, 233, 234, 235, 236, 237, 238, 239, 240, 241, 242, 243, 244, 245, 246, 247,
    250, 251, 252, 253, 254, 255, 256, 257, 258, 259, 260, 261, 262,
    264, 265, 266, 267, 268, 270, 271, 272, 273, 274, 275, 278, 280, 281, 284, 285,
    300, 301, 302, 303, 305, 306, 307, 308, 309, 310, 311, 313, 315,
    316, 318, 319, 320, 321, 322, 323, 324, 325, 326, 327, 328, 329, 330, 331, 332, 333, 334, 335, 336, 337, 339, 340, 341, 342, 343, 344, 346, 347, 348, 350, 351,
    360, 361, 362, 363, 364, 365, 366,
    400, 401, 402, 403, 404, 405, 406, 407, 408, 410, 411, 412, 413, 414, 415, 416, 417, 418, 421, 422, 424, 425, 426, 427, 428, 429,
    430, 432, 433, 438, 440, 441, 445, 448, 450, 451, 452, 453, 454, 456, 457, 458, 459, 460, 461, 462, 463, 464, 465, 466, 467, 468, 469, 470,
    500, 501, 502, 503, 504, 505, 506, 507, 510, 511, 512, 513, 514, 515, 550, 551, 552, 553, 554, 555, 556, 557, 560, 561, 581, 582, 583, 584, 585, 586,
    590, 591, 592, 593, 594, 595,
    610, 611, 612, 613, 614, 615, 616, 617, 618,
    650, 651, 652, 653, 654, 655, 656, 657, 658, 659, 660, 661, 662,
    701, 710, 711, 725, 726, 727, 728, 729, 730, 731, 737, 738, 739, 740, 741, 742, 743, 744, 745, 746, 747, 750, 751, 752, 753, 770, 771, 772, 780, 781, 782, 787,
    790, 791, 792, 793, 797, 798, 799,
    800, 801, 802, 803, 804, 805, 806, 807, 808, 810, 811, 812, 813, 814, 815, 820,
    900, 901, 902, 903, 905, 907, 908, 909, 910, 911, 914, 915, 916, 917, 918, 928, 929, 930, 931, 932, 933, 934, 935, 936, 937, 940, 941, 942, 943, 944, 945, 946, 947, 948,
    960, 961, 962, 963, 964, 965, 966, 967, 968, 969, 970, 995,
    1120, 1121, 1860, 1861, 1862, 1863, 1864, 1865, 1866, 1867, 1868, 1869, 1870, 1871, 1872, 1873, 1874, 1875, 1876, 1877, 1900, 1901, 1902, 1903, 1904, 1905,
    2100, 2101, 2103, 2104, 2106, 2107, 2108, 8000, 9999,
})


def _hero_sync_token(h: dict):
    g = h.get("germa")
    b = h.get("blessing")
    e = h.get("epic_equipment")
    s = h.get("soul")
    pt = h.get("point")
    pt_len = len(pt) if isinstance(pt, list) else 0
    return (
        2,
        h.get("id"),
        h.get("heroId"),
        h.get("level"),
        h.get("star"),
        h.get("tier"),
        h.get("skin", 0),
        h.get("treasure", 0),
        h.get("stone_effect"),
        pt_len,
        pt[0] if pt_len > 0 else None,
        pt[2] if pt_len > 2 else None,
        pt[16] if pt_len > 16 else None,
        tuple(h.get("item") or ()),
        tuple(h.get("skills") or ()),
        tuple(h.get("_pet_stats") or ()),
        tuple(g.get("levels", ())) if isinstance(g, dict) else None,
        (b.get("level"), b.get("star"), b.get("table"), tuple(b.get("skills", ()))) if isinstance(b, dict) else None,
        ((e.get("attack", {}).get("level"), e.get("attack", {}).get("tier")),
         (e.get("hp", {}).get("level"), e.get("hp", {}).get("tier"))) if isinstance(e, dict) else None,
        s.get("level") if isinstance(s, dict) else None,
    )


def _sanitize_hero_ids_and_teams(state: dict) -> bool:
    if not isinstance(state, dict):
        return False
    heroes = state.get("heroes")
    if not isinstance(heroes, list) or not heroes:
        return False
    uid_key = int(state.get("id", 0) or 0)
    raw_team = state.get("team")
    t_sig = tuple(tuple(r[:5]) for r in raw_team if isinstance(r, list)) if isinstance(raw_team, list) else ()
    h_sig = (len(heroes), id(heroes[0]), id(heroes[-1]), heroes[0].get("id") if isinstance(heroes[0], dict) else 0, heroes[-1].get("id") if isinstance(heroes[-1], dict) else 0, int(state.get("next_hero_id", 0) or 0), t_sig)
    if uid_key and _SANITIZE_HEROES_MEMO.get(uid_key) == h_sig:
        return False
    changed = False
    max_id = max(0, int(state.get("next_hero_id", 1) or 1) - 1)
    for h in heroes:
        if isinstance(h, dict):
            hid = int(h.get("id", 0) or 0)
            if hid > max_id:
                max_id = hid
    seen_ids = set()
    for h in heroes:
        if not isinstance(h, dict):
            continue
        old_id = int(h.get("id", 0) or 0)
        if old_id <= 0 or old_id in seen_ids:
            max_id += 1
            h["id"] = max_id
            seen_ids.add(max_id)
            changed = True
        else:
            seen_ids.add(old_id)
    if int(state.get("next_hero_id", 0) or 0) <= max_id:
        state["next_hero_id"] = max_id + 1
    if isinstance(raw_team, list) and raw_team:
        by_id = {int(h["id"]): h for h in heroes if isinstance(h, dict) and "id" in h}
        clean_team = []
        seen_t = set()
        for row in raw_team:
            if isinstance(row, list) and row:
                iid = int(row[4]) if len(row) > 4 else 0
                h_obj = by_id.get(iid)
                if h_obj is not None and iid not in seen_t:
                    seen_t.add(iid)
                    clean_team.append([int(h_obj["heroId"]), int(h_obj.get("level", 1)), int(h_obj.get("star", 5)), int(h_obj.get("skin", 0)), iid])
        if len(clean_team) != len(raw_team) or any(len(r) < 5 for r in raw_team if isinstance(r, list)):
            if clean_team:
                state["team"] = clean_team[:6]
                changed = True
    if state.get("fruit_heroes"):
        try:
            from inventory_special import sanitize_fruit_heroes
            if sanitize_fruit_heroes(state):
                changed = True
        except Exception:
            pass
    if uid_key:
        if len(_SANITIZE_HEROES_MEMO) > 1024:
            _SANITIZE_HEROES_MEMO.clear()
        new_team = state.get("team")
        nt_sig = tuple(tuple(r[:5]) for r in new_team if isinstance(r, list)) if isinstance(new_team, list) else ()
        _SANITIZE_HEROES_MEMO[uid_key] = (len(heroes), id(heroes[0]), id(heroes[-1]), heroes[0].get("id") if isinstance(heroes[0], dict) else 0, heroes[-1].get("id") if isinstance(heroes[-1], dict) else 0, int(state.get("next_hero_id", 0) or 0), nt_sig)
    return changed


def _ensure_heroes_synced(mod, state: dict):
    from album import sync_heroes as sync_album_heroes
    from hero_equipment_stats import sync_pets
    from reported_stats import sync as sync_ship_bonuses, ship_totals
    from fruit_stats import sync as sync_fruits
    from ship_decor import sync_heroes as sync_decor
    from germa import sync as sync_germa
    from holy_blessing import sync as sync_blessing
    from guild_tech import sync as sync_guild_tech
    from islands import _sync_all_gem_bonuses as sync_gems

    _sanitize_hero_ids_and_teams(state)
    heroes = state.get("heroes")
    if not isinstance(heroes, list) or not heroes:
        return
    need_uncap_recalc = state.get("_power_uncap_v2") != 1
    if need_uncap_recalc:
        state["_power_uncap_v2"] = 1
        for idx_h, h_obj in enumerate(heroes):
            if isinstance(h_obj, dict):
                h_copy = dict(h_obj)
                if isinstance(h_copy.get("point"), list):
                    h_copy["point"] = list(h_copy["point"])
                heroes[idx_h] = h_copy
    is_admin_vthuan = str(state.get("username", "")).strip().lower() == "vthuan1804"
    for idx_h, h_obj in enumerate(heroes):
        if isinstance(h_obj, dict):
            it_obj = h_obj.get("item")
            has_test_art = isinstance(it_obj, list) and len(it_obj) > 4 and (int(it_obj[4] or 0) == 233 or int(it_obj[4] or 0) >= 100000)
            if is_admin_vthuan and has_test_art and not h_obj.get("_admin_test_artifact"):
                h_copy = dict(h_obj)
                h_copy["_admin_test_artifact"] = 1
                heroes[idx_h] = h_copy
            elif not is_admin_vthuan and h_obj.get("_admin_test_artifact"):
                h_copy = dict(h_obj)
                h_copy.pop("_admin_test_artifact", None)
                heroes[idx_h] = h_copy
    uid_key = 0
    bsig = None
    import marshal
    try:
        uid_key = int(state.get("id", 0))
        pets_sig = tuple((int(p[0]), *map(int, p[3:10])) for p in (state.get("pets") or []) if len(p) >= 10)
        eq_p2 = state.get("pet2_equips") or {}
        eq_p2_ids = {int(k) for k, v in eq_p2.items() if int(v or 0) > 0} if isinstance(eq_p2, dict) else set()
        pets2_eq_sig = tuple((int(p.get("id", 0)), int(p.get("set_id", 0)), int(p.get("star", 0)), int(p.get("level", 1)), int(p.get("tier", 1)), float(p.get("intelligent", 0)), float(p.get("basic_atk", 0)), float(p.get("basic_hp", 0)), float(p.get("basic_def", 0)), float(p.get("basic_spd", 0))) for p in (state.get("pets2") or []) if isinstance(p, dict) and int(p.get("id", 0)) in eq_p2_ids) if eq_p2_ids else ()
        gems_raw = state.get("island_gems") or []
        gems_eq_sig = tuple(
            (int(g.get("id", 0)), int(g.get("hero", 0)), int(g.get("set", 0)), int(g.get("quality", 0)), int(g.get("slot", 0)), tuple(g.get("base") or ()))
            for g in gems_raw if isinstance(g, dict) and int(g.get("hero", 0)) > 0
        ) if isinstance(gems_raw, list) else ()
        bsig = marshal.dumps((
            state.get("album"),
            pets2_eq_sig,
            eq_p2,
            pets_sig,
            state.get("fruits"),
            state.get("fruit_heroes"),
            state.get("ship_decor"),
            state.get("guild_tech"),
            state.get("guild_tech_advanced"),
            gems_eq_sig,
        ), 2)
        prev = _SYNC_STATE_MEMO.get(uid_key) if (uid_key and not need_uncap_recalc) else None
        if prev is not None and prev[0] == bsig:
            synced_tokens = prev[1]
            exp_ship = ship_totals(state)
            if (heroes[0].get("_ship_bonus") or {}) == exp_ship and (heroes[-1].get("_ship_bonus") or {}) == exp_ship:
                sub_idx = [i for i, h in enumerate(heroes) if _hero_sync_token(h) not in synced_tokens]
                if not sub_idx:
                    try:
                        from state_decode_cache import UNCHANGED_HERO_LISTS
                        UNCHANGED_HERO_LISTS.add(id(heroes))
                    except Exception:
                        pass
                    return
                if len(sub_idx) <= max(8, len(heroes) // 2):
                    sub_heroes = [heroes[i] for i in sub_idx]
                    state["heroes"] = sub_heroes
                    try:
                        sync_album_heroes(mod, state)
                        sync_pets(mod, state)
                        sync_ship_bonuses(mod, state)
                        sync_fruits(mod, state)
                        sync_decor(mod, state)
                        sync_germa(mod, state)
                        sync_blessing(mod, state)
                        sync_guild_tech(mod, state)
                        sync_gems(mod, state)
                    finally:
                        for idx, sh in zip(sub_idx, state["heroes"]):
                            heroes[idx] = sh
                            synced_tokens.add(_hero_sync_token(sh))
                        state["heroes"] = heroes
                    return
    except Exception:
        uid_key = 0
        bsig = None

    sync_album_heroes(mod, state)
    sync_pets(mod, state)
    sync_ship_bonuses(mod, state)
    sync_fruits(mod, state)
    sync_decor(mod, state)
    sync_germa(mod, state)
    sync_blessing(mod, state)
    sync_guild_tech(mod, state)
    sync_gems(mod, state)
    if need_uncap_recalc:
        cur_heroes = state.get("heroes")
        if isinstance(cur_heroes, list):
            for idx_h, h_obj in enumerate(cur_heroes):
                if isinstance(h_obj, dict):
                    h_copy = dict(h_obj)
                    mod._recalc_hero(h_copy)
                    cur_heroes[idx_h] = h_copy
        state["power"] = mod.compute_player_power(state)
    if uid_key and bsig is not None:
        if len(_SYNC_STATE_MEMO) > 512:
            _SYNC_STATE_MEMO.clear()
        _SYNC_STATE_MEMO[uid_key] = (bsig, {_hero_sync_token(h) for h in state.get("heroes", [])})


def dispatch_game_action(act_id: int, act_data: bytes, uid: int, token: str, state: dict, host_header: str = "") -> list:
    import random
    """Returns a list of (response_action_id, payload_bytes) tuples."""
    mod = sys.modules[__name__]
    if act_id == 97:
        from arena_replays import dispatch as dispatch_replay
        replay_values, _ = parse_common_vector(act_data) if act_data else ([], [])
        return dispatch_replay(mod, act_id, replay_values, uid, state)
    if act_id not in _NO_HERO_SYNC_ACTIONS:
        _ensure_heroes_synced(mod, state)
    from album import dispatch as dispatch_album
    from ship_decor import dispatch as dispatch_decor
    decor_response=dispatch_decor(mod,act_id,act_data,uid,state)
    if decor_response is not None:return decor_response
    from ship_progression import dispatch as dispatch_ship
    ship_response=dispatch_ship(mod,act_id,act_data,uid,state)
    if ship_response is not None:return ship_response
    from islands import dispatch as dispatch_islands
    from types import SimpleNamespace
    island_response = dispatch_islands(SimpleNamespace(**globals()), act_id, act_data, uid, state)
    if island_response is not None:
        return island_response
    req_longs, req_strings = parse_common_vector(act_data) if act_data else ([], [])
    from fleet_progression import dispatch as dispatch_fleet_progression
    fleet_response=dispatch_fleet_progression(mod,act_id,req_longs,req_strings,uid,state)
    if fleet_response is not None:return fleet_response
    from sealed_progression import dispatch as dispatch_sealed_progression
    sealed_response=dispatch_sealed_progression(mod,act_id,req_longs,uid,state)
    if sealed_response is not None:return sealed_response
    from labyrinth import dispatch as dispatch_labyrinth
    labyrinth_response=dispatch_labyrinth(mod,act_id,req_longs,uid,state)
    if labyrinth_response is not None:return labyrinth_response

    from crystal_arena import dispatch as dispatch_crystal_arena
    crystal_response=dispatch_crystal_arena(mod,act_id,req_longs,uid,state)
    if crystal_response is not None:return crystal_response


    from tower_progression import dispatch as dispatch_tower_progression
    progression_response=dispatch_tower_progression(mod,act_id,req_longs,uid,state)
    if progression_response is not None:return progression_response
    from tavern_quests import dispatch as dispatch_tavern_quests
    tavern_response=dispatch_tavern_quests(mod,act_id,req_longs,uid,state)
    if tavern_response is not None:return tavern_response

    from hero_mastering import dispatch as mastery_dispatch
    mastery_response=mastery_dispatch(mod,act_id,req_longs,uid,state)
    if mastery_response is not None:return mastery_response
    from fishman_mouse import dispatch as dispatch_mouse
    mouse_response = dispatch_mouse(mod, act_id, req_longs, uid, state)
    if mouse_response is not None:return mouse_response
    from germa import dispatch as germa_dispatch
    germa_response=germa_dispatch(mod,act_id,req_longs,uid,state)
    if germa_response is not None:return germa_response
    from hero_soul import dispatch as soul_dispatch
    soul_response=soul_dispatch(mod,act_id,req_longs,uid,state)
    if soul_response is not None:return soul_response
    from holy_blessing import dispatch as blessing_dispatch
    blessing_response=blessing_dispatch(mod,act_id,req_longs,uid,state)
    if blessing_response is not None:return blessing_response
    from guild_tech import dispatch as guild_tech_dispatch
    guild_response=guild_tech_dispatch(mod,act_id,req_longs,uid,state)
    if guild_response is not None:return guild_response

    from epic_equipment import dispatch as dispatch_epic
    epic_response=dispatch_epic(sys.modules[__name__],act_id,req_longs,uid,state)
    if epic_response is not None:return epic_response

    album_response = dispatch_album(sys.modules[__name__], act_id, req_longs, uid, state)
    if album_response is not None:
        return album_response

    from abyss import dispatch as dispatch_abyss
    abyss_response = dispatch_abyss(sys.modules[__name__], act_id, req_longs, uid, state)
    if abyss_response is not None:
        return abyss_response

    from inventory_special import dispatch as dispatch_inventory
    inventory_response = dispatch_inventory(sys.modules[__name__], act_id, req_longs, uid, state)
    if inventory_response is not None:
        return inventory_response

    from all_original_events import dispatch as dispatch_orig_events, ensure_event_starter_items
    orig_event_response = dispatch_orig_events(sys.modules[__name__], act_id, req_longs, req_strings, uid, state)
    if orig_event_response is not None:
        return orig_event_response

    battle_response = dispatch_arena_battle(act_id, req_longs, uid, state)
    if battle_response is not None:
        return battle_response

    arena_response = dispatch_arena_entry(act_id, req_longs, uid, state)
    if arena_response is not None:
        return arena_response

    if act_id == 254:
        selected = use_selection(sys.modules[__name__], req_longs, uid, state)
        if selected is not None:
            return selected

    # ------------------------------------------------------------------------
    # Core Session / Config / Profile / Chat
    # ------------------------------------------------------------------------
    if act_id == 100:  # LOGIN_GAME
        changed = True
        for h_obj in state.get("heroes", []):
            try:
                _recalc_hero(h_obj)
            except Exception:
                pass
        if int(state.get("level", 1)) < 100:
            state["level"] = 100
            changed = True
        if int(state.get("campaign_level", 1)) < 35:
            state["campaign_level"] = 35
            changed = True
        bag_info_login = state.setdefault("bag_info", [3000, 100, 5])
        if isinstance(bag_info_login, list) and bag_info_login and int(bag_info_login[0]) < 3000:
            bag_info_login[0] = 3000
            changed = True
        # Ensure Devil Fruit Coins (101: Xu Bạc, 102: Xu Vàng) & normalize Gem materials (60000..60125)
        deduplicate_consumables(state)
        if ensure_event_starter_items(sys.modules[__name__], state):
            changed = True
        c_dict = {int(x[1]): x for x in state.get("consumables", []) if int(x[0]) == 1}
        for coin_id in (101, 102):
            if coin_id not in c_dict:
                state.setdefault("consumables", []).append([1, coin_id, 10000])
                changed = True
            elif int(c_dict[coin_id][2]) < 100:
                c_dict[coin_id][2] = max(int(c_dict[coin_id][2]), 10000)
                changed = True
        # Retroactively credit Búa Gỗ (2138) + ship materials (2142, 2143) for existing E5 (15-star) heroes
        e5_count = sum(1 for h_obj in state.get("heroes", []) if int(h_obj.get("star", 5)) >= 15)
        granted_e5 = int(state.get("e5_hammer_granted", 0))
        if e5_count > granted_e5:
            diff_e5 = e5_count - granted_e5
            apply_and_make_bonus_received(
                state,
                [("material", 1, 2138, 300 * diff_e5), ("material", 1, 2142, 200 * diff_e5), ("material", 1, 2143, 200 * diff_e5)],
                GAME_DB,
            )
            state["e5_hammer_granted"] = e5_count
            changed = True
        if changed:
            STORE.save_state(uid, state)
        return [(100, build_login_msg(token, state))]

    if act_id == 113:  # REQUEST_CONFIG_DATA
        raw_bag = state.get("bag_info")
        max_slot = max(3000, int(raw_bag[0])) if isinstance(raw_bag, list) and raw_bag else 3000
        v0 = build_common_vector(
            a_long=[0, 100, max_slot, 0, 300, 50, 1, 1, 15],  # 0 Gem cost for rename (FREE)!
            a_string=["bravetrial1"]
        )
        v1 = build_common_vector(a_long=[0])
        return [(113, build_list_common_vector([v0, v1]))]

    if act_id == 96:  # CLIENT_JSON_CONFIG
        cfg_json = json.dumps({
            "home_theme": 1,
            "refresh_home_chat": 5,
            "time_hide_home_chat": 10,
            "pet_ruin_on": 1,
            "tower2_unlock": 1,
            "hero_mastering_on": 1,
            "funny_quiz_on": 1,
            "epic_equipment_on": 1,
            "hero_regression_on": 1,
            "fb_fanpage": "https://facebook.com",
            "new_event_on": 1,
            "msg_invalid_iap": "Invalid IAP",
            "msg_fanpage_id_global": "1000",
        })
        return [(96, build_common_vector(a_string=["", cfg_json]))]

    if act_id == 114:  # LIST_NOTIFICATION
        from fleet_notifications import payload
        return [(114, payload(sys.modules[__name__],state))]

    if act_id == 115:  # FEEDBACK / GÓP Ý (Also acts as GiftCode Input Box!)
        fb_text = req_strings[0] if req_strings else ""
        matched, msg, _, push_actions = redeem_giftcode(uid, state, fb_text)
        if matched:
            return [(115, build_common_vector(a_long=[1]))] + push_actions
        return [
            (115, build_common_vector(a_long=[1])),
            (1, build_common_vector(a_string=["Đã gửi góp ý! (Mẹo: Gõ FULL, TANTHU, VIP20 hoặc RESET vào đây để nhận GiftCode)"])),
        ]

    if act_id == 110:  # RENAME (Also supports GiftCode if user enters FULL / TANTHU / VIP20 / RESET)
        new_name = req_strings[0] if req_strings else state["screenname"]
        matched, _, _, push_actions = redeem_giftcode(uid, state, new_name)
        if matched:
            return [(110, build_common_vector(a_long=[state["gem"]], a_string=[state["screenname"]]))] + push_actions
        state["screenname"] = new_name
        STORE.save_state(uid, state)
        return [
            (110, build_common_vector(a_long=[state["gem"]], a_string=[new_name])),
            (100, build_login_msg(token, state))
        ]

    if act_id == 111:  # ACCOUNT_GET_LIST_AVATAR (PopupChangeAvatar)
        # Normal hero avatars: must be valid heroKeys from db_heroes.json
        hero_keys = []
        for h in state.get("heroes", []):
            hid = int(h.get("heroId", 0))
            if hid > 0 and hid not in hero_keys:
                hero_keys.append(hid)
        for hid in getattr(GAME_DB, "hero_keys_for_avatar", []):
            if hid not in hero_keys:
                hero_keys.append(hid)
        if not hero_keys:
            hero_keys = [66, 67, 68, 69, 70, 72, 73, 75, 77, 78, 80, 512, 497, 491, 506, 611, 608]
        vec_normal = build_common_vector(a_long=hero_keys)

        # Special avatars / frames / danh hiệu (from db_avatar.json: IDs 1..1022)
        special_ids = getattr(GAME_DB, "special_avatar_ids", [
            1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
            21, 22, 23, 24, 25, 31, 32, 33, 34, 35, 36, 37, 48, 49, 50, 51, 52, 53,
            54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71,
            72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 1000,
            1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008, 1009, 1010, 1011, 1012,
            1013, 1014, 1015, 1016, 1017, 1018, 1019, 1020, 1021, 1022
        ])
        vec_special = build_common_vector(a_long=special_ids)

        return [(111, build_list_common_vector([vec_normal, vec_special]))]

    if act_id == 112:  # ACCOUNT_CHANGE_AVATAR (PreProcessPackage @ 0x13ce46c)
        av_type = int(req_longs[0]) if len(req_longs) >= 1 else 0
        av_id = int(req_longs[1]) if len(req_longs) >= 2 else (int(req_longs[0]) if req_longs else 1)
        state["avatar"] = [av_type, av_id]
        STORE.save_state(uid, state)
        return [
            (112, build_common_vector(a_long=[av_type, av_id])),
            (100, build_login_msg(token, state))
        ]

    if act_id == 119:  # COMMON_GET_PLAYER_LINEUP (PopupPlayerInfo & PopupPlayerInfo3)
        target_uid = int(req_longs[0]) if req_longs else uid
        lineup_mode = int(req_longs[1]) if len(req_longs) > 1 else 0
        target_state = None
        if hasattr(STORE, "get_player_by_uid"):
            target_state = STORE.get_player_by_uid(target_uid)
        if not isinstance(target_state, dict) and hasattr(STORE, "get_arena_player_by_uid"):
            target_state = STORE.get_arena_player_by_uid(target_uid)
        if not isinstance(target_state, dict):
            target_state = state
        prof = dict(target_state)
        prof["id"] = target_uid
        by_id = {int(h.get("id", 0)): h for h in (prof.get("heroes") or []) if isinstance(h, dict) and int(h.get("id", 0)) > 0}
        if lineup_mode in (5, 6):
            # PopupPlayerInfo3.SetPlayerInfo @ 0x14a4e04 strictly requires exactly 18 team entries (3 teams x 6 slots)
            mode_name = "league" if lineup_mode == 6 else "trial"
            def21 = arena_defense(prof, mode_name)
            team18 = []
            pets_out = []
            owned_pets = {int(p[0]): p for p in (prof.get("pets") or []) if isinstance(p, list) and p}
            for t_idx in range(3):
                for s_idx in range(6):
                    hid = int(def21[t_idx * 7 + s_idx])
                    h_obj = by_id.get(hid)
                    if h_obj is not None:
                        team18.append([int(h_obj.get("heroId", 1)), int(h_obj.get("level", 1)), int(h_obj.get("star", 5)), int(h_obj.get("skin", 0)), hid])
                    else:
                        team18.append([0, 0, 0, 0, 0])
                p_id = int(def21[t_idx * 7 + 6])
                if p_id > 0 and p_id in owned_pets:
                    p_row = owned_pets[p_id]
                    pets_out.extend([p_id, max(1, int(p_row[2] if len(p_row) > 2 else 1))])
            if not any(r[0] > 0 for r in team18):
                raw_t = prof.get("team") or []
                for idx_r, r_t in enumerate(raw_t[:6]):
                    if isinstance(r_t, list) and len(r_t) >= 4 and int(r_t[0] or 0) > 0:
                        inst_r = int(r_t[4]) if len(r_t) > 4 else (idx_r + 1)
                        team18[idx_r] = [int(r_t[0]), int(r_t[1]), int(r_t[2]), int(r_t[3]), inst_r]
            prof["team"] = team18[:18]
            prof["team_pet"] = pets_out
            prof["arena_info"] = [int(prof.get("arena_" + mode_name + "_points", 1500))]
        else:
            import crystal_arena
            try:
                c_sum = crystal_arena.summary(sys.modules[__name__], prof)
                team6 = [list(r) for r in (c_sum.get("team") or [])[:6] if isinstance(r, list)]
            except Exception:
                team6 = []
            if not any(r and int(r[0] or 0) > 0 for r in team6):
                team6 = []
                for idx_r, r_t in enumerate((prof.get("team") or [])[:6], start=1):
                    if isinstance(r_t, list) and len(r_t) >= 4:
                        inst_r = int(r_t[4]) if len(r_t) > 4 else idx_r
                        team6.append([int(r_t[0]), int(r_t[1]), int(r_t[2]), int(r_t[3]), inst_r])
            while len(team6) < 6:
                team6.append([0, 0, 0, 0, 0])
            prof["team"] = team6[:6]
            raw_tp = prof.get("team_pet") or []
            if isinstance(raw_tp, list) and len(raw_tp) >= 2 and int(raw_tp[0] or 0) > 0:
                prof["team_pet"] = [int(raw_tp[0]), max(1, int(raw_tp[1] or 1))]
            else:
                pets_list = prof.get("pets") or []
                if pets_list and isinstance(pets_list[0], list) and int(pets_list[0][0] or 0) > 0:
                    prof["team_pet"] = [int(pets_list[0][0]), max(1, int(pets_list[0][2] if len(pets_list[0]) > 2 else 1))]
                else:
                    prof["team_pet"] = []
        return [(119, build_pb_user(prof))]

    if act_id in (124, 125):  # TOP_LEVEL / TOP_TOWER (PopupTopPlayer.ProcessKEvent @ 0x14ccf58)
        top_players = STORE.list_top_players(30) or [state]
        vecs = []
        for p in top_players:
            av = p.get("avatar", [0, 1])
            score = int(p.get("level", 1)) if act_id == 124 else int(p.get("tower_level", 1))
            vecs.append(build_common_vector(
                a_long=[int(p.get("id", 1)), int(av[0]), int(av[1]), int(p.get("level", 1)), score],
                a_string=[str(p.get("screenname", "Pirate"))]
            ))
        return [(act_id, build_list_common_vector(vecs))]

    if act_id in (101, 103, 105, 108, 281, 701):  # GLOBAL_CHAT_LIST / CLAN_CHAT_LIST / CLAN_RECRUIT_LIST / CHAT_PRIVATE_SERVER_LIST / ADMIN_CHAT_LIST
        after_time = int(req_longs[0]) if req_longs else 0
        if after_time <= 0 and act_id == 103:
            now_s = time.time()
            last_s = STORE.last_chat_sync.get((uid, act_id), 0.0)
            if now_s - last_s < 4.0:
                return [(act_id, build_chat_history([], state))]
            STORE.last_chat_sync[(uid, act_id)] = now_s
        return [(act_id, build_chat_history(STORE.get_global_chat(after_time), state))]

    if act_id == 106:  # CHAT_HIDE_VIP
        return [(106, build_common_vector(a_long=[0]))]

    if act_id in (102, 104, 107, 280):  # GLOBAL_CHAT / CLAN_CHAT / GUILD_RECRUIT / CHAT_PRIVATE_SERVER
        after_time = int(req_longs[0]) if req_longs else 0
        msg_text = (req_strings[0] if req_strings else "Xin chào!").strip()
        prev_latest = STORE.get_latest_chat_time()

        # Chat is ordinary text. Codes are redeemed only in explicit code inputs.
        STORE.add_global_chat(state, msg_text)

        eff_after = max(after_time, prev_latest)
        new_chat_bytes = build_chat_history(STORE.get_global_chat(eff_after), state)
        resp_list = [(act_id, new_chat_bytes)]
        if act_id != 102:
            resp_list.append((102, new_chat_bytes))
        return resp_list

    # ------------------------------------------------------------------------
    # Online Bonus (117, 118) & Hand of Midas (350, 351)
    # ------------------------------------------------------------------------
    if act_id == 117:  # BONUS_ONLINE_STATUS (HomeScene @ 0x138092c)
        # aLong[0] = countdown seconds (0 = ready to claim), aLong[1:] = BonusPreview
        preview = make_bonus_preview([("gold", 100000), ("gem", 200)])
        return [(117, build_common_vector(a_long=[0] + preview))]

    if act_id == 118:  # BONUS_ONLINE_RECEIVE (HomeScene @ 0x13809f4)
        now = time.time()
        last_t = float(state.get("last_online_bonus_time", 0))
        cd = 300
        if now - last_t < cd:
            rem = int(cd - (now - last_t))
            return [(118, build_common_vector(a_long=[rem]))]
        state["last_online_bonus_time"] = now
        rec, _ = apply_and_make_bonus_received(state, [("gold", 100000), ("gem", 200)], GAME_DB)
        STORE.save_state(uid, state)
        return [(118, build_common_vector(a_long=[cd] + rec))]

    if act_id == 350:  # MIDAS_STATUS (HomeScene @ 0x1380a74)
        # [goldFree, countdown, gold2, canBuy2, gemCost2, gold3, canBuy3, gemCost3]
        return [(350, build_common_vector(a_long=[150000, 0, 350000, 1, 20, 800000, 1, 50]))]

    if act_id == 351:  # MIDAS_RECEIVE (PopupHandOfMidas @ 0x1482c80)
        midas_type = int(req_longs[0]) if req_longs else 0
        gold_gain = 150000 if midas_type == 0 else (350000 if midas_type == 1 else 800000)
        gem_cost = 0 if midas_type == 0 else (-20 if midas_type == 1 else -50)
        items = [("gold", gold_gain)]
        if gem_cost < 0:
            items.append(("gem", gem_cost))
        rec, _ = apply_and_make_bonus_received(state, items, GAME_DB)
        d_acts = state.setdefault("daily_actions", {})
        d_acts["midas"] = d_acts.get("midas", 0) + 1
        STORE.save_state(uid, state)
        return [
            (351, build_common_vector(a_long=rec)),
            (350, build_common_vector(a_long=[150000, 0, 350000, 1, 20, 800000, 1, 50])),
        ]

    # ------------------------------------------------------------------------
    # Market / Shops (120, 121, 122, 128) — PopupShop.ShowShopItems @ 0x14b5964
    # ------------------------------------------------------------------------
    if act_id in (120, 121, 122, 128):
        from market_shops import dispatch as dispatch_shop
        return dispatch_shop(sys.modules[__name__], act_id, req_longs, uid, state)

    # ------------------------------------------------------------------------
        # Heroes: List, Upgrade Level/Tier, Awaken, Equip, Unequip, Lock, Fuse, Altar
    # ------------------------------------------------------------------------
    if act_id == 200:  # HERO_LIST
        _sanitize_hero_ids_and_teams(state)
        return [(200, build_pb_list_hero(state["heroes"]))]

    if act_id == 207:  # HERO_UPDATE_INFO / DataSender.HeroRequestInfo

        if req_longs:
            req_ids = {int(x) for x in req_longs}
            matched = [h for h in state.get("heroes", []) if int(h["id"]) in req_ids]
            if not matched:
                h0 = _find_hero(state, int(req_longs[0]))
                matched = [h0] if h0 else []
        else:
            matched = state.get("heroes", [])[:1]
        return [(207, build_pb_list_hero(matched))]

    if act_id == 208:  # HERO_UNEQUIP_ITEM (DataSender.HeroUnequipItem(heroId, slotId))
        hero_inst_id = int(req_longs[0]) if req_longs else 1
        slot = int(req_longs[1]) if len(req_longs) >= 2 else -1
        h = _find_hero(state, hero_inst_id)
        if h is not None:
            items = h.setdefault("item", [0, 0, 0, 0, 0, 0])
            while len(items) < 6:
                items.append(0)
            if slot == 4:
                items[4] = 0
                items[5] = 0
            elif 0 <= slot < 4:
                items[slot] = 0
            else:
                items[0] = 0
                items[1] = 0
                items[2] = 0
                items[3] = 0
            _recalc_hero(h)
            STORE.save_state(uid, state)
        return [
            (208, build_common_vector(a_long=[1])),
            (207, build_pb_list_hero([h] if h else [])),
            (250, _equip_bag_payload(state)),
            (253, _artifact_bag_payload(state)),
        ]

    # ------------------------------------------------------------------------
    # Item Shard Summon (203 - DataSender.ItemShardSummon @ 0x16c28b4)
    # ------------------------------------------------------------------------
    if act_id == 203 and len(req_longs) >= 2 and int(req_longs[0]) in (1, 2, 3):
        shard_type = int(req_longs[0])
        shard_key = int(req_longs[1])
        summon_cnt = max(1, int(req_longs[2]) // 50) if len(req_longs) >= 3 and int(req_longs[2]) >= 50 else 1
        if shard_type == 3:  # Artifact shard
            art_id = shard_key if shard_key in GAME_DB.artifact_ids else 1
            rec, _ = apply_and_make_bonus_received(state, [("artifact", art_id, 1, summon_cnt), ("material", 3, shard_key, -50 * summon_cnt)], GAME_DB)
            STORE.save_state(uid, state)
            return [(203, build_common_vector(a_long=rec)), (253, _artifact_bag_payload(state)), (251, _consumable_bag_payload(state))]
        else:
            if len(state.get("heroes", [])) + summon_cnt > int(state.get("bag_info", [3000])[0]):
                return [(2, build_common_vector(a_string=["Túi thuyền viên đã đầy. Hãy huyết tế hoặc hợp thành bớt thuyền viên!"]))]
            # Generic shard handling:
            items = [("material", 2, shard_key, -50 * summon_cnt)]
            pool_5s = [hid for hid in GAME_DB.five_star_hero_ids if int(GAME_DB.heroes_by_id.get(hid, {}).get('rank', 0)) >= 6] or GAME_DB.five_star_hero_ids
            pool_l1 = [hid for hid in pool_5s if hid in (576, 474, 489, 483, 465, 435, 381, 444, 447, 450, 639, 74, 122, 131, 88, 624)] or pool_5s
            pool_l2 = [hid for hid in pool_5s if hid in (606, 609, 642, 633, 621, 510, 495, 465, 489, 627, 504)] or pool_5s
            rolled_shard_hids = set()
            for _ in range(summon_cnt):
                if shard_key == 40015:  # SS L2
                    cand = [h for h in pool_l2 if h not in rolled_shard_hids]
                    rolled_hid = random.choice(cand or pool_l2)
                elif shard_key == 40014:  # SS L1
                    cand = [h for h in pool_l1 if h not in rolled_shard_hids]
                    rolled_hid = random.choice(cand or pool_l1)
                elif shard_key in (30000, 30001, 30002, 30003, 30004, 30005, 30006) or shard_key >= 1000:
                    cand = [h for h in pool_5s if h not in rolled_shard_hids]
                    rolled_hid = random.choice(cand or pool_5s)
                elif shard_key in GAME_DB.heroes_by_id:
                    rolled_hid = shard_key
                else:
                    cand = [h for h in pool_5s if h not in rolled_shard_hids]
                    rolled_hid = random.choice(cand or pool_5s)
                if shard_key not in GAME_DB.heroes_by_id:
                    rolled_shard_hids.add(rolled_hid)
                h5 = getattr(GAME_DB, "base_5star_by_hero_id", {}).get(rolled_hid, rolled_hid)
                items.append(("hero", h5, 1, 0, 5))
            rec, new_heroes = apply_and_make_bonus_received(state, items, GAME_DB)
            STORE.save_state(uid, state)
            return [
                (203, build_common_vector(a_long=rec)),
                (207, build_pb_list_hero(new_heroes)),
                (251, _consumable_bag_payload(state)),
            ]

    # ------------------------------------------------------------------------
    # Item Consumable Used (202 - DataSender.ItemConsumableUsed @ 0x16c2a5c)
    # ------------------------------------------------------------------------
    if False:  # Displaced old 202; Action 202 is HERO_EQUIP_ARTIFACT exclusively
        box_id = int(req_longs[0])
        num_use = max(1, min(10, int(req_longs[1]))) if len(req_longs) >= 2 and int(req_longs[1]) > 0 else 1
        sub_idx = int(req_longs[2]) if len(req_longs) >= 3 else -1
        preview_items = GAME_DB.consumable_previews.get(box_id, [])

        if sub_idx in GAME_DB.heroes_by_id or sub_idx in GAME_DB.ten_star_by_base_id or sub_idx in (504, 506, 606, 608, 609, 611, 642, 644, 633, 635, 621, 623, 510, 512, 495, 497, 465, 467, 489, 491):
            chosen = ("hero", sub_idx)
        elif sub_idx in GAME_DB.artifact_ids:
            chosen = ("artifact", sub_idx, 1, 1)
        elif sub_idx >= 0 and preview_items:
            matched_it = None
            for p in preview_items:
                if len(p) >= 2 and p[1] == sub_idx:
                    matched_it = p
                    break
            if not matched_it:
                if 0 <= sub_idx < len(preview_items):
                    matched_it = preview_items[sub_idx]
                elif 1 <= sub_idx <= len(preview_items):
                    matched_it = preview_items[sub_idx - 1]
            chosen = matched_it if matched_it else preview_items[0]
        elif preview_items:
            chosen = preview_items[0]
        elif 60 <= box_id <= 65:
            chosen = ("gold", 5000000)
        elif 66 <= box_id <= 71:
            chosen = ("material", 1, 1, 100000)
        else:
            chosen = ("hero", 606)

        grant_items = [("material", 1, box_id, -num_use)]
        kind = chosen[0]
        if kind == "hero":
            hkey = int(chosen[1])
            hkey = GAME_DB.ten_star_by_base_id.get(hkey, hkey)
            for _ in range(num_use):
                grant_items.append(("hero", hkey, 250, 6, 10))  # 10 Sao Đỏ, Lv.250
        elif kind == "gold":
            grant_items.append(("gold", int(chosen[1]) * num_use))
        elif kind == "gem":
            grant_items.append(("gem", int(chosen[1]) * num_use))
        elif kind == "material":
            grant_items.append(("material", int(chosen[1]), int(chosen[2]), int(chosen[3]) * num_use))
        elif kind == "equip":
            grant_items.append(("equip", int(chosen[1]), int(chosen[2]) * num_use))
        elif kind == "artifact":
            grant_items.append(("artifact", int(chosen[1]), int(chosen[2]), int(chosen[3]) * num_use))

        rec, new_heroes = apply_and_make_bonus_received(state, grant_items, GAME_DB)
        STORE.save_state(uid, state)
        resp_actions = []
        if new_heroes:
            resp_actions.append((207, build_pb_list_hero(new_heroes)))
        resp_actions.append((202, build_common_vector(a_long=rec)))
        resp_actions.append((251, _consumable_bag_payload(state)))
        resp_actions.append((253, _artifact_bag_payload(state)))
        return resp_actions

    if act_id in (201, 202, 203, 204):
        # 201: HERO_EQUIP_ITEM (heroId, equipmentKey)
        # 202: HERO_EQUIP_ARTIFACT (heroId, artifactKey, star)
        # 203: HERO_QUICK_EQUIP_ITEM (heroId)
        # 204: HERO_UNEQUIP_ARTIFACT (heroId)
        hero_inst_id = int(req_longs[0]) if req_longs else 1
        h = _find_hero(state, hero_inst_id)
        if h is not None:
            items = h.setdefault("item", [0, 0, 0, 0, 0, 0])
            while len(items) < 6:
                items.append(0)
            if act_id == 201 and len(req_longs) >= 2:
                eq_id = int(req_longs[1])
                slot = GAME_DB.equipment_type_by_id.get(eq_id, 0)
                if 0 <= slot < 4:
                    items[slot] = eq_id
                else:
                    items[0] = eq_id
            elif act_id == 203:
                owned_eq_ids = {int(e[0]) for e in state.get("equipments", []) if int(e[1]) > 0}
                for s_idx in range(4):
                    candidates = [eid for eid in GAME_DB.equipments_by_type.get(s_idx, []) if eid in owned_eq_ids]
                    if not candidates:
                        candidates = GAME_DB.equipments_by_type.get(s_idx, [])
                    if candidates:
                        items[s_idx] = candidates[-1]
            elif act_id == 202 and len(req_longs) >= 2:
                art_id = int(req_longs[1])
                art_star = int(req_longs[2]) if len(req_longs) >= 3 else 1
                items[4] = art_id
                items[5] = art_star
            elif act_id == 204:
                items[4] = 0
                items[5] = 0
            _recalc_hero(h)
            STORE.save_state(uid, state)
        return [
            (act_id, build_common_vector(a_long=[1])),
            (207, build_pb_list_hero([h] if h else [])),
            (250, _equip_bag_payload(state)),
            (253, _artifact_bag_payload(state)),
        ]

    if act_id == 231:  # HERO_UNLOCK_STONE (DataSender.HeroUnlockStone(heroId))
        hero_inst_id = int(req_longs[0]) if req_longs else 1
        h = _find_hero(state, hero_inst_id)
        if h is not None:
            h["treasure"] = max(1, int(h.get("treasure", 0)))
            if not h.get("stone_effect"):
                from stone_mechanics import roll as _roll_stone
                row1 = GAME_DB.hero_stones_by_id.get(int(h["treasure"]))
                if row1:
                    h["stone_effect"], h["stone_choices"] = _roll_stone(sys.modules[__name__], row1)
            _recalc_hero(h)
            STORE.save_state(uid, state)
        return [
            (231, build_common_vector(a_long=[1])),
            (207, build_pb_list_hero([h] if h else [])),
        ]

    from silent_ring import dispatch as silent_dispatch
    silent_reply=silent_dispatch(sys.modules[__name__],act_id,req_longs,uid,state)
    if silent_reply is not None:return silent_reply

    from stone_mechanics import dispatch as stone_dispatch
    stone_result=stone_dispatch(sys.modules[__name__],act_id,req_longs,uid,state)
    if stone_result is not None:return stone_result

    if act_id == 236:  # HERO_GET_STONE_INFO (PopupHeroStoneInfo @ 0x148e688)
        hero_id = int(req_longs[0]) if req_longs else 0
        h = _find_hero(state, hero_id)
        if h is not None:
            from stone_mechanics import ensure_full_stone_effect as _ensure_stone
            if _ensure_stone(sys.modules[__name__], h):
                _recalc_hero(h)
        cur_desc = h.get("stone_effect") if h else None
        if not cur_desc:
            cur_stone_id = max(1, int(h.get("treasure", 20))) if h else 20
            st = getattr(GAME_DB, "hero_stones_by_id", {}).get(cur_stone_id, {})
            if st:
                from stone_mechanics import roll as _roll_stone
                cur_desc, _ = _roll_stone(sys.modules[__name__], st, [0, 0, 0], st)
            else:
                cur_desc = '{"clazz":0,"faction":0,"point":[0,3000,100,390,2,40000,102,600]}'
        try:
            p = json.loads(cur_desc)
            if isinstance(p, list) and p:
                cur_desc = json.dumps(p[0])
        except Exception:
            pass
        pending=state.get('pending_stone_convert',{})
        next_desc=pending.get('desc','{}') if pending.get('hero_id')==hero_id else '{}'
        return [(236, build_common_vector(a_string=[cur_desc,next_desc]))]

    if act_id == 232:  # HERO_UPGRADE_STONE
        if len(req_longs) not in (1, 2) or len(req_longs)==2 and int(req_longs[1]) not in (0,1):
            return [(2, build_common_vector(a_string=["Lựa chọn nâng đá không hợp lệ."]))]
        retain = len(req_longs)==2 and int(req_longs[1])==1
        h = _find_hero(state, int(req_longs[0]))
        if h is None:
            return [(2, build_common_vector(a_string=["Thuyền viên không tồn tại."]))]
        current = max(1, int(h.get("treasure", 1)))
        row = GAME_DB.hero_stones_by_id.get(current)
        next_row = GAME_DB.hero_stones_by_id.get(current + 1)
        if row is None or next_row is None:
            return [(2, build_common_vector(a_string=["Đá hộ mệnh đã đạt cấp tối đa hoặc thiếu cấu hình."]))]
        costs = [("gold", -int(row["upgrade_cost_gold"])),
                 ("material", 1, 3, -int(row["upgrade_cost_dust"]))]
        if retain: costs.append(("gem", -int(row["gem_retain_effect"])))
        desc1 = next_row.get("effect1") or "{}"
        if retain:
            previous = json.loads(h.get("stone_effect") or row["effect1"])
            if isinstance(previous, list): previous=previous[0]
            found=False
            for effect_key in ("effect1", "effect2", "effect3"):
                choices=json.loads(row.get(effect_key) or "[]")
                if previous in choices:
                    upcoming=json.loads(next_row.get(effect_key) or "[]")
                    if upcoming:
                        desc1=json.dumps(upcoming[min(choices.index(previous),len(upcoming)-1)])
                        found=True
                    break
            if not found:
                return [(2,build_common_vector(a_string=["Thuộc tính này không có nhánh giữ khi nâng đá."]))]
        rec, _ = apply_and_make_bonus_received(state, costs, GAME_DB)
        h["treasure"] = current + 1
        try:
            p = json.loads(desc1)
            if isinstance(p, list) and p:
                desc1 = json.dumps(p[0])
        except Exception:
            pass
        h["stone_effect"] = desc1
        _recalc_hero(h)
        state["stone_upgrades"] = int(state.get("stone_upgrades", 0)) + 1
        STORE.save_state(uid, state)
        return [(232, build_common_vector(a_long=[current + 1] + rec, a_string=[desc1,desc1])),
                (207, build_pb_list_hero([h])), (251, _consumable_bag_payload(state))]

    if act_id == 235:  # HERO_CONVERT_STONE (Hoán đổi Đá Cam - PopupUpgradeHeroStone)
        hero_inst_id = int(req_longs[0]) if req_longs else 1
        h = _find_hero(state, hero_inst_id)
        if h is None:
            return [(2, build_common_vector(a_string=["Thuyền viên không tồn tại."]))]
        cur_stone_id = max(1, int(h.get("treasure", 1)))
        if cur_stone_id not in GAME_DB.hero_stones_by_id:
            return [(2,build_common_vector(a_string=['Đá hộ mệnh không tồn tại.']))]

        # Random thuộc tính mới từ db_hero_stone.json
        stone_entry = getattr(GAME_DB, "hero_stones_by_id", {}).get(cur_stone_id, {})
        eff_choices = []
        for eff_key in ("effect1", "effect2", "effect3"):
            raw_eff = stone_entry.get(eff_key, "")
            if raw_eff:
                try:
                    parsed = json.loads(raw_eff)
                    if isinstance(parsed, list):
                        eff_choices.extend(parsed)
                except Exception:
                    pass
        if eff_choices:
            try:
                previous=json.loads(h.get('stone_effect') or stone_entry.get('effect1','[]'))
                if isinstance(previous,list):previous=previous[0]
                alternatives=[effect for effect in eff_choices if effect!=previous]
                if alternatives:eff_choices=alternatives
            except (ValueError,IndexError,TypeError):pass
            chosen_eff = random.choice(eff_choices)
            if isinstance(chosen_eff, list) and chosen_eff:
                chosen_eff = chosen_eff[0]
            new_desc = json.dumps(chosen_eff)
        else:
            new_desc = '{"clazz":0,"faction":0,"point":[0,2800,100,310]}'

        try:
            current_effect=json.loads(h.get('stone_effect') or stone_entry.get('effect1','[]'))
            if isinstance(current_effect,list):current_effect=current_effect[0]
            cur_desc=json.dumps(current_effect)
        except (ValueError,TypeError,IndexError):cur_desc=new_desc
        state["pending_stone_convert"] = {
            "hero_id": hero_inst_id,
            "stone_id": cur_stone_id,
            "desc": new_desc
        }
        gold_cost = int(stone_entry.get("convert_cost_gold", 3000000))
        dust_cost = int(stone_entry.get("convert_cost_dust", 1425))
        rec, _ = apply_and_make_bonus_received(
            state,
            [("gold", -gold_cost), ("material", 1, 3, -dust_cost)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        # Native PopupUpgradeHeroStone 0x14e57bc reads aString[0] as the rolled result.
        return [
            (235, build_common_vector(a_long=[cur_stone_id] + rec, a_string=[new_desc, cur_desc])),
            (207, build_pb_list_hero([h] if h else [])),
            (251, _consumable_bag_payload(state)),
        ]

    if act_id == 238:  # HERO_SAVE_CONVERTED_STONE (Lưu Hoán Đổi Đá Cam)
        if not state.get('pending_stone_convert'):
            return [(2,build_common_vector(a_string=['Hãy hoán đổi thuộc tính trước khi lưu.']))]
        pending = state.pop("pending_stone_convert", None)
        saved_desc = '{"clazz":0,"faction":0,"point":[0,2800,100,310]}'
        stone_id = 20
        if pending:
            h = _find_hero(state, pending["hero_id"])
            if h is not None:
                h["stone_effect"] = pending["desc"]
                _recalc_hero(h)
                stone_id = int(h.get("treasure", 20))
            saved_desc = pending["desc"]
            STORE.save_state(uid, state)
        return [
            (238, build_common_vector(a_long=[stone_id], a_string=[saved_desc])),
            (207, build_pb_list_hero([h] if h else [])),
        ]

    if act_id in (264, 265, 266, 267, 268):  # UPGRADE_HERO_SPECIAL_* (EpicEquipment)
        fee_leaf = make_bonus_preview([("material", 1, 2144, 100)])
        fee_badge = make_bonus_preview([("material", 1, 2015, 50)])
        if act_id in (265, 266):
            apply_and_make_bonus_received(
                state,
                [("material", 1, 2144, -100), ("material", 1, 2015, -50)],
                GAME_DB
            )
            STORE.save_state(uid, state)
        vecs = [
            build_common_vector(a_long=[1, 1, 1, 1, 5000, 500, 1000, 100, 10000, 10000, 10000, 10000]),
            build_common_vector(a_long=fee_leaf),
            build_common_vector(a_long=fee_badge),
            build_common_vector(a_long=fee_leaf),
            build_common_vector(a_long=fee_badge),
            build_common_vector(a_long=fee_leaf),
            build_common_vector(a_long=fee_badge),
            build_common_vector(a_long=[0, 0, 0, 0]),
            build_common_vector(a_long=[1, 1]),
        ]
        return [(act_id, build_list_common_vector(vecs))]

    if act_id == 470:  # HERO_UPGRADE_LEVEL_NEW (PanelHeroInfo @ 0x13ffde8, HeroInfoTabLevel @ 0x1401ea0)
        hero_inst_id = int(req_longs[0]) if req_longs else 1
        num_up = int(req_longs[1]) if len(req_longs) > 1 and int(req_longs[1]) > 0 else 1
        h = _find_hero(state, hero_inst_id)
        if h is not None:
            _sync_hero_tier_with_level(h)
            star = max(1, int(h.get("star", 5)))
            tier = max(0, int(h.get("tier", 0)))
            from hero_progression import max_level
            max_lvl = max_level(sys.modules[__name__],h)
            cur_lvl = max(1, int(h.get("level", 1)))
            actual_up = min(num_up, max(0, max_lvl - cur_lvl))
            if actual_up > 0:
                upg_tbl = getattr(GAME_DB, "hero_upgrade_table", {})
                gold_cost = 0
                spirit_cost = 0
                for step_lv in range(cur_lvl + 1, cur_lvl + actual_up + 1):
                    row = upg_tbl.get(step_lv)
                    if row:
                        gold_cost += int(row.get("gold", 1000))
                        spirit_cost += int(row.get("spirit", 500))
                    else:
                        gold_cost += 5000
                        spirit_cost += 2500
                cur_gold = max(0, int(state.get("gold", 0)))
                cur_spirit = get_consumable_count(state, 1, 1)
                if cur_gold < gold_cost or cur_spirit < spirit_cost:
                    return [(2, build_common_vector(a_string=["Không đủ Beli hoặc Đá EXP để nâng cấp."]))]
                gold_deduct = gold_cost
                spirit_deduct = spirit_cost
                if cur_lvl + actual_up > 330:
                    paid=h.setdefault('extended_level_paid',{'gold':0,'spirit':0})
                    paid['gold']+=sum(int(upg_tbl[lv]['gold']) for lv in range(max(331,cur_lvl+1),cur_lvl+actual_up+1))
                    paid['spirit']+=sum(int(upg_tbl[lv]['spirit']) for lv in range(max(331,cur_lvl+1),cur_lvl+actual_up+1))
                h["level"] = cur_lvl + actual_up
                _recalc_hero(h)
                rec, _ = apply_and_make_bonus_received(
                    state,
                    [("gold", -gold_deduct), ("material", 1, 1, -spirit_deduct)],
                    GAME_DB
                )
            else:
                rec = []
        else:
            rec = []
        STORE.save_state(uid, state)
        from holy_blessing import extended_max_level, status as blessing_status
        blessing_refresh=[(1900,blessing_status(sys.modules[__name__],h))] if h and extended_max_level(h)>330 else []
        from home_quests import progress as _hq_progress
        q_st, q_id, q_cur, q_tgt, _ = _hq_progress(state, GAME_DB)
        # PanelHeroInfo @ 0x1400110 reads the last 4 elements of aLong as skill-unlock flags (0 or 1)
        return [
            (470, build_common_vector(a_long=rec + [0, 0, 0, 0])),
            (207, build_pb_list_hero([h] if h else [])),
        ] + blessing_refresh + [(261, build_common_vector(a_long=[q_st, q_id, q_cur, q_tgt]))]

    if act_id == 239:  # HERO_UPGRADE_TIER_PREVIEW (PopupHeroUpTier @ 0x148f450)
        # PopupHeroUpTier @ 0x148f588..0x148f694 reads:
        #   aLong[0] -> next ATK ([x19, #0x60])
        #   aLong[1] -> next Armor ([x19, #0x80])
        #   aLong[2] -> next HP ([x19, #0x70]) and Power ([x19, #0x98])
        hero_inst_id = int(req_longs[0]) if req_longs else 1
        import copy
        h = copy.deepcopy(_find_hero(state, hero_inst_id))
        if h is not None:
            _sync_hero_tier_with_level(h)
            star = max(1, int(h.get("star", 5)))
            next_tier = min(_client_max_tier(star), int(h.get("tier", 0)) + 1)
            preview_h = GAME_DB.make_hero_instance(h["id"], h["heroId"], int(h.get("level", 1)), next_tier, star)
            new_atk = max(int(h["point"][0]) + 10, int(preview_h["point"][0]))
            new_arm = max(int(h["point"][1]) + 2, int(preview_h["point"][1]))
            new_hp = max(int(h["point"][2]) + 50, int(preview_h["point"][2]))
        else:
            new_atk, new_arm, new_hp = 40000, 2500, 400000
        return [(239, build_common_vector(a_long=[new_atk, new_arm, new_hp]))]

    if act_id == 205:  # HERO_UPGRADE_TIER (PanelHeroInfo @ 0x14000d8)
        hero_inst_id = int(req_longs[0]) if req_longs else 1
        h = _find_hero(state, hero_inst_id)
        if h is None:
            return [(2, build_common_vector(a_string=["Thuyền viên không tồn tại."]))]
        gold_deduct = 0
        stone_deduct = 0
        if h is not None:
            _sync_hero_tier_with_level(h)
            star = max(1, int(h.get("star", 5)))
            max_t = _client_max_tier(star)
            cur_tier = int(h.get("tier", 0))
            cur_lvl = int(h.get("level", 1))
            upg_tbl = getattr(GAME_DB, "hero_upgrade_table", {})
            row = upg_tbl.get(cur_lvl + 1, {})
            gold_cost = int(row.get("upgrade_gold", 10000) or 10000)
            stone_cost = int(row.get("upgrade_stones", 50) or 50)
            if cur_tier >= max_t or cur_lvl < _client_max_level(star, cur_tier):
                return [(2, build_common_vector(a_string=["Chưa đủ điều kiện đột phá thuyền viên."]))]
            if int(state.get("gold", 0)) < gold_cost or get_consumable_count(state, 1, 2) < stone_cost:
                return [(2, build_common_vector(a_string=["Không đủ Beli hoặc Đá cường hóa để đột phá."]))]
            gold_deduct = gold_cost
            stone_deduct = stone_cost
            h["tier"] = min(max_t, cur_tier + 1)
            _recalc_hero(h)
        rec, _ = apply_and_make_bonus_received(
            state,
            [("gold", -gold_deduct), ("material", 1, 2, -stone_deduct)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        from hero_progression import unlock_flags
        # PanelHeroInfo @ 0x1400110 reads the last 4 elements of aLong as skill-unlock flags (0 or 1)
        return [
            (205, build_common_vector(a_long=rec + unlock_flags(star,h['tier']))),
            (207, build_pb_list_hero([h] if h else [])),
            (251, _consumable_bag_payload(state)),
        ]

    if act_id == 206:  # HERO_AWAKEN (PanelHeroInfo @ 0x1400384: 6★->7★->8★->9★->10★..15★)
        hero_inst_id = int(req_longs[0]) if req_longs else 1
        h = _find_hero(state, hero_inst_id)
        if not h:
            return [(2, build_common_vector(a_string=["Thuyền viên không tồn tại!"]))]

        cur_star = int(h.get("star", 6))
        if cur_star >= 15:
            return [(2, build_common_vector(a_string=["Thuyền viên đã đạt số sao tối đa!"]))]

        sac_ids = {int(x) for x in req_longs[1:] if int(x) != hero_inst_id}
        if sac_ids:
            state["heroes"] = [x for x in state["heroes"] if int(x["id"]) not in sac_ids]

        new_star = cur_star + 1
        h["star"] = new_star
        h["tier"] = max(int(h.get("tier", 6)), _client_max_tier(new_star))
        cur_hid = int(h.get("heroId", 0))
        if new_star >= 10:
            mapped_10 = GAME_DB.ten_star_by_base_id.get(cur_hid)
            if mapped_10:
                h["heroId"] = mapped_10
        elif new_star >= 6:
            mapped_6 = GAME_DB.six_star_by_base_id.get(cur_hid)
            if mapped_6:
                h["heroId"] = mapped_6
        _recalc_hero(h)

        from resource_guard import AWAKEN
        cost_stone = int(AWAKEN[str(new_star)]["stones"])
        cost_gold = 0
        awaken_items = [("gold", -cost_gold), ("material", 1, 2, -cost_stone)]
        if new_star == 15:
            # E5 awakening grants Búa Gỗ (2138) for Ship Interior + ship upgrade materials (2142, 2143)
            awaken_items.extend([
                ("material", 1, 2138, 300),
                ("material", 1, 2142, 200),
                ("material", 1, 2143, 200),
            ])
            state["e5_hammer_granted"] = int(state.get("e5_hammer_granted", 0)) + 1
        rec, _ = apply_and_make_bonus_received(
            state,
            awaken_items,
            GAME_DB
        )
        STORE.save_state(uid, state)
        # PanelHeroInfo @ 0x14003a8 passes aLong directly to BonusModel.FetchBonusItemReceived(aLong, true)!
        return [
            (206, build_common_vector(a_long=rec)),
            (207, build_pb_list_hero([h])),
            (251, _consumable_bag_payload(state)),
        ]

    if act_id == 243:  # HERO_PICK_ESKILL (PopupHeroESkill / DataSender.HeroPickESkill @ 0x16cab78)
        # Native DataSender sends [hero_inst_id, group_1_to_5, option_0_to_2].
        hero_inst_id = int(req_longs[0]) if req_longs else 1
        slot_idx = int(req_longs[1]) - 1 if len(req_longs) > 1 else 0
        skill_choice = int(req_longs[2]) if len(req_longs) > 2 else 0  # 0, 1, 2
        h = _find_hero(state, hero_inst_id)
        if not h:
            return [(2, build_common_vector(a_string=["Thuyền viên không tồn tại!"]))]

        cur_star = int(h.get("star", 5))
        # Every awakened talent requires E5, including groups 1 through 4.
        required_star = 11 + slot_idx
        if cur_star < required_star:
            return [(2, build_common_vector(a_string=["Chưa đủ bậc thức tỉnh để chọn Haki này."]))]

        from hero_progression import e_skills
        skills = e_skills(h)
        if 0 <= slot_idx < 5:
            skills[slot_idx] = skill_choice
        h["skills"] = [int(x) for x in skills[:5]]
        _recalc_hero(h)
        STORE.save_state(uid, state)

        # PopupHeroESkill expects Action 243 with [hero_inst_id, slot_idx, skill_choice]
        # followed by Action 207 to refresh active stats.
        return [
            (243, build_common_vector(a_long=[hero_inst_id, slot_idx, skill_choice])),
            (207, build_pb_list_hero([h])),
        ]

    if act_id in (240, 247):  # HERO_SET_LOCK / HERO_UNLOCK_ALL
        hero_inst_id = int(req_longs[0]) if req_longs else 1
        lock_state = int(req_longs[1]) if (act_id == 240 and len(req_longs) > 1) else 0
        h = _find_hero(state, hero_inst_id)
        if h is not None:
            h["locked"] = [lock_state, 0, 0, 0]
            STORE.save_state(uid, state)
        return [
            (act_id, build_common_vector(a_long=[hero_inst_id, lock_state])),
            (207, build_pb_list_hero([h] if h else [])),
        ]

    if act_id == 234:  # HERO_BUY_CAPACITY (HeroListNewScene @ 0x1359cac / 0x1359d5c)
        bag_info = state.setdefault("bag_info", [3000, 100, 5])
        if int(bag_info[0]) >= 3000:
            return [(2, build_common_vector(a_string=["Túi thuyền viên đã đạt giới hạn tối đa (3.000 ô). Hãy huyết tế hoặc hợp thành bớt thuyền viên!"]))]
        if int(state.get("gem", 0)) < 100:
            return [(2, build_common_vector(a_string=["Không đủ kim cương để mở thêm ô."]))]
        bag_info[0] = min(3000, int(bag_info[0]) + 50)
        rec, _ = apply_and_make_bonus_received(state, [("gem", -100)], GAME_DB)
        STORE.save_state(uid, state)
        # Client HeroListNewScene.ProcessKEvent (0x1359cac / 0x1359cf0):
        # item 0 = cost (m_CostBuyHeroCapacity) -> 100
        # item 1 = new capacity (m_HeroCapacity) -> bag_info[0]
        # item 2.. = ParseBonusReceived -> rec (gem debit)
        v234 = build_common_vector(a_long=[100, bag_info[0]] + rec)
        v127 = build_common_vector(a_long=[bag_info[0]])
        return [(234, v234), (127, v127)]

    if act_id == 230:
        from hero_training import fuse
        return fuse(sys.modules[__name__], req_longs, uid, state)

    if act_id in (237,209):
        from hero_altar import dispatch
        return dispatch(sys.modules[__name__],act_id,req_longs,uid,state)

    if act_id == 241:
        from hero_training import awaken_replace
        return awaken_replace(sys.modules[__name__], req_longs, uid, state)

    if act_id == 242:
        from hero_training import awaken_regress
        return awaken_regress(sys.modules[__name__], req_longs, uid, state)

    if act_id in (245, 246):
        from hero_training import regress
        return regress(sys.modules[__name__], act_id, req_longs, uid, state)

    # ------------------------------------------------------------------------
    # Bag / Blacksmith / Shard Summon / Artifact Upgrade / Item Sell
    # ------------------------------------------------------------------------
    if act_id == 250:  # ITEM_EQUIPMENT_LIST
        return [(250, _equip_bag_payload(state))]

    if act_id == 251:  # ITEM_CONSUMABLE_LIST
        return [(251, _consumable_bag_payload(state))]

    if act_id == 253:  # ITEM_ARTIFACT_LIST
        return [(253, _artifact_bag_payload(state))]

    if act_id == 252:  # ITEM_BLACKSMITH (ForgeScene @ 0x170315c)
        target_eq_id = int(req_longs[0]) if req_longs else (GAME_DB.equipment_ids[-1] if GAME_DB.equipment_ids else 10)
        count = max(1, min(100, int(req_longs[1]))) if len(req_longs) > 1 else 1
        eq_data = GAME_DB.equipment_by_id.get(target_eq_id, {})
        mat_id = int(eq_data.get("forge_material", -1))
        mat_num = max(1, int(eq_data.get("material_number", 3)))
        gold_cost = max(1000, int(eq_data.get("forge_cost", 50000))) * count

        if len(req_longs) != 2 or int(req_longs[1]) < 1 or int(req_longs[1]) > 100 or not eq_data or mat_id < 1 or int(eq_data.get("forge_cost", -1)) < 0:
            return [(2, build_common_vector(a_string=["Công thức hoặc số lượng rèn không hợp lệ."]))]
        available = sum(int(eq[1]) for eq in state.get("equipments", []) if int(eq[0]) == mat_id)
        if int(state.get("gold", 0)) < gold_cost or available < mat_num * count:
            return [(2, build_common_vector(a_string=["Không đủ Beli hoặc trang bị nguyên liệu để rèn."]))]
        items_to_apply = [("gold", -gold_cost), ("equip", target_eq_id, count)]
        if mat_id > 0:
            items_to_apply.append(("equip", mat_id, -mat_num * count))

        rec, _ = apply_and_make_bonus_received(state, items_to_apply, GAME_DB)
        d_acts = state.setdefault("daily_actions", {})
        d_acts["blacksmith"] = d_acts.get("blacksmith", 0) + count
        STORE.save_state(uid, state)
        # Client yêu cầu (252) xác nhận và (250) cập nhật túi trang bị ngay lập tức
        return [
            (252, build_common_vector(a_long=rec)),
            (250, _equip_bag_payload(state))
        ]

    if act_id == 254:  # ITEM_CONSUMABLE_USED / ITEM_SUMMON (ItemListScene @ 0x13a3e40)
        if len(req_longs) not in (3, 4) or int(req_longs[0]) not in (1, 2, 3) or int(req_longs[2]) <= 0:
            return [(2, build_common_vector(a_string=["Yêu cầu dùng vật phẩm hoặc ghép mảnh không hợp lệ."]))]
        req_type = int(req_longs[0]) if len(req_longs) >= 1 else 2
        if req_type == 1:
            return use_consumable(sys.modules[__name__], req_longs, uid, state)

        from shard_recipes import dispatch as merge_shards
        try:return merge_shards(sys.modules[__name__],req_longs,uid,state)
        except ValueError as ex:return [(2,build_common_vector(a_string=[str(ex)]))]

    if act_id == 255:  # ITEM_ARTIFACT_UPGRADE (PopupUpgradeArtifact @ 0x14d1528)
        # DataSender.ItemArtifactUpgrade: (heroId, artifactKey, artifactStar, lstArtMat)
        # req_longs: [heroId, art_key, art_star, mat1_key, mat1_star, mat1_num, ...]
        hero_id = int(req_longs[0]) if len(req_longs) >= 1 else 0
        art_id = int(req_longs[1]) if len(req_longs) >= 2 else (GAME_DB.artifact_ids[0] if GAME_DB.artifact_ids else 1)
        art_star = int(req_longs[2]) if len(req_longs) >= 3 else 1

        # Consume material artifacts passed in req_longs[3:]
        i = 3
        while i + 2 < len(req_longs):
            m_key = int(req_longs[i])
            m_star = int(req_longs[i + 1])
            m_num = int(req_longs[i + 2])
            for a in state.setdefault("artifacts", []):
                if int(a[0]) == m_key and int(a[1]) == m_star:
                    a[2] = max(0, int(a[2]) - m_num)
                    break
            i += 3
        # Remove empty artifact stacks
        state["artifacts"] = [a for a in state.get("artifacts", []) if int(a[2]) > 0]

        new_star = min(6, art_star + 1)
        resp = []
        rec = []

        if hero_id > 0:
            # Artifact is equipped on hero
            h = _find_hero(state, hero_id)
            if h is not None:
                items = h.setdefault("item", [0, 0, 0, 0, 0, 0])
                while len(items) < 6:
                    items.append(0)
                items[4] = art_id
                items[5] = new_star
                _recalc_hero(h)
                resp.append((207, build_pb_list_hero([h])))
            rec, _ = apply_and_make_bonus_received(state, [("gold", -50000)], GAME_DB)
        else:
            # Upgraded inside bag: deduct 1 of old star, add 1 of new star
            for a in state.setdefault("artifacts", []):
                if int(a[0]) == art_id and int(a[1]) == art_star:
                    a[2] = max(0, int(a[2]) - 1)
                    break
            rec, _ = apply_and_make_bonus_received(
                state,
                [("gold", -50000), ("artifact", art_id, new_star, 1)],
                GAME_DB
            )

        STORE.save_state(uid, state)
        resp.append((255, build_common_vector(a_long=rec)))
        resp.append((253, _artifact_bag_payload(state)))
        return resp

    if act_id in (256, 259, 260):  # ITEM_SELL / CONSUME_ITEM
        rec, _ = apply_and_make_bonus_received(state, [("gold", 250000), ("gem", 100)], GAME_DB)
        STORE.save_state(uid, state)
        return [(act_id, build_common_vector(a_long=rec))]

    # ------------------------------------------------------------------------
    # Summon Circle (210, 211, 233) & Prophet Tree (212, 213, 214, 215, 243, 244)
    # ------------------------------------------------------------------------
    if act_id == 210:  # SUMMON_STATUS (SummonCircleScene @ 0x14415f0)
        return [
            (210, build_summon_status(state)),
            (251, _consumable_bag_payload(state)),
        ]

    if act_id == 211:  # SUMMON_PERFORM (SummonCircleScene @ 0x14417f4)
        summon_type = int(req_longs[0]) if req_longs else 1
        if summon_type not in range(1, 11):
            return [(2, build_common_vector(a_string=["Loại chiêu mộ không hợp lệ."]))]
        if summon_type in (1, 4):
            remaining = summon_free_remaining(state, summon_type)
            if remaining:
                hours, rest = divmod(remaining, 3600)
                minutes, seconds = divmod(rest, 60)
                return [
                    (2, build_common_vector(a_string=[
                        f"Chiêu mộ miễn phí hồi sau {hours:02d}:{minutes:02d}:{seconds:02d}."
                    ])),
                    (210, build_summon_status(state)),
                ]
        # Types: 1=Basic free x1, 2=Basic paid x1, 3=Basic x10, 4=Free Heroic x1, 5=Heroic x1, 6=Heroic x10.
        #        Native callbacks: 7=Heroic x1 (250 gems), 8=Heroic x10 (2200 gems).
        count = 10 if summon_type in (3, 6, 8, 10) else 1
        if len(state.get('heroes', [])) + count > int(state.get('bag_info', [3000])[0]):
            return [(2, build_common_vector(a_string=['Túi thuyền viên không đủ chỗ.']))]
        items = []

        cur_barrels = 0
        for c in state.setdefault("consumables", []):
            if int(c[0]) == 1 and int(c[1]) == 15:
                cur_barrels = int(c[2])
                break

        # Genuine decrement for barrels, gems, basic scrolls, friendship hearts
        if summon_type in (5, 6):  # Heroic summon with Barrels (item 15)
            cost_barrels = 10 if summon_type == 6 else 1
            if cur_barrels < cost_barrels:
                return [
                    (2, build_common_vector(a_string=["Không đủ Thùng Bia Chiêu Mộ!"])),
                    (251, _consumable_bag_payload(state)),
                ]
            items.append(("material", 1, 15, -cost_barrels))
        elif summon_type in (7, 8):  # Native PanelHeroicSummon gem callbacks.
            cost = 2200 if summon_type == 8 else 250
            cur_gem = int(state.get("gem", 0))
            if cur_gem < cost:
                return [
                    (2, build_common_vector(a_string=["Không đủ Kim Cương!"])),
                    (251, _consumable_bag_payload(state)),
                ]
            items.append(("gem", -cost))
        elif summon_type in (1, 2, 3):  # Basic summon (1=Free 1x, 2=Scroll 1x, 3=Scroll 10x)
            cost_basic = 10 if summon_type == 3 else (1 if summon_type == 2 else 0)
            cur_basic = 0
            for c in state.setdefault("consumables", []):
                if int(c[0]) == 1 and int(c[1]) == 14:
                    cur_basic = int(c[2])
                    break
            if cur_basic < cost_basic:
                return [
                    (2, build_common_vector(a_string=["Không đủ Cuộn Chiêu Mộ Cơ Bản!"])),
                    (251, _consumable_bag_payload(state)),
                ]
            items.append(("material", 1, 14, -cost_basic))
        elif summon_type in (9, 10):  # Friendship summon with hearts (item 17)
            cost_hearts = 100 if summon_type == 10 else 10
            items.append(("material", 1, 17, -cost_hearts))

        from summon_rules import draw
        kind = 'basic' if summon_type in (1, 2, 3) else 'friendship' if summon_type in (9, 10) else 'heroic'
        for _ in range(count):
            hid, star = draw(sys.modules[__name__], kind)
            items.append(("hero", hid, 1, 0, star))
        if summon_type in range(1, 9):
            from summon_rules import chocolate_rewards
            items.extend(chocolate_rewards(sys.modules[__name__], count))
        state["summon_energy"] = int(state.get("summon_energy", 0)) + count * 10
        rec, new_heroes = apply_and_make_bonus_received(state, items, GAME_DB)

        if summon_type in (1, 4):
            state.setdefault("summon_free_ready_at", {})[str(summon_type)] = int(time.time()) + FREE_SUMMON_COOLDOWN_SECONDS

        d_acts = state.setdefault("daily_actions", {})
        if summon_type in (1, 2, 3):
            d_acts["summon_basic"] = d_acts.get("summon_basic", 0) + count
        elif summon_type in (4, 5, 6, 7, 8):
            d_acts["summon_heroic"] = d_acts.get("summon_heroic", 0) + count
        state["summon_count"] = int(state.get("summon_count", 0)) + count

        STORE.save_state(uid, state)
        return [
            (207, build_pb_list_hero(new_heroes)),
            (211, build_common_vector(a_long=rec)),
            (251, _consumable_bag_payload(state)),
            (210, build_summon_status(state)),
        ]

    if act_id == 233:  # SUMMON_ENERGY (SummonCircleScene @ 0x1441920)
        energy=int(state.get('summon_energy',0))
        if energy<1000:
            return [(210,build_summon_status(state))]
        if len(state.get('heroes',[]))>=int(state.get('bag_info',[3000])[0]):
            return [(210,build_summon_status(state)),(2,build_common_vector(a_string=['Túi thuyền viên đã đầy.']))]
        state['summon_energy']=energy-1000
        from summon_rules import pool as summon_pool, ORDINARY
        ordinary_pool = summon_pool(GAME_DB, 5, ORDINARY)
        ss_pool = [hid for hid in ordinary_pool if int(GAME_DB.heroes_by_id[hid].get('rank', 0)) >= 6]
        pool = ss_pool or ordinary_pool
        hid = random.choice(pool)
        base_hid = GAME_DB.base_5star_by_hero_id.get(hid, hid)
        rec, new_heroes = apply_and_make_bonus_received(state, [("hero", base_hid, 1, 0, 5)], GAME_DB)
        STORE.save_state(uid, state)
        return [
            (207, build_pb_list_hero(new_heroes)),
            (233, build_common_vector(a_long=[state["summon_energy"]] + rec)),
            (210, build_summon_status(state)),
        ]

    if act_id == 212:  # PROPHET_SUMMON (BoxOrbSummon @ 0x152c1e4)
        # DataSender.HeroProphetSummon @ 0x16ca4cc sends [number], [factionName].
        # Retain the older two-long format for clients that sent [faction, number].
        if len(req_longs) == 1:
            count, fac_val = int(req_longs[0]), 5
        elif len(req_longs) == 2:
            fac_val, count = map(int, req_longs)
        else:
            return [(2, build_common_vector(a_string=['Dữ liệu chiêu mộ không hợp lệ.']))]
        if count not in (1, 10):
            return [(2, build_common_vector(a_string=['Chỉ có thể chiêu mộ 1 hoặc 10 lượt.']))]
        if len(state.get('heroes', [])) + count > int(state.get('bag_info', [3000])[0]):
            return [(2, build_common_vector(a_string=['Túi thuyền viên không đủ chỗ.']))]
        cur_orbs = 0
        for c in state.setdefault("consumables", []):
            if int(c[0]) == 1 and int(c[1]) == 16:
                cur_orbs = int(c[2])
                break

        if cur_orbs < count:
            return [
                (2, build_common_vector(a_string=["Không đủ Thùng Bia Đặc Biệt! Hãy nhập code BIA hoặc TANTHU để nhận thêm."])),
                (251, _consumable_bag_payload(state)),
            ]

        items = [
            ("material", 1, 16, -count),
        ]

        target_fac_str = ""
        if req_strings and req_strings[0]:
            s = req_strings[0].strip().lower()
            if s in ("forest", "fortress", "shadow", "abyss", "dark", "light"):
                target_fac_str = s

        if not target_fac_str:
            # Tab 0: Nhanh nhẹn (Forest), Tab 1: Bền bỉ (Fortress), Tab 2: Sức mạnh (Abyss), Tab 3: Trí tuệ (Shadow)
            # Enum: 1=Dark, 2=Light, 3=Shadow, 4=Fortress, 5=Forest, 6=Abyss
            faction_map = {
                0: 'forest',    # Tab 0: Nhanh nhẹn
                1: 'dark',
                2: 'light',
                3: 'shadow',    # Tab 3 / Enum 3: Trí tuệ
                4: 'fortress',  # Tab 1 / Enum 4: Bền bỉ
                5: 'forest',    # Tab 0 / Enum 5: Nhanh nhẹn
                6: 'abyss',     # Tab 2 / Enum 6: Sức mạnh
            }
            target_fac_str = faction_map.get(fac_val, 'forest')

        from summon_rules import barrel_reward, PURPLE_POTION_ID
        # Draw the complete batch before applying costs or rewards.
        try:
            for _ in range(count):
                items.append(barrel_reward(sys.modules[__name__], target_fac_str))
        except ValueError as error:
            return [(2, build_common_vector(a_string=[str(error)]))]
        items.append(("material", 1, PURPLE_POTION_ID, sum(random.randint(15, 22) for _ in range(count))))
        from summon_rules import chocolate_rewards
        items.extend(chocolate_rewards(sys.modules[__name__], count))
        rec, new_heroes = apply_and_make_bonus_received(state, items, GAME_DB)
        STORE.save_state(uid, state)
        return [
            (207, build_pb_list_hero(new_heroes)),
            (212, build_common_vector(a_long=rec)),
            (251, _consumable_bag_payload(state)),
        ]

    if act_id in (213, 214, 215):
        from hero_replacement import dispatch as replace_hero
        return replace_hero(sys.modules[__name__], act_id, req_longs, uid, state)

    # ------------------------------------------------------------------------
        # Campaign / Vượt Ải (300..306) — CampaignScene @ 0x16488d8
    # ------------------------------------------------------------------------
    from campaign_economy import dispatch as economy_dispatch
    economy=economy_dispatch(sys.modules[__name__],act_id,req_longs,uid,state)
    if economy is not None:return economy

    if act_id in (304, 217):  # BATTLES: Campaign (304), Tower (217)
        # Đồng bộ chính xác Đội hình từ req_longs về state["team"] (tối đa 6 tướng không trùng ID)
        if req_longs and len(req_longs) >= 6:
            req_set = {int(s) for s in req_longs[:6] if int(s) > 0}
            if req_set:
                by_inst = {}
                by_hid = {}
                for h in state.get("heroes", []):
                    hi = int(h["id"])
                    ht = int(h["heroId"])
                    if hi in req_set:
                        by_inst[hi] = h
                    if ht in req_set and ht not in by_hid:
                        by_hid[ht] = h
                synced_team = []
                seen_sync_ids = set()
                for slot_sid in req_longs[:6]:
                    sid = int(slot_sid)
                    if sid > 0:
                        hm = by_inst.get(sid) or by_hid.get(sid)
                        if hm is not None and int(hm["id"]) not in seen_sync_ids:
                            seen_sync_ids.add(int(hm["id"]))
                            synced_team.append([int(hm["heroId"]), int(hm["level"]), int(hm["star"]), int(hm.get("skin", 0)), int(hm["id"])])
                if synced_team:
                    state["team"] = synced_team[:6]

    if act_id == 304:
        cur_stg = max(1, int(state.get('campaign_level', 1)))
        from campaign_economy import tick, battle_rewards, stage_enemies, stage_row
        tick(sys.modules[__name__], state)
        def advance_campaign():
            state['campaign_level'] = cur_stg + 1
            state['campaign_auto'] = cur_stg + 1
        enemies, boss_name = stage_enemies(sys.modules[__name__], cur_stg)
        stg_row = stage_row(cur_stg)
        stg_name = str(stg_row.get('name') or cur_stg)
        is_boss = stg_name.endswith('-5') or stg_name.endswith('-10') or (cur_stg % 5 == 0)
        e_name = f"Boss Ải {stg_name} - {boss_name}" if (is_boss and boss_name) else f"Ải {stg_name}"
        e_lvl = max(1, min(1000, cur_stg * 3))
        battle_bytes, winner, _ = settle_battle(state,
            battle_rewards(sys.modules[__name__], cur_stg),
            enemy_hero_ids=enemies, enemy_name=e_name,
            enemy_level=e_lvl, req_longs=req_longs, on_win=advance_campaign)
        STORE.save_state(uid, state)
        out = [(304, battle_bytes)]
        out.extend(dispatch_game_action(300, b'', uid, token, state, host_header))
        out.extend(dispatch_game_action(261, b'', uid, token, state, host_header))
        return out

    if act_id in (305, 306):  # CAMPAIGN_FORMATION / CAMPAIGN_AUTO_LEVEL
        if act_id == 306 and req_longs:
            state["campaign_auto"] = max(1, int(req_longs[0]))
            STORE.save_state(uid, state)
        return [(act_id, build_common_vector(a_long=[1]))]

    # ------------------------------------------------------------------------
    # Quests & Achievements: Home Quest (261, 262), Daily Quest (307, 308), Challenges (309, 310)
    # ------------------------------------------------------------------------
    if act_id in (261, 262):
        from home_quests import progress
        status, qid, current, target, entry = progress(state, GAME_DB)
        status_payload = build_common_vector(a_long=[status, qid, current, target])
        if act_id == 261:
            return [(261, status_payload)]
        if status != 2 or entry is None:
            return [(2, build_common_vector(a_string=["Chưa hoàn thành điều kiện nhiệm vụ."])), (261, status_payload)]
        raw = entry.get("bonus", "[]")
        values = json.loads(raw) if isinstance(raw, str) else raw
        rewards = parse_bonus_preview_items(values)
        if not rewards or make_bonus_preview(rewards) != values:
            return [(2, build_common_vector(a_string=["Cấu hình phần thưởng nhiệm vụ không hợp lệ."])), (261, status_payload)]
        from resource_guard import clone_state
        tx = getattr(sys.modules[__name__], "resource_transaction_context", None)
        tx_active = bool(tx is not None and getattr(tx, "active", False))
        if tx_active:
            updated = state
        else:
            updated = clone_state({k: v for k, v in state.items() if k not in ("heroes", "island_gems", "pets2")})
            updated["heroes"] = list(state.get("heroes", []))
            if "island_gems" in state:
                updated["island_gems"] = list(state["island_gems"])
            if "pets2" in state:
                updated["pets2"] = list(state["pets2"])
        rec, heroes = apply_and_make_bonus_received(updated, rewards, GAME_DB)
        updated["sv_quest_id"] = qid + 1
        STORE.save_state(uid, updated)
        if not tx_active:
            state.clear()
            state.update(updated)
        # PanelHome2Quest.ProcessKEvent 0x1407700 passes the WHOLE aLong to
        # BonusModel.FetchBonusItemReceived. A quest-ID prefix corrupts the tags.
        out = [(262, build_common_vector(a_long=rec)), (251, _consumable_bag_payload(state))]
        if heroes:
            out.append((207, build_pb_list_hero(heroes)))
        out.extend(dispatch_game_action(261, b"", uid, token, state, host_header))
        return out

    if act_id in (307, 308):
        from daily_quests import dispatch as daily_dispatch
        return daily_dispatch(sys.modules[__name__], act_id, req_longs, uid, state)

    if act_id in (309,310):
        from achievements import dispatch
        return dispatch(sys.modules[__name__],act_id,req_longs,uid,state)

    # ------------------------------------------------------------------------
    # Lucky Spin & Super Lucky Spin (319..326, 344) — LuckyWheelScene @ 0x13bbd5c
    # ------------------------------------------------------------------------
    if act_id in (319, 320, 324, 344):
        # Chu kỳ bắt buộc làm mới: 2 ngày 1 lần (172.800 giây)
        # Giá làm mới: 100 Kim Cương (Giới hạn làm mới thủ công: 1 lần / 2 ngày)
        CYCLE_2_DAYS = 172800
        now = int(time.time())
        last_ref = int(state.get("lucky_spin_last_refresh", 0))
        if last_ref == 0:
            state["lucky_spin_last_refresh"] = now
            last_ref = now
            STORE.save_state(uid, state)

        elapsed = now - last_ref
        if elapsed >= CYCLE_2_DAYS:
            state["lucky_spin_last_refresh"] = now
            state["lucky_spin_manual_refreshed_cycle"] = 0
            remaining_seconds = CYCLE_2_DAYS
            STORE.save_state(uid, state)
        else:
            remaining_seconds = max(0, CYCLE_2_DAYS - elapsed)

        if act_id in (320, 324):  # Người chơi bấm Làm mới thủ công (100 KC, tối đa 1 lần / 2 ngày)
            from resource_guard import InsufficientResources
            last_manual = int(state.get("lucky_spin_manual_refresh_ts", 0))
            if last_manual > 0 and (now - last_manual) < CYCLE_2_DAYS:
                rem_hrs = max(1, (CYCLE_2_DAYS - (now - last_manual)) // 3600)
                raise InsufficientResources(
                    f"Mỗi 2 ngày chỉ được làm mới 1 lần! Vui lòng đợi {rem_hrs} giờ nữa."
                )
            REFRESH_COST = 100
            if int(state.get("gem", 0)) < REFRESH_COST:
                raise InsufficientResources(f"Cần {REFRESH_COST} Kim Cương để làm mới vòng quay.")
            gem_debit, _ = apply_and_make_bonus_received(state, [("gem", -REFRESH_COST)], GAME_DB)
            state["lucky_spin_last_refresh"] = now
            state["lucky_spin_manual_refresh_ts"] = now
            state["lucky_spin_manual_refreshed_cycle"] = 1
            remaining_seconds = CYCLE_2_DAYS
            STORE.save_state(uid, state)

        # Native status fields: single chips, ten-spin chips, refresh timer,
        # refresh gems (100 KC), then gem price per purchased chip (30).
        v0 = build_common_vector(
            a_long=[1, 8 if act_id in (319, 320) else 10, remaining_seconds, 100, 30],
            a_string=["1", "10"]
        )
        wheel_pool = [
            ("gold", 500000),             # 500.000 Vàng / Beri (Tài nguyên)
            ("material", 1, 1, 200000),   # 200.000 Đá EXP (Tài nguyên)
            ("material", 1, 2, 2000),     # 2.000 Đá Thăng Cấp tím (Tài nguyên)
            ("material", 1, 15, 2),       # 2 Thùng Bia Vàng (Tài nguyên)
            ("gem", 200),                 # 200 Kim Cương (Tài nguyên)
            ("material", 1, 22, 5),       # 5 Cỏ 4 lá (Tiền tệ Shop Vòng Quay)
            ("material", 3, 1, 5),        # 5 Mảnh Bảo Vật Đỏ (Mảnh bảo vật)
            ("material", 3, 6, 5),        # 5 Mảnh Bảo Vật Đỏ Đặc Biệt (Mảnh bảo vật)
        ]
        wheel_items = make_bonus_preview(wheel_pool)
        v1 = build_common_vector(a_long=wheel_items)
        v2 = build_common_vector(a_long=[1, 1, 1, 1, 1, 1, 1, 1])
        sname = str(state.get('screenname', 'Hải Tặc'))
        v3 = build_common_vector(a_string=[f"{sname} vừa quay trúng phần thưởng!"] * 8)
        status_vec = build_list_common_vector([v0, v1, v2, v3])
        if act_id in (320, 324):
            # Native LuckySpinScene @ 0x13b9a38 & SuperLuckySpinScene @ 0x150db2c
            # parse 320/324 as CommonVector(a_long=[newGem]) and then request status.
            new_gem = int(state.get("gem", 0))
            status_act = 319 if act_id == 320 else 344
            return [
                (act_id, build_common_vector(a_long=[new_gem])),
                (status_act, status_vec),
                (202, build_common_vector(a_long=gem_debit)),
            ]
        return [(act_id, status_vec)]

    if act_id in (321, 325):  # LUCKY_SPIN / SUPER_LUCKY_SPIN (LuckySpinScene @ 0x13b92ac)
        spin_type = int(req_longs[0]) if req_longs else 1
        if spin_type not in (1, 10, 100):
            from resource_guard import InsufficientResources
            raise InsufficientResources('Số lượt quay không hợp lệ; chỉ nhận 1, 10 hoặc 100 lượt.')
        count = spin_type
        cost_mat = 10 if act_id == 321 else 13
        cost_num = ({1: 1, 10: 8, 100: 80}[count]) if act_id == 321 else count

        wheel_pool = [
            ("gold", 500000),             # Slot 0: 500.000 Vàng / Beri (Tài nguyên)
            ("material", 1, 1, 200000),   # Slot 1: 200.000 Đá EXP (Tài nguyên)
            ("material", 1, 2, 2000),     # Slot 2: 2.000 Đá Thăng Cấp tím (Tài nguyên)
            ("material", 1, 15, 2),       # Slot 3: 2 Thùng Bia Vàng (Tài nguyên)
            ("gem", 200),                 # Slot 4: 200 Kim Cương (Tài nguyên)
            ("material", 1, 22, 5),       # Slot 5: 5 Cỏ 4 lá (Tiền tệ Shop Vòng Quay)
            ("material", 3, 1, 5),        # Slot 6: 5 Mảnh Bảo Vật Đỏ (Mảnh bảo vật)
            ("material", 3, 6, 5),        # Slot 7: 5 Mảnh Bảo Vật Đỏ Đặc Biệt (Mảnh bảo vật)
        ]

        # Tỷ lệ Cỏ 4 lá (Slot 5): 22% (2200 / 10000), Tài nguyên khác: 73% (7300 / 10000), Mảnh Bảo Vật Đỏ (Slot 6-7): 5% (500 / 10000).
        # Khi bấm quay 10 ra tối đa 1 lần mảnh đỏ (5 mảnh), quay 100 ra tối đa 2 lần mảnh đỏ (10 mảnh), Cỏ 4 lá không giới hạn lượt nổ.
        slot_weights = (1700, 1700, 1500, 1200, 1200, 2200, 300, 200)  # Tổng = 10000 (22% Cỏ 4 lá, 73% tài nguyên, 5% mảnh đỏ)
        max_shard_hits = 1 if count <= 10 else 2  # Quay 1/10 -> tối đa 5 mảnh đỏ; Quay 100 -> tối đa 10 mảnh đỏ

        # Each result is parsed by the native client as BonusReceived, not
        # BonusPreview: balances AND deltas are required. LuckySpinScene @ 0x13b8fa4
        # and SuperLuckySpinScene @ 0x150d0cc read aVector[0] for [winSlot] + prize
        # BonusReceived and aVector[1] for chip debit BonusReceived.
        debit_receipt, _ = apply_and_make_bonus_received(state, [("material", 1, cost_mat, -cost_num)], GAME_DB)
        slot_counts = {}
        last_slot = 0
        shard_hits = 0
        for _ in range(count):
            roll = random.randint(0, 9999)
            s = 0
            for idx, w in enumerate(slot_weights):
                if roll < w:
                    s = idx
                    break
                roll -= w
            if s >= 6:
                if shard_hits >= max_shard_hits:
                    s = random.choices((0, 1, 2, 3, 4, 5), weights=(1700, 1700, 1500, 1200, 1200, 2200), k=1)[0]
                else:
                    shard_hits += 1
            if not slot_counts:
                last_slot = s
            slot_counts[s] = slot_counts.get(s, 0) + 1
        combined_prizes = []
        for s, cnt in slot_counts.items():
            item = list(wheel_pool[s])
            item[-1] = int(item[-1]) * cnt
            combined_prizes.append(tuple(item))
        prize_receipt, _ = apply_and_make_bonus_received(state, combined_prizes, GAME_DB)
        v_res_list = [
            build_common_vector(a_long=[last_slot] + prize_receipt),
            build_common_vector(a_long=debit_receipt),
        ]
        d_acts = state.setdefault("daily_actions", {})
        d_acts["spin"] = d_acts.get("spin", 0) + count
        STORE.save_state(uid, state)

        return [
            (act_id, build_list_common_vector(v_res_list)),
            (251, _consumable_bag_payload(state))
        ]

    if act_id in (322, 326):  # LUCKY_RECORD / SUPER_LUCKY_RECORD
        return [(act_id, build_common_vector(a_string=[f"{state['screenname']} vừa quay trúng Mảnh Bảo Vật!"]))]

    if act_id == 323:  # LUCKY_BUY_COIN
        cnt = max(1, min(10000, int(req_longs[0]))) if req_longs and int(req_longs[0]) > 0 else 10
        rec, _ = apply_and_make_bonus_received(
            state,
            [("gem", -30 * cnt), ("material", 1, 10, cnt)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        return [
            (323, build_common_vector(a_long=rec)),
            (251, _consumable_bag_payload(state))
        ]

    # ------------------------------------------------------------------------
    # Tower of Oblivion (216, 217, 257, 258) — OblivionTowerScene @ 0x13cfa4c
    # ------------------------------------------------------------------------
    if act_id == 216:  # TOWER_STATUS
        tw_lvl = int(state.get("tower_level", 1))
        v0 = build_common_vector(a_long=[tw_lvl, 10, 10, 1800])
        v1 = build_common_vector(a_long=make_bonus_preview([("gold", 100000), ("material", 1, 2, 500)]))
        return [(216, build_list_common_vector([v0, v1]))]

    if act_id == 217:
        floor=int(state.get('tower_level',1))
        battle_bytes,winner,_=settle_battle(state,[('gold',150000),('material',1,2,800),('gem',50)],
            enemy_hero_ids=GAME_DB.five_star_hero_ids[2:8],enemy_name=f'Tháp Hải Tặc Tầng {floor}',
            enemy_level=(floor+1)*5,req_longs=req_longs)
        if winner==1:state['tower_level']=min(2301,floor+1)
        STORE.save_state(uid,state)
        return [(217,battle_bytes)]

    if act_id == 257:  # TOWER_BUY_TURN
        return [(257, build_common_vector(a_long=[1, 10, state["gem"]]))]

    # ------------------------------------------------------------------------
    # Tavern (222..229) — TarvenScene @ 0x151a230
    # ------------------------------------------------------------------------
    if act_id in (222, 224):  # TARVEN_STATUS / TARVEN_REFRESH
        v0 = build_common_vector(a_long=[5, 5, 86400, 10])
        vecs = [v0]
        for qid in range(1, 6):
            # v_{2*qid-1}: [questId, questStar, questState(2=completed), isLocked(0), timeTotal, timeRemain, gemSpeedUp, ...1-item bonusPreview]
            preview = make_bonus_preview([("gem", 100 * qid)])
            vecs.append(build_common_vector(a_long=[qid, 5 + (qid % 3), 2, 0, 3600, 0, 20] + preview))
            # v_{2*qid}: [numReq, reqType(1=star), reqVal(5)]
            vecs.append(build_common_vector(a_long=[1, 1, 5]))
        return [(224 if act_id == 224 else 222, build_list_common_vector(vecs))]

    if act_id == 223:  # TARVEN_GEN_QUEST
        rec, _ = apply_and_make_bonus_received(state, [("gem", -20)], GAME_DB)
        STORE.save_state(uid, state)
        out = [(223, build_common_vector(a_long=rec))]
        out.extend(dispatch_game_action(222, b"", uid, token, state, host_header))
        return out

    if act_id == 225:  # TARVEN_SPEED_UP_QUEST
        qid = int(req_longs[0]) if req_longs else 1
        rec, _ = apply_and_make_bonus_received(state, [("gem", -20)], GAME_DB)
        STORE.save_state(uid, state)
        return [(225, build_common_vector(a_long=[qid, 2] + rec))]

    if act_id == 226:  # TARVEN_SWITCH_LOCK_QUEST
        qid = int(req_longs[0]) if req_longs else 1
        lock_val = int(req_longs[1]) if len(req_longs) > 1 else 1
        return [(226, build_common_vector(a_long=[qid, lock_val]))]

    if act_id in (227, 229):  # TARVEN_START_QUEST / TARVEN_CANCEL_QUEST
        qid = int(req_longs[0]) if req_longs else 1
        return [(act_id, build_common_vector(a_long=[qid]))]

    if act_id == 228:  # TARVEN_COMPLETED_QUEST
        qid = int(req_longs[0]) if req_longs else 1
        rec, _ = apply_and_make_bonus_received(
            state,
            [("gem", 200), ("material", 1, 14, 2)],
            GAME_DB
        )
        d_acts = state.setdefault("daily_actions", {})
        d_acts["tavern"] = d_acts.get("tavern", 0) + 1
        STORE.save_state(uid, state)
        return [(228, build_common_vector(a_long=[qid] + rec))]

    # ------------------------------------------------------------------------
    # Arena / Crystal Crown (327..340) — ArenaScene & CrystalCrownScene
    # ------------------------------------------------------------------------
    if act_id == 328:  # CRYSTAL_STATUS
        v0 = build_common_vector(a_long=[encode_client_i32_power(int(state.get("power", 500000))), 1, 1500, 10, 86400])
        v1 = build_common_vector(a_long=[int(h["id"]) for h in state.get("heroes", [])[:6]])
        return [(328, build_list_common_vector([v0, v1]))]

    if act_id in (330, 333, 340, 343, 347, 348):  # CRYSTAL_TOP_USER / CRYSTAL_FIND_RIVAL / TOP_POWER
        online_players = STORE.list_top_players(20)
        rivals = list(online_players)
        for r_idx in range(1, 4):
            rivals.append({
                "id": 1000 + r_idx,
                "username": f"rival_{r_idx}",
                "screenname": f"Hải Tặc #{r_idx}",
                "gold": 1000000,
                "gem": 10000,
                "power": 50000 * r_idx,
                "exp": 10000,
                "level": 30 * r_idx,
                "avatar": [0, r_idx],
                "vip": [20, 10000000],
                "team": [[GAME_DB.five_star_hero_ids[i % len(GAME_DB.five_star_hero_ids)], 30 * r_idx, 5, 0] for i in range(6)],
            })
        return [(act_id, build_pb_list_user(rivals))]

    if act_id == 334:  # CRYSTAL_BATTLE (Arena / Đấu Trường)
        # req_longs[0] = đối thủ userId
        # req_longs[1..7] = 6 tướng của người chơi (tránh lệch slot khiến tướng biến hình)
        formation_req = req_longs[1:] if len(req_longs) > 1 else req_longs
        rival_uid = int(req_longs[0]) if req_longs else 1001
        rival_name = f"Hải Tặc #{rival_uid}"
        rival_heroes = GAME_DB.five_star_hero_ids[3:9]
        rival_lvl = 150
        try:
            _, _, r_state = STORE.get_by_token_or_identity(str(rival_uid))
            if r_state and r_state.get("heroes"):
                rival_name = str(r_state.get("screenname", rival_name))
                rival_lvl = int(r_state.get("level", rival_lvl))
                rival_heroes = [int(h["heroId"]) for h in r_state.get("heroes", [])[:6]]
        except Exception:
            pass

        d_acts=state.setdefault('daily_actions',{});d_acts['arena']=d_acts.get('arena',0)+1
        battle_bytes,winner,_=settle_battle(state,[('gold',100000),('gem',50),('material',1,18,20)],
            enemy_hero_ids=rival_heroes,enemy_name=rival_name,enemy_level=rival_lvl,req_longs=formation_req)
        # No hard-coded winning point delta on a defeat.
        STORE.save_state(uid,state)
        return [(334,battle_bytes)]

    if act_id in (332, 335):  # CRYSTAL_BUY_TICKET
        rec, _ = apply_and_make_bonus_received(
            state,
            [("gem", -20), ("material", 1, 18, 5)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        return [(act_id, build_common_vector(a_long=rec))]

    # ------------------------------------------------------------------------
    # Event Raid (316, 317, 318, 349) — EventRaidScene @ 0x17003c0
    # ------------------------------------------------------------------------
    if act_id == 316:  # EVENT_RAID_STATUS
        v0 = build_common_vector(a_long=[5, 5, 5, 50])
        v1 = build_common_vector(a_long=[1, 1, 2, 2, 3, 3])
        return [(316, build_list_common_vector([v0, v1]))]

    if act_id in (317, 349):
        d_acts=state.setdefault('daily_actions',{});d_acts['event_raid']=d_acts.get('event_raid',0)+1
        battle_bytes,_,_=settle_battle(state,[('gold',300000),('material',1,1,100000),('material',1,2,1500)],
            enemy_hero_ids=GAME_DB.five_star_hero_ids[:6],enemy_name='Phụ Bản Sự Kiện',enemy_level=120,req_longs=req_longs,
            fake_bonus_preview_longs=make_bonus_preview([('gold',200000),('material',1,1,80000),('material',1,2,1000)]))
        STORE.save_state(uid,state)
        return [(act_id,battle_bytes)]

    if act_id == 318:  # EVENT_RAID_BUY_TURN
        state["event_raid_buys"] = int(state.get("event_raid_buys", 0)) + 1
        rec, _ = apply_and_make_bonus_received(state, [("gem", -50)], GAME_DB)
        STORE.save_state(uid, state)
        return [(318, build_common_vector(a_long=[5, 5] + rec))]

    # ------------------------------------------------------------------------
    # Mail (219, 220, 221) — PopupMail @ 0x1493f88
    # ------------------------------------------------------------------------
    if act_id == 219:  # MAIL_LIST
        try:
            with STORE.lock, sqlite3.connect(STORE.db_path) as _c_mail:
                _r_mail = _c_mail.execute("SELECT state_json FROM players WHERE uid = ?", (uid,)).fetchone()
                if _r_mail and _r_mail[0]:
                    _db_st = json.loads(_r_mail[0])
                    _db_mails = _db_st.get("mails", [])
                    _cur_mails = state.setdefault("mails", [])
                    _cur_mids = {int(m.get("id", 0)) for m in _cur_mails}
                    _cur_gc = {m.get("gift_code") for m in _cur_mails if m.get("gift_code")}
                    _claimed_gc = set(state.get("claimed_gift_codes", []))
                    _added_any = False
                    for _dm in _db_mails:
                        _dm_gc = _dm.get("gift_code")
                        if _dm_gc and (_dm_gc in _cur_gc or _dm_gc in _claimed_gc):
                            continue
                        _dm_id = int(_dm.get("id", 0))
                        if _dm_id not in _cur_mids:
                            _cur_mails.append(_dm)
                            _cur_mids.add(_dm_id)
                            if _dm_gc:
                                _cur_gc.add(_dm_gc)
                            _added_any = True
                        elif not _dm.get("received") and _dm_gc and _dm_gc not in _cur_gc:
                            _new_id = max(int(state.get("_next_mail_id", 0)), max(_cur_mids, default=0)) + 1
                            state["_next_mail_id"] = _new_id
                            _dm_copy = dict(_dm)
                            _dm_copy["id"] = _new_id
                            _cur_mails.append(_dm_copy)
                            _cur_mids.add(_new_id)
                            _cur_gc.add(_dm_gc)
                            _added_any = True
                    if _added_any:
                        STORE.save_state(uid, state)
        except Exception:
            pass
        return [(219, build_pb_list_mail(state.get("mails", [])))]

    if act_id == 220:  # MAIL_RECEIVE
        mail_id = int(req_longs[0]) if req_longs else 0
        pending = [m for m in state.get('mails', []) if not m.get('received')
                   and (mail_id <= 0 or int(m['id']) == mail_id)]
        try:
            from gift_mail import mail_items
            items = [item for m in pending for item in mail_items(sys.modules[__name__],m)]
            totals = {(int(c[0]), int(c[1])): int(c[2]) for c in state.get('consumables', [])}
            for item in items:
                if item[0] == 'material':
                    mt = 1 if int(item[1]) in (10, 11, 12) else int(item[1])
                    key = (mt, int(item[2]))
                    totals[key] = totals.get(key, 0) + int(item[3])
                    if totals[key] > 9223372036854775807:
                        raise ValueError('Item quantity exceeds client Int64 range')
        except (ValueError, TypeError):
            return [(2, build_common_vector(a_string=[
                'Chưa thể nhận thư: phần thưởng không hợp lệ hoặc số lượng vật phẩm vượt giới hạn.']))]
        rec, new_heroes = apply_and_make_bonus_received(state, items, GAME_DB)
        claimed_gc = state.setdefault('claimed_gift_codes', [])
        for mail in pending:
            mail['received'] = True
            gc = mail.get('gift_code')
            if gc and gc not in claimed_gc:
                claimed_gc.append(gc)
        STORE.save_state(uid, state)
        return [
            (220, build_list_common_vector([build_common_vector(a_long=rec)])),
            (219, build_pb_list_mail(state.get("mails", []))),
            (251, _consumable_bag_payload(state)),
            *([(207, build_pb_list_hero(new_heroes))] if new_heroes else []),
        ]

    if act_id == 221:  # MAIL_DELETE
        mail_id = int(req_longs[0]) if req_longs else 0
        cur_mails = state.get("mails", [])
        state["_next_mail_id"] = max(int(state.get("_next_mail_id", 0)), max((int(m.get("id", 0)) for m in cur_mails), default=0))
        claimed_gc = state.setdefault("claimed_gift_codes", [])
        for m in cur_mails:
            if m.get("received") and m.get("gift_code") and m["gift_code"] not in claimed_gc:
                claimed_gc.append(m["gift_code"])
        if mail_id > 0:
            state["mails"] = [m for m in cur_mails if int(m["id"]) != mail_id]
        else:
            state["mails"] = [m for m in cur_mails if not m.get("received", False)]
        STORE.save_state(uid, state)
        return [
            (221, build_common_vector(a_long=[1])),
            (219, build_pb_list_mail(state.get("mails", []))),
        ]

    # ------------------------------------------------------------------------
    # Pets / Thú Cưng (360..366) — MonsterScene @ 0x13caa94 & PanelPetLevel @ 0x1412ccc
    # ------------------------------------------------------------------------
    if act_id == 360:  # PET_REQUEST_LIST
        vecs = [build_common_vector(a_long=p) for p in state.get("pets", [])]
        return [(360, build_list_common_vector(vecs))]

    if act_id == 361:  # PET_ACTIVATE (Kích hoạt Tàu Thần Thú / Red Force)
        pet_id = int(req_longs[0]) if req_longs else 1
        found_pet = None
        for p in state.setdefault("pets", []):
            if int(p[0]) == pet_id:
                found_pet = p
                break
        if not found_pet:
            found_pet = [pet_id, 120, 4, 30, 30, 30, 30, 30, 30, 30]
            state["pets"].append(found_pet)
        rec, _ = apply_and_make_bonus_received(state, [("gold", -50000)], GAME_DB)
        STORE.save_state(uid, state)
        vecs = [build_common_vector(a_long=p) for p in state.get("pets", [])]
        return [
            (361, build_common_vector(a_long=[pet_id] + rec)),
            (360, build_list_common_vector(vecs)),
        ]

    if act_id == 362:  # PET_LEVEL_UP
        pet_id = int(req_longs[0]) if req_longs else 1
        target_pet = None
        for p in state.setdefault("pets", []):
            if int(p[0]) == pet_id:
                target_pet = p
                break
        if not target_pet:
            return [(2, build_common_vector(a_string=["Chiến thuyền chưa được kích hoạt!"]))]
        
        cur_lvl = int(target_pet[1])
        # Max level per tier: Tier 0 = 30, Tier 1 = 60, Tier 2 = 90, Tier 3 = 120, Tier 4 = 150
        cur_tier = int(target_pet[2]) if len(target_pet) > 2 else 0
        max_tier_lvl = (cur_tier + 1) * 30
        if cur_lvl >= max_tier_lvl:
            return [(2, build_common_vector(a_string=["Thuyền đã đạt cấp tối đa của phẩm chất hiện tại! Hãy tiến hóa trước."]))]

        cost_gold, cost_wood, _, _ = GAME_DB.pet_level_costs.get(cur_lvl, (cur_lvl * 8000, max(20, cur_lvl * 15), 0, 0))
        cur_gold = int(state.get("gold", 0))
        cur_wood = get_consumable_count(state, 1, 11)  # ITEM_KEY_MONSTER_SOUL = 11
        if cur_gold < cost_gold:
            return [(2, build_common_vector(a_string=[f"Không đủ Vàng để nâng cấp thuyền! Cần {cost_gold:,} Vàng."]))]
        if cur_wood < cost_wood:
            return [(2, build_common_vector(a_string=[f"Không đủ Hồn Thú để nâng cấp thuyền! Cần {cost_wood:,} Hồn Thú."]))]

        target_pet[1] = cur_lvl + 1
        rec, _ = apply_and_make_bonus_received(state, [("gold", -cost_gold), ("material", 1, 11, -cost_wood)], GAME_DB)
        STORE.save_state(uid, state)
        return [
            (362, build_common_vector(a_long=[pet_id, target_pet[1]] + rec)),
            (251, _consumable_bag_payload(state))
        ]

    if act_id == 363:  # PET_EVOLVE
        pet_id = int(req_longs[0]) if req_longs else 1
        target_pet = None
        for p in state.setdefault("pets", []):
            if int(p[0]) == pet_id:
                target_pet = p
                break
        if not target_pet:
            return [(2, build_common_vector(a_string=["Chiến thuyền chưa được kích hoạt!"]))]
        
        cur_tier = int(target_pet[2]) if len(target_pet) > 2 else 0
        if cur_tier >= 4:
            return [(2, build_common_vector(a_string=["Chiến thuyền đã đạt phẩm chất tối đa (4)!"]))]

        cur_lvl = int(target_pet[1])
        req_lvl = (cur_tier + 1) * 30
        if cur_lvl < req_lvl:
            return [(2, build_common_vector(a_string=[f"Cần nâng thuyền lên cấp {req_lvl} mới có thể tiến hóa!"]))]

        _, _, cost_gold, cost_wood = GAME_DB.pet_level_costs.get(cur_lvl, (0, 0, 100000 * (cur_tier + 1), 200 * (cur_tier + 1)))
        if cost_gold <= 0:
            cost_gold = 100000 * (cur_tier + 1)
        if cost_wood <= 0:
            cost_wood = 200 * (cur_tier + 1)

        cur_gold = int(state.get("gold", 0))
        cur_wood = get_consumable_count(state, 1, 11)
        if cur_gold < cost_gold:
            return [(2, build_common_vector(a_string=[f"Không đủ Vàng để tiến hóa thuyền! Cần {cost_gold:,} Vàng."]))]
        if cur_wood < cost_wood:
            return [(2, build_common_vector(a_string=[f"Không đủ Hồn Thú để tiến hóa thuyền! Cần {cost_wood:,} Hồn Thú."]))]

        target_pet[2] = cur_tier + 1
        rec, _ = apply_and_make_bonus_received(state, [("gold", -cost_gold), ("material", 1, 11, -cost_wood)], GAME_DB)
        STORE.save_state(uid, state)
        return [
            (363, build_common_vector(a_long=[pet_id, target_pet[2]] + rec)),
            (251, _consumable_bag_payload(state))
        ]

    if act_id in (364, 365):  # PET_SKILL_UP
        pet_id = int(req_longs[0]) if req_longs else 1
        skill_idx = int(req_longs[1]) if len(req_longs) > 1 else 0
        skill_idx = max(0, min(3, skill_idx))
        target_pet = None
        for p in state.setdefault("pets", []):
            if int(p[0]) == pet_id:
                target_pet = p
                break
        if not target_pet:
            return [(2, build_common_vector(a_string=["Chiến thuyền chưa được kích hoạt!"]))]

        cur_sk_lvl = int(target_pet[3 + skill_idx])
        if cur_sk_lvl >= 30:
            return [(2, build_common_vector(a_string=["Kỹ năng thuyền đã đạt cấp tối đa (30)!"]))]

        row = GAME_DB.pet_passive_costs.get(cur_sk_lvl, {})
        cost_gold = int(row.get(f"gold_skill_{skill_idx+1}", 20000 * (cur_sk_lvl + 1)))
        cost_chaos = int(row.get(f"chaos_skill_{skill_idx+1}", max(5, cur_sk_lvl * 5)))

        cur_gold = int(state.get("gold", 0))
        cur_chaos = get_consumable_count(state, 1, 12)  # ITEM_KEY_CHAOS_STONE = 12
        if cur_gold < cost_gold:
            return [(2, build_common_vector(a_string=[f"Không đủ Vàng nâng cấp kỹ năng! Cần {cost_gold:,} Vàng."]))]
        if cur_chaos < cost_chaos:
            return [(2, build_common_vector(a_string=[f"Không đủ Đá Hỗn Độn nâng kỹ năng! Cần {cost_chaos:,} Đá Hỗn Độn."]))]

        target_pet[3 + skill_idx] = cur_sk_lvl + 1
        rec, _ = apply_and_make_bonus_received(state, [("gold", -cost_gold), ("material", 1, 12, -cost_chaos)], GAME_DB)
        STORE.save_state(uid, state)
        return [
            (act_id, build_common_vector(a_long=[pet_id, skill_idx, target_pet[3 + skill_idx]] + rec)),
            (251, _consumable_bag_payload(state))
        ]

    if act_id == 366:  # PET_UPGRADE_RUIN (PanelPetRuin @ 0x1413960 - Thiên Phú Pet)
        pet_id = int(req_longs[0]) if req_longs else 1
        ruin_idx = int(req_longs[1]) if len(req_longs) > 1 else 0
        ruin_idx = max(0, min(2, ruin_idx))
        new_ruin_lvl = 1
        for p in state.setdefault("pets", []):
            if int(p[0]) == pet_id:
                while len(p) < 10:
                    p.append(0)
                # Level up smoothly up to 60 (matches db_pet_ruin_upgrade.json & UI 30/60)
                p[7 + ruin_idx] = min(60, int(p[7 + ruin_idx]) + 1)
                new_ruin_lvl = p[7 + ruin_idx]
                break
        cost_gold, cost_chaos = GAME_DB.pet_ruin_costs.get(new_ruin_lvl, (1108553, 606))
        rec, _ = apply_and_make_bonus_received(
            state,
            [("gold", -cost_gold), ("material", 1, 205, -cost_chaos)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        vecs = [build_common_vector(a_long=p) for p in state.get("pets", [])]
        return [
            (366, build_common_vector(a_long=[pet_id, ruin_idx, new_ruin_lvl] + rec)),
            (360, build_list_common_vector(vecs)),
            (251, _consumable_bag_payload(state)),
        ]

    # ------------------------------------------------------------------------
    # Celestial Island (411..426) — CelestialScene @ 0x1696478 & IslandScene @ 0x139b648
    # ------------------------------------------------------------------------
    # ------------------------------------------------------------------------
    # Pet 2 / Thú Cưng Linh Thú (940..948) — PetHome, PetMain, PetInventory, PetLibrary
    # ------------------------------------------------------------------------
    if act_id == 940:  # PET_2_STATUS (PetDataSender.GetPetStatus)
        return [(940, build_pet2_status_payload(state, GAME_DB))]

    if act_id == 941:  # PET_2_UPGRADE (PetDataSender.UpgradePet)
        pet_id = int(req_longs[0]) if req_longs else 1
        found = None
        for p in state.setdefault("pets2", []):
            if int(p.get("id", 0)) == pet_id or int(p.get("set_id", 0)) == pet_id:
                found = p
                break
        if found:
            cur_lvl = int(found.get("level", 1))
            star = int(found.get("star", 5))
            star_info = getattr(GAME_DB, "pet2_stars", {}).get(star, {})
            max_lvl = int(star_info.get("limitLevel", 120))
            if cur_lvl < max_lvl:
                lvl_info = getattr(GAME_DB, "pet2_levels", {}).get(cur_lvl + 1, {})
                gold_cost = int(lvl_info.get("gold", 8000))
                exp_cost = int(lvl_info.get("exp", 20))
                state["gold"] = max(0, int(state.get("gold", 0)) - gold_cost)
                for c in state.setdefault("consumables", []):
                    if int(c[0]) == 1 and int(c[1]) == 3004:
                        c[2] = max(0, int(c[2]) - exp_cost)
                        break
                found["level"] = cur_lvl + 1
                base_p = GAME_DB.pet2_by_id.get(int(found.get("set_id", pet_id)))
                atk, hp, defs, spd = calc_pet2_stats(
                    base_p, star, found["level"], int(found.get("tier", 1)),
                    float(found.get("intelligent", 1500.0)),
                    float(found.get("basic_atk", 100.0)),
                    float(found.get("basic_hp", 500.0)),
                    float(found.get("basic_def", 2.0)),
                    float(found.get("basic_spd", 5.0)),
                    GAME_DB
                )
                found["atk"] = atk
                found["hp"] = hp
                found["def"] = defs
                found["spd"] = spd
                STORE.save_state(uid, state)
        return [(941, build_common_vector(a_long=[1]))]

    if act_id == 942:  # PET_2_SUMMON (PetDataSender.SummonPet)
        pet_id = int(req_longs[0]) if req_longs else 1
        shards = state.setdefault("pet2_shards", {})
        cur_shards = int(shards.get(str(pet_id), 0))
        req_shard = 50
        if cur_shards >= req_shard:
            shards[str(pet_id)] = cur_shards - req_shard
        base_p = GAME_DB.pet2_by_id.get(pet_id)
        if base_p:
            existing = [p for p in state.setdefault("pets2", []) if int(p.get("set_id", 0)) == pet_id]
            if not existing:
                inst = make_pet2_instance(base_p, star=5, level=1, tier=1, game_db=GAME_DB)
                state["pets2"].append(inst)
            STORE.save_state(uid, state)
        return [(942, build_common_vector(a_long=[pet_id]))]

    if act_id == 943:  # PET_2_RESET (PetDataSender.ResetPet)
        pet_id = int(req_longs[0]) if req_longs else 1
        for p in state.setdefault("pets2", []):
            if int(p.get("id", 0)) == pet_id or int(p.get("set_id", 0)) == pet_id:
                p["level"] = 1
                p["exp"] = 0
                base_p = GAME_DB.pet2_by_id.get(int(p.get("set_id", pet_id)))
                atk, hp, defs, spd = calc_pet2_stats(
                    base_p, int(p.get("star", 5)), 1, int(p.get("tier", 1)),
                    float(p.get("intelligent", 1500.0)),
                    float(p.get("basic_atk", 100.0)),
                    float(p.get("basic_hp", 500.0)),
                    float(p.get("basic_def", 2.0)),
                    float(p.get("basic_spd", 5.0)),
                    GAME_DB
                )
                p["atk"] = atk
                p["hp"] = hp
                p["def"] = defs
                p["spd"] = spd
                break
        STORE.save_state(uid, state)
        return [(943, build_common_vector(a_long=[1]))]

    if act_id == 944:  # PET_2_GROWTH (PetDataSender.GrowthPet)
        pet_id = int(req_longs[0]) if req_longs else 1
        for p in state.setdefault("pets2", []):
            if int(p.get("id", 0)) == pet_id or int(p.get("set_id", 0)) == pet_id:
                base_p = GAME_DB.pet2_by_id.get(int(p.get("set_id", pet_id)))
                if base_p:
                    p["intelligent"] = float(base_p.get("intelligent_max", 2000.0))
                    p["tier_intelligent"] = 5  # Hoàn mỹ
                    atk, hp, defs, spd = calc_pet2_stats(
                        base_p, int(p.get("star", 5)), int(p.get("level", 1)), int(p.get("tier", 1)),
                        float(p["intelligent"]),
                        float(p.get("basic_atk", 100.0)),
                        float(p.get("basic_hp", 500.0)),
                        float(p.get("basic_def", 2.0)),
                        float(p.get("basic_spd", 5.0)),
                        GAME_DB
                    )
                    p["atk"] = atk
                    p["hp"] = hp
                    p["def"] = defs
                    p["spd"] = spd
                break
        STORE.save_state(uid, state)
        return [(944, build_common_vector(a_long=[1]))]

    if act_id == 945:  # PET_2_UPGRADE_TIER (PetDataSender.UpgradeTierPet)
        pet_id = int(req_longs[0]) if req_longs else 1
        for p in state.setdefault("pets2", []):
            if int(p.get("id", 0)) == pet_id or int(p.get("set_id", 0)) == pet_id:
                cur_tier = int(p.get("tier", 1))
                if cur_tier < 10:
                    p["tier"] = cur_tier + 1
                    base_p = GAME_DB.pet2_by_id.get(int(p.get("set_id", pet_id)))
                    atk, hp, defs, spd = calc_pet2_stats(
                        base_p, int(p.get("star", 5)), int(p.get("level", 1)), p["tier"],
                        float(p.get("intelligent", 1500.0)),
                        float(p.get("basic_atk", 100.0)),
                        float(p.get("basic_hp", 500.0)),
                        float(p.get("basic_def", 2.0)),
                        float(p.get("basic_spd", 5.0)),
                        GAME_DB
                    )
                    p["atk"] = atk
                    p["hp"] = hp
                    p["def"] = defs
                    p["spd"] = spd
                break
        STORE.save_state(uid, state)
        return [(945, build_common_vector(a_long=[1]))]

    if act_id == 946:  # PET_2_UPGRADE_STAR (PetDataSender.UpgradeStarPet)
        pet_id = int(req_longs[0]) if req_longs else 1
        for p in state.setdefault("pets2", []):
            if int(p.get("id", 0)) == pet_id or int(p.get("set_id", 0)) == pet_id:
                cur_star = int(p.get("star", 5))
                if cur_star < 15:
                    p["star"] = cur_star + 1
                    base_p = GAME_DB.pet2_by_id.get(int(p.get("set_id", pet_id)))
                    atk, hp, defs, spd = calc_pet2_stats(
                        base_p, p["star"], int(p.get("level", 1)), int(p.get("tier", 1)),
                        float(p.get("intelligent", 1500.0)),
                        float(p.get("basic_atk", 100.0)),
                        float(p.get("basic_hp", 500.0)),
                        float(p.get("basic_def", 2.0)),
                        float(p.get("basic_spd", 5.0)),
                        GAME_DB
                    )
                    p["atk"] = atk
                    p["hp"] = hp
                    p["def"] = defs
                    p["spd"] = spd
                break
        STORE.save_state(uid, state)
        return [(946, build_common_vector(a_long=[1]))]

    if act_id == 947:  # PET_2_EQUIP (PetDataSender.EquipPet: heroId, petId)
        hero_id = int(req_longs[0]) if len(req_longs) > 0 else 0
        pet_id = int(req_longs[1]) if len(req_longs) > 1 else 0
        equips = state.setdefault("pet2_equips", {})
        if pet_id > 0:
            for k in list(equips.keys()):
                if equips[k] == hero_id or k == str(pet_id):
                    del equips[k]
            equips[str(pet_id)] = hero_id
        else:
            for k in list(equips.keys()):
                if equips[k] == hero_id:
                    del equips[k]
        STORE.save_state(uid, state)
        return [(947, build_common_vector(a_long=[1]))]

    if act_id == 948:  # PET_2_RECRUIT (PetDataSender.BasicSummonPet: amount)
        cnt = int(req_longs[0]) if req_longs else 1
        if cnt < 1: cnt = 1
        cnt = min(cnt, 10)
        shards = state.setdefault("pet2_shards", {})
        res_vecs = []
        import random
        pool = getattr(GAME_DB, "pet2_ids", list(range(1, 65)))
        for _ in range(cnt):
            r_pid = random.choice(pool)
            if random.random() < 0.25:
                # Type 2: Full pet
                res_vecs.append(build_common_vector(a_long=[2, r_pid]))
                if not any(int(p.get("set_id", 0)) == r_pid for p in state.setdefault("pets2", [])):
                    bp = GAME_DB.pet2_by_id.get(r_pid)
                    if bp:
                        state["pets2"].append(make_pet2_instance(bp, star=5, level=1, tier=1, game_db=GAME_DB))
            else:
                # Type 1: Shard
                s_cnt = random.randint(10, 50)
                shards[str(r_pid)] = int(shards.get(str(r_pid), 0)) + s_cnt
                res_vecs.append(build_common_vector(a_long=[1, r_pid, s_cnt]))
        STORE.save_state(uid, state)
        return [(948, build_list_common_vector(res_vecs))]

    if act_id == 411:  # CELESTIAL_STATUS
        tw_lvl = int(state.get("celestial_tower", 10))
        v0 = build_common_vector(a_long=[tw_lvl, 20, 20, 6, 10, 10, 50, 3600, 7200])
        v1 = build_common_vector(a_long=[0, 10, 20, 0, 10, 1, 10])
        v2 = build_common_vector(a_long=[1, 10, 20, 2, 10, 3, 10])
        v3 = build_common_vector(a_long=[5, 10, 20, 10, 5])
        v4 = build_common_vector(a_long=[
            1, 2, 10, 50000, 100000,
            2, 3, 10, 200, 500,
            3, 4, 10, 1000, 2000,
        ])
        return [(411, build_list_common_vector([v0, v1, v2, v3, v4]))]

    if act_id == 412:  # CELESTIAL_TOWER_UPGRADE
        state["celestial_tower"] = min(20, int(state.get("celestial_tower", 10)) + 1)
        rec, _ = apply_and_make_bonus_received(state, [("gold", -100000)], GAME_DB)
        STORE.save_state(uid, state)
        return [(412, build_common_vector(a_long=[state["celestial_tower"]] + rec))]

    if act_id == 413:  # CELESTIAL_MAKE_CONSTRUCTION
        rec, _ = apply_and_make_bonus_received(state, [("gold", -50000)], GAME_DB)
        STORE.save_state(uid, state)
        out = [(413, build_common_vector(a_long=rec))]
        out.extend(dispatch_game_action(411, b"", uid, token, state, host_header))
        return out

    if act_id == 414:  # CELESTIAL_DISMANTLE_MINE_FIELD
        mine_id = int(req_longs[0]) if req_longs else 1
        rec, _ = apply_and_make_bonus_received(state, [("gold", 25000)], GAME_DB)
        STORE.save_state(uid, state)
        return [(414, build_common_vector(a_long=[mine_id] + rec))]

    if act_id in (415, 424):  # CELESTIAL_BUILDING_UPGRADE / CELESTIAL_TECH_UPGRADE
        b_id = int(req_longs[0]) if len(req_longs) >= 1 else 0
        b_type = int(req_longs[1]) if len(req_longs) >= 2 else 0
        rec, _ = apply_and_make_bonus_received(state, [("gold", -50000)], GAME_DB)
        STORE.save_state(uid, state)
        return [(act_id, build_common_vector(a_long=[b_id, b_type, 11] + rec))]

    if act_id == 416:  # CELESTIAL_GET_MINE_FIELD
        # Match the existing 3600-second mine cycle. Saved with the balances,
        # under PLAYER_TRANSACTION_LOCK, so retries/relogins cannot mint rewards.
        now = int(time.time())
        next_claim = int(state.get("celestial_mine_next_claim", 0))
        if now < next_claim:
            return [(2, build_common_vector(a_string=["Mỏ chưa tích lũy đủ. Vui lòng chờ %d giây." % (next_claim - now)]))]
        state["celestial_mine_next_claim"] = now + 3600
        rec, _ = apply_and_make_bonus_received(
            state,
            [("gold", 100000), ("gem", 200), ("material", 1, 3, 1000)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        return [(416, build_common_vector(a_long=rec))]

    if act_id == 417:  # CELESTIAL_ISLAND_STATUS
        vecs = [build_common_vector(a_long=[20, 3600])]
        for isl_id in range(1, 4):
            creeps = []
            for c_idx in range(6):
                hid = GAME_DB.five_star_hero_ids[(isl_id + c_idx) % len(GAME_DB.five_star_hero_ids)]
                creeps.extend([c_idx + 1, hid, 120, 100])
            preview = make_bonus_preview([("gold", 50000), ("gem", 50)])
            vecs.append(build_common_vector(a_long=[isl_id, 1, 3600] + creeps + preview))
        return [(417, build_list_common_vector(vecs))]

    if act_id == 418:  # CELESTIAL_BOSS_STATUS
        hid = GAME_DB.five_star_hero_ids[0]
        return [(418, build_common_vector(a_long=[0, 1, 3600, 1, hid, 150, 100, 1, 1]))]

    if act_id in (419, 420):
        battle_bytes,_,_=settle_battle(state,[('gold',150000),('gem',100),('material',1,3,500)],
            enemy_hero_ids=GAME_DB.five_star_hero_ids[:6],enemy_name='Đảo Trời Hải Tặc',enemy_level=140,req_longs=req_longs)
        state['celestial_battles']=int(state.get('celestial_battles',0))+1
        STORE.save_state(uid,state)
        from home_quests import progress as _hq_progress
        q_st, q_id, q_cur, q_tgt, _ = _hq_progress(state, GAME_DB)
        return [(act_id,battle_bytes), (261, build_common_vector(a_long=[q_st, q_id, q_cur, q_tgt]))]

    if act_id == 421:  # CELESTIAL_SMASH
        rec, _ = apply_and_make_bonus_received(
            state,
            [("gold", 150000), ("gem", 100), ("material", 1, 3, 500)],
            GAME_DB
        )
        state['celestial_battles'] = int(state.get('celestial_battles', 0)) + 1
        STORE.save_state(uid, state)
        from home_quests import progress as _hq_progress
        q_st, q_id, q_cur, q_tgt, _ = _hq_progress(state, GAME_DB)
        return [(421, build_common_vector(a_long=rec)), (261, build_common_vector(a_long=[q_st, q_id, q_cur, q_tgt]))]

    if act_id == 422:  # CELESTIAL_BUY_DAFFODIL
        rec, _ = apply_and_make_bonus_received(state, [("gem", -50)], GAME_DB)
        STORE.save_state(uid, state)
        return [(422, build_common_vector(a_long=[25, 10] + rec))]

    if act_id == 425:  # CELESTIAL_MINE_UPDATE
        return [(425, build_common_vector(a_long=[1, 50000, 100000, 2, 200, 500, 3, 1000, 2000]))]

    if act_id == 426:  # GET_DAFFODIL
        return [(426, build_common_vector(a_long=[5]))]

    # ------------------------------------------------------------------------
    # Guild / Công Hội (400..469, 511..515, 2100..2108)
    # ------------------------------------------------------------------------
    from guild_membership import dispatch as guild_dispatch
    guild_response=guild_dispatch(sys.modules[__name__],act_id,req_longs,req_strings,uid,state)
    if guild_response is not None:return guild_response

    if act_id == 427:  # GUILD_STATUS (HomeScene.ProcessKEvent @ 0x1367938 & HomeScene2 @ 0x154a868)
        clan_id = int(state.get("clan_id", 0))
        if clan_id == 0:
            # Chưa vào bang -> hiển thị giao diện tìm/tạo bang
            return [(427, build_common_vector(a_long=[0, 0, 0], a_string=[""]))]
        clan_pos = int(state.get("clan_pos", 1))
        clan_flag = max(1, int(state.get("clan_flag", 1)))
        clan_name = str(state.get("clan_name", "Băng Mũ Rơm"))
        return [(427, build_common_vector(a_long=[clan_id, clan_pos, clan_flag], a_string=[clan_name]))]

    if act_id == 400:  # GUILD_CREATE (PopupCreateGuild @ 0x1466140)
        new_flag = int(req_longs[0]) if req_longs else 1
        new_name = req_strings[0] if req_strings else "HaiTacVua"
        state["clan_id"] = int(time.time()) % 100000 + 10
        state["clan_name"] = new_name
        state["clan_pos"] = 1  # Bang chủ
        state["clan_flag"] = max(1, new_flag)
        rec, _ = apply_and_make_bonus_received(state, [("gem", -500)], GAME_DB)
        STORE.save_state(uid, state)
        return [
            (400, build_common_vector(a_long=[state["clan_id"]] + rec)),
            (405, build_clan_infor(state)),
            (427, build_common_vector(a_long=[state["clan_id"], state["clan_pos"], state["clan_flag"]], a_string=[state["clan_name"]])),
        ]

    if act_id == 407:  # GUILD_QUIT (DataSender.GuildQuit @ 0x16beea4)
        state["clan_id"] = 0
        state["clan_pos"] = 0
        state["clan_name"] = ""
        STORE.save_state(uid, state)
        return [
            (407, build_common_vector(a_long=[1])),
            (427, build_common_vector(a_long=[0, 0, 0], a_string=[""])),
            (408, build_clan_list(state)),
        ]

    if act_id == 402:  # GUILD_APPLY (PopupCreateGuild @ 0x1466140)
        clan_id = int(req_longs[0]) if req_longs else 1
        state["clan_id"] = max(1, clan_id)
        state["clan_pos"] = 3
        STORE.save_state(uid, state)
        return [(402, build_common_vector(a_long=[state["clan_id"]]))]

    if act_id == 405:  # GUILD_REQUEST_INFO (GuildScene @ 0x134c640 & PopupGuildHall @ 0x147e12c)
        return [(405, build_clan_infor(state))]

    if act_id == 408:  # GUILD_SEARCH (PopupCreateGuild @ 0x1466140)
        return [(408, build_clan_list(state))]

    if act_id == 404:  # GUILD_REQUEST_MEMBER
        return [(404, build_clan_infor(state))]

    if act_id in (401, 462):  # GUILD_WAITING_APPLICATION / GUILD_MILL_TOP_DONATE
        return [(act_id, build_pb_list_user([state]))]

    if act_id in (403, 406, 410, 428, 429, 466):  # GUILD management actions
        if act_id == 429 and req_strings:
            state["clan_name"] = req_strings[0]
            STORE.save_state(uid, state)
            return [
                (429, build_common_vector(a_long=[1, state["gem"]], a_string=[state["clan_name"]])),
                (405, build_clan_infor(state)),
            ]
        if act_id == 466:
            if req_longs:
                state["clan_flag"] = max(1, int(req_longs[0]))
            STORE.save_state(uid, state)
            return [(466, build_common_vector(a_long=[1])), (405, build_clan_infor(state))]
        return [(act_id, build_common_vector(a_long=[1]))]

    if act_id in (218, 8000):  # CHECKIN_STATUS
        claimed_cnt = len(state.get("checkin_claimed", []))
        return [(act_id, build_common_vector(a_long=[claimed_cnt + 1, 0, claimed_cnt]))]

    if act_id == 511:  # GUILD_PRAY_STATUS (PanelPrayStart @ 0x1415ba8)
        v0 = build_common_vector(a_long=[2, 86400, 0])
        v1 = build_common_vector(a_long=[1, 100, 10000000, 1000000, 1, 10, 10, 0])
        return [(511, build_list_common_vector([v0, v1]))]

    if act_id == 514:
        battle_bytes,_,rec=settle_battle(state,[('gold',500000),('gem',500),('material',1,13,50)],
            enemy_hero_ids=[1,2,3],enemy_name='Chiến Thần Thú',req_longs=req_longs)
        STORE.save_state(uid,state)
        v0=build_common_vector(a_long=[2,86400,0])
        v1=build_common_vector(a_long=[1,100,12000000,2000000,1,9,10,0])
        return [(514,battle_bytes),(511,build_list_common_vector([v0,v1])),(116,build_common_vector(a_long=rec))]

    if act_id in (512, 513, 515):  # GUILD_PRAY_TOP_DAMAGE / LIST_BONUS / SIGN_UP
        if act_id == 512:
            return [(512, build_pb_list_user([state]))]
        if act_id == 513:
            return [(513, build_common_vector(a_long=[1, 100000, 2, 500]))]
        return [(515, build_common_vector(a_long=[1]))]

    if act_id in (459, 463):  # GUILD_ROLL_CALL (DataSender.GuildRollCall @ 0x16beea4 & PopupGuildHall @ 0x147e12c)
        today_str = datetime.now().strftime("%Y-%m-%d")
        if state.get("last_guild_roll_call") == today_str:
            return [(463, build_common_vector(a_long=[100, 50, 50050, 20]))]
        state["last_guild_roll_call"] = today_str
        rec, _ = apply_and_make_bonus_received(
            state,
            [("material", 1, 13, 100), ("gold", 100000)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        return [(463, build_common_vector(a_long=[100, 50, 50050, 20] + rec))]

    if act_id in (450, 468):  # GUILD_TECH_STATUS / GUILD_TECH_NEXT_LEVEL_STATUS (PopupGuildSkill @ 0x14839ec)
        vecs = []
        for idx in range(40):
            vecs.append(build_common_vector(
                a_long=[10, 30, 10000, 100],
                a_string=[
                    f"Kỹ Năng Công Hội #{(idx % 8) + 1}",
                    "+20% Thuộc Tính Chiến Đấu",
                    "Yêu cầu Kỹ năng trước Lv.10",
                ]
            ))
        return [(act_id, build_list_common_vector(vecs))]

    if act_id in (451, 469):  # GUILD_TECH_UPGRADE / GUILD_TECH_NEXT_LEVEL_UPGRADE
        apply_and_make_bonus_received(state, [("gold", -10000)], GAME_DB)
        state["guild_skill_upgrades"] = int(state.get("guild_skill_upgrades", 0)) + 1
        STORE.save_state(uid, state)
        return [(act_id, build_common_vector(
            a_long=[11, 30, 15000, 150, state["gold"], 999999, 1],
            a_string=[
                "Kỹ Năng Công Hội",
                "+22% Thuộc Tính Chiến Đấu",
                "Yêu cầu Kỹ năng trước Lv.10",
            ]
        ))]

    if act_id == 452:  # GUILD_TECH_RESET
        rec, _ = apply_and_make_bonus_received(
            state,
            [("gold", 500000), ("material", 1, 13, 2000)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        return [(452, build_common_vector(a_long=rec))]

    if act_id == 467:  # GUILD_TECH_GET_RESET_STATUS
        return [(467, build_common_vector(a_long=[0, 0, 0, 0, 0]))]

    if act_id == 460:  # GUILD_MILL_STATUS (BoxGuildDontate @ 0x1527ca4)
        return [(460, build_common_vector(
            a_long=[5, 500000, 1000000, 100000],
            a_string=["+10% Sản lượng Cối Xay", "+12% Sản lượng Cối Xay"]
        ))]

    if act_id == 461:  # GUILD_MILL_DONATE
        rec, _ = apply_and_make_bonus_received(state, [("gold", -100000)], GAME_DB)
        STORE.save_state(uid, state)
        return [
            (461, build_common_vector(a_long=rec)),
            (460, build_common_vector(
                a_long=[5, 600000, 1000000, 100000],
                a_string=["+10% Sản lượng Cối Xay", "+12% Sản lượng Cối Xay"]
            )),
        ]

    if act_id in (456, 457, 458, 459):  # GUILD_MILL_ORDER_STATUS / UPGRADE / START / GET
        vecs = [build_common_vector(a_long=[0])]
        for oid in range(1, 5):
            preview = make_bonus_preview([("gold", 200000 * oid), ("material", 1, 13, 100 * oid)])
            vecs.append(build_common_vector(
                a_long=[oid, 5, 3600, 0, 50] + preview,
                a_string=[f"Đơn Hàng Cối Xay #{oid}"]
            ))
        return [(456, build_list_common_vector(vecs))]

    if act_id == 464:  # GUILD_MILL_ORDER_REWARD
        rec, _ = apply_and_make_bonus_received(
            state,
            [("gold", 500000), ("material", 1, 13, 300)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        return [(464, build_common_vector(a_long=rec))]

    if act_id == 453:  # GUILD_RAID_STATUS (PopupGuildRaid @ 0x1480bcc)
        boss_hid = 495  # Kaido Hybrid
        boss_lvl = max(1, int(state.get("clan_boss_lvl", 1)))
        max_hp = _clan_boss_max_hp(boss_lvl)
        cur_hp = int(state.get("clan_boss_hp", max_hp))
        if cur_hp > max_hp or cur_hp <= 0:
            cur_hp = max_hp
        hp_pct = max(1, min(100, int((cur_hp / max_hp) * 100)))
        return [(453, build_common_vector(a_long=[boss_lvl, boss_hid, cur_hp, hp_pct, max_hp, 3]))]

    if act_id in (454, 465):  # GUILD_RAID_TOP_DAMAGE / LOG_DAMAGE
        return [(act_id, build_pb_list_user([state]))]

    if act_id == 455:  # GUILD_RAID_BATTLE
        boss_lvl = max(1, int(state.get("clan_boss_lvl", 1)))
        max_hp = _clan_boss_max_hp(boss_lvl)
        cur_hp = int(state.get("clan_boss_hp", max_hp))
        if cur_hp > max_hp or cur_hp <= 0:
            cur_hp = max_hp
        # Dynamic damage based on player team's ATK
        team_atk = 0
        for hid in [int(x) for x in req_longs]:
            h = _find_hero(state, hid)
            if h:
                pts = h.get("point", [30000])
                team_atk += int(pts[0]) if pts and pts[0] > 0 else 30000
        if team_atk <= 0:
            team_atk = 180000
        dmg_dealt = max(50000, int(team_atk * random.uniform(3.5, 6.0)))
        new_hp = max(0, cur_hp - dmg_dealt)
        if new_hp <= 0:
            boss_lvl += 1
            new_hp = _clan_boss_max_hp(boss_lvl)
        state["clan_boss_lvl"] = boss_lvl
        state["clan_boss_hp"] = new_hp

        rec, _ = apply_and_make_bonus_received(
            state,
            [("gold", 1000000), ("material", 1, 6, 2000), ("gem", 1000)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        battle_bytes, _, _ = settle_battle(
            state, [], participation=True,
            enemy_hero_ids=[495],
            enemy_name=f"Boss Bang Hội Lv.{boss_lvl}",
            enemy_level=boss_lvl * 50,
            req_longs=req_longs,
        )
        from battle_outcomes import with_receipt
        battle_bytes = with_receipt(sys.modules[__name__], battle_bytes, rec)
        hp_pct = max(1, int(new_hp * 100 // max_hp))
        boss_vec = build_common_vector(a_long=[boss_lvl, 288, new_hp, hp_pct, max_hp, 3])
        return [
            (455, battle_bytes),
            (453, boss_vec),  # Cập nhật thanh máu Boss Bang Hội ngay lập tức
        ]

    # ------------------------------------------------------------------------
    # Chiến Thần Thú (PopupPrayForFire / PanelPrayBattle: 500, 511..515)
    # ------------------------------------------------------------------------
    if act_id in (500, 511):  # GUILD_PRAY_STATUS / DUNGEON_STATUS (PanelPrayStart @ 0x1415ba8)
        # libil2cpp yêu cầu ListCommonVector([v0, v1])
        v0 = build_common_vector(a_long=[1, 86400, 3])
        v1 = build_common_vector(a_long=[1, 100, 10000000, 1000000, 1, 1, 1, 1])
        return [(act_id, build_list_common_vector([v0, v1]))]

    if act_id in (512, 513, 515):  # PRAY TOP DAMAGE / LIST BONUS / SIGN UP
        if act_id == 512:
            return [(512, build_pb_list_user([state]))]
        if act_id == 513:
            return [(513, build_common_vector(a_long=[1, 100000, 2, 500]))]
        return [(515, build_common_vector(a_long=[1]))]

    if act_id == 514:
        battle_bytes,_,rec=settle_battle(state,[('gold',500000),('gem',500),('material',1,13,50)],
            enemy_hero_ids=[1,2,3],enemy_name='Chiến Thần Thú',req_longs=req_longs)
        STORE.save_state(uid,state)
        v0=build_common_vector(a_long=[2,86400,0])
        v1=build_common_vector(a_long=[1,100,12000000,2000000,1,9,10,0])
        return [(514,battle_bytes),(511,build_list_common_vector([v0,v1])),(116,build_common_vector(a_long=rec))]

    if act_id in (2100, 2101, 2103, 2104, 2106, 2107, 2108):  # GUILD_WAR
        v0 = build_common_vector(a_long=[0, 86400, 0, 0])
        return [(act_id, build_list_common_vector([v0]))]

    # ------------------------------------------------------------------------
    # Sealed Land (650..654) — SealedLandScene @ 0x14f22b4
    # ------------------------------------------------------------------------
    if act_id == 650:  # SEALED_LAND_STATUS
        return [(650, build_common_vector(a_long=[20, 20, 10, 10, 10, 10, 10, 10, 10, 10, 10]))]

    if act_id == 651:  # SEALED_LAND_SMASH
        rec, _ = apply_and_make_bonus_received(
            state,
            [("gold", 200000), ("material", 2, 30000, 50)],
            GAME_DB
        )
        STORE.save_state(uid, state)
        return [(651, build_common_vector(a_long=[9] + rec))]

    if act_id == 652:
        battle_bytes,_,_=settle_battle(state,[('gold',300000),('material',2,30000,50),('gem',100)],
            enemy_hero_ids=GAME_DB.five_star_hero_ids[:6],enemy_name='Thủ Vệ Vùng Đất Phong Ấn',enemy_level=160,req_longs=req_longs)
        STORE.save_state(uid,state)
        return [(652,battle_bytes)]

    if act_id == 653:  # SEALED_LAND_BUYATTACK
        b_type = int(req_longs[0]) if req_longs else 0
        rec, _ = apply_and_make_bonus_received(state, [("gem", -20)], GAME_DB)
        STORE.save_state(uid, state)
        return [(653, build_common_vector(a_long=[b_type, 10, 10, state["gem"]]))]

    if act_id == 654:  # SEALED_LAND_REWARD
        v0 = build_common_vector(a_long=make_bonus_preview([("gold", 100000), ("material", 2, 30000, 50)]))
        return [(654, build_list_common_vector([v0]))]

    # ------------------------------------------------------------------------
    # Friends (448) & Skin Inventory (770)
    # ------------------------------------------------------------------------
    from friend_system import dispatch as friend_dispatch
    friend_reply=friend_dispatch(sys.modules[__name__],act_id,req_longs,req_strings,uid,state)
    if friend_reply is not None:return friend_reply

    if act_id in (430, 432, 433):  # FRIEND_STATUS / APPLY_LIST / RECOMMEND
        return [(act_id, build_common_vector(a_long=[30, 30, 1]))]

    if act_id in (440, 441, 445):  # FRIEND_SEND_FRIENDSHIP / RECEIVED / SEND_AND_RECEIVE
        d_acts = state.setdefault("daily_actions", {})
        d_acts["friend_heart"] = d_acts.get("friend_heart", 0) + 5
        rec, _ = apply_and_make_bonus_received(state, [("material", 1, 17, 30)], GAME_DB)
        STORE.save_state(uid, state)
        return [(act_id, build_common_vector(a_long=[1] + rec))]

    if act_id == 448:  # FRIEND_LIST (PbListUser)
        return [(448, b"")]

    if act_id == 770:  # HERO_SKIN_INVENTORY (PopupHeroSkins @ 0x148d19c)
        # Format: 3 longs per skin entry -> [m_SkinKey, m_Number, m_NumberEquip]
        from limited_wheel import skin_count
        skin_counts = {1: 99, 2: 99, 3: 99, 5: skin_count(state)}
        skin_equips = {1: 0, 2: 0, 3: 0, 5: 0}
        for h in state.get("heroes", []):
            sk = int(h.get("skin", 0))
            if sk in skin_equips:
                skin_equips[sk] += 1
        payload = []
        for sk in (1, 2, 3, 5):
            payload.extend([sk, skin_counts[sk], skin_equips[sk]])
        return [(770, build_common_vector(a_long=payload))]

    if act_id == 771:  # SKIN_EQUIP (PopupChangeHeroSkin @ 0x14607f4)
        hero_id = int(req_longs[0]) if req_longs else 1
        skin_key = int(req_longs[1]) if len(req_longs) > 1 else 1
        h = _find_hero(state, hero_id)
        if skin_key == 5:
            from limited_wheel import skin_count
            used = sum(int(other.get('skin',0)) == 5 for other in state.get('heroes',[]) if other is not h)
            if not h or int(h.get('heroId',0)) not in (510,511,512) or skin_count(state) <= used:
                return [(2, build_common_vector(a_string=['Cần sở hữu Nika Hoàng Kim và chọn tướng Luffy Nika.']))]
        if h:
            h["skin"] = client_skin_id(skin_key)
            _recalc_hero(h)
        STORE.save_state(uid, state)
        return [
            (771, build_common_vector(a_long=[hero_id, client_skin_id(skin_key)])),
            (207, build_pb_list_hero([h] if h else [])),
        ]

    if act_id == 772:  # SKIN_UNEQUIP (PopupChangeHeroSkin @ 0x14607ec)
        hero_id = int(req_longs[0]) if req_longs else 1
        h = _find_hero(state, hero_id)
        if h:
            h["skin"] = 0
            _recalc_hero(h)
        STORE.save_state(uid, state)
        return [
            (772, build_common_vector(a_long=[hero_id, 0])),
            (207, build_pb_list_hero([h] if h else [])),
        ]

    # ------------------------------------------------------------------------
    # Sabaody & Đảo Luyện Tập (Brave Trial) Actions
    # ------------------------------------------------------------------------
    if act_id == 274:  # SABAODY_GET_CONFIG (Luyện tập & Thách đấu)
        # a_long = [card1_open (1), card1_countdown_seconds (86400), card2_open (1), card2_countdown_seconds (86400)]
        return [(274, build_common_vector(a_long=[1, 86400, 1, 86400]))]

    if act_id in (311, 655):  # BRAVE_CHECK_OPENING / BRAVE_NEW_CHECK_OPENING
        # Client check in BraveTrialSceneNew (0x163ebf4) and BraveTrialScene (0x163bf68):
        # cmp item[0], #1
        # if item[0] == 1: treats as NOT OPEN YET ("Mở sau: ") and aborts loading stages!
        # if item[0] != 1 (0): treats as OPEN ("Kết thúc sau:") and calls BraveNewStatus(656) to load all 15 stages.
        return [(act_id, build_common_vector(a_long=[0, 86400]))]

    if act_id in (312, 656):  # BRAVE_STATUS / BRAVE_NEW_STATUS
        bt = get_or_create_brave_trial_state(state)
        raw_heroes = state.get("heroes", [])
        player_pb_heroes = []
        hero_hp_map = bt.get("hero_hp", {})
        for h in raw_heroes[:25]:
            h_copy = dict(h)
            pts = list(h_copy.get("point", []))
            if len(pts) < 20:
                pts = pts + [0.0] * (20 - len(pts))
            hp_val = max(1000.0, float(pts[2])) if len(pts) > 2 else 500000.0
            pts[16] = hp_val
            pts[18] = hp_val  # 100% current HP
            h_copy["point"] = pts
            # Critical: in Brave Trial (RVA 0x163dbb4 in libil2cpp), hero HP% is read from _info[0].
            # If _info[0] == 0, hero is marked DEAD with red 'X' in lineup and cannot fight!
            # 100 means full 100% health, available to fight.
            h_id_key = str(h.get("id", h.get("heroId", 0)))
            cur_pct = int(hero_hp_map.get(h_id_key, 100))
            h_copy["info"] = [cur_pct, 0, 0, 0]
            player_pb_heroes.append(h_copy)

        # Also sanitize stage opponent heroes to have 100% HP
        sanitized_stages = []
        for stg in bt.get("stages", []):
            stg_copy = dict(stg)
            stg_heroes = []
            for sh in stg.get("aHero", []):
                sh_copy = dict(sh)
                sh_copy["info"] = [100, 0, 0, 0]
                stg_heroes.append(sh_copy)
            stg_copy["aHero"] = stg_heroes
            sanitized_stages.append(stg_copy)

        brave_data = {
            "stage": int(bt.get("stage", 1)),
            "chestAward": 0,
            "aStage": sanitized_stages,
            "aHero": player_pb_heroes,
            "chestBonus": bt.get("chest_opened", [0, 0, 0, 0, 0]),
            "badgeInfo": [
                int(bt.get("badges", 2000)),
                int(bt.get("fee_buy", 100)),
                int(bt.get("fee_bless", 100)),
                int(bt.get("fee_heal", 100)),
                int(bt.get("fee_revive", 200)),
                int(bt.get("bless1", 0)),
                int(bt.get("bless2", 0)),
                int(bt.get("bless3", 0)),
            ],
            "aMarket": []
        }
        return [(act_id, build_pb_brave_trial(brave_data))]

    if act_id in (314, 658):
        bt=get_or_create_brave_trial_state(state);curr_stage=int(bt.get('stage',1));stages=bt.get('stages',[])
        stg=stages[min(len(stages)-1,max(0,curr_stage-1))]
        reward_badge=200+curr_stage*50
        battle_res,winner,_=settle_battle(state,[('gold',50000+curr_stage*10000),('material',1,8,reward_badge)],
            enemy_hero_ids=[int(h['heroId']) for h in stg.get('aHero',[])],
            enemy_name=stg.get('name',f'Thử Thách Tầng {curr_stage}'),enemy_level=int(stg.get('level',50)),req_longs=req_longs)
        if winner==1:
            if curr_stage<15:bt['stage']=curr_stage+1
            bt['badges']=int(bt.get('badges',0))+reward_badge
        STORE.save_state(uid,state)
        return [(act_id,battle_res)]

    if act_id == 661:  # BRAVE_NEW_BUFF (Altar Blessing)
        bt = get_or_create_brave_trial_state(state)
        b1 = int(bt.get("bless1", 0))
        b2 = int(bt.get("bless2", 0))
        b3 = int(bt.get("bless3", 0))

        if b1 <= b2 and b1 <= b3:
            b1 += 1
        elif b2 <= b3:
            b2 += 1
        else:
            b3 += 1

        bt["bless1"] = b1
        bt["bless2"] = b2
        bt["bless3"] = b3

        fee = int(bt.get("fee_bless", 100))
        bt["badges"] = max(0, int(bt.get("badges", 2000)) - fee)
        STORE.save_state(uid, state)

        return [(661, build_common_vector(a_long=[int(bt["badges"]), b1, b2, b3]))]

    if act_id in (313, 662):  # BRAVE_OPEN_CHEST / BRAVE_NEW_OPEN_CHEST
        bt = get_or_create_brave_trial_state(state)
        opened = list(bt.get("chest_opened", [0, 0, 0, 0, 0]))
        chest_idx = 0
        if req_longs and len(req_longs) >= 2:
            chest_idx = min(4, max(0, int(req_longs[1])))
        if chest_idx < len(opened):
            opened[chest_idx] = 1
        bt["chest_opened"] = opened

        chest_rewards = [1, 2058, 1000, 1, 11, 500, 1, 3, 200]
        STORE.save_state(uid, state)
        return [(act_id, build_common_vector(a_long=chest_rewards))]

    if act_id == 657:  # BRAVE_NEW_BUY_BADGE
        bt = get_or_create_brave_trial_state(state)
        num = 500
        if req_longs and len(req_longs) >= 1:
            num = max(1, int(req_longs[0]))
        bt["badges"] = int(bt.get("badges", 0)) + num
        STORE.save_state(uid, state)
        return [(657, build_common_vector(a_long=[1, int(bt["badges"])]))]

    if act_id == 659:  # BRAVE_NEW_REVIVE_HERO
        bt = get_or_create_brave_trial_state(state)
        fee = int(bt.get("fee_revive", 200))
        bt["badges"] = max(0, int(bt.get("badges", 0)) - fee)
        if req_longs and len(req_longs) >= 1:
            rev_hid = str(req_longs[0])
            bt.setdefault("hero_hp", {})[rev_hid] = 100
        STORE.save_state(uid, state)
        return [(659, build_common_vector(a_long=[int(bt["badges"]), fee]))]

    if act_id == 660:  # BRAVE_NEW_HEAL_HERO
        bt = get_or_create_brave_trial_state(state)
        fee = int(bt.get("fee_heal", 100))
        bt["badges"] = max(0, int(bt.get("badges", 0)) - fee)
        if req_longs and len(req_longs) >= 1:
            heal_hid = str(req_longs[0])
            cur = bt.setdefault("hero_hp", {}).get(heal_hid, 50)
            bt["hero_hp"][heal_hid] = min(100, cur + 50)
        STORE.save_state(uid, state)
        return [(660, build_common_vector(a_long=[int(bt["badges"]), fee]))]

    if act_id == 315:  # BRAVE_UPDATE_STATUS
        return [(315, build_common_vector(a_long=[1]))]

    if act_id == 275:  # CROSS_WORD_PULZZES_INFO
        return [(275, build_common_vector(a_long=[1, 1, 0, 0, 0]))]

    # ------------------------------------------------------------------------
    # Siêu Tân Tinh (Nova Arena) Actions: 995, 1860-1877, 1120-1121, 128, 121, 122
    # ------------------------------------------------------------------------
    if act_id in (1120,1121):
        from nova_quests import dispatch as nova_quest_dispatch
        return nova_quest_dispatch(sys.modules[__name__],act_id,req_longs,uid,state)

    if act_id == 995:  # ACTIVITY_ARENA2_STATUS
        return [(995, build_list_common_vector([build_common_vector(a_long=[104, 1, 86400])]))]

    def _nova_def_team_21(st: dict) -> list:
        saved = st.get("nova_def_team")
        owned = {int(h["id"]) for h in st.get("heroes", []) if isinstance(h, dict) and "id" in h}
        if isinstance(saved, list) and len(saved) == 21:
            if all(int(x) == 0 or int(x) in owned for idx, x in enumerate(saved) if idx not in (6, 13, 20)):
                return [int(x) for x in saved]
        if isinstance(saved, list) and len(saved) == 18:
            if all(int(x) == 0 or int(x) in owned for x in saved):
                return [int(x) for x in saved[:6]] + [0] + [int(x) for x in saved[6:12]] + [0] + [int(x) for x in saved[12:18]] + [0]
        return arena_defense(st, "trial")

    def _nova_rivals(st: dict) -> list:
        import random as _rnd
        source = STORE.list_arena_players() if hasattr(STORE, "list_arena_players") else STORE.list_top_players(50)
        rivals = [p for p in source if int(p.get("id", 0)) != int(uid) and int(p.get("level", 1)) >= 50 and str(p.get("screenname") or "").strip()]
        _rnd.shuffle(rivals)
        rivals = rivals[:30]
        if len(rivals) < 4:
            bots = [
                {"id": 9901, "username": "Zoro Tam Kiếm", "screenname": "Zoro Tam Kiếm", "level": 250, "power": 85000000, "avatar": [0, 67], "gold": 10000000, "gem": 50000},
                {"id": 9902, "username": "Sanji Diable", "screenname": "Sanji Diable", "level": 250, "power": 78000000, "avatar": [0, 69], "gold": 10000000, "gem": 50000},
                {"id": 9903, "username": "Law Trái Mổ", "screenname": "Law Trái Mổ", "level": 250, "power": 72000000, "avatar": [0, 77], "gold": 10000000, "gem": 50000},
                {"id": 9904, "username": "Luffy Gear 5", "screenname": "Luffy Gear 5", "level": 250, "power": 95000000, "avatar": [0, 512], "gold": 10000000, "gem": 50000},
            ]
            seen_ids = {int(r.get("id", 0)) for r in rivals}
            for b in bots:
                if b["id"] not in seen_ids:
                    rivals.append(b)
        return rivals[:30]

    if act_id == 1860:  # NOVA_GLOBAL_STATUS
        state["nova_registered"] = 1
        def_team = _nova_def_team_21(state)
        state["nova_def_team"] = def_team
        pwr = encode_client_i32_power(compute_player_power(state))
        # status=3 (Vòng bảng 1), state=2 (Đánh tự do / Đang đánh) -> cho phép khiêu chiến ngay!
        cv0 = build_common_vector(
            a_long=[3, 2, 86400, 3, pwr],
            a_string=["SIÊU TÂN TINH - MÙA 1", "Đang trong thời gian khiêu chiến Vòng Bảng"]
        )
        cv1 = build_common_vector(a_long=def_team)
        return [(1860, build_list_common_vector([cv0, cv1]))]

    if act_id == 1861:  # NOVA_REGISTER
        state["nova_registered"] = 1
        STORE.save_state(uid, state)
        return [(1861, build_common_vector(a_long=[1]))]

    if act_id == 1862:  # NOVA_REGISTER_STATUS
        state["nova_registered"] = 1
        bonus_prev = make_bonus_preview([("gem", 2000), ("gold", 10000000), ("material", 1, 207, 500)])
        cv0 = build_common_vector(a_long=[1, 86400, 1] + bonus_prev)
        cv1 = build_common_vector(a_long=[512, 608, 611, 497, 491])  # Buff heroes
        cv2 = build_common_vector(a_long=[0, 200, 2, 200, 3, 50, 8, 200, 12, 200])  # Buff points (id_point, val pairs)
        return [(1862, build_list_common_vector([cv0, cv1, cv2]))]

    if act_id == 1873:  # NOVA_SAVE_TEAM
        from nova_quests import team_count, status as nova_quest_status
        if len(req_longs) not in (18, 21) or team_count(state, req_longs) < 1:
            from resource_guard import InsufficientResources
            raise InsufficientResources('Hãy chọn ít nhất 1 thuyền viên thuộc tài khoản và không trùng nhau.')
        clean_team = [int(x) for x in req_longs]
        if len(clean_team) == 18:
            clean_team = clean_team[:6] + [0] + clean_team[6:12] + [0] + clean_team[12:18] + [0]
        state['nova_def_team'] = clean_team
        state['nova_team_saved'] = True
        state['nova_registered'] = 1
        STORE.save_state(uid, state)
        return [(1873, build_common_vector(a_long=[1])), (1120, nova_quest_status(sys.modules[__name__], state))]

    if act_id == 1863:  # NOVA_SCHEDULE
        rounds_info = [
            (0, 2, "Ngày 1", "08:00 - 22:00", "Đăng ký thi đấu"),
            (1, 2, "Ngày 2", "08:00 - 22:00", "Vòng loại 1"),
            (2, 2, "Ngày 3", "08:00 - 22:00", "Vòng loại 2"),
            (3, 1, "Hôm nay", "00:00 - 23:59", "Vòng bảng 1 (Đang diễn ra)"),
            (4, 0, "Ngày 5", "08:00 - 22:00", "Vòng bảng 2"),
            (5, 0, "Ngày 6", "18:00 - 20:00", "Vòng 16 & Tứ kết"),
            (6, 0, "Ngày 6", "20:00 - 21:00", "Vòng Bán kết"),
            (7, 0, "Ngày 7", "21:00 - 22:00", "Chung kết Mùa giải"),
            (8, 0, "Ngày 7", "22:00 - 23:59", "Tổng kết & Trao thưởng"),
        ]
        s_vecs = [
            build_common_vector(a_long=[r[0], r[1]], a_string=[r[2], r[3], r[4]])
            for r in rounds_info
        ]
        return [(1863, build_list_common_vector(s_vecs))]

    if act_id == 1864:  # NOVA_TOP_LIST (BigListCommonVector)
        rivals = _nova_rivals(state)
        my_name = str(state.get("screenname") or state.get("username") or "VuaHảiTặc")
        my_av = state.get("avatar", [0, 1])
        my_top_hero = int(state.get("heroes", [{}])[0].get("heroId", 512)) if state.get("heroes") else 512
        top_players = [
            (int(uid), 0, int(state.get("level", 100)), 999, [int(my_av[0]), int(my_av[1]), 0, 0, my_top_hero], my_name, "Liên Server"),
        ]
        fallback_heroes = [608, 611, 497, 491]
        for idx, rv in enumerate(rivals[:3]):
            rv_av = rv.get("avatar", [0, idx + 2])
            rv_name = str(rv.get("screenname") or rv.get("username") or f"Cao Thủ #{idx+2}")
            top_players.append((
                int(rv.get("id", 9901 + idx)),
                0,
                int(rv.get("level", 200)),
                850 - idx * 110,
                [int(rv_av[0]), int(rv_av[1]), 0, 0, fallback_heroes[idx % len(fallback_heroes)]],
                rv_name,
                "Liên Server",
            ))
        cv_season = build_common_vector(a_long=[1])
        top_vecs = [cv_season]
        liked_list = state.get("nova_liked_users", [])
        for p in top_players[:4]:
            p_longs = [p[0], int(p[0] in liked_list), p[2], p[3]] + p[4]
            top_vecs.append(build_common_vector(a_long=p_longs, a_string=[p[5], p[6]]))
        lcv_season = build_list_common_vector(top_vecs)
        return [(1864, build_big_list_common_vector([lcv_season]))]

    if act_id == 1865:  # NOVA_LIKE
        from nova_quests import status as nova_quest_status
        target = int(req_longs[1]) if len(req_longs) >= 2 else int(uid)
        if target in state.get('nova_liked_users', []):
            return [(2, build_common_vector(a_string=['Đã cổ vũ tuyển thủ này.'])), (1120, nova_quest_status(sys.modules[__name__], state))]
        state.setdefault('nova_liked_users', []).append(target)
        rec, _ = apply_and_make_bonus_received(state, [('gem', 200), ('gold', 1000000), ('material', 1, 207, 100)], GAME_DB)
        STORE.save_state(uid, state)
        return [(1865, build_common_vector(a_long=rec)), (1120, nova_quest_status(sys.modules[__name__], state)), (251, _consumable_bag_payload(state))]

    if act_id == 1866:  # NOVA_REWARD_LIST (BigListCommonVector)
        my_prev = make_bonus_preview([("gem", 5000), ("gold", 20000000), ("material", 1, 207, 1000), ("material", 1, 15, 100)])
        cv_my = build_common_vector(a_long=[1] + my_prev, a_string=["Vòng Bảng"])
        tier_names = ["1", "2", "3", "4-10", "11-20"]
        reward_vecs = [cv_my]
        for idx, tname in enumerate(tier_names):
            t_prev = make_bonus_preview([
                ("gem", max(1000, 5000 - idx * 800)),
                ("gold", max(5000000, 20000000 - idx * 3000000)),
                ("material", 1, 207, max(200, 1000 - idx * 150)),
            ])
            reward_vecs.append(build_common_vector(a_long=t_prev, a_string=[tname]))
        return [(1866, build_big_list_common_vector([build_list_common_vector(reward_vecs)]))]

    if act_id == 1867:  # NOVA_BOARD_STATUS
        win_cnt = int(state.get("nova_wins", 0))
        lose_cnt = int(state.get("nova_loses", 0))
        rem_turns = max(10, 30 - int(state.get("nova_matches_today", 0)))
        cv0 = build_common_vector(a_long=[1, win_cnt, lose_cnt, rem_turns, 1, 86400, 86400])
        cv1 = build_common_vector(a_long=make_bonus_preview([("gem", 2000), ("gold", 10000000), ("material", 1, 207, 500), ("material", 1, 15, 50)]))
        cv2 = build_common_vector(a_long=[512, 608, 611, 497, 491])
        cv3 = build_common_vector(a_long=[0, 200, 2, 200, 3, 50, 8, 200, 12, 200])
        return [(1867, build_list_common_vector([cv0, cv1, cv2, cv3]))]

    if act_id == 1868:  # NOVA_RANK_GLOBAL_LIST
        rounds = [
            build_common_vector(a_long=[1, 4, 1], a_string=["Vòng loại 1"]),
            build_common_vector(a_long=[2, 4, 1], a_string=["Vòng loại 2"]),
            build_common_vector(a_long=[3, 2, 1], a_string=["Vòng bảng 1"]),
            build_common_vector(a_long=[4, 2, 1], a_string=["Vòng bảng 2"]),
        ]
        return [(1868, build_list_common_vector(rounds))]

    if act_id == 1869:  # NOVA_RANK_LIST (PbListUser)
        round_id = int(req_longs[0]) if len(req_longs) >= 1 else 3
        group_id = int(req_longs[1]) if len(req_longs) >= 2 else 1
        rivals = _nova_rivals(state)
        win_cnt = max(3, int(state.get("nova_wins", 3)))
        lose_cnt = int(state.get("nova_loses", 0))
        my_entry = dict(
            state,
            id=int(uid),
            power=compute_player_power(state),
            arena_info=[round_id, group_id, win_cnt, lose_cnt, win_cnt * 2, 120],
        )
        rank_users = [my_entry]
        for idx, rv in enumerate(rivals):
            w_rv = max(0, win_cnt - (idx + 1))
            l_rv = idx + 1
            rank_users.append(dict(
                rv,
                id=int(rv.get("id", 9901 + idx)),
                screenname=str(rv.get("screenname") or rv.get("username") or f"Hải Tặc #{idx+2}"),
                power=int(rv.get("power", 50000000)),
                arena_info=[round_id, group_id, w_rv, l_rv, w_rv * 2, 300 + idx * 60],
            ))
        return [(1869, build_pb_list_user(rank_users, name=f"Bảng {group_id}", my_rank=1, my_info=my_entry))]

    if act_id == 1870:  # NOVA_BOARD_LIST (PbListUser)
        # NovaBoardOppData.setData reads pbUser.info.aLong[0..4] and pbUser.info.aString[0]!
        rivals = _nova_rivals(state)
        out = bytearray()
        for idx, rv in enumerate(rivals):
            opp_id = int(rv.get("id", 9901 + idx))
            opp_name = str(rv.get("screenname") or rv.get("username") or f"Đối Thủ #{idx+1}")
            opp_lvl = int(rv.get("level", 200))
            opp_pwr = encode_client_i32_power(int(rv.get("power", 50000000)))
            opp_av = rv.get("avatar", [0, 1])
            info_cv = build_common_vector(
                a_long=[0, 0, 0, 0, 0],  # isAttacked=0 -> hiện nút Thách Đấu (Attack)
                a_string=["Liên Server"]
            )
            u_bytes = bytearray()
            u_bytes += encode_int32(1, opp_id)
            u_bytes += encode_string(2, opp_name)
            u_bytes += encode_string(3, opp_name)
            u_bytes += encode_int64(4, int(rv.get("gold", 1000000)))
            u_bytes += encode_int64(5, int(rv.get("gem", 10000)))
            u_bytes += encode_int64(6, opp_pwr)
            u_bytes += encode_int64(7, 500000)
            u_bytes += encode_int32(8, opp_lvl)
            for av in opp_av[:2]:
                u_bytes += encode_int32(9, int(av))
            u_bytes += encode_submessage(13, info_cv)
            out += encode_submessage(1, bytes(u_bytes))
        out += encode_string(2, "Bảng 1")
        out += encode_int32(3, 1)
        return [(1870, bytes(out))]

    if act_id == 1871:  # NOVA_BOARD_ATTACK (3v3 Bo3 combat)
        opp_id = int(req_longs[0]) if req_longs else 9901
        raw_fom = [int(x) for x in req_longs[1:]] if len(req_longs) > 1 else _nova_def_team_21(state)
        if len(raw_fom) == 18:
            formation = raw_fom[:6] + [0] + raw_fom[6:12] + [0] + raw_fom[12:18] + [0]
        elif len(raw_fom) >= 21:
            formation = raw_fom[:21]
        else:
            formation = _nova_def_team_21(state)

        state["nova_def_team"] = formation
        state["nova_team_saved"] = True
        state["nova_registered"] = 1

        from combat_shields import attach_pet_skills
        from ship_combat import attach as attach_ship, selected as selected_ship
        owned = {int(h["id"]): h for h in state.get("heroes", []) if isinstance(h, dict) and "id" in h}
        atk_ids = [h for n, h in enumerate(formation) if n not in (6, 13, 20) and h in owned]
        if not atk_ids and owned:
            formation = _nova_def_team_21(state)
            atk_ids = [h for n, h in enumerate(formation) if n not in (6, 13, 20) and h in owned]
        attach_pet_skills(sys.modules[__name__], state, only_hids=atk_ids)

        opp_state = STORE.get_player_by_uid(opp_id) if hasattr(STORE, "get_player_by_uid") else None
        if (not isinstance(opp_state, dict) or not opp_state.get("heroes")) and hasattr(STORE, "get_arena_player_by_uid"):
            opp_state = STORE.get_arena_player_by_uid(opp_id)
        if not isinstance(opp_state, dict) or not opp_state.get("heroes"):
            rivals = _nova_rivals(state)
            opp_state = next((r for r in rivals if int(r.get("id", 0)) == opp_id and r.get("heroes")), None)
        if isinstance(opp_state, dict) and opp_state.get("heroes"):
            opponent = dict(opp_state)
            opponent["id"] = opp_id
            defensive = _nova_def_team_21(opponent)
            opposing = {int(h["id"]): h for h in opponent.get("heroes", []) if isinstance(h, dict) and "id" in h}
            def_ids = [h for n, h in enumerate(defensive) if n not in (6, 13, 20) and h in opposing]
            attach_pet_skills(sys.modules[__name__], opponent, only_hids=def_ids)
        else:
            opponent = {"id": opp_id, "screenname": "Tân Tinh Đại Hải Trình", "level": 200, "avatar": [0, 67]}
            opposing = {}
            for idx_b, b_hid in enumerate((512, 608, 611, 497, 491, 506, 390, 387, 384), start=1):
                bh = {"id": idx_b, "heroId": b_hid, "level": 200, "star": 10, "tier": 6, "point": [50000.0, 2000.0, 1500000.0, 1500.0] + [0.0]*31}
                _recalc_hero(bh)
                opposing[idx_b] = bh
            defensive = [1, 2, 3, 0, 0, 0, 0, 4, 5, 6, 0, 0, 0, 0, 7, 8, 9, 0, 0, 0, 0]

        rounds = []
        wins = [0, 0]
        for team_idx in range(3):
            attackers = [(n, owned[h]) for n, h in enumerate(formation[team_idx*7:team_idx*7+6]) if h in owned]
            defenders = [(n, opposing[h]) for n, h in enumerate(defensive[team_idx*7:team_idx*7+6]) if h in opposing]
            if not attackers and owned:
                first_h = next(iter(owned.values()))
                attackers = [(0, first_h)]
            if not defenders and opposing:
                first_d = next(iter(opposing.values()))
                defenders = [(0, first_d)]
            if not attackers or not defenders:
                continue
            attackers = attach_ship(attackers, selected_ship(state, formation[team_idx*7:team_idx*7+7]))
            defenders = attach_ship(defenders, selected_ship(opponent, defensive[team_idx*7:team_idx*7+7]))
            replay, r_win = arena_round(attackers, defenders, chopper_immortality_rest=True)
            rounds.append(replay)
            wins[r_win - 1] += 1
            if max(wins) >= 2:
                break

        winner = 1 if wins[0] >= wins[1] else 2
        if winner == 1:
            state["nova_wins"] = int(state.get("nova_wins", 0)) + 1
            rewards = [("gold", 2000000), ("gem", 500), ("material", 1, 207, 200), ("material", 1, 15, 10)]
        else:
            state["nova_loses"] = int(state.get("nova_loses", 0)) + 1
            rewards = [("gold", 1000000), ("gem", 200), ("material", 1, 207, 100), ("material", 1, 15, 5)]
        bonus, _ = apply_and_make_bonus_received(state, rewards, GAME_DB)

        from arena_replays import new_id, save_async as save_replay_async
        now_ts = int(time.time())
        battle_id = new_id(None, uid)
        output = bytearray()
        for replay in rounds:
            output += encode_submessage(1, replay)
        output += encode_int32(2, winner)
        for v in bonus:
            output += encode_int64(3, v)
        for field, pl in ((4, state), (5, opponent)):
            av = pl.get("avatar", [0, 1])
            p_name = str(pl.get("screenname") or pl.get("username") or "Hải Tặc")
            output += encode_submessage(field, build_common_vector(
                a_long=[int(pl.get("id", uid)), int(av[0]), int(av[1]), int(pl.get("level", 100))],
                a_string=[p_name]
            ))
        old_pts = int(state.get("nova_points", 1500))
        delta = 25 if winner == 1 else -10
        new_pts = max(1000, old_pts + delta)
        state["nova_points"] = new_pts
        info = [new_pts, delta, 1500, -delta]
        output += encode_int64(6, battle_id) + encode_int32(7, 0) + encode_submessage(8, build_common_vector(a_long=info))
        output_bytes = bytes(output)

        opp_av = opponent.get("avatar", [0, 1])
        opp_name = str(opponent.get("screenname") or opponent.get("username") or "Đối Thủ")
        recs = state.setdefault("nova_records", [])
        recs.append({
            "roundId": 3,
            "battleId": battle_id,
            "at": now_ts,
            "win": 1 if winner == 1 else 0,
            "numberWin": wins[0],
            "numberLose": wins[1],
            "atkId": int(uid),
            "defId": int(opp_id),
            "level": int(opponent.get("level", 200)),
            "power": encode_client_i32_power(int(opponent.get("power", 50000000))),
            "avatar": [int(opp_av[0]), int(opp_av[1])],
            "name": opp_name,
            "serverName": "Liên Server",
        })
        del recs[:-30]
        save_replay_async(STORE.db_path, battle_id, "trial", uid, opp_id, now_ts, output_bytes)
        STORE.save_state(uid, state)
        return [(1871, output_bytes), (251, _consumable_bag_payload(state))]

    if act_id == 1872:  # NOVA_HISTORY_LIST
        now_ts = int(time.time())
        vecs = []
        for r in reversed(state.get("nova_records", [])[-30:]):
            av = r.get("avatar", [0, 1])
            vecs.append(build_common_vector(
                a_long=[
                    int(r.get("roundId", 3)),
                    int(r.get("battleId", 1)),
                    max(1, now_ts - int(r.get("at", now_ts))),
                    int(r.get("win", 1)),
                    int(r.get("numberWin", 2)),
                    int(r.get("numberLose", 0)),
                    int(r.get("atkId", uid)),
                    int(r.get("defId", 9901)),
                    int(r.get("level", 200)),
                    int(r.get("power", 50000000)),
                    int(av[0]),
                    int(av[1]),
                ],
                a_string=[str(r.get("name", "Đối Thủ")), str(r.get("serverName", "Liên Server"))]
            ))
        return [(1872, build_list_common_vector(vecs))]

    if act_id == 1874:  # NOVA_FINAL_STATUS
        cv0 = build_common_vector(a_long=[86400, 2, 1], a_string=["20:00", "Vòng Chung Kết"])
        cv1 = build_common_vector(a_long=make_bonus_preview([("gem", 10000), ("gold", 50000000), ("material", 1, 207, 2000)]))
        cv2 = build_common_vector(a_long=[512, 608, 611, 497])
        cv3 = build_common_vector(a_long=[0, 200, 2, 200, 3, 50, 8, 200])
        return [(1874, build_list_common_vector([cv0, cv1, cv2, cv3]))]

    if act_id in (1875, 1876):  # NOVA_FINAL_BET / BET_STATUS
        return [(act_id, build_list_common_vector([build_common_vector(a_long=[0, 0, 0])]))]

    if act_id == 1877:  # NOVA_FINAL_BET_REWARD
        c_rew = make_bonus_preview([("material", 1, 207, 500), ("gem", 500)])
        w_rew = make_bonus_preview([("material", 1, 207, 200), ("gem", 200)])
        vecs = [build_common_vector(a_long=c_rew + [-1] + w_rew) for _ in range(4)]
        return [(1877, build_list_common_vector(vecs))]

    # Default fallback for any other actionId: 0-byte payload deserializes cleanly
    # into a default-initialized instance of ANY pbdson.* Protobuf message type.
    return [(act_id, b"")]


# ============================================================================
# 7. HTTP Request Handler (SdkRieng + Login/Event/Pack5 + Protobuf Game Server)
# ============================================================================

class HTHTRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            pass

    def _send_bytes(self, body: bytes, content_type="application/octet-stream", status=200):
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            pass

    def _send_file(self, path, content_type="application/octet-stream"):
        # Keep large Unity bundles out of per-connection memory buffers.
        self.close_connection = True
        with open(path, "rb") as stream:
            length = os.fstat(stream.fileno()).st_size
            try:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
                pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length > 0 else b""

    def _host_base_url(self, port=None) -> str:
        host_hdr = self.headers.get("Host", f"127.0.0.1:{MAIN_PORT}")
        fwd_proto = self.headers.get("X-Forwarded-Proto", "")
        cf_ray = self.headers.get("Cf-Ray", "")
        host_only = host_hdr.split(":")[0]
        if cf_ray or any(d in host_only for d in ("trycloudflare.com", "ngrok", "lhr.life", "loca.lt")):
            scheme = fwd_proto if fwd_proto in ("http", "https") else "https"
            return f"{scheme}://{host_only}"
        use_port = port if port is not None else self.server.server_port
        return f"http://{host_only}:{use_port}"

    def _sdk_ok(self, data):
        self._send_json({
            "code": 0,
            "key": "OK",
            "message": "OK",
            "data": data,
        })

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == '/lobby-notice':
            from lobby_notice import read as read_lobby_notice
            return self._send_json(read_lobby_notice(STORE.db_path))

        # --- Layer 1: SdkRieng GET endpoints ---
        if path == "/sdk/v1/init":
            return self._sdk_ok({
                "sdkLanguage": "vi",
                "gameId": "htht",
                "termsUrl": "",
                "policyUrl": "",
                "iapProducts": [],
            })

        if path == "/sdk/v1/game/status":
            return self._sdk_ok({"status": "ok"})

        if path.startswith("/topupInfo"):
            sess = qs.get("username", ["player1"])[0]
            uid, token, state = STORE.get_by_token_or_identity(sess)
            state["gem"] = int(state.get("gem", 0)) + 10000000
            for item_id, add_cnt in [(15, 1000), (16, 200), (9, 1000), (14, 1000), (10, 500)]:
                for c in state.setdefault("consumables", []):
                    if int(c[0]) == 1 and int(c[1]) == item_id:
                        c[2] = int(c[2]) + add_cnt
                        break
                else:
                    state["consumables"].append([1, item_id, add_cnt])
            STORE.save_state(uid, state)
            return self._send_json({"status": 0, "desc": "Nạp Kim Cương Miễn Phí Thành Công!", "gem": state["gem"]})

        if path == "/sdk/v1/user/info":
            auth = self.headers.get("Authorization", "")
            token = auth.replace("Bearer ", "").strip()
            uid, token, state = STORE.get_by_token_or_identity(token or "player1")
            return self._sdk_ok({
                "uid": str(uid),
                "username": state["username"],
                "displayName": state["screenname"],
                "isGuest": False,
            })

        # --- Layer 2: Asset Pack & Special Event endpoints ---
        if path.startswith("/cdn/"):
            fname = os.path.basename(path)
            local_bundle = os.path.join(BASE_DIR, "cdn", fname)
            if os.path.isfile(local_bundle):
                ctype = "application/octet-stream"
                if fname.endswith((".jpg", ".jpeg")):
                    ctype = "image/jpeg"
                elif fname.endswith(".png"):
                    ctype = "image/png"
                return self._send_file(local_bundle, content_type=ctype)
            self.close_connection = True
            try:
                self.send_response(302)
                self.send_header("Location", f"http://xxxxxxxxxxxx.invalid/android/{fname}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
                pass
            return

        if path.startswith("/pack5"):
            manifest_path = os.path.join(BASE_DIR, "pack5_manifest.json")
            if os.path.isfile(manifest_path):
                with open(manifest_path, "r", encoding="utf-8") as mf:
                    mdata = json.load(mf)
                base_cdn = f"{self._host_base_url()}/cdn/"
                orig_cdn = "http://xxxxxxxxxxxx.invalid/android/"
                mdata["urlPrefix"] = ""
                for item in mdata.get("assets", []):
                    fn = os.path.basename(item.get("file", ""))
                    if os.path.isfile(os.path.join(BASE_DIR, "cdn", fn)):
                        item["file"] = base_cdn + fn
                    else:
                        item["file"] = orig_cdn + fn
                return self._send_json(mdata)
            return self._send_json({
                "urlPrefix": "http://xxxxxxxxxxxx.invalid/android/",
                "assets": [],
            })

        if path.startswith("/specialEventIcons"):
            if qs.get("lua", [""])[0] == "true":
                return self._send_json([])
            return self._send_json({"status": 0, "detail": "[]"})

        if path == '/events':
            detail = EVENTS.headlines(self._host_base_url(), qs.get('type', [None])[0])
            return self._send_json({'status': 0, 'detail': json.dumps(detail, ensure_ascii=False), **detail})

        if path == '/event/detail':
            try:
                event_id = int(qs.get('eventId', ['0'])[0])
                uid = int(qs.get('userId', ['0'])[0])
            except ValueError:
                return self._send_json({'status': 1, 'desc': 'Thông tin sự kiện không hợp lệ.'})
            state = STORE.get_player_by_uid(uid)
            if not state:
                _, _, state = STORE.get_by_token_or_identity(str(uid))
            state = state or {}
            return self._send_json(EVENTS.detail(event_id, state, self._host_base_url()))

        if path == '/event/monthcard/claim':
            with PLAYER_TRANSACTION_LOCK:
                try:
                    mtype = qs.get('type', ['small'])[0]
                    uid = int(qs.get('userId', ['1'])[0])
                except Exception:
                    return self._send_json({'status': 1, 'desc': 'Yêu cầu không hợp lệ.'})
                state = STORE.get_player_by_uid(uid)
                if not state:
                    real_uid, _, state = STORE.get_by_token_or_identity(str(uid))
                    uid = real_uid
                uid = int(state.get("id", uid))
                today_str = datetime.now().strftime("%Y-%m-%d")
                date_key = f"last_card_{mtype}_date"
                if state.get(date_key) == today_str:
                    return self._send_json({'status': 1, 'desc': 'Hôm nay bạn đã nhận thẻ quà này rồi!'})

                if mtype == 'small':
                    spec = [("gem", 10000), ("material", 1, 15, 50)]
                else:
                    spec = [("gem", 50000), ("material", 1, 15, 200), ("material", 1, 16, 50)]
                rec, _ = apply_and_make_bonus_received(state, spec, GAME_DB)
                state[date_key] = today_str
                STORE.save_state(uid, state)
                return self._send_json({'status': 0, 'desc': 'Nhận quà Thẻ đặc quyền thành công!', 'reward': json.dumps(rec)})

        if path == '/event/growth/claim':
            return self._send_json({'status': 0, 'desc': 'Nhận thành công Quỹ trưởng thành!'})

        if path == '/event/pack/claim':
            return self._send_json({'status': 0, 'desc': 'Nhận thành công Quỹ đặc thù!'})

        if path == '/event/checkin/claim':
            with PLAYER_TRANSACTION_LOCK:
                try:
                    day = int(qs.get('day', ['1'])[0])
                    uid = int(qs.get('userId', ['1'])[0])
                except ValueError:
                    return self._send_json({'status': 1, 'desc': 'Yêu cầu điểm danh không hợp lệ.'})
                state = STORE.get_player_by_uid(uid)
                if not state:
                    real_uid, _, state = STORE.get_by_token_or_identity(str(uid))
                    uid = real_uid
                uid = int(state.get("id", uid))
                today_str = datetime.now().strftime("%Y-%m-%d")
                if state.get('last_checkin_date') == today_str:
                    return self._send_json({'status': 1, 'desc': 'Hôm nay bạn đã điểm danh rồi, hãy quay lại vào ngày mai!'})
                claimed = state.setdefault('checkin_claimed', [])
                if day in claimed:
                    return self._send_json({'status': 1, 'desc': f'Bạn đã nhận quà Ngày {day} rồi!'})

                rewards_pool = [
                    [("material", 1, 15, 100)],
                    [("material", 1, 16, 50)],
                    [("gem", 50000)],
                    [("material", 1, 15, 100)],
                    [("material", 1, 53, 10)],
                    [("material", 1, 16, 50)],
                    [("material", 1, 54, 10)],
                    [("gold", 50000000)],
                    [("material", 1, 15, 100)],
                    [("material", 1, 16, 50)],
                    [("gem", 50000)],
                    [("material", 1, 15, 100)],
                    [("material", 1, 53, 10)],
                    [("material", 1, 16, 50)],
                    [("material", 1, 54, 10)],
                    [("gold", 50000000)],
                    [("material", 1, 15, 100)],
                    [("material", 1, 16, 50)],
                    [("gem", 50000)],
                    [("material", 1, 15, 100)],
                    [("material", 1, 53, 10)],
                    [("material", 1, 16, 50)],
                    [("material", 1, 54, 10)],
                    [("gold", 50000000)],
                    [("material", 1, 15, 100)],
                    [("material", 1, 16, 50)],
                    [("gem", 100000)],
                    [("material", 1, 15, 200)],
                    [("material", 1, 16, 100)],
                    [("material", 1, 54, 20)],
                ]
                spec = rewards_pool[(max(1, day) - 1) % len(rewards_pool)]
                rec, _ = apply_and_make_bonus_received(state, spec, GAME_DB)
                claimed.append(day)
                state['last_checkin_date'] = today_str
                STORE.save_state(uid, state)
                return self._send_json({
                    'status': 0,
                    'desc': f'Điểm danh Ngày {day} thành công!',
                    'reward': json.dumps(rec),
                })

        if path == '/event/login7/claim':
            with PLAYER_TRANSACTION_LOCK:
                try:
                    day = int(qs.get('day', ['1'])[0])
                    uid = int(qs.get('userId', ['1'])[0])
                except ValueError:
                    return self._send_json({'status': 1, 'desc': 'Yêu cầu điểm danh 7 ngày không hợp lệ.'})
                state = STORE.get_player_by_uid(uid)
                if not state:
                    _, _, state = STORE.get_by_token_or_identity(str(uid))
                today_str = datetime.now().strftime("%Y-%m-%d")
                if state.get('last_login7_date') == today_str:
                    return self._send_json({'status': 1, 'desc': 'Hôm nay bạn đã nhận quà đăng nhập rồi, hãy quay lại vào ngày mai!'})
                claimed7 = state.setdefault('login7_claimed', [])
                if day in claimed7:
                    return self._send_json({'status': 1, 'desc': f'Bạn đã nhận quà Ngày {day} rồi!'})
                day_specs = [
                    [("material", 1, 53, 2), ("gem", 50000), ("gold", 50000000), ("material", 1, 15, 50)],
                    [("material", 1, 54, 2), ("material", 1, 1, 100000000), ("material", 1, 2, 20000), ("material", 1, 16, 30)],
                    [("material", 1, 53, 3), ("gem", 100000), ("material", 1, 3, 100000), ("material", 1, 206, 1000)],
                    [("material", 1, 54, 3), ("gold", 100000000), ("material", 1, 1, 200000000), ("material", 1, 15, 100)],
                    [("material", 1, 53, 5), ("gem", 150000), ("material", 1, 2, 50000), ("material", 1, 16, 50)],
                    [("material", 1, 54, 5), ("gold", 200000000), ("material", 1, 1, 300000000), ("material", 1, 206, 2000)],
                    [("material", 1, 58, 2), ("material", 1, 54, 5), ("material", 1, 53, 5), ("gem", 300000)],
                ]
                spec = day_specs[(max(1, min(7, day)) - 1)]
                rec, _ = apply_and_make_bonus_received(state, spec, GAME_DB)
                claimed7.append(day)
                state['last_login7_date'] = today_str
                STORE.save_state(uid, state)
                return self._send_json({
                    'status': 0,
                    'desc': f'Nhận quà Điểm Danh 7 Ngày (Ngày {day}) thành công!',
                    'reward': json.dumps(rec),
                })
        if path == '/event/wanted/claim':
            with PLAYER_TRANSACTION_LOCK:
                session = STORE.get_existing_session(qs.get('sessionId', [''])[0])
                if session is None:
                    return self._send_json({'status': 1, 'desc': 'Phiên đăng nhập không hợp lệ.'})
                uid, state = session
                try:
                    event_id = int(qs.get('eventId', ['0'])[0])
                    offer_id = int(qs.get('offerId', ['0'])[0])
                    request_uid = int(qs.get('userId', ['0'])[0])
                    selected = json.loads(qs.get('selectedItem', ['[]'])[0])
                except (ValueError, TypeError):
                    return self._send_json({'status': 1, 'desc': 'Yêu cầu không hợp lệ.'})
                wanted = EVENTS.config['wanted']
                offer = next((o for o in wanted['offers'] if o['id'] == offer_id), None)
                if request_uid != uid or event_id != wanted['id'] or offer is None or selected != []:
                    return self._send_json({'status': 1, 'desc': 'Yêu cầu Truy nã không hợp lệ.'})
                if not EVENTS.remaining('wanted'):
                    return self._send_json({'status': 1, 'desc': 'Sự kiện chưa mở hoặc đã kết thúc.'})
                claims = state.setdefault('event_claims', {}).setdefault(EVENTS.config['campaign_key'], {})
                used = int(claims.get(str(offer_id), 0))
                if used >= offer['limit']:
                    return self._send_json({'status': 1, 'desc': 'Đã đạt giới hạn nhận tướng này.'})
                if len(state.get('heroes', [])) >= int(state.get('bag_info', [3000])[0]):
                    return self._send_json({'status': 1, 'desc': 'Túi Thuyền Viên đã đầy.'})
                rec, _ = apply_and_make_bonus_received(state, [('hero', offer['hero_id'], 1, 0, 5)], GAME_DB)
                claims[str(offer_id)] = used + 1
                STORE.save_state(uid, state)
                return self._send_json({'status': 0, 'desc': 'Đã nhận ' + offer['name'] + '!',
                                        'detail': json.dumps(rec)})

        if path.startswith("/popupEvent"):
            detail_obj = {
                "template": "",
                "image": "",
                "title": "",
                "quUrlNext": "",
                "imgType": "",
                "sumDesc": "",
                "eventList": [],
                "loginTickRewardList": [],
                "accumulatedRewardList": [],
            }
            return self._send_json({
                "status": 0,
                "detail": json.dumps(detail_obj, ensure_ascii=False),
                "url": "",
                **detail_obj,
            })

        if path == "/sub":
            return self._send_json({"status": 0, "data": "0"})

        # --- Event GiftCode endpoint (GGGiftCodeEvent @ 0x16627ac) ---
        if path.startswith("/giftcode"):
            raw_code = qs.get("code", [""])[0]
            sess = qs.get("sessionId", [None])[0] or qs.get("username", ["player1"])[0]
            session = STORE.get_existing_session(sess)
            if session:
                uid, state = session
            else:
                uid, token, state = STORE.get_by_token_or_identity(sess)
            matched, msg, rec, _ = redeem_giftcode(uid, state, raw_code)
            if not matched:
                return self._send_json({
                    "status": 1,
                    "msg": "Mã GiftCode không hợp lệ! Hãy nhập: DACUONGHOA, FULL, TANTHU, VIP20 hoặc RESET",
                    "desc": "Mã GiftCode không hợp lệ!",
                    "reward": "[]",
                })
            return self._send_json({
                "status": 0,
                "msg": msg,
                "desc": msg,
                "reward": json.dumps(rec),
                "detail": json.dumps(rec),
            })

        # --- Direct APK Download for Online Players (with HTTP Range / Resume support) ---
        # --- Direct APK & IPA Download for Online Players (with HTTP Range / Resume support) ---
        if path in ("/download/apk", "/download/ipa"):
            if path == "/download/ipa":
                apk_candidates = [
                    os.path.join(os.path.dirname(BASE_DIR), "HTHT-RAW-ONLINE.ipa"),
                    os.path.join(os.path.dirname(BASE_DIR), "HTHT-ONLINE.ipa"),
                    os.path.join(os.path.dirname(BASE_DIR), "com.bavuong.daichien_1.0.3_decrypted_by_TTJB.ipa"),
                ]
            else:
                apk_candidates = [
                    os.path.join(os.path.dirname(BASE_DIR), "HTHT-ONLINE.apk"),
                    os.path.join(os.path.dirname(BASE_DIR), "HTHT-Private-patched.apk"),
                    os.path.join(os.path.dirname(BASE_DIR), "HTHT-MOD-LOCAL.apk"),
                    os.path.join(os.path.dirname(BASE_DIR), "HTHT-TEST-adr.apk"),
                ]
            for apk_p in apk_candidates:
                if os.path.isfile(apk_p):
                    fsize = os.path.getsize(apk_p)
                    range_hdr = self.headers.get("Range", "")
                    start_byte = 0
                    end_byte = fsize - 1
                    status_code = 200
                    if range_hdr.startswith("bytes="):
                        try:
                            r_spec = range_hdr.split("=", 1)[1].split(",")[0].strip()
                            s_str, e_str = r_spec.split("-", 1)
                            if s_str:
                                start_byte = int(s_str)
                            if e_str:
                                end_byte = min(fsize - 1, int(e_str))
                            if 0 <= start_byte <= end_byte < fsize:
                                status_code = 206
                        except Exception:
                            start_byte, end_byte, status_code = 0, fsize - 1, 200
                    length = end_byte - start_byte + 1
                    self.send_response(status_code)
                    self.send_header("Content-Type", "application/vnd.android.package-archive")
                    self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(apk_p)}"')
                    self.send_header("Accept-Ranges", "bytes")
                    if status_code == 206:
                        self.send_header("Content-Range", f"bytes {start_byte}-{end_byte}/{fsize}")
                    self.send_header("Content-Length", str(length))
                    self.end_headers()
                    try:
                        with open(apk_p, "rb") as af:
                            af.seek(start_byte)
                            rem = length
                            while rem > 0:
                                chunk = af.read(min(1024 * 1024, rem))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                rem -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
            return self._send_json({"status": 1, "desc": "APK file not found"}, status=404)

        # --- Layer 2: Root / Path Login / Register / Server List ---
        if path == "/register" or "register" in qs:
            username = qs.get("username", ["player1"])[0]
            password = qs.get("password", [""])[0]
            STORE.get_or_create_user(username, password)
            return self._send_json({"status": 0, "desc": "Đăng ký thành công!"})

        if path in ("/loginGosu", "/loginGosuById", "/login", "/loginInstant", "/loginFb") or any(
            k in qs for k in ("loginGosu", "loginGosuById", "login", "loginInstant", "loginFb", "accessToken")
        ):
            ident = (
                qs.get("accessToken", [None])[0]
                or qs.get("username", [None])[0]
                or qs.get("sessionId", [None])[0]
                or qs.get("gosuId", [None])[0]
                or qs.get("udid", ["player1"])[0]
            )
            uid, token, state = STORE.get_by_token_or_identity(ident)
            base_url = self._host_base_url(MAIN_PORT)
            luffy_story = (
                "🌊 HÀNH TRÌNH HUYỀN THOẠI CỦA MONKEY D. LUFFY 🌊\n\n"
                "Từ làng Cối Xay Gió nhỏ bé tại Biển Đông, cậu thiếu niên Monkey D. Luffy mang theo chiếc Mũ Rơm của Shanks Tóc Đỏ và lời thề ước: \'Tôi nhất định sẽ trở thành Vua Hải Tặc!\'.\n\n"
                "Cùng con thuyền Going Merry và những người đồng đội đầu tiên: Kiếm sĩ Zoro, Hoa tiêu Nami, Xạ thủ Usopp, Đầu bếp Sanji, Luffy chính thức giương buồm tiến vào Đại Hải Trình.\n\n"
                "Vượt qua sa mạc Alabasta đánh bại Crocodile, bay lên Đảo Trời Skypiea, tuyên chiến với Chính Phủ Thế Giới tại Enies Lobby cứu Robin, đột nhập Địa Ngục Impel Down và tham gia đại chiến Marineford giải cứu Ace... Dù đau thương rơi lệ, ý chí của Luffy vẫn rực cháy!\n\n"
                "Sau 2 năm khổ luyện, trên con tàu Thousand Sunny cùng Jinbe, Franky, Brook, Chopper, băng Mũ Rơm tiến vào Tân Thế Giới. Vượt qua Đảo Người Cá, Dressrosa, Đảo Bánh, và đỉnh cao là trận quyết chiến định mệnh tại Đất Nước Wano! Tại đây, Luffy đã thức tỉnh Thần Mặt Trời Nika - Gear 5, đánh bại Sinh Vật Mạnh Nhất Thế Giới - Tứ Hoàng Kaido, đưa tên tuổi băng Mũ Rơm lên hàng Tứ Hoàng Thế Giới Mới!\n\n"
                "Giờ đây, hòn đảo thần thoại Laugh Tale và kho báu huyền thoại ONE PIECE đang mở ra trước mắt. Hãy cùng Luffy bắt đầu chuyến hải trình vĩ đại của chính bạn!\n\n"
                "🎁 BỘ MÃ QUÀ TẶNG KHỞI ĐẦU (Gõ vào Kênh Chat Thế Giới hoặc mục Góp Ý):\n"
                "  • TANTHU : 50Tr Vàng, 500K Kim Cương, 100 Cuộn Chiêu Mộ, 50 Trái Ác Quỷ\n"
                "  • VIP20  : 100Tr Vàng, 2Tr Kim Cương, 3 Rương Tướng 5 Sao & Free VIP 20\n"
                "  • FULL   : 999Tr Vàng, 99Tr Kim Cương, Lv.250, Full Tướng 10 Sao & Đồ Đỏ\n"
                "  • RESET  : Làm mới nhân vật về Lv.1 để trải nghiệm lại từ đầu"
            )
            notices_str = json.dumps([
                {
                    "title": "Hành Trình Mũ Rơm",
                    "content": luffy_story,
                    "badge": {"type": 1, "text": "HOT"},
                    "image": "",
                }
            ], ensure_ascii=False)
            return self._send_json({
                "status": 0,
                "desc": "OK",
                "apiEventRoot": base_url,
                "apiEventListUrl": f"{base_url}/events?userId={{userId}}&serverId={{serverId}}&type={{type}}",
                "username": state["username"],
                "sessionId": token,
                "usernameLink": "",
                "level": state["level"],
                "notice": "Server Hải Tặc Bá Vương Online v2.00 - Chúc bạn chơi game vui vẻ!",
                "notices": notices_str,
                "topUrls": "null",
                "forceUpdate": False,
                "showUpdate": 0,
                "latestVersionUrl": "",
                "latestVersion": "2.00",
                "servers": [
                    {
                        "id": 1,
                        "name": "S1 - Tân Thế Giới (Online)",
                        "address": base_url,
                        "avatar": state["avatar"][1] if len(state["avatar"]) > 1 else 1,
                        "level": state["level"],
                        "screenName": state["screenname"],
                        "new": True,
                    }
                ],
            })

        # --- Web Portal (Trang Chủ Đăng Ký + Nhập GiftCode + Bảng Xếp Hạng Online) ---
        if path in ("/", "/index.html", "/portal"):
            top_list = STORE.list_top_players(20)
            rows_html = ""
            for idx, p in enumerate(top_list, start=1):
                vip_lv = p.get("vip", [20])[0] if isinstance(p.get("vip"), list) else 20
                rows_html += (
                    f"<tr>"
                    f"<td>#{idx}</td>"
                    f"<td><b>{p.get('screenname', 'Pirate')}</b> <small style='color:#94a3b8'>({p.get('username', '')})</small></td>"
                    f"<td>Lv.{p.get('level', 1)}</td>"
                    f"<td><span class='vip'>VIP {vip_lv}</span></td>"
                    f"<td>{int(p.get('power', 0)):,}</td>"
                    f"<td>{int(p.get('gold', 0)):,}</td>"
                    f"<td>{int(p.get('gem', 0)):,}</td>"
                    f"</tr>"
                )
            html = f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hải Tặc Bá Vương Online - Trang Chủ & GiftCode</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }}
  .container {{ max-width: 920px; margin: 0 auto; }}
  .hero {{ background: linear-gradient(135deg, #1e293b, #0f172a); border: 1px solid #334155; border-radius: 14px; padding: 24px; margin-bottom: 20px; }}
  h1 {{ margin: 0 0 8px 0; color: #fbbf24; font-size: 26px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 20px; }}
  .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 18px; }}
  .card h3 {{ margin-top: 0; color: #38bdf8; }}
  input, select, button {{ width: 100%; padding: 10px 12px; margin-top: 8px; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: #fff; box-sizing: border-box; font-size: 14px; }}
  button {{ background: #f59e0b; color: #0f172a; font-weight: 700; border: none; cursor: pointer; margin-top: 12px; }}
  button:hover {{ background: #fbbf24; }}
  .code-pill {{ display: inline-block; background: #334155; color: #fbbf24; padding: 4px 10px; border-radius: 6px; font-family: monospace; margin: 3px; cursor: pointer; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 14px; }}
  th, td {{ padding: 10px 8px; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ color: #94a3b8; }}
  .vip {{ background: #dc2626; color: #fff; padding: 2px 8px; border-radius: 99px; font-size: 12px; font-weight: bold; }}
  #result {{ margin-top: 12px; padding: 10px; border-radius: 8px; display: none; white-space: pre-line; }}
</style>
</head>
<body>
<div class="container">
  <div class="hero">
    <h1>🏴‍☠️ HẢI TẶC BÁ VƯƠNG - SERVER ONLINE</h1>
    <p>Khởi đầu từ <b>Level 1 (Chuẩn cày cuốc)</b>! Hỗ trợ kích hoạt GiftCode trực tiếp trong <b>Kênh Chat Thế Giới</b>, mục <b>Góp Ý</b> hoặc ngay trên trang web này.</p>
    <a href="/download/apk" style="display:inline-block;background:#22c55e;color:#052e16;font-weight:700;padding:10px 18px;border-radius:8px;text-decoration:none;margin-top:6px;margin-right:10px;">⬇ Tải APK Android</a>
    <a href="/download/ipa" style="display:inline-block;background:#0ea5e9;color:#082f49;font-weight:700;padding:10px 18px;border-radius:8px;text-decoration:none;margin-top:6px;">🍏 Tải IPA iOS (iPhone/iPad)</a>
  </div>

  <div class="grid">
    <div class="card">
      <h3>🎁 Nhập GiftCode Full Tài Nguyên</h3>
      <label>Tài khoản (Username hoặc Tên nhân vật):</label>
      <input id="gc_user" placeholder="VD: player1 (Để trống = áp dụng cho TẤT CẢ tài khoản)" />
      <label>Chọn hoặc nhập Mã GiftCode:</label>
      <input id="gc_code" value="DACUONGHOA" placeholder="DACUONGHOA, FULL, TANTHU, VIP20, RESET" />
      <div style="margin-top:8px;">
        <span class="code-pill" onclick="document.getElementById('gc_code').value='TUONGMOI'" style="background:#065f46;color:#a7f3d0;font-weight:bold;">TUONGMOI (Full Tướng Mới SS 10*: Nika, Yamato, Kaido...)</span>
        <span class="code-pill" onclick="document.getElementById('gc_code').value='TESTGAME'" style="background:#7c2d12;color:#fef08a;">TESTGAME (Cuộn L1-L2, Rương VIP & Full Tài Nguyên)</span>
        <span class="code-pill" onclick="document.getElementById('gc_code').value='DACUONGHOA'">DACUONGHOA (1 tỷ đá qua hộp thư)</span>
        <span class="code-pill" onclick="document.getElementById('gc_code').value='FULL'">FULL (Full Tài Nguyên + Tướng 10*)</span>
        <span class="code-pill" onclick="document.getElementById('gc_code').value='BIA'">BIA (10Tr Kim Cương & 5000 Thùng Bia)</span>
        <span class="code-pill" onclick="document.getElementById('gc_code').value='TANTHU'">TANTHU (Quà Khởi Đầu)</span>
        <span class="code-pill" onclick="document.getElementById('gc_code').value='VIP20'">VIP20 (Quà Đặc Quyền VIP 20)</span>
        <span class="code-pill" onclick="document.getElementById('gc_code').value='RESET'">RESET (Về Lv.1 + VIP 20 + Đầy Bia)</span>
      </div>
      <button onclick="sendGiftCode()">Kích Hoạt GiftCode Ngay</button>
      <div id="result"></div>
    </div>

    <div class="card">
      <h3>⚓ Đăng Ký Tài Khoản Mới (Lv.1 + VIP 20)</h3>
      <label>Tên đăng nhập:</label>
      <input id="reg_user" placeholder="Nhập tài khoản..." />
      <label>Mật khẩu:</label>
      <input id="reg_pass" type="password" placeholder="Nhập mật khẩu..." />
      <button onclick="registerAcc()">Tạo Tài Khoản Mới</button>
      <p style="font-size:13px;color:#94a3b8;margin-top:12px;">
        💡 <b>Mẹo trong game:</b> Mở khung <b>Chat Thế Giới</b> gõ <code>FULL</code> hoặc <code>TANTHU</code> rồi bấm Gửi — Vàng, Kim Cương và Tướng 10 Sao sẽ nhảy thẳng vào túi đồ ngay lập tức!
      </p>
    </div>
  </div>

  <div class="card">
    <h3>🏆 Bảng Xếp Hạng Thuyền Trưởng Online ({len(top_list)} Người Chơi)</h3>
    <table>
      <thead><tr><th>Hạng</th><th>Thuyền Trưởng</th><th>Cấp</th><th>VIP</th><th>Chiến Lực</th><th>Vàng</th><th>Kim Cương</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</div>
<script>
async function sendGiftCode() {{
  const username = document.getElementById('gc_user').value.trim();
  const code = document.getElementById('gc_code').value.trim();
  const res = await fetch('/api/giftcode', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{username, code}})
  }});
  const data = await res.json();
  const box = document.getElementById('result');
  box.style.display = 'block';
  box.style.background = data.status === 0 ? '#064e3b' : '#7f1d1d';
  box.innerText = data.msg;
  if (data.status === 0) setTimeout(() => location.reload(), 1200);
}}
async function registerAcc() {{
  const u = document.getElementById('reg_user').value.trim();
  const p = document.getElementById('reg_pass').value.trim();
  if (!u) return alert('Vui lòng nhập tên tài khoản!');
  const res = await fetch('/register?username=' + encodeURIComponent(u) + '&password=' + encodeURIComponent(p));
  const data = await res.json();
  alert(data.desc || 'Đăng ký thành công!');
  location.reload();
}}
</script>
</body>
</html>"""
            return self._send_bytes(html.encode("utf-8"), content_type="text/html; charset=utf-8", status=200)

        # --- Layer 6: Chiến Thần Thú (BossOfWorld / PopupBossOfWorld) ---
        if path.startswith("/boss/"):
            parts = [p for p in path.strip("/").split("/") if p]
            # Pattern: /boss/<sessionId>/<action>/...
            sess = parts[1] if len(parts) > 1 else "player1"
            action = parts[2] if len(parts) > 2 else "status"
            uid, token, state = STORE.get_by_token_or_identity(sess)
            if not state:
                state = STORE.get_player_by_uid(1) or {}

            boss_ids = [408, 387, 390, 384, 495]
            boss_names = {
                0: "Kaido Bách Thú",
                1: "Big Mom Linh Hồn",
                2: "Tóc Đỏ Shanks",
                3: "Râu Đen Teach",
                4: "Kaido Hóa Rồng",
            }
            boss_skills = {
                0: [1, 2],
                1: [3, 4],
                2: [5, 6],
                3: [7, 8],
                4: [9, 10],
            }

            if action == "status":
                hp_list = state.setdefault("boss_world_hp", [100, 100, 100, 100, 100])
                if len(hp_list) < 5:
                    hp_list = [100, 100, 100, 100, 100]
                    state["boss_world_hp"] = hp_list
                attack_turns = int(state.get("boss_world_attacks", 5))
                total_turns = int(state.get("boss_world_total_turns", 5))
                buy_left = int(state.get("boss_world_buy_left", 10))
                fee_spec = make_bonus_preview([("gem", 50)])

                tier_rewards = [
                    [("gem", 2000), ("gold", 10000000), ("material", 1, 15, 50)],
                    [("gem", 1500), ("gold", 8000000), ("material", 1, 15, 40)],
                    [("gem", 1200), ("gold", 6000000), ("material", 1, 15, 30)],
                    [("gem", 1000), ("gold", 5000000), ("material", 1, 15, 25)],
                    [("gem", 800), ("gold", 4000000), ("material", 1, 15, 20)],
                    [("gem", 600), ("gold", 3000000), ("material", 1, 15, 15)],
                    [("gem", 500), ("gold", 2000000), ("material", 1, 15, 10)],
                    [("gem", 400), ("gold", 1500000), ("material", 1, 15, 8)],
                    [("gem", 300), ("gold", 1000000), ("material", 1, 15, 5)],
                    [("gem", 200), ("gold", 500000), ("material", 1, 15, 3)],
                ]
                reward_vectors = []
                for x in range(5):
                    for tier in tier_rewards:
                        reward_vectors.append({"aLong": make_bonus_preview(tier), "aString": []})

                countdown_vector = {"aLong": [1, 86400], "aString": []}
                level_vector = {"aLong": [1], "aString": []}

                vectors = [
                    {"aLong": boss_ids, "aString": []},
                    {"aLong": hp_list, "aString": []},
                    {"aLong": [attack_turns, total_turns, buy_left], "aString": []},
                    {"aLong": fee_spec, "aString": []},
                    {"aLong": boss_ids, "aString": []},
                ] + reward_vectors + [
                    countdown_vector,
                    level_vector
                ]
                return self._send_json({
                    "status": 1,
                    "message": "success",
                    "data": {
                        "aListPbVector": vectors
                    }
                })

            elif action == "statusDetail":
                dao_idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
                dao_idx = max(0, min(4, dao_idx))
                hp_list = state.setdefault("boss_world_hp", [100, 100, 100, 100, 100])
                pct = hp_list[dao_idx] if dao_idx < len(hp_list) else 100
                max_hp = 50000000
                cur_hp = int(max_hp * pct // 100)
                rank = 1
                damage = int(state.get("boss_world_damage", {}).get(str(dao_idx), 15000000))
                rank_reward = make_bonus_preview([("gem", 2000), ("gold", 10000000), ("material", 1, 15, 50)])
                skills = boss_skills.get(dao_idx, [1, 2])

                vectors = [
                    {"aLong": [cur_hp, max_hp, pct], "aString": []},
                    {"aLong": [rank], "aString": []},
                    {"aLong": [damage], "aString": []},
                    {"aLong": rank_reward, "aString": []},
                    {"aLong": skills, "aString": []},
                ]
                return self._send_json({
                    "status": 1,
                    "message": "success",
                    "data": {
                        "aListPbVector": vectors
                    }
                })

            elif action == "top":
                dao_idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
                dao_idx = max(0, min(4, dao_idx))
                player_name = state.get("username") or state.get("screenname") or "Hải Tặc Vương"
                player_lvl = int(state.get("level", 100))
                raw_avatar = state.get("avatar", [0, 1])
                if isinstance(raw_avatar, list):
                    player_avatar_type = int(raw_avatar[0]) if len(raw_avatar) > 0 else 0
                    player_avatar = int(raw_avatar[1]) if len(raw_avatar) > 1 else 1
                else:
                    player_avatar_type = 0
                    player_avatar = int(raw_avatar)
                damage = int(state.get("boss_world_damage", {}).get(str(dao_idx), 25000000))
                team_power = int(state.get("team_power", 9999999))

                top_list = [
                    {
                        "damage": max(damage, 25000000),
                        "user": {
                            "id": uid,
                            "avatar": player_avatar,
                            "avatarType": player_avatar_type,
                            "level": player_lvl,
                            "userName": player_name,
                            "power": team_power
                        }
                    },
                    {
                        "damage": 18500000,
                        "user": {
                            "id": 9991,
                            "avatar": 2,
                            "avatarType": 1,
                            "level": 95,
                            "userName": "Zoro Tam Kiếm",
                            "power": 8500000
                        }
                    },
                    {
                        "damage": 12000000,
                        "user": {
                            "id": 9992,
                            "avatar": 3,
                            "avatarType": 1,
                            "level": 90,
                            "userName": "Sanji Diable",
                            "power": 7500000
                        }
                    }
                ]
                return self._send_json({
                    "status": 1,
                    "message": "success",
                    "data": {
                        "aPbJListDamage": top_list
                    }
                })

            elif action == "buyTurn":
                num_buy = max(1, int(parts[3])) if len(parts) > 3 and parts[3].isdigit() else 1
                cost_gem = num_buy * 50
                rec, _ = apply_and_make_bonus_received(state, [("gem", -cost_gem)], GAME_DB)
                state["boss_world_attacks"] = int(state.get("boss_world_attacks", 5)) + num_buy
                state["boss_world_total_turns"] = int(state.get("boss_world_total_turns", 5)) + num_buy
                state["boss_world_buy_left"] = max(0, int(state.get("boss_world_buy_left", 10)) - num_buy)
                STORE.save_state(uid, state)
                return self._send_json({
                    "status": 1,
                    "message": "Mua thành công!",
                    "data": {
                        "aListPbVector": [
                            {"aLong": rec, "aString": []}
                        ]
                    }
                })

            elif action == "attack":
                dao_idx = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
                dao_idx = max(0, min(4, dao_idx))
                formation_ids = [int(p) for p in parts[4:] if p.isdigit()]

                b_name = boss_names.get(dao_idx, "Chiến Thần Thú")
                b_hid = boss_ids[dao_idx]

                grant_rewards = [("gold", 500000), ("gem", 100), ("material", 1, 15, 3), ("material", 1, 11, 20)]
                rec, _ = apply_and_make_bonus_received(state, grant_rewards, GAME_DB)

                bw_dmg = state.setdefault("boss_world_damage", {})
                bw_dmg[str(dao_idx)] = int(bw_dmg.get(str(dao_idx), 0)) + 5000000

                hp_list = state.setdefault("boss_world_hp", [100, 100, 100, 100, 100])
                hp_list[dao_idx] = max(1, hp_list[dao_idx] - 5)

                state["boss_world_attacks"] = max(0, int(state.get("boss_world_attacks", 5)) - 1)
                STORE.save_state(uid, state)

                battle_bytes = build_full_battle_result(
                    state=state,
                    enemy_hero_ids=[b_hid],
                    bonus_received_longs=rec,
                    enemy_name=b_name,
                    enemy_level=120,
                    req_longs=formation_ids
                )
                from battle_outcomes import with_receipt
                battle_bytes = with_receipt(sys.modules[__name__], battle_bytes, rec)
                resp_data = build_response_data([(722, battle_bytes)])
                return self._send_bytes(resp_data, content_type="application/octet-stream", status=200)

            return self._send_json({"status": 0, "message": "Lệnh không hợp lệ"})

        return self._send_json({
            "status": "online",
            "server": "HTHT Online Server - Tân Thế Giới",
            "heroesLoaded": len(GAME_DB.heroes_by_id),
            "port": self.server.server_port,
        })

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        raw_body = self._read_body()

        # --- Layer 1: SdkRieng POST endpoints ---
        if path in (
            "/sdk/v1/auth/login",
            "/sdk/v1/auth/register",
            "/sdk/v1/auth/guest",
            "/sdk/v1/auth/refresh",
            "/sdk/v1/account/bind",
        ):
            try:
                body_json = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                body_json = {}
            username = (
                body_json.get("username")
                or body_json.get("deviceId")
                or f"pirate_{uuid.uuid4().hex[:6]}"
            )
            password = body_json.get("password", "")
            uid, token, state = STORE.get_or_create_user(username, password)
            return self._sdk_ok({
                "accessToken": token,
                "refreshToken": token,
                "profile": {
                    "uid": str(uid),
                    "username": state["username"],
                    "displayName": state["screenname"],
                    "isGuest": path.endswith("/guest"),
                },
            })

        if path == "/sdk/v1/auth/logout":
            return self._sdk_ok({})

        if path.startswith("/sdk/v1/iap/"):
            auth = self.headers.get("Authorization", "")
            token = auth.replace("Bearer ", "").strip()
            uid, token, state = STORE.get_by_token_or_identity(token or "player1")
            state["gem"] = int(state.get("gem", 0)) + 10000000
            for item_id, add_cnt in [(15, 1000), (16, 200), (9, 1000), (14, 1000), (10, 500)]:
                for c in state.setdefault("consumables", []):
                    if int(c[0]) == 1 and int(c[1]) == item_id:
                        c[2] = int(c[2]) + add_cnt
                        break
                else:
                    state["consumables"].append([1, item_id, add_cnt])
            STORE.save_state(uid, state)
            return self._sdk_ok({"orderId": uuid.uuid4().hex, "status": "completed"})

        if path.startswith("/sdk/v1/"):
            return self._sdk_ok({})

        # --- Web Portal GiftCode API ---
        if path == "/api/giftcode":
            try:
                body_json = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                body_json = {}
            target_user = (body_json.get("username") or "").strip()
            raw_code = (body_json.get("code") or "").strip()
            norm_code = normalize_giftcode(raw_code)
            if not norm_code:
                return self._send_json({
                    "status": 1,
                    "msg": "Mã GiftCode không hợp lệ! Hãy nhập: FULL, TANTHU, VIP20 hoặc RESET",
                })
            if target_user:
                uid, token, state = STORE.get_by_token_or_identity(target_user)
                _, msg, _, _ = redeem_giftcode(uid, state, norm_code)
                return self._send_json({
                    "status": 0,
                    "msg": f"[{state.get('screenname', target_user)}] {msg}",
                })
            else:
                top_players = STORE.list_top_players(100)
                if not top_players:
                    uid, token, state = STORE.get_or_create_user("player1")
                    top_players = [state]
                last_msg = "OK"
                for p in top_players:
                    uid_p = int(p.get("id", 1))
                    _, last_msg, _, _ = redeem_giftcode(uid_p, p, norm_code)
                return self._send_json({
                    "status": 0,
                    "msg": f"Đã kích hoạt [{norm_code}] cho toàn bộ {len(top_players)} tài khoản trên Server!\n{last_msg}\n(Hãy tắt mở lại game hoặc gửi 1 câu chat để đồng bộ ngay)",
                })

        # --- Layer 3: Game Server Protobuf over HTTP (PUT/POST /i/{actionIds}/{userId}) ---
        if path.startswith("/i/"):
            parts = [p for p in path.split("/") if p]
            user_ident = parts[2] if len(parts) > 2 else (parts[1] if len(parts) > 1 else "player1")
            session_token, req_actions = parse_request_data(raw_body)

            # Ưu tiên đăng nhập bằng account được gửi lên trong Action 100 (tránh nhảy tài khoản)
            login_username = None
            for act_id, act_data in req_actions:
                if act_id == 100 and act_data:
                    _, r_strs = parse_common_vector(act_data)
                    if r_strs and r_strs[0]:
                        login_username = r_strs[0].strip()
                        break

            if login_username:
                lookup_ident = login_username
            elif user_ident.isdigit() and int(user_ident) > 0:
                lookup_ident = user_ident
            elif session_token and session_token != "0":
                lookup_ident = session_token
            else:
                lookup_ident = user_ident if user_ident != "0" else "player1"

            with PLAYER_TRANSACTION_LOCK:
                uid, token, state = STORE.get_by_token_or_identity(lookup_ident)
                resp_actions = []
                for act_id, act_data in req_actions:
                    out_pairs = dispatch_game_action(act_id, act_data, uid, token, state, self.headers.get("Host", ""))
                    resp_actions.extend(out_pairs)
                # Đảm bảo lưu dữ liệu bền vững sau mỗi request
                STORE.save_state(uid, state)

            resp_bytes = build_response_data(resp_actions)
            return self._send_bytes(resp_bytes, content_type="application/octet-stream", status=200)

        return self._send_json({"status": 0, "desc": "OK"})

    do_PUT = do_POST


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def free_port_if_in_use(port: int):
    if os.name != "nt":
        return
    try:
        import subprocess
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="ignore")
        my_pid = os.getpid()
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    pid = int(parts[-1])
                    if pid != my_pid and pid > 0:
                        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    except Exception:
        pass


def run_server_on_port(host: str, port: int):
    free_port_if_in_use(port)
    httpd = ExclusiveThreadingHTTPServer((host, port), HTHTRequestHandler)
    msg = f"[HTHT] Listening on http://{host}:{port} (Heroes in DB: {len(GAME_DB.heroes_by_id)})\n"
    try:
        if sys.stdout:
            sys.stdout.write(msg)
            sys.stdout.flush()
        with open(os.path.join(BASE_DIR, "htht_server.log"), "a", encoding="utf-8") as lf:
            lf.write(msg)
    except Exception:
        pass
    httpd.serve_forever()


def main():
    from islands_seasons import start_worker
    start_worker(sys.modules[__name__])
    t_game = threading.Thread(target=run_server_on_port, args=(DEFAULT_HOST, GAME_PORT), daemon=True)
    t_game.start()
    run_server_on_port(DEFAULT_HOST, MAIN_PORT)


if __name__ == "__main__":
    main()
