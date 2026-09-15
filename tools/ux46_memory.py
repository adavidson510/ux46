"""Local Constellation access. JSON in/out; never calls a model or a network."""
import argparse
import json
import sys
from ux46_local import home, initialize
from ux46_workspace_api import constellation_call
from constellation_store import Store, Principal


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=['brief','catalog','review','lookup','get','source','capture','feedback','changes','health'])
    p.add_argument('--args', default='{}', help='JSON query arguments; use --stdin for private record bodies')
    p.add_argument('--stdin', action='store_true')
    a=p.parse_args()
    root=home(); config=initialize(root)
    if not config.get('modules',{}).get('constellation'):p.error('Constellation is disabled')
    # Use stdin for record bodies so private lessons need not appear in command
    # arguments. Retrieval is a local database operation, not a model call.
    values=json.load(sys.stdin) if a.stdin else json.loads(a.args)
    result=constellation_call(Store(root/'workspace/constellation.sqlite3'),Principal('local-agent',('*',),True),a.operation,values)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
