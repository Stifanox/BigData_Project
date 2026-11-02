"""
Użycie:  python importer.py [--raw-dir KATALOG] [--what matches,ranks,timelines] [--workers 8] [--drop]
Katalog z danymi można też podać zmienną środowiskową LOL_RAW_DIR.
"""
import argparse
import gzip
import json
import os
import time
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path

import orjson
from pymongo import ASCENDING, MongoClient
from pymongo.errors import BulkWriteError

RAW_DIR = Path(os.environ.get(
    'LOL_RAW_DIR', Path.home() / 'Projects' / 'League-Of-Legends-Data-Collector' / 'data' / 'raw'))
MONGO_URI = 'mongodb://localhost:27017'
DB_NAME = 'lol'
BATCH_SIZE = 500
DUPLICATE_KEY = 11000
STATS_FILE = Path(__file__).with_name('import_stats.json')

PLAYER_FRAME_MINUTES = (10, 15, 20)
KEEP_EVENTS = {'CHAMPION_KILL', 'ELITE_MONSTER_KILL', 'BUILDING_KILL', 'TURRET_PLATE_DESTROYED',
               'DRAGON_SOUL_GIVEN', 'GAME_END'}
EVENT_FIELDS = ('type', 'timestamp', 'killerId', 'victimId', 'killerTeamId', 'teamId',
                'assistingParticipantIds', 'monsterType', 'monsterSubType', 'buildingType',
                'towerType', 'laneType', 'winningTeam', 'name')


def parse_ts(value):
    return datetime.fromisoformat(value) if value else None


def match_doc(rec):
    return {
        '_id': rec['match_id'],
        'platform_id': rec['platform_id'],
        'fetched_at': parse_ts(rec.get('fetched_at')),
        'seed_tier': rec.get('seed_tier'),
        'strategy': rec.get('strategy'),
        'metadata': rec['data']['metadata'],
        'info': rec['data']['info'],
    }


def timeline_doc(rec):
    info = rec['data']['info']
    team_frames, player_frames, events = [], {}, []
    for minute, frame in enumerate(info['frames']):
        teams = {'blue': dict.fromkeys(('gold', 'xp', 'cs', 'jungle_cs', 'levels'), 0),
                 'red': dict.fromkeys(('gold', 'xp', 'cs', 'jungle_cs', 'levels'), 0)}
        for pid, pf in frame['participantFrames'].items():
            t = teams['blue' if int(pid) <= 5 else 'red']
            t['gold'] += pf['totalGold']
            t['xp'] += pf['xp']
            t['cs'] += pf['minionsKilled']
            t['jungle_cs'] += pf['jungleMinionsKilled']
            t['levels'] += pf['level']
        team_frames.append({'minute': minute, 'timestamp': frame['timestamp'], **teams})
        if minute in PLAYER_FRAME_MINUTES:
            player_frames[str(minute)] = {
                pid: {'totalGold': pf['totalGold'], 'xp': pf['xp'], 'level': pf['level'],
                      'minionsKilled': pf['minionsKilled'],
                      'jungleMinionsKilled': pf['jungleMinionsKilled'],
                      'damageToChampions': pf['damageStats']['totalDamageDoneToChampions']}
                for pid, pf in frame['participantFrames'].items()}
        for ev in frame['events']:
            if ev['type'] in KEEP_EVENTS:
                events.append({k: ev[k] for k in EVENT_FIELDS if k in ev})
    return {
        '_id': rec['match_id'],
        'platform_id': rec['platform_id'],
        'frame_interval_ms': info.get('frameInterval'),
        'n_frames': len(info['frames']),
        'team_frames': team_frames,
        'player_frames': player_frames,
        'events': events,
    }


def rank_doc(rec):
    return {
        '_id': f"{rec['puuid']}|{rec['fetched_at']}",
        'puuid': rec['puuid'],
        'platform_id': rec['platform_id'],
        'fetched_at': parse_ts(rec['fetched_at']),
        'status': rec.get('status'),
        'entries': rec.get('entries', []),
    }


TRANSFORMS = {'matches': match_doc, 'timelines': timeline_doc, 'ranks': rank_doc}


def _flush(coll, batch, stats):
    if not batch:
        return
    try:
        res = coll.insert_many(batch, ordered=False)
        stats['inserted'] += len(res.inserted_ids)
    except BulkWriteError as e:
        errors = e.details['writeErrors']
        dups = sum(err['code'] == DUPLICATE_KEY for err in errors)
        if dups != len(errors):
            raise
        stats['inserted'] += e.details['nInserted']
        stats['duplicates'] += dups
    batch.clear()


def import_file(args):
    """Importuje jeden plik part-NNNN.jsonl.gz; zwraca statystyki."""
    kind, path, raw_dir, uri = args
    transform = TRANSFORMS[kind]
    stats = {'kind': kind, 'file': path.relative_to(raw_dir).as_posix(), 'gz_bytes': path.stat().st_size,
             'lines': 0, 'raw_bytes': 0, 'inserted': 0, 'duplicates': 0, 'bad_lines': 0}
    client = MongoClient(uri)
    coll = client[DB_NAME][kind]
    batch = []
    with gzip.open(path, 'rb') as f:
        for line in f:
            stats['lines'] += 1
            stats['raw_bytes'] += len(line)
            try:
                batch.append(transform(orjson.loads(line)))
            except (orjson.JSONDecodeError, KeyError, TypeError, ValueError):
                stats['bad_lines'] += 1
                continue
            if len(batch) >= BATCH_SIZE:
                _flush(coll, batch, stats)
    _flush(coll, batch, stats)
    client.close()
    return stats


def create_indexes(db):
    db.matches.create_index([('platform_id', ASCENDING)])
    db.matches.create_index([('seed_tier', ASCENDING)])
    db.matches.create_index([('info.queueId', ASCENDING)])
    db.matches.create_index([('metadata.participants', ASCENDING)])
    db.ranks.create_index([('puuid', ASCENDING), ('fetched_at', ASCENDING)])
    db.timelines.create_index([('platform_id', ASCENDING)])


def run_import(what=('matches', 'ranks', 'timelines'), raw_dir=RAW_DIR, uri=MONGO_URI,
               workers=8, drop=False, verbose=True):
    """Importuje wybrane kolekcje równolegle (jeden plik = jedno zadanie). Zwraca listę statystyk."""
    client = MongoClient(uri)
    db = client[DB_NAME]
    if drop:
        for kind in what:
            db.drop_collection(kind)
    raw_dir = Path(raw_dir)
    tasks = [(kind, p, raw_dir, uri) for kind in what
             for p in sorted(Path(raw_dir, kind).glob('platform=*/*.jsonl.gz'))]
    # największe pliki najpierw, żeby procesy kończyły w podobnym czasie
    tasks.sort(key=lambda t: t[1].stat().st_size, reverse=True)
    if verbose:
        print(f'{len(tasks)} plików, {workers} procesów')
    t0 = time.time()
    results = []
    with Pool(workers) as pool:
        for i, st in enumerate(pool.imap_unordered(import_file, tasks), 1):
            results.append(st)
            if verbose:
                print(f'[{i}/{len(tasks)}] {time.time() - t0:6.0f}s  {st["file"]}: '
                      f'+{st["inserted"]} dup={st["duplicates"]} bad={st["bad_lines"]}')
    create_indexes(db)
    client.close()
    # dopisujemy statystyki do pliku (klucz = ścieżka względem raw_dir), żeby notebook mógł je pokazać bez ponownego importu
    saved = json.loads(STATS_FILE.read_text()) if STATS_FILE.exists() else {}
    saved.update({st['file']: st for st in results})
    STATS_FILE.write_text(json.dumps(saved, indent=1))
    return results


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--what', default='matches,ranks,timelines')
    ap.add_argument('--raw-dir', type=Path, default=RAW_DIR)
    ap.add_argument('--uri', default=MONGO_URI)
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--drop', action='store_true', help='usuń kolekcje przed importem')
    a = ap.parse_args()
    run_import(a.what.split(','), a.raw_dir, a.uri, a.workers, a.drop)
