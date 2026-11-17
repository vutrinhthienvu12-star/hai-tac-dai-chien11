"""Start an independent realm with a clean database and private configuration."""
import argparse
import getpass
import json
import os
from pathlib import Path
import runpy
import secrets
import sys

base = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--realm', type=int, choices=(1, 2, 3), default=1)
parser.add_argument('--api-port', type=int)
parser.add_argument('--game-port', type=int)
parser.add_argument('--state-dir', type=Path)
parser.add_argument('--origin', help='Public CDN/API origin, for example http://example.com')
parser.add_argument('--local-assets', action='store_true')
parser.add_argument('--initialize-only', action='store_true')
args = parser.parse_args()
state = (args.state_dir or base / 'state' / f'ss{args.realm}').resolve()
state.mkdir(parents=True, exist_ok=True)
api = args.api_port or 9029 + args.realm
game = args.game_port or 7003 + args.realm
origin = (args.origin or f'http://127.0.0.1:{api}').rstrip('/')
config = state / 'account_config.json'
sys.path.insert(0, str(base / 'server'))
if not config.exists():
    from account_core import password_hash
    password = os.environ.get('HTHT_INITIAL_ADMIN_PASSWORD') or getpass.getpass('New admin password (12+ characters): ')
    if len(password) < 12:
        raise SystemExit('Use at least 12 characters')
    data = dict(signing_key=secrets.token_hex(32), realm=f'SS{args.realm}', online_max=100,
                admin_username='owner_admin', admin_password_hash=password_hash(password), reserved_codes=[],
                catalog_path=str(base / 'decompiled/db/db_item_consumable.json'))
    config.write_text(json.dumps(data, indent=2), encoding='utf8')
    try:
        config.chmod(0o600)
    except OSError:
        pass
registry = state / 'servers.json'
if not registry.exists():
    registry.write_text(json.dumps([dict(id=args.realm, name=f'SS{args.realm}', address=origin)]), encoding='utf8')
os.environ.update(HTHT_SOURCE_DIR=str(base / 'server'), HTHT_DB=str(state / 'htht_data.db'),
                  HTHT_PORT=str(api), HTHT_GAME_PORT=str(game), HTHT_ACCOUNT_CONFIG=str(config),
                  HTHT_REGISTRY=str(registry), HTHT_LOG=str(state / 'runtime.log'),
                  HTHT_REALM_ID=str(args.realm), HTHT_PUBLIC_ASSET_BASE=origin)
os.environ.pop('HTHT_SHARED_CHAT_CONFIG', None)
if args.local_assets:
    os.environ['HTHT_LOCAL_ASSETS'] = '1'
else:
    os.environ.pop('HTHT_LOCAL_ASSETS', None)
if args.initialize_only:
    from account_core import initialize
    initialize(state / 'htht_data.db')
    print('Clean database and private configuration initialized')
else:
    runpy.run_path(str(base / 'server/runtime_overlay.py'), run_name='__main__')
