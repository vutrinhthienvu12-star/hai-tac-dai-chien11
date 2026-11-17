"""Resolve native L1/L2 selection indices using the exact CDN db_101 order."""
import json
import copy
from pathlib import Path
from resource_guard import clone_state

SELECTIONS = json.loads(Path(__file__).with_name('selection_previews.json').read_text(encoding='utf-8'))


def exp_chest_72h_amount(level):
    """Owner balance: hero EXP stones per 72-hour chest, by account level."""
    level = max(1, int(level))
    if level <= 40:
        return 2500000
    if level <= 85:
        return 3500000
    if level <= 120:
        return 5000000
    return 10000000


def use_selection(game, request, uid, state):
    """Action 254 payload: [1, itemKey, numberSummon, zeroBasedSubItemIndex]."""
    if len(request)<2 or request[0]!=1 or str(request[1]) not in SELECTIONS:
        return None
    def reject(message):
        return [(2,game.build_common_vector(a_string=[message])),
                (251,game._consumable_bag_payload({'consumables':clone_state(state.get('consumables',[]))}))]
    if len(request)!=4:
        return reject('Hãy chọn thuyền viên trong cuộn L1/L2 trước khi sử dụng.')
    _, box, count, index=map(int,request)
    preview=SELECTIONS[str(box)]
    if not 0<=index<len(preview):
        return reject('Lựa chọn thuyền viên không hợp lệ. Hãy mở lại cuộn L1/L2.')
    if not 1<=count<=99:
        return reject('Mỗi lần sử dụng từ 1 đến 99 cuộn.')
    available=sum(int(c[2]) for c in state.get('consumables',[]) if len(c)>=3 and c[0]==1 and c[1]==box)
    if available<count:
        return reject('Bạn không đủ cuộn thuyền viên L1/L2.')
    hero_id=preview[index]
    if hero_id not in game.GAME_DB.heroes_by_id:
        return reject('Dữ liệu thuyền viên chưa sẵn sàng. Hãy thử lại sau.')
    template=game.GAME_DB.heroes_by_id[hero_id]
    if box in (53,54):
        rare=str(template.get('type','')).lower() in ('dark','light')
        if int(template.get('star',0))!=5 or int(template.get('rank',0))<6 or rare!=(box==54):
            return reject('Lựa chọn không đúng hệ của cuộn SS. Hãy đóng túi và mở lại cuộn.')
    base=hero_id if int(template.get('star',0))==5 else game.GAME_DB.base_5star_by_hero_id.get(hero_id,hero_id)
    if int(game.GAME_DB.heroes_by_id.get(base,{}).get('star',0))!=5:
        return reject('Cuộn thuyền viên chỉ được chọn tướng gốc 5 sao.')
    if len(state.get('heroes', [])) + count > int(state.get('bag_info', [3000])[0]):
        return reject('Túi thuyền viên đã đầy. Hãy dọn túi trước khi mở.')
    tx=getattr(game,'resource_transaction_context',None)
    tx_active=bool(tx is not None and getattr(tx,'active',False))
    if tx_active:
        updated=state
    else:
        updated=clone_state({k:v for k,v in state.items() if k not in ('heroes','island_gems','pets2')})
        updated['heroes']=list(state.get('heroes',[]))
        if 'island_gems' in state:updated['island_gems']=list(state['island_gems'])
        if 'pets2' in state:updated['pets2']=list(state['pets2'])
    game.deduplicate_consumables(updated)
    rewards=[('material',1,box,-count)]+[('hero',base,1,0,5)]*count
    received, heroes=game.apply_and_make_bonus_received(updated,rewards,game.GAME_DB)
    if not tx_active:
        game.STORE.save_state(uid,updated)
        state.clear();state.update(updated)
    return [(207,game.build_pb_list_hero(heroes)),
            (254,game.build_common_vector(a_long=received)),
            (251,game._consumable_bag_payload(state))]


def use_consumable(game, request, uid, state):
    """Resolve usable items without interpreting acquisition locations as rewards."""
    import random

    def reject(message):
        return [(2, game.build_common_vector(a_string=[message])),
                (251, game._consumable_bag_payload({'consumables': clone_state(state.get('consumables', []))}))]

    from inventory_special import use_special
    special = use_special(game, request, uid, state)
    if special is not None:
        return special
    if len(request) not in (3, 4):
        return reject('Yêu cầu sử dụng vật phẩm không hợp lệ.')
    _, box, count = map(int, request[:3])
    index = int(request[3]) if len(request) == 4 else -1
    if not 1 <= count <= 999999:
        return reject('Mỗi lần sử dụng từ 1 đến 999.999 vật phẩm.')
    definition = game.GAME_DB.consumable_definitions.get(box)
    if not definition or int(definition['type']) not in (2, 3):
        return reject('Nguyên liệu này được dùng tại chức năng nâng cấp hoặc chế tạo tương ứng.')
    available = sum(int(c[2]) for c in state.get('consumables', []) if len(c)>=3 and int(c[0]) == 1 and int(c[1]) == box)
    if available < count:
        return reject('Bạn không đủ vật phẩm để sử dụng.')

    rewards = []
    pet = {600846: 60, 600849: 61, 600852: 62, 600857: 63, 600861: 64}.get(box)
    if box == 71:
        rewards = [('material', 1, 1, exp_chest_72h_amount(state.get('level', 1)) * count)]
    elif 60 <= box <= 70:
        hours = (2, 5, 8, 12, 24, 72)[(box - 60) % 6]
        stage = max(1, int(state.get('campaign_auto', state.get('campaign_level', 1))))
        stage = min(stage, max(1, int(state.get('campaign_level', 1))))
        output = game.GAME_DB.campaign_outputs.get(stage)
        if not output:
            return reject('Chưa có dữ liệu sản lượng AFK của ải này. Vật phẩm được giữ nguyên.')
        # output_old is explicitly per five seconds, excluding VIP.
        amount = int(output['aG' if box <= 65 else 'soul']) * (hours * 3600 // 5) * count
        rewards = [('gold', amount)] if box <= 65 else [('material', 1, 1, amount)]
    elif not pet:
        preview = game.GAME_DB.consumable_previews.get(box, [])
        if not preview:
            return reject('Vật phẩm chưa có cấu hình phần thưởng. Vật phẩm được giữ nguyên.')
        if int(definition['type']) == 3:
            if not 0 <= index < len(preview):
                return reject('Hãy chọn một phần thưởng hợp lệ trước khi mở rương.')
            rolls = [preview[index]] * count
        elif box == 2069:
            rolls = preview * count
        elif box == 200045:
            # Consecutive groups in the preview are full four-piece equipment sets.
            groups = [preview[i:i+4] for i in range(0, len(preview), 4)]
            rolls = [reward for _ in range(count) for reward in random.choice(groups)]
        else:
            rolls = [random.choice(preview) for _ in range(count)]
        for reward in rolls:
            if reward[0] == 'hero':
                hid = int(reward[1])
                template = game.GAME_DB.heroes_by_id.get(hid)
                if template is None:
                    return reject('Dữ liệu thuyền viên chưa sẵn sàng. Vật phẩm được giữ nguyên.')
                rewards.append(('hero', hid, 1, 0, int(template.get('star', 5))))
            elif reward[0] in ('gold', 'gem', 'material', 'exp', 'equip', 'artifact', 'avatar', 'vip_exp'):
                rewards.append(reward)
            else:
                return reject('Cấu hình phần thưởng không hợp lệ. Vật phẩm được giữ nguyên.')
    hero_count = sum(r[0] == 'hero' for r in rewards)
    if hero_count and len(state.get('heroes', [])) + hero_count > int(state.get('bag_info', [3000])[0]):
        return reject('Túi thuyền viên đã đầy. Hãy dọn túi trước khi mở.')

    # Apply the entire operation to a copy; failures cannot consume the original item.
    tx = getattr(game, 'resource_transaction_context', None)
    tx_active = bool(tx is not None and getattr(tx, 'active', False))
    if tx_active:
        updated = state
    else:
        updated = clone_state({k: v for k, v in state.items() if k not in ('heroes', 'island_gems', 'pets2')})
        updated['heroes'] = list(state.get('heroes', []))
        if 'island_gems' in state:
            updated['island_gems'] = list(state['island_gems'])
        if 'pets2' in state:
            updated['pets2'] = list(state['pets2'])
    game.deduplicate_consumables(updated)
    received, heroes = game.apply_and_make_bonus_received(updated, [('material', 1, box, -count)] + rewards, game.GAME_DB)
    if pet:
        shards = updated.setdefault('pet2_shards', {})
        shards[str(pet)] = int(shards.get(str(pet), 0)) + 50 * count
    if not tx_active:
        game.STORE.save_state(uid, updated)
        state.clear()
        state.update(updated)
    actions = [(207, game.build_pb_list_hero(heroes))] if heroes else []
    actions += [(254, game.build_common_vector(a_long=received)),
                (251, game._consumable_bag_payload(state)),
                (250, game._equip_bag_payload(state)),
                (253, game._artifact_bag_payload(state))]
    if pet:
        actions.append((940, game.build_pet2_status_payload(state, game.GAME_DB)))
    return actions
