"""Backfill ``structured.participants[].source`` on stored conversations.

Rows written by upstream's v3 planner before the omi_local_patches v3_participants
fix have participants without the required ``source`` field, which makes the
conversations list endpoint fail response validation (HTTP 500). This sets the
missing value to 'transcript' (the only evidence the v3 outline has).

Usage (inside the backend image): python fix_participants.py [--dry-run] <uid> [<uid> ...]
"""

import sys

sys.path.insert(0, '/app')


def main(argv):
    dry = '--dry-run' in argv
    uids = [a for a in argv if a != '--dry-run']
    if not uids:
        sys.exit(__doc__)
    from database import conversations_v3 as store

    client = store.get_firestore_client()
    total = 0
    for uid in uids:
        for doc in client.collection('users').document(uid).collection('conversations').stream():
            structured = (doc.to_dict() or {}).get('structured')
            participants = structured.get('participants') if isinstance(structured, dict) else None
            if not isinstance(participants, list):
                continue
            missing = [p for p in participants if isinstance(p, dict) and not p.get('source')]
            if not missing:
                continue
            fixed = [{**p, 'source': 'transcript'} if p in missing else p for p in participants]
            print(f'{uid}/{doc.id}: {len(missing)} participant(s) {[p.get("name") for p in missing]}')
            total += 1
            if not dry:
                doc.reference.update({'structured.participants': fixed})
    print(f'{"would fix" if dry else "fixed"} {total} conversation(s)')


if __name__ == '__main__':
    main(sys.argv[1:])
