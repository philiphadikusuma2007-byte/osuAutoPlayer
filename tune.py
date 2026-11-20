"""
Teach the solver better scoring weights by letting it play itself - for as
long as you let it run.

    python tune.py              -> learn forever (Ctrl+C to stop, progress is saved)
    python tune.py 3            -> learn for 3 rounds
    python tune.py 3 128        -> 3 rounds, 128 games per test (slower, surer)

Each round it deals a NEW set of games, nudges every weight up and down,
and keeps a change only if the AI survives clearly longer on the same games.
At the end of the round the new weights must also win on a second, unseen set
of games - so it never keeps something that was just luck. Steps get smaller
round by round to fine-tune. The best weights go to weights.json, which
blockblast.py and autoplay.py load automatically. Progress log: tune_log.txt.
Uses all CPU cores.
"""

import json
import multiprocessing as mp
import os
import sys
import time

import blockblast as bb

MAX_TURNS = 300     # stop a game here (a good game could otherwise run forever)
MARGIN = 1.01       # a change must survive 1% longer to count (less luck)
TUNED = ["W_REGION", "W_HOLE", "W_ROUGH", "W_SPOT", "W_LINE", "W_COMBO", "W_RESERVE",
         "W_FIT", "W_TINY", "W_DEAD"]
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tune_log.txt")
STATE_KEY = "_tune_round"   # saved in weights.json so a restart deals new games


def _play(args):
    weights, seed = args
    bb.set_weights(weights)
    _, turns = bb.play_game(seed=seed, show=False, max_turns=MAX_TURNS)
    return turns


def strength(pool, weights, seeds):
    """Average turns survived over the given games."""
    turns = pool.map(_play, [(weights, s) for s in seeds], chunksize=1)
    return sum(turns) / len(turns)


def log(text):
    print(text, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(text + "\n")


def save(weights, rnd):
    with open(bb.WEIGHTS_FILE, "w") as f:
        json.dump(dict(weights, **{STATE_KEY: rnd}), f, indent=1)


def tries_for(value, step):
    if value == 0:
        return [10, 40]
    return [round(value * step), round(value / step)]


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 10 ** 9
    games = int(sys.argv[2]) if len(sys.argv) > 2 else 96
    best = bb.get_weights()
    try:
        with open(bb.WEIGHTS_FILE) as f:
            first = json.load(f).get(STATE_KEY, 0) + 1
    except (OSError, ValueError):
        first = 1
    start = time.time()
    with mp.Pool(max(1, mp.cpu_count() - 1)) as pool:
        for rnd in range(first, first + rounds):
            step = max(1.15, 1.6 - 0.1 * (rnd - 1))     # big steps first, finer later
            train = list(range(rnd * 100000, rnd * 100000 + games))
            check = list(range(rnd * 100000 + 50000, rnd * 100000 + 50000 + games))
            start_weights = dict(best)
            best_score = strength(pool, best, train)
            log(f"Round {rnd} (step x{step:.2f}): now {best_score:.1f} turns on new games "
                f"[{(time.time() - start) / 60:.0f} min]")
            for name in TUNED:
                for value in tries_for(best[name], step):
                    if value == best[name] or value < 0:
                        continue
                    while True:   # keep going the same way while it helps
                        trial = dict(best, **{name: value})
                        score = strength(pool, trial, train)
                        if score <= best_score * MARGIN:
                            break
                        log(f"  {name} {best[name]} -> {value}: {best_score:.1f} -> {score:.1f} turns")
                        prev = best[name]
                        best, best_score = trial, score
                        nxt = round(value * value / prev) if prev else value * 2
                        if nxt == value or nxt <= 0:
                            break
                        value = nxt
            if best == start_weights:
                log("  no change helped this round")
                save(best, rnd)
                continue
            old = strength(pool, start_weights, check)
            new = strength(pool, best, check)
            if new >= old:
                save(best, rnd)
                log(f"  KEPT: on unseen games {old:.1f} -> {new:.1f} turns. Saved: "
                    + json.dumps({k: best[k] for k in TUNED}))
            else:
                best = start_weights
                save(best, rnd)
                log(f"  dropped (unseen games {old:.1f} -> {new:.1f}: was luck)")
    log(f"Done after {(time.time() - start) / 60:.0f} min.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped - the best weights so far are in weights.json.")
