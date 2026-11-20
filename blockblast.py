"""
Block Blast Solver
==================
An 8x8 block puzzle AI (Block Blast / 1010!) that follows the rules in Claude.md:

  1. Try all 6 orders of the 3 pieces (A->B->C, A->C->B, ...).
  2. For every placement, score the board with:
       +100  per row/column cleared
       +150  combo bonus when 2+ lines clear in one placement
       -60 per hole, -10 per rough edge (smoothness penalty, tuned by testing)
       +80   if a 3x3 square still fits, +80 if a 5x1 bar still fits
       +10   per valid spot where common pieces can still go (mobility)
  3. Pick the order + coordinates with the best total score.

How to run:
    python blockblast.py            -> menu
    python blockblast.py play       -> watch the AI play a full game
    python blockblast.py solve      -> type in YOUR board + pieces, get the best moves
    python blockblast.py bench 20   -> AI plays 20 games quickly, shows average/high score
    python tune.py                  -> AI learns better weights by playing itself (weights.json)

The board is stored as one big number (a "bitboard"): bit (row*8 + col) is 1
when that cell is filled. This keeps the code short and makes the search fast.
"""

import itertools
import json
import os
import random
import sys
import time

SIZE = 8
FULL = (1 << (SIZE * SIZE)) - 1

# ---------------------------------------------------------------------------
# Scoring weights (from Claude.md) - change these to tune the AI
# ---------------------------------------------------------------------------
W_LINE = 100        # per row/column cleared
W_COMBO = 150       # extra when 2+ lines clear at once
W_HOLE = 60         # penalty per isolated empty hole (tuned up from 20)
W_ROUGH = 10        # penalty per filled/empty edge (a "height variation")
W_RESERVE = 80      # bonus if a 3x3 fits, and again if a 5x1 bar fits
W_SPOT = 10         # per valid spot for the probe pieces
W_DEAD = 1000       # penalty per common piece that would fit NOWHERE
W_DEAD_ANY = 0      # penalty per piece type (of ALL pieces) that would fit nowhere
W_FIT = 0           # bonus per piece type (of ALL pieces) that still fits somewhere
W_REGION = 0        # penalty per separate empty area (a broken-up board is hard to fill)
W_TINY = 0          # penalty per empty area of 1-2 cells (only tiny pieces fit there)
BEAM = 12           # how many best boards to keep after each step of the search

# weights.json (written by "python tune.py") overrides the numbers above
WEIGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weights.json")


def set_weights(w):
    """Change the scoring weights (dict like {"W_HOLE": 60, ...})."""
    g = globals()
    for k, v in w.items():
        if k in g and (k.startswith("W_") or k == "BEAM"):
            g[k] = v
    _eval_cache.clear()


def get_weights():
    w = {k: v for k, v in globals().items() if k.startswith("W_")}
    w["BEAM"] = BEAM
    return w

# ---------------------------------------------------------------------------
# Pieces ("#" = block, "." = gap)
# ---------------------------------------------------------------------------
PIECES = {
    "1":   ["#"],
    "h2":  ["##"],           "v2":  ["#", "#"],
    "h3":  ["###"],          "v3":  ["#", "#", "#"],
    "h4":  ["####"],         "v4":  ["#", "#", "#", "#"],
    "h5":  ["#####"],        "v5":  ["#", "#", "#", "#", "#"],
    "sq2": ["##", "##"],
    "sq3": ["###", "###", "###"],
    "r23": ["###", "###"],   "r32": ["##", "##", "##"],
    # small corners (2x2 minus one)
    "c1": ["#.", "##"], "c2": [".#", "##"], "c3": ["##", "#."], "c4": ["##", ".#"],
    # big corners (3x3)
    "C1": ["#..", "#..", "###"], "C2": ["..#", "..#", "###"],
    "C3": ["###", "#..", "#.."], "C4": ["###", "..#", "..#"],
    # L shapes
    "L1": ["#.", "#.", "##"], "L2": [".#", ".#", "##"],
    "L3": ["##", "#.", "#."], "L4": ["##", ".#", ".#"],
    "L5": ["###", "#.."],     "L6": ["###", "..#"],
    "L7": ["#..", "###"],     "L8": ["..#", "###"],
    # T shapes
    "T1": ["###", ".#."], "T2": [".#.", "###"], "T3": ["#.", "##", "#."], "T4": [".#", "##", ".#"],
    # S / Z shapes
    "S1": [".##", "##."], "S2": ["##.", ".##"], "S3": ["#.", "##", ".#"], "S4": [".#", "##", "#."],
    # diagonals
    "d1": ["#.", ".#"], "d2": [".#", "#."],
}

# Pieces used to measure "how much room is left" on a board
PROBES = ["sq3", "h5", "v5", "sq2", "C1", "C2", "C3", "C4", "h3", "v3"]

# ---------------------------------------------------------------------------
# Board helpers
# ---------------------------------------------------------------------------
def bit(r, c):
    return 1 << (r * SIZE + c)


ROW_MASKS = [sum(bit(r, c) for c in range(SIZE)) for r in range(SIZE)]
COL_MASKS = [sum(bit(r, c) for r in range(SIZE)) for c in range(SIZE)]
LINE_MASKS = ROW_MASKS + COL_MASKS
FIRST_ROW, LAST_ROW = ROW_MASKS[0], ROW_MASKS[-1]
FIRST_COL, LAST_COL = COL_MASKS[0], COL_MASKS[-1]

_placement_cache = {}


def placements(shape):
    """All (row, col, mask) where the piece fits inside the 8x8 grid."""
    key = tuple(shape)
    if key not in _placement_cache:
        cells = [(r, c) for r, line in enumerate(shape) for c, ch in enumerate(line) if ch == "#"]
        h = len(shape)
        w = max(len(line) for line in shape)
        result = []
        for r in range(SIZE - h + 1):
            for c in range(SIZE - w + 1):
                mask = 0
                for dr, dc in cells:
                    mask |= bit(r + dr, c + dc)
                result.append((r, c, mask))
        _placement_cache[key] = result
    return _placement_cache[key]


def place(board, mask):
    """Put a piece on the board, clear full lines. Returns (new_board, lines_cleared)."""
    board |= mask
    full = [m for m in LINE_MASKS if board & m == m]
    for m in full:
        board &= ~m
    return board, len(full)


def line_points(lines):
    """Heuristic points for clearing lines in one placement."""
    if lines == 0:
        return 0
    points = lines * W_LINE
    if lines >= 2:
        points += W_COMBO * (lines - 1)
    return points


_fit_cache = {}


def _fit_info(shape):
    """(cell offsets, mask of top-left spots where the piece stays inside the grid)."""
    key = tuple(shape)
    if key not in _fit_cache:
        offsets = [r * SIZE + c for r, line in enumerate(shape) for c, ch in enumerate(line) if ch == "#"]
        origins = 0
        for r, c, _ in placements(shape):
            origins |= bit(r, c)
        _fit_cache[key] = (offsets, origins)
    return _fit_cache[key]


def count_spots(board, shape):
    """How many places the piece fits. Uses bit shifts: a spot works when every
    cell of the piece, shifted there, lands on an empty cell."""
    offsets, fits = _fit_info(shape)
    empty = ~board & FULL
    for off in offsets:
        fits &= empty >> off
    return fits.bit_count()


_eval_cache = {}


def evaluate(board):
    """Score how 'healthy' a board is (higher = better). Remembers boards it saw."""
    score = _eval_cache.get(board)
    if score is None:
        if len(_eval_cache) > 500000:
            _eval_cache.clear()
        score = _eval_cache[board] = _evaluate(board)
    return score


def _evaluate(board):
    score = 0
    empty = ~board & FULL

    # Smoothness: count edges between a filled cell and an empty cell
    horiz = (board ^ (board >> 1)) & ~LAST_COL & FULL
    vert = (board ^ (board >> SIZE)) & ~LAST_ROW & FULL
    score -= W_ROUGH * (horiz.bit_count() + vert.bit_count())

    # Holes: empty cells boxed in on all 4 sides (by blocks or walls)
    up = ((board << SIZE) & FULL) | FIRST_ROW
    down = (board >> SIZE) | LAST_ROW
    left = ((board << 1) & ~FIRST_COL & FULL) | FIRST_COL
    right = ((board >> 1) & ~LAST_COL) | LAST_COL
    holes = empty & up & down & left & right
    score -= W_HOLE * holes.bit_count()

    # Reserve space + mobility
    big_square = count_spots(board, PIECES["sq3"])
    long_bar = count_spots(board, PIECES["h5"]) + count_spots(board, PIECES["v5"])
    if big_square:
        score += W_RESERVE
    if long_bar:
        score += W_RESERVE
    for name in PROBES:
        spots = count_spots(board, PIECES[name])
        score += W_SPOT * spots
        if spots == 0:
            score -= W_DEAD
    if W_DEAD_ANY or W_FIT:
        fit = sum(1 for shape in PIECES.values() if count_spots(board, shape))
        score += W_FIT * fit - W_DEAD_ANY * (len(PIECES) - fit)
    if W_REGION or W_TINY:
        regions, tiny = empty_regions(empty)
        score -= W_REGION * regions + W_TINY * tiny
    return score


def empty_regions(empty):
    """(number of separate empty areas, how many of them have only 1-2 cells)."""
    regions = tiny = 0
    while empty:
        area = empty & -empty                # start from one empty cell
        while True:                          # flood fill to its neighbours
            grow = area | (area << SIZE) | (area >> SIZE)                 | ((area << 1) & ~FIRST_COL) | ((area >> 1) & ~LAST_COL)
            grow &= empty
            if grow == area:
                break
            area = grow
        empty &= ~area
        regions += 1
        if area.bit_count() <= 2:
            tiny += 1
    return regions, tiny


def load_weights():
    """Use the weights tune.py learned, if there are any."""
    try:
        with open(WEIGHTS_FILE) as f:
            set_weights(json.load(f))
    except (OSError, ValueError):
        pass


load_weights()


# ---------------------------------------------------------------------------
# The search: try all 6 orders, keep the best few boards after each piece
# ---------------------------------------------------------------------------
def search_order(board, pieces, order):
    """Best moves for ONE fixed order of pieces (beam search)."""
    states = [(board, 0, [])]  # (board, clear points so far, moves)
    best = {"order": order, "placed": 0, "score": evaluate(board), "moves": [], "board": board}
    for step, idx in enumerate(order):
        shape = pieces[idx][1]
        children = {}
        for b, gained, moves in states:
            for r, c, mask in placements(shape):
                if b & mask:
                    continue
                nb, lines = place(b, mask)
                g = gained + line_points(lines)
                if nb not in children or children[nb][1] < g:
                    children[nb] = (nb, g, moves + [(idx, r, c, lines)])
        if not children:
            break  # this piece fits nowhere in this order
        ranked = sorted(children.values(), key=lambda s: s[1] + evaluate(s[0]), reverse=True)
        states = ranked[:BEAM]
        top = states[0]
        best = {"order": order, "placed": step + 1, "score": top[1] + evaluate(top[0]),
                "moves": top[2], "board": top[0]}
    return best


def solve(board, pieces):
    """Return one result per distinct order, best first."""
    results = []
    seen = set()
    for order in itertools.permutations(range(len(pieces))):
        names = tuple(pieces[i][0] for i in order)
        if names in seen:
            continue
        seen.add(names)
        results.append(search_order(board, pieces, order))
    results.sort(key=lambda res: (res["placed"], res["score"]), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------
def board_to_text(board, marks=None):
    """marks: {(r, c): letter} to highlight new pieces."""
    marks = marks or {}
    lines = ["    " + " ".join(str(c + 1) for c in range(SIZE))]
    for r in range(SIZE):
        row = []
        for c in range(SIZE):
            if (r, c) in marks:
                row.append(marks[(r, c)])
            elif board & bit(r, c):
                row.append("#")
            else:
                row.append(".")
        lines.append(f" {r + 1}  " + " ".join(row))
    return "\n".join(lines)


def piece_to_text(shape):
    return "\n".join("  " + " ".join(ch if ch == "#" else " " for ch in line) for line in shape)


def show_catalog():
    print("\nAvailable piece names:")
    names = list(PIECES)
    for i in range(0, len(names), 6):
        group = names[i:i + 6]
        height = max(len(PIECES[n]) for n in group)
        print("  " + "".join(f"{n:<9}" for n in group))
        for row in range(height):
            text = ""
            for n in group:
                shape = PIECES[n]
                line = shape[row] if row < len(shape) else ""
                text += f"{line.replace('.', ' '):<9}"
            print("  " + text)
        print()


def show_turn(board, pieces, result):
    """Print the chosen moves step by step."""
    letters = "ABC"
    print("\nOptimal Placement Sequence:")
    b = board
    for n, (idx, r, c, lines) in enumerate(result["moves"]):
        name, shape = pieces[idx]
        marks = {}
        for rr, line in enumerate(shape):
            for cc, ch in enumerate(line):
                if ch == "#":
                    marks[(r + rr, c + cc)] = letters[idx]
        mask = next(m for pr, pc, m in placements(shape) if pr == r and pc == c)
        clear_text = f"  -> clears {lines} line(s)!" if lines else ""
        print(f"\n  {n + 1}. Piece {letters[idx]} ({name}) at Row {r + 1}, Column {c + 1}{clear_text}")
        print(board_to_text(b, marks))
        b, _ = place(b, mask)
    print("\n  Board after all moves:")
    print(board_to_text(b))


# ---------------------------------------------------------------------------
# Mode 1: watch the AI play
# ---------------------------------------------------------------------------
def play_game(seed=None, show=True, delay=0.0, max_turns=None):
    rng = random.Random(seed)
    names = list(PIECES)
    board = 0
    score = 0
    streak = 0      # consecutive turns with a line clear
    turn = 0
    while True:
        turn += 1
        hand = [(n, PIECES[n]) for n in (rng.choice(names) for _ in range(3))]
        results = solve(board, hand)
        best = results[0]
        if show:
            print("\n" + "=" * 40)
            print(f"Turn {turn}   Score {score}")
            print("Pieces: " + ", ".join(f"{'ABC'[i]}={n}" for i, (n, _) in enumerate(hand)))
            for i, (_, shape) in enumerate(hand):
                print(f" {'ABC'[i]}:")
                print(piece_to_text(shape))

        cleared_this_turn = False
        for idx, r, c, _ in best["moves"]:
            shape = hand[idx][1]
            mask = next(m for pr, pc, m in placements(shape) if pr == r and pc == c)
            board, lines = place(board, mask)
            score += mask.bit_count()                  # 1 point per block placed
            if lines:
                cleared_this_turn = True
                streak += 1
                score += 10 * lines * lines * streak   # bigger clears + streaks = more points
        if not cleared_this_turn:
            streak = 0

        if show:
            order = " -> ".join("ABC"[i] for i in best["order"][:best["placed"]])
            moves = ", ".join(f"{'ABC'[i]}@(R{r + 1},C{c + 1})" for i, r, c, _ in best["moves"])
            print(f"AI plays {order}: {moves}")
            print(board_to_text(board))
            if streak > 1:
                print(f"COMBO STREAK x{streak}!")
            if delay:
                time.sleep(delay)

        if best["placed"] < 3 or (max_turns and turn >= max_turns):
            if show:
                print(f"\nGAME OVER after {turn} turns. Final score: {score}")
            return score, turn


def mode_play():
    seed_text = input("Random seed (Enter = random): ").strip()
    seed = int(seed_text) if seed_text.isdigit() else None
    delay_text = input("Seconds between turns (Enter = 0.3, 0 = fastest): ").strip()
    delay = float(delay_text) if delay_text else 0.3
    play_game(seed, show=True, delay=delay)


def mode_bench(games=10):
    print(f"AI is playing {games} games...")
    scores = []
    start = time.time()
    for g in range(games):
        score, turns = play_game(seed=g, show=False)
        scores.append(score)
        print(f"  Game {g + 1}: score {score} ({turns} turns)")
    print(f"\nAverage: {sum(scores) / len(scores):.0f}   High: {max(scores)}   "
          f"Time: {time.time() - start:.1f}s")


# ---------------------------------------------------------------------------
# Mode 2: solve YOUR board
# ---------------------------------------------------------------------------
def read_board():
    print("\nType the 8 rows of your board, top to bottom.")
    print("Use # or 1 for filled, . or 0 for empty.  Example:  ..##....")
    print("(Just press Enter on the first row for an empty board.)")
    board = 0
    for r in range(SIZE):
        while True:
            text = input(f"Row {r + 1}: ").replace(" ", "")
            if r == 0 and text == "":
                return 0
            if len(text) == SIZE and all(ch in "#1.0xX" for ch in text):
                break
            print("  Please type exactly 8 characters of # . 1 0")
        for c, ch in enumerate(text):
            if ch in "#1xX":
                board |= bit(r, c)
    return board


def read_piece(letter):
    while True:
        name = input(f"Piece {letter} name (or 'draw' to draw it, '?' for list): ").strip()
        if name == "?":
            show_catalog()
        elif name in PIECES:
            return name, PIECES[name]
        elif name.lower() == "draw":
            print("  Draw the piece row by row with # and . , empty line to finish:")
            rows = []
            while True:
                line = input("  ").strip()
                if not line:
                    break
                rows.append(line)
            if rows and any("#" in row for row in rows):
                return "custom", rows
            print("  Piece needs at least one #")
        else:
            print("  Unknown name. Type ? to see all pieces.")


def describe(board):
    notes = []
    if count_spots(board, PIECES["sq3"]):
        notes.append("a 3x3 square still fits")
    if count_spots(board, PIECES["h5"]) or count_spots(board, PIECES["v5"]):
        notes.append("a 5-long bar still fits")
    notes.append(f"{SIZE * SIZE - board.bit_count()} empty cells left")
    return ", ".join(notes)


def mode_solve():
    board = read_board()
    show_catalog()
    pieces = [read_piece(letter) for letter in "ABC"]

    print("\n1. Board Grid State:")
    print(board_to_text(board))
    print("\nPieces:")
    for letter, (name, shape) in zip("ABC", pieces):
        print(f" {letter} ({name}):")
        print(piece_to_text(shape))

    results = solve(board, pieces)
    best = results[0]
    if best["placed"] == 0:
        print("\nNo piece fits anywhere - game over.")
        return

    show_turn(board, pieces, best)

    print("\n2/3. Summary of moves:")
    for n, (idx, r, c, _) in enumerate(best["moves"]):
        print(f"   {['1st', '2nd', '3rd'][n]} -> Piece {'ABC'[idx]} at top-left (Row {r + 1}, Column {c + 1})")

    print("\n   All orders tested:")
    for res in results:
        order = "->".join("ABC"[i] for i in res["order"])
        print(f"     {order}: placed {res['placed']}/3, score {res['score']:.0f}")

    total_lines = sum(m[3] for m in best["moves"])
    print("\n4. Reasoning Summary:")
    if best["placed"] < 3:
        print(f"   Only {best['placed']} of 3 pieces can be placed - this order saves the most.")
    reason = f"   Clears {total_lines} line(s)" if total_lines else "   No lines clear this turn, but it"
    if any(m[3] >= 2 for m in best["moves"]):
        reason += " with a multi-line combo"
    reason += f" and keeps the board open: {describe(best['board'])}."
    print(reason)
    if len(results) > 1:
        gap = best["score"] - results[1]["score"]
        print(f"   Beats the next best order by {gap:.0f} points.")


# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    if args and args[0] == "play":
        mode_play()
    elif args and args[0] == "solve":
        mode_solve()
    elif args and args[0] == "bench":
        mode_bench(int(args[1]) if len(args) > 1 else 10)
    else:
        print("BLOCK BLAST SOLVER")
        print("  1) Watch the AI play a game")
        print("  2) Solve my board (enter board + 3 pieces)")
        print("  3) Benchmark (AI plays 10 games fast)")
        choice = input("Choose 1/2/3: ").strip()
        if choice == "1":
            mode_play()
        elif choice == "2":
            mode_solve()
        else:
            mode_bench(10)


if __name__ == "__main__":
    main()
