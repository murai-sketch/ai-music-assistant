"""退場（fall/drift）と保持が実際に画に出ているかを数字で確かめる。
同じ時刻を「その部品あり」「なし」で描いて、文字の周辺で違うピクセルの割合を出す。"""
import sys, json, copy
sys.path.insert(0, '.')
import numpy as np
import kinetic
from styles import DEFAULT_STYLE, get_style

image, hash_ = sys.argv[1], sys.argv[2]
work = f'_work/{hash_}'
data = json.load(open(f'{work}/kinetic_plan.json'))
plan = data['plan']; beats = json.load(open(f'{work}/beats.json'))
beats = beats['beats'] if isinstance(beats, dict) and 'beats' in beats else beats
style = get_style(DEFAULT_STYLE)

def diff_ratio(plan_a, plan_b, t):
    ra = kinetic.KineticRenderer(image, plan_a, beats, style, backgrounds=data.get('backgrounds'))
    rb = kinetic.KineticRenderer(image, plan_b, beats, style, backgrounds=data.get('backgrounds'))
    a = np.asarray(ra.frame_at(t).convert('L'), dtype=np.int16)
    b = np.asarray(rb.frame_at(t).convert('L'), dtype=np.int16)
    return float((np.abs(a - b) > 24).mean() * 100)

done = set()
# long_shadow は和風・子ども向けでは選ばれないので、サビの単色背景のカットに仮に付けて描けるかだけ確かめる
forced = [c for c in plan if c['level'] == 3 and c['bg'] != 'image' and c['end'] - c['start'] >= 0.8]
if forced and not any(c.get('texture') == 'long_shadow' for c in plan):
    c = forced[0]; on = copy.deepcopy(plan); on[plan.index(c)]['texture'] = 'long_shadow'
    t = c['start'] + (c['end'] - c['start']) * 0.5
    print(f"#{c['index']} texture=long_shadow（仮付け）: 止まっている間 {diff_ratio(on, plan, t):.2f}%")
for c in plan:
    for key in ('exit', 'hold', 'texture'):
        v = c.get(key)
        if v in ('fall', 'drift', 'breathe', 'wave', 'jitter', 'long_shadow') and (key, v) not in done:
            done.add((key, v))
            off = copy.deepcopy(plan); off[plan.index(c)][key] = None
            dur = c['end'] - c['start']
            if key == 'exit':
                out = max(min(dur * 0.3, 0.55), 0.14)
                ts = [('退場の直前', c['end'] - out - 0.05), ('退場の半ば', c['end'] - out / 2), ('終わり', c['end'] - 0.02)]
            elif key == 'hold':
                ts = [('入りの直後', c['start'] + 0.1), ('止まっている間', c['start'] + dur * 0.5)]
            else:
                ts = [('止まっている間', c['start'] + dur * 0.5)]
            print(f"#{c['index']} {key}={v} dur={dur:.2f}s: " + ', '.join(f"{n} {diff_ratio(plan, off, t):.2f}%" for n, t in ts))
