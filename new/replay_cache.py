"""Offline causal replay of forecast routing; no provider calls or future leakage."""
import argparse
import json
from pathlib import Path
from statistics import median

from app.context_aware_decision import ContextAwareDecisionEngine
from app.models import Candidate


def replay(directory):
    engine = ContextAwareDecisionEngine()
    rows = []
    for path in sorted(Path(directory).glob('*.jsonl')):
        candidates, previous, epoch, complete = [], '', None, False
        pending = {}
        for line in path.read_text().splitlines():
            e = json.loads(line)
            kind = e['event']
            if kind == 'prediction_ready':
                candidates = [Candidate.model_validate(c) for c in e['candidates']]
                epoch = e['epoch']
            elif kind == 'delivered':
                previous = e['text']
            elif kind == 'reply_finished':
                complete = True
                for row in pending.get(e['epoch'], []):
                    row.update({k: e[k] for k in ('reused', 'candidates_available', 'first_text_ms', 'first_packet_ms', 'voice_to_playback_ms', 'prediction_audio_hit', 'prediction_audio_bytes', 'bridge_used', 'answer_first_packet_ms', 'voice_to_answer_ms') if k in e})
            elif kind == 'interrupted':
                candidates, complete, previous = [], False, ''
            elif kind == 'user':
                usable = candidates if complete and epoch == e['epoch'] - 1 else []
                decision, selected = engine.should_reuse(e['text'], usable, previous)
                row = dict(session=path.stem, epoch=e['epoch'], user=e['text'], previous=previous,
                           available=len(usable), hit=decision == 'reuse', selected=selected.id if selected else None,
                           conditions=[c.when for c in usable])
                rows.append(row)
                pending.setdefault(e['epoch'], []).append(row)
                candidates, complete = [], False
    measured = [r for r in rows if 'reused' in r]
    available = [r for r in rows if r['available']]
    summary = dict(turns=len(rows), with_candidates=len(available), local_hits=sum(r['hit'] for r in rows),
                   local_rate_all=round(sum(r['hit'] for r in rows)/max(1,len(rows)),4),
                   local_rate_available=round(sum(r['hit'] for r in available)/max(1,len(available)),4),
                   historical_measured=len(measured), historical_hits=sum(r['reused'] for r in measured))
    audio_measured = [r for r in measured if 'prediction_audio_hit' in r]
    summary['audio_measured_turns'] = len(audio_measured)
    summary['prediction_audio_hits'] = sum(r['prediction_audio_hit'] for r in audio_measured)
    summary['prediction_audio_hit_rate'] = (
        sum(r['prediction_audio_hit'] for r in audio_measured) / len(audio_measured)
        if audio_measured else None
    )
    for reuse in (True, False):
        group = [r for r in measured if r['reused'] == reuse]
        summary['historical_reuse' if reuse else 'historical_miss'] = {
            k: median([r[k] for r in group if k in r]) if any(k in r for r in group) else None
            for k in ('first_text_ms', 'first_packet_ms', 'voice_to_playback_ms',
                      'answer_first_packet_ms', 'voice_to_answer_ms')}
    return dict(summary=summary, rows=rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sessions', default='runtime/sessions')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = replay(args.sessions)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    for r in report['rows']:
        if not r['hit']:
            print(r['session'], r['epoch'], r['available'], r['user'], r['conditions'])
