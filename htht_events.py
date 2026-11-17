"""Event JSON shapes verified against the bundled GGEvent client parsers.

Headlines: GGEventScene.ProcessKEvent 0x170e74c.
Templates verified in libil2cpp.so 0x170e550:
- Template 24: GGGiftCodeEvent (0x16628d4)
- Template 10: GGMonthCardEvent (0x16662a0)
- Template 114: GGGrowthFundEventV2 (0x1662fec)
- Template 9: GGPackValueEvent (0x1667430)
"""
import json
import time
from datetime import datetime
from pathlib import Path


def bonus_items_from_preview(preview):
    """Decode a complete mail BonusPreview before granting anything."""
    arities = {1: 1, 2: 1, 3: 3, 4: 1, 5: 1, 6: 2, 7: 3, 8: 2, 9: 1}
    kinds = {1: 'gold', 2: 'gem', 3: 'material', 4: 'exp', 5: 'hero',
             6: 'equip', 7: 'artifact', 8: 'avatar', 9: 'vip_exp'}
    result = []
    pos = 0
    while pos < len(preview):
        tag = int(preview[pos])
        count = arities.get(tag)
        if count is None or pos + count >= len(preview):
            raise ValueError('Invalid mail reward')
        values = [int(v) for v in preview[pos + 1:pos + count + 1]]
        if any(v < 0 for v in values):
            raise ValueError('Negative mail reward')
        result.append((kinds[tag], *values))
        pos += count + 1
    return result


class EventCatalog:
    def __init__(self, path):
        self.config = json.loads(Path(path).read_text(encoding='utf-8'))
        self.start = int(datetime.fromisoformat(self.config['starts_at']).timestamp())

    def remaining(self, kind='giftcode', now=None):
        now = int(time.time()) if now is None else int(now)
        if now < self.start:
            return 86400 * 30
        return max(86400 * 10, self.start + int(self.config.get(kind + '_days', 30)) * 86400 - now)

    def headlines(self, base, tab=None, now=None):
        left = self.remaining('giftcode', now)
        result = [
            # 0a. Điểm danh hàng ngày (Template 106 = PanelCheckinEvent @ 0x13dafd0)
            {
                'id': 1106,
                'template': 106,
                'image': 'Event/event_11',
                'name': 'Điểm danh hàng ngày',
                'timeRemain': left,
                'notice': True,
                'urlNext': "/event/detail?eventId=1106&userId={userId}",
            },
            # 0b. Điểm danh 7 ngày (Template 6 = GG7DaysLoginEvent @ 0x136dfc8)
            {
                'id': 1006,
                'template': 6,
                'image': 'Event/event_13',
                'name': 'Điểm danh 7 ngày',
                'timeRemain': left,
                'notice': True,
                'urlNext': "/event/detail?eventId=1006&userId={userId}",
            },
            # 1. Giftcode (Template 24 = GGGiftCodeEvent)
            {
                'id': 1024,
                'template': 24,
                'image': 'Event/event_giftcode',
                'name': 'Giftcode',
                'timeRemain': left,
                'notice': True,
                'urlNext': "/event/detail?eventId=1024&userId={userId}",
            },
            # 2. Thẻ quà nhỏ (Template 10 = GGMonthCardEvent)
            {
                'id': 1010,
                'template': 10,
                'image': 'Event/event_11',
                'name': 'Thẻ quà nhỏ',
                'timeRemain': left,
                'notice': False,
                'urlNext': "/event/detail?eventId=1010&userId={userId}",
            },
            # 3. Thẻ quà lớn (Template 10 = GGMonthCardEvent)
            {
                'id': 1011,
                'template': 10,
                'image': 'Event/event_13',
                'name': 'Thẻ quà lớn',
                'timeRemain': left,
                'notice': False,
                'urlNext': "/event/detail?eventId=1011&userId={userId}",
            },
            # 4. Quỹ trưởng thành V2 (Template 114 = GGGrowthFundEventV2)
            {
                'id': 1114,
                'template': 114,
                'image': 'Event/chest2',
                'name': 'Quỹ trưởng thành V2',
                'timeRemain': left,
                'notice': False,
                'urlNext': "/event/detail?eventId=1114&userId={userId}",
            },
            # 5. Quỹ Đặc Thù (Template 9 = GGPackValueEvent)
            {
                'id': 1009,
                'template': 9,
                'image': 'Event/event_16',
                'name': 'Quỹ Đặc Thù',
                'timeRemain': left,
                'notice': False,
                'urlNext': "/event/detail?eventId=1009&userId={userId}",
            }
        ]
        return {'eventList': result}

    def detail(self, event_id, state, base):
        today_str = datetime.now().strftime("%Y-%m-%d")

        if event_id == 1106:  # Điểm danh 30 ngày (PanelCheckinEvent.ParseCheckinEvent @ 0x137317c)
            claimed_days = set(state.get('checkin_claimed', []))
            last_date = state.get('last_checkin_date', '')
            can_claim_today = (last_date != today_str)
            active_day = len(claimed_days) + 1 if can_claim_today else -1

            # 30 Ngày phần thưởng ngập tràn Bia Chiêu Mộ & Cuộn SS
            rewards_pool = [
                [3, 1, 15, 100],      # Ngày 1: 100 Thùng Bia Chiêu Mộ Cao Cấp
                [3, 1, 16, 50],       # Ngày 2: 50 Thùng Bia Chiêu Mộ Đặc Biệt
                [2, 50000],           # Ngày 3: 50k Kim Cương
                [3, 1, 15, 100],      # Ngày 4: 100 Thùng Bia Chiêu Mộ Cao Cấp
                [3, 1, 53, 10],       # Ngày 5: 10 Cuộn Tướng SS L1
                [3, 1, 16, 50],       # Ngày 6: 50 Thùng Bia Chiêu Mộ Đặc Biệt
                [3, 1, 54, 10],       # Ngày 7: 10 Cuộn Tướng SS L2
                [1, 50000000],        # Ngày 8: 50Tr Vàng
                [3, 1, 15, 100],      # Ngày 9: 100 Thùng Bia Cao Cấp
                [3, 1, 16, 50],       # Ngày 10: 50 Thùng Bia Đặc Biệt
                [2, 50000],           # Ngày 11: 50k Kim Cương
                [3, 1, 15, 100],      # Ngày 12: 100 Thùng Bia Cao Cấp
                [3, 1, 53, 10],       # Ngày 13: 10 Cuộn Tướng SS L1
                [3, 1, 16, 50],       # Ngày 14: 50 Thùng Bia Đặc Biệt
                [3, 1, 54, 10],       # Ngày 15: 10 Cuộn Tướng SS L2
                [1, 50000000],        # Ngày 16: 50Tr Vàng
                [3, 1, 15, 100],      # Ngày 17: 100 Thùng Bia Cao Cấp
                [3, 1, 16, 50],       # Ngày 18: 50 Thùng Bia Đặc Biệt
                [2, 50000],           # Ngày 19: 50k Kim Cương
                [3, 1, 15, 100],      # Ngày 20: 100 Thùng Bia Cao Cấp
                [3, 1, 53, 10],       # Ngày 21: 10 Cuộn Tướng SS L1
                [3, 1, 16, 50],       # Ngày 22: 50 Thùng Bia Đặc Biệt
                [3, 1, 54, 10],       # Ngày 23: 10 Cuộn Tướng SS L2
                [1, 50000000],        # Ngày 24: 50Tr Vàng
                [3, 1, 15, 100],      # Ngày 25: 100 Thùng Bia Cao Cấp
                [3, 1, 16, 50],       # Ngày 26: 50 Thùng Bia Đặc Biệt
                [2, 100000],          # Ngày 27: 100k Kim Cương
                [3, 1, 15, 200],      # Ngày 28: 200 Thùng Bia Cao Cấp
                [3, 1, 16, 100],      # Ngày 29: 100 Thùng Bia Đặc Biệt
                [3, 1, 54, 20],       # Ngày 30: 20 Cuộn Tướng SS L2
            ]
            tick_list = []
            for d in range(1, 31):
                if d in claimed_days:
                    tick_list.append({
                        'id': d,
                        'urlNext': "",
                        'buttonLabel': "Đã nhận",
                        'countdown': 0,
                        'reward': rewards_pool[d - 1],
                    })
                elif d == active_day:
                    tick_list.append({
                        'id': d,
                        'urlNext': f"/event/checkin/claim?day={d}&userId={state.get('id', 1)}",
                        'buttonLabel': "Nhận",
                        'countdown': 0,
                        'reward': rewards_pool[d - 1],
                    })
                else:
                    tick_list.append({
                        'id': d,
                        'urlNext': "",
                        'buttonLabel': "Chưa mở",
                        'countdown': 86400,
                        'reward': rewards_pool[d - 1],
                    })
            return {
                'status': 0,
                'image': 'Event/bgr_month_card',
                'title': 'Điểm Danh Hàng Ngày',
                'desc': 'Điểm danh mỗi ngày nhận Thùng Bia Chiêu Mộ, Kim Cương và Cuộn Tướng SS!',
                'sum': len(claimed_days),
                'sumDesc': f'Đã điểm danh: {len(claimed_days)}/30 ngày',
                'receiveButtonLabel': 'Điểm danh',
                'loginTickRewardList': tick_list,
            }

        if event_id == 1006:  # Điểm danh 7 ngày (Parse7DaysLoginEvent @ 0x136dfc8)
            claimed_7 = set(state.get('login7_claimed', []))
            last_date7 = state.get('last_login7_date', '')
            can_claim7 = (last_date7 != today_str)
            active_day7 = len(claimed_7) + 1 if can_claim7 else -1

            day_rewards = [
                [3, 1, 53, 2, 2, 50000, 1, 50000000, 3, 1, 15, 50],       # Ngày 1: 2 Cuộn SS L1 + 50k KC + 50Tr Vàng + 50 Bia Cao Cấp
                [3, 1, 54, 2, 3, 1, 1, 100000000, 3, 1, 2, 20000, 3, 1, 16, 30], # Ngày 2: 2 Cuộn SS L2 + 100Tr EXP + 20k Đá CH + 30 Thùng Bia
                [3, 1, 53, 3, 2, 100000, 3, 1, 3, 100000, 3, 1, 206, 1000],      # Ngày 3: 3 Cuộn SS L1 + 100k KC + 100k Bụi Ma Thuật + 1k Kết Tinh Ý Chí
                [3, 1, 54, 3, 1, 100000000, 3, 1, 1, 200000000, 3, 1, 15, 100],  # Ngày 4: 3 Cuộn SS L2 + 100Tr Vàng + 200Tr EXP + 100 Bia Cao Cấp
                [3, 1, 53, 5, 2, 150000, 3, 1, 2, 50000, 3, 1, 16, 50],          # Ngày 5: 5 Cuộn SS L1 + 150k KC + 50k Đá CH + 50 Thùng Bia
                [3, 1, 54, 5, 1, 200000000, 3, 1, 1, 300000000, 3, 1, 206, 2000],# Ngày 6: 5 Cuộn SS L2 + 200Tr Vàng + 300Tr EXP + 2k Kết Tinh Ý Chí
                [3, 1, 58, 2, 3, 1, 54, 5, 3, 1, 53, 5, 2, 300000],              # Ngày 7: 2 Bảo Vật Đỏ VIP Tuỳ Chọn + 5 Cuộn SS L2 + 5 Cuộn SS L1 + 300k KC
            ]
            tick_list = []
            for d in range(1, 8):
                if d in claimed_7:
                    tick_list.append({
                        'id': d,
                        'urlNext': "",
                        'buttonLabel': "Đã nhận",
                        'reward': day_rewards[d - 1],
                    })
                elif d == active_day7:
                    tick_list.append({
                        'id': d,
                        'urlNext': f"/event/login7/claim?day={d}&userId={state.get('id', 1)}",
                        'buttonLabel': "Nhận",
                        'reward': day_rewards[d - 1],
                    })
                else:
                    tick_list.append({
                        'id': d,
                        'urlNext': "",
                        'buttonLabel': "Chưa mở",
                        'reward': day_rewards[d - 1],
                    })
            return {
                'status': 0,
                'image': 'Event/bgr_month_card',
                'title': 'Điểm Danh 7 Ngày Tân Thủ',
                'desc': 'Đăng nhập đủ 7 ngày nhận trọn bộ Kim Cương, Thùng Bia và Cuộn Tướng SS!',
                'loginTickRewardList': tick_list,
            }

        if event_id == 1010:  # Thẻ quà nhỏ
            last_card = state.get('last_card_small_date', '')
            is_claimed = (last_card == today_str)
            return {
                'status': 0,
                'image': 'Event/bgr_month_card',
                'price': 'Miễn phí',
                'buttonLabel': 'Đã nhận' if is_claimed else 'Nhận',
                'desc': 'Thẻ quà nhỏ: Nhận miễn phí 10,000 Kim Cương và 50 Thùng Bia mỗi ngày!',
                'link': "" if is_claimed else f'/event/monthcard/claim?type=small&userId={state.get("id", 1)}',
                'urlNext': "" if is_claimed else f'/event/monthcard/claim?type=small&userId={state.get("id", 1)}',
            }

        if event_id == 1011:  # Thẻ quà lớn
            last_card = state.get('last_card_large_date', '')
            is_claimed = (last_card == today_str)
            return {
                'status': 0,
                'image': 'Event/bgr_month_card',
                'price': 'Miễn phí',
                'buttonLabel': 'Đã nhận' if is_claimed else 'Nhận',
                'desc': 'Thẻ quà lớn: Nhận miễn phí 50,000 Kim Cương và 200 Thùng Bia mỗi ngày!',
                'link': "" if is_claimed else f'/event/monthcard/claim?type=large&userId={state.get("id", 1)}',
                'urlNext': "" if is_claimed else f'/event/monthcard/claim?type=large&userId={state.get("id", 1)}',
            }

        if event_id == 1114:  # Quỹ trưởng thành V2
            return {
                'status': 0,
                'image': 'Event/bgr_month_card',
                'eventList': [],
                'urlNext': f'/event/growth/claim?userId={state.get("id", 1)}',
            }

        if event_id == 1009:  # Quỹ Đặc Thù
            return {
                'status': 0,
                'image': 'Event/bgr_market_event',
                'desc': 'Quỹ Đặc Thù dành cho Tân Thủ & VIP',
                'packBuyingList': [],
                'urlNext': f'/event/pack/claim?userId={state.get("id", 1)}',
            }

        # Default / 1024: Giftcode
        gift = self.config.get('giftcode', {})
        url_action = "/giftcode?code={code}&sessionId={sessionId}"
        return {
            'status': 0,
            'image': gift.get('image', 'Event/banner_giftcode'),
            'urlAction': url_action,
            'useCodeUrl': url_action,
            'urlNext': url_action,
        }
