"""Local launcher. Credentials are generated once; never embed them in frontend code."""
import argparse
import json
import os
from pathlib import Path
import secrets

import uvicorn

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', action='store_true', help='Synthetic parking states, no camera needed')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--host', default='127.0.0.1', help='Use 0.0.0.0 to allow devices on your local network')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    keyfile = root/'data/local-keys.json'
    keyfile.parent.mkdir(exist_ok=True)
    if not keyfile.exists():
        keyfile.write_text(json.dumps({'admin': secrets.token_urlsafe(32), 'vision': secrets.token_urlsafe(32)}), encoding='utf-8')
    keys = json.loads(keyfile.read_text(encoding='utf-8'))
    os.environ.setdefault('PARKFLOW_ADMIN_KEY', keys['admin'])
    os.environ.setdefault('PARKFLOW_VISION_KEY', keys['vision'])
    os.environ['PARKFLOW_DEMO'] = '1' if args.demo else '0'
    os.environ.setdefault('PARKFLOW_DB', str(root/'data'/('demo.db' if args.demo else 'parkflow.db')))
    print(f"ParkFlow: http://127.0.0.1:{args.port}\nAdmin key: {os.environ['PARKFLOW_ADMIN_KEY']}\nVision key is stored in data/local-keys.json", flush=True)
    uvicorn.run('backend.app:create_app', factory=True, host=args.host, port=args.port)
