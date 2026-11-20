"""
Auto-player for the "Block Blast Gem Find Puzzle" Windows app.

It looks at the game window, reads the board + pieces from the screen,
asks blockblast.py for the best move, and drags the piece with the mouse.

    python autoplay.py          -> play game after game until you stop it
    python autoplay.py 10       -> play at most 10 pieces

Keep the game window visible. When no piece fits it uses the rotate power-up.
Ad pieces in the tray (an "AD" / video label, a play icon, or anything that is
not a normal piece) are never dragged - it plans with the other pieces only.
It drags fast, and slows down by itself for 10 moves after a miss.
At game over it presses "No thanks" on the revive question, then the Play
(triangle) button on the Game Over screen for a
new round, again and again forever, and keeps learning (learn.json).
No screenshots are saved.

Shortcuts (Ctrl+C works in any window; Shift+U and E only while the game is in front):
    Ctrl+C   -> pause the bot (it lets go of the mouse); press again to play on
                (Ctrl+C inside the bot's own terminal window closes the bot)
    Shift+U  -> "lose mode": put pieces in the worst places (still rotating
                when stuck) until game over, then close the game + bot
    E        -> close the game + bot right now
Moving the mouse to the TOP-LEFT corner of the screen also stops it.
Needs: pip install pillow winrt-runtime winrt-Windows.Media.Ocr
       winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams
       winrt-Windows.Foundation winrt-Windows.Foundation.Collections
"""

import asyncio
import ctypes
import json
import os
import re
import sys
import threading
import time
from ctypes import wintypes

from PIL import ImageGrab

import blockblast as bb

try:   # Windows' built-in text reader, to find the "No thanks" / "Play" buttons
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
except ImportError:
    OcrEngine = None

WINDOW_TITLE = "Block Blast Gem Find Puzzle"
APP_ID = r"shell:AppsFolder\62769PiEGame.BlockBlastGemFindPuzzle_wv5et22va5kx0!App"
HERE = os.path.dirname(os.path.abspath(__file__))
LEARN_FILE = os.path.join(HERE, "learn.json")
DRAG_LIFT = 0.257   # dragged piece's centre is drawn this much (x board width) above the mouse (measured)

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

VK_SHIFT, VK_CONTROL, VK_C, VK_E, VK_U = 0x10, 0x11, 0x43, 0x45, 0x55
lose_mode = False   # switched on by pressing Shift+U
paused = False      # switched on/off by pressing Ctrl+C


def key_down(vk):
    return user32.GetAsyncKeyState(vk) & 0x8000 != 0


def game_in_front():
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    return hwnd != 0 and user32.GetForegroundWindow() == hwnd


def watch_keys():
    """Runs in the background: Ctrl+C pauses / resumes, Shift+U turns on lose mode,
    E closes the game + bot."""
    global lose_mode, paused
    while True:
        if key_down(VK_CONTROL) and key_down(VK_C):
            paused = not paused
            if paused:
                user32.mouse_event(0x0004, 0, 0, 0, 0)   # let go of any held piece
                print("\nCtrl+C - PAUSED. Press Ctrl+C again to play on.", flush=True)
            else:
                print("\nCtrl+C - playing again (keeps what it learned).", flush=True)
            while key_down(VK_CONTROL) and key_down(VK_C):   # wait for the keys to be let go
                time.sleep(0.03)
        # Shift+U and E only count while the game is in front, so typing
        # in another window (terminal, chat, ...) never triggers them
        if not game_in_front():
            time.sleep(0.03)
            continue
        if key_down(VK_SHIFT) and key_down(VK_U) and not key_down(VK_CONTROL) and not lose_mode:
            lose_mode = True
            print("\nShift+U pressed - LOSE MODE: placing pieces badly until game over.", flush=True)
        if key_down(VK_E) and not key_down(VK_CONTROL) and not key_down(VK_SHIFT):
            user32.mouse_event(0x0004, 0, 0, 0, 0)   # let go of any held piece
            close_game_and_exit("\nE pressed - closing the game and the bot.")
        time.sleep(0.03)


# ---------------------------------------------------------------------------
# Window + mouse
# ---------------------------------------------------------------------------
def find_window():
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if not hwnd:
        print("Game window not found - starting the game...")
        os.startfile(APP_ID)
        for _ in range(30):
            time.sleep(1)
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            if hwnd:
                time.sleep(4)   # let it load
                break
        else:
            sys.exit("Could not start the game.")
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # restore if minimized
        time.sleep(0.5)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.1)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    fit_on_screen(hwnd, rect)
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


def close_game():
    """Close the Block Blast app (same as clicking its X) and wait until it is gone."""
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if hwnd:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
        for i in range(50):
            time.sleep(0.2)
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            if not hwnd:
                break
            if i == 25:   # still open after 5s: ask again via the window menu's Close
                user32.PostMessageW(hwnd, 0x0112, 0xF060, 0)   # WM_SYSCOMMAND, SC_CLOSE
        print("Closed the game.")


def close_game_and_exit(message):
    """Lose mode game over: close the Block Blast app, then close this bot."""
    print(message)
    close_game()
    print("Bot closed.")
    sys.stdout.flush()   # os._exit skips the normal flush
    os._exit(0)


def game_over(message, stats, learn):
    """Lose mode: close everything. Normal mode: note the game and return the time
    it ended - the main loop then presses "No thanks" and "Play" for a new game."""
    if lose_mode:
        close_game_and_exit(message)
    stats["games"] += 1
    best = max(learn.get("_best_game_moves", 0), stats["moves"])
    learn["_best_game_moves"] = best
    learn["_games_played"] = learn.get("_games_played", 0) + 1
    save_learning(learn)
    print(f"{message}  (game {stats['games']} finished after {stats['moves']} moves, best ever {best})")
    print("Waiting for the revive question -> No thanks -> Play...")
    sys.stdout.flush()
    stats["moves"] = 0
    return time.time()


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", ctypes.c_ulong)]


def fit_on_screen(hwnd, rect):
    """Screenshots only see what is on a monitor, so keep the whole window on one."""
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
    work = info.rcWork
    slack = 12   # windows have an invisible border a few pixels wide
    if (rect.left >= work.left - slack and rect.top >= work.top - slack
            and rect.right <= work.right + slack and rect.bottom <= work.bottom + slack):
        return
    width = min(rect.right - rect.left, work.right - work.left)
    height = min(rect.bottom - rect.top, work.bottom - work.top)
    x = min(max(rect.left, work.left), work.right - width)
    y = min(max(rect.top, work.top), work.bottom - height)
    print("  moving game window fully onto the screen")
    user32.MoveWindow(hwnd, x, y, width, height, True)
    time.sleep(0.8)


def screenshot(rect):
    return ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True).convert("RGB")


def move_mouse(rect, x, y):
    """Send a real mouse-move event (the game ignores plain cursor jumps)."""
    sx, sy = rect.left + x, rect.top + y
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    user32.SetCursorPos(int(sx), int(sy))
    user32.mouse_event(0x0001 | 0x8000 | 0x4000,          # MOVE | ABSOLUTE | VIRTUALDESK
                       int((sx - vx) * 65535 / (vw - 1)), int((sy - vy) * 65535 / (vh - 1)), 0, 0)


def fail_safe():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    if pt.x <= 2 and pt.y <= 2:
        sys.exit("Stopped (mouse in top-left corner).")


FAST, SAFE = (10, 0.006), (5, 0.008)   # (biggest mouse step in px, seconds between steps)
STEP_PX, STEP_S = FAST
careful = 0        # moves left in SAFE speed (set after a miss / a piece that did not follow)


def set_speed():
    global STEP_PX, STEP_S
    STEP_PX, STEP_S = SAFE if careful > 0 else FAST


def cursor_pos(rect):
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x - rect.left, pt.y - rect.top


def glide(rect, x0, y0, x1, y1):
    """Move the mouse smoothly to (x1, y1) like a hand: small RELATIVE steps
    (the game follows those 1:1, while absolute jumps can leave the dragged
    piece behind or frozen), steered by where the cursor really is (this also
    cancels Windows mouse acceleration), fast in the middle and slow at the ends."""
    for _ in range(2000):
        cx, cy = cursor_pos(rect)
        dx, dy = x1 - cx, y1 - cy
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < 0.8:
            return
        gone = ((cx - x0) ** 2 + (cy - y0) ** 2) ** 0.5
        # ease: speed up over the first 30px, slow down over the last 30px
        step = max(1.0, min(STEP_PX, 1 + gone / 6, dist / 6))
        sx, sy = dx / dist * step, dy / dist * step
        mx, my = int(round(sx)), int(round(sy))
        if mx == 0 and my == 0:   # tiny remainder: one pixel in the main direction
            mx, my = (int(dx > 0) - int(dx < 0), 0) if abs(dx) >= abs(dy) else (0, int(dy > 0) - int(dy < 0))
        user32.mouse_event(0x0001, mx, my, 0, 0)   # MOVE (relative)
        time.sleep(STEP_S)


def piece_box(before, now, screen, over_blocks=False):
    """Where is the dragged piece drawn? Pixels on the board that were
    empty-dark before and are bright now. With over_blocks, also bright
    pixels that changed a lot (the piece hovering over filled cells)."""
    a, b = before.load(), now.load()
    xs, ys = [], []
    for y in range(int(screen.y0), int(screen.y1), 2):
        for x in range(int(screen.x0), int(screen.x1), 2):
            p, q = a[x, y], b[x, y]
            if max(q) > 140 and (max(p) < 80 or over_blocks and
                                 abs(p[0] - q[0]) + abs(p[1] - q[1]) + abs(p[2] - q[2]) > 120):
                xs.append(x)
                ys.append(y)
    if len(xs) < 30:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def edge_error(lo, hi, want_lo, want_hi, board_lo, board_hi):
    """How far the drawn piece is from the target along one axis.
    Ignores an edge that is cut off by the board border."""
    cut_lo = lo <= board_lo + 4
    cut_hi = hi >= board_hi - 4
    if cut_lo and not cut_hi:
        return hi - want_hi
    if cut_hi and not cut_lo:
        return lo - want_lo
    return ((lo - want_lo) + (hi - want_hi)) / 2


def aim_error(before, now, screen, target):
    """(x, y) pixels the dragged piece is off from the target box, or None
    when the piece can't be seen clearly. A measurement only counts when the
    piece shows its true size (else it is hidden behind blocks, mixed up with
    a line-clear glow, ...); a side cut off by the board edge may be smaller."""
    tx0, ty0, tx1, ty1 = target
    tol = screen.cell * 0.3
    for over_blocks in (False, True):
        box = piece_box(before, now, screen, over_blocks)
        if not box:
            continue
        bw, bh = box[2] - box[0], box[3] - box[1]
        cut_x = box[0] <= screen.x0 + 4 or box[2] >= screen.x1 - 4
        cut_y = box[1] <= screen.y0 + 4 or box[3] >= screen.y1 - 4
        ok_w = abs(bw - (tx1 - tx0)) <= tol or (cut_x and bw < tx1 - tx0)
        ok_h = abs(bh - (ty1 - ty0)) <= tol or (cut_y and bh < ty1 - ty0)
        if ok_w and ok_h:
            return (edge_error(box[0], box[2], tx0, tx1, screen.x0, screen.x1),
                    edge_error(box[1], box[3], ty0, ty1, screen.y0, screen.y1))
    return None


def drag(rect, x1, y1, x2, y2, screen=None, target=None):
    """Drag from (x1,y1) to (x2,y2) in one smooth movement. If screen + target
    box are given, check where the piece is drawn and fix the aim before
    letting go. Returns (x offset, y offset, cancelled): the final mouse offset
    from (x2,y2) - used for learning - and whether the piece was carried back
    to the tray because it would not go where it should (safer than a wrong drop).

    Picked up properly (hold still a moment) and moved smoothly, the piece
    sits exactly DRAG_LIFT above the mouse, so a fix is rarely needed."""
    before = screenshot(rect) if screen else None
    move_mouse(rect, x1, y1)
    time.sleep(0.03)
    user32.mouse_event(0x0002, 0, 0, 0, 0)    # left button down
    time.sleep(0.15 if careful else 0.1)      # let the game take the piece before moving
    glide(rect, x1, y1, x2, y2)
    x, y = x2, y2
    if screen and target:
        for fixes in range(3):
            time.sleep(0.06)
            err = aim_error(before, screenshot(rect), screen, target)
            if err is None:
                break   # can't see the piece clearly: trust the aim
            ex, ey = err
            # the game snaps to the nearest cell, so a small error still lands right;
            # fixing it anyway only makes the piece swing back and forth
            if abs(ex) < screen.cell * 0.3 and abs(ey) < screen.cell * 0.3:
                break
            if fixes == 2 or max(abs(ex), abs(ey)) > screen.cell * 1.5:
                # the piece got stuck / lost the movement: put it back and pick it up again
                below = screen.y1 + 0.1 * screen.size
                glide(rect, x, y, x, below)
                glide(rect, x, below, x1, y1)
                time.sleep(0.05)
                user32.mouse_event(0x0004, 0, 0, 0, 0)
                return x - x2, y - y2, True
            glide(rect, x, y, x - ex, y - ey)
            x, y = x - ex, y - ey
    time.sleep(0.03)
    user32.mouse_event(0x0004, 0, 0, 0, 0)    # left button up
    return x - x2, y - y2, False


def wait_board_still(rect, screen, timeout=0.8):
    """Wait until the board stops changing (line-clear glow, blocks flying in):
    the game does not follow the mouse properly while it animates."""
    def sample():
        img = screenshot(rect)
        return [img.getpixel((int(screen.x0 + screen.cell * (i + 0.5) / 2), int(screen.y0 + screen.cell * (j + 0.5) / 2)))
                for i in range(16) for j in range(16)]
    end = time.time() + timeout
    last = sample()
    while time.time() < end:
        time.sleep(0.06)
        now = sample()
        if sum(sum(abs(p - q) for p, q in zip(a, b)) > 30 for a, b in zip(last, now)) <= 2:
            return
        last = now


# ---------------------------------------------------------------------------
# Reading the screen
# ---------------------------------------------------------------------------
def is_empty_cell_color(p):
    return abs(p[0] - 15) < 10 and abs(p[1] - 32) < 10 and abs(p[2] - 48) < 10


def is_block(p):
    return max(p) > 140


def is_tray_block(p):
    """Pieces in the tray - also greyed-out ones (pieces that fit nowhere)
    are clearly different from the dark-blue tray background (about 20,40,75)."""
    if max(p) - min(p) < 40 and max(p) > 110:
        return False   # grey/white = the rotate icon drawn over pieces in rotate mode
    return (p[0] - 20) ** 2 + (p[1] - 40) ** 2 + (p[2] - 75) ** 2 > 45 ** 2


class Screen:
    def __init__(self, img):
        self.img = img
        self.found = self.find_board()

    def find_board(self):
        """Every row/column always has an empty cell, so the dark empty-cell
        color outlines the whole board."""
        img = self.img
        px = img.load()
        w, h = img.size
        if w < 200 or h < 200:
            return False
        col_count, row_count = [0] * w, [0] * h
        for y in range(40, h, 3):
            for x in range(0, w, 3):
                if is_empty_cell_color(px[x, y]):
                    col_count[x] += 1
                    row_count[y] += 1
        if max(col_count) < 20:
            return False
        xs = [x for x in range(w) if col_count[x] > max(col_count) * 0.08]
        ys = [y for y in range(h) if row_count[y] > max(row_count) * 0.08]
        self.x0, self.x1, self.y0 = xs[0], xs[-1] + 2, ys[0]
        self.y1 = self.y0 + (self.x1 - self.x0)   # board is square
        self.size = self.x1 - self.x0
        self.cell = self.size / 8
        return self.y1 < h

    def cell_center(self, r, c):
        return self.x0 + self.cell * (c + 0.5), self.y0 + self.cell * (r + 0.5)

    def board(self):
        board = 0
        d = self.cell * 0.25
        for r in range(8):
            for c in range(8):
                x, y = self.cell_center(r, c)
                hits = sum(is_block(self.img.getpixel((int(x + dx), int(y + dy))))
                           for dx in (-d, 0, d) for dy in (-d, 0, d))
                if hits >= 5:
                    board |= bb.bit(r, c)
        return board

    def pieces(self):
        """Find the pieces in the tray under the board. Returns list of
        (shape, grab_x, grab_y). shape is None for something in the tray that
        is not a normal piece (an ad piece with an icon / label, or a piece
        still flying in) - the bot never drags those."""
        img, size = self.img, self.size
        tx0, tx1 = int(self.x0 + 0.175 * size), int(self.x1 + 0.05 * size)   # skip the rotate button
        # the tray is framed by light lines above and below: find them
        px = img.load()
        mid = int(self.y1 + 0.30 * size)

        def is_frame_line(y):
            hits = sum(is_tray_block(px[x, y]) for x in range(tx0, tx1, 4))
            return hits > 0.8 * len(range(tx0, tx1, 4))
        ty0 = mid
        while ty0 > self.y1 and not is_frame_line(ty0):
            ty0 -= 1
        ty1 = mid
        while ty1 < img.size[1] - 1 and not is_frame_line(ty1):
            ty1 += 1
        ty0, ty1 = ty0 + 3, ty1 - 2
        tray_cell = 0.05 * size

        # split the tray into groups of columns that contain blocks
        cols = [x for x in range(tx0, tx1) if any(is_tray_block(img.getpixel((x, y))) for y in range(ty0, ty1, 2))]
        groups, start, prev = [], None, None
        for x in cols:
            if start is None:
                start = x
            elif x - prev > tray_cell * 0.5:
                groups.append((start, prev))
                start = x
            prev = x
        if start is not None:
            groups.append((start, prev))

        result = []
        for gx0, gx1 in groups:
            xs_here = range(gx0, gx1 + 1, 2)
            # a pixel row belongs to the piece if a fair part of it is piece-coloured
            # (a few tinted pixels of the rotate icon are not enough)
            ys = [y for y in range(ty0, ty1)
                  if sum(is_tray_block(img.getpixel((x, y))) for x in xs_here) >= max(2, len(xs_here) * 0.25)]
            if not ys:
                result.append((None, (gx0 + gx1) / 2, (ty0 + ty1) / 2))
                continue
            gy0, gy1 = ys[0], ys[-1]
            w = max(1, round((gx1 - gx0 + 1) / tray_cell))
            h = max(1, round((gy1 - gy0 + 1) / tray_cell))
            cw, ch = (gx1 - gx0 + 1) / w, (gy1 - gy0 + 1) / h
            shape, filled = [], []
            for r in range(h):
                line = ""
                for c in range(w):
                    x, y = gx0 + cw * (c + 0.5), gy0 + ch * (r + 0.5)
                    # look at 5 spots in the cell (the rotate icon may cover the middle)
                    spots = [(x, y), (x - cw * 0.3, y - ch * 0.3), (x + cw * 0.3, y - ch * 0.3),
                             (x - cw * 0.3, y + ch * 0.3), (x + cw * 0.3, y + ch * 0.3)]
                    if sum(is_tray_block(img.getpixel((int(sx), int(sy)))) for sx, sy in spots) >= 2:
                        line += "#"
                        filled.append((x, y))
                    else:
                        line += "."
                shape.append(line)
            # no blocks, a row/column with no blocks, or bigger than any real
            # piece: mid-animation or an ad piece - not something to drag
            if not filled or w > 5 or h > 5 or any("#" not in line for line in shape) or any(
                    all(line[c] == "." for line in shape) for c in range(w)):
                result.append((None, (gx0 + gx1) / 2, (gy0 + gy1) / 2))
                continue
            # grab the block closest to the middle of the piece
            mx, my = (gx0 + gx1) / 2, (gy0 + gy1) / 2
            gx, gy = min(filled, key=lambda p: (p[0] - mx) ** 2 + (p[1] - my) ** 2)
            result.append((shape, gx, gy))
        return result


# ---------------------------------------------------------------------------
# Learning: remember how far off our drops land, per piece size
# ---------------------------------------------------------------------------
def load_learning():
    try:
        with open(LEARN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_learning(learn):
    with open(LEARN_FILE, "w") as f:
        json.dump(learn, f, indent=1)


def get_correction(learn, h, w):
    """Aim correction in cells (rows, cols) for a piece of this size."""
    e = learn.get(f"{h}x{w}")
    return (e["dr"], e["dc"]) if e else (0.0, 0.0)


def learn_rate(n):
    """1/2, 1/3, ... for the first moves (= the plain average), never below 0.15."""
    return max(0.15, 1 / (n + 1))


def update_correction(learn, h, w, fix_r, fix_c, err_r, err_c):
    """fix_r/fix_c: how much the live aim-check had to move (cells).
    err_r/err_c: how far the piece still landed from target (cells)."""
    e = learn.setdefault(f"{h}x{w}", {"dr": 0.0, "dc": 0.0, "moves": 0, "misses": 0})
    e["moves"] += 1
    if err_r or err_c:
        e["misses"] += 1
    if abs(err_r) <= 1 and abs(err_c) <= 1:          # big jumps = misread, skip
        # learns fast while new (big steps for the first moves), then small
        # steps so one odd move can't spoil a well-learned aim
        rate = learn_rate(e["moves"])
        e["dr"] = round(e["dr"] + rate * (fix_r - err_r), 3)
        e["dc"] = round(e["dc"] + rate * (fix_c - err_c), 3)
        e["dr"] = max(-0.4, min(0.4, e["dr"]))
        e["dc"] = max(-0.4, min(0.4, e["dc"]))


def where_did_it_land(old_board, new_board, shape, target_r, target_c):
    """Find the placement that turns old_board into new_board."""
    found = []
    for r, c, mask in bb.placements(shape):
        if old_board & mask == 0 and bb.place(old_board, mask)[0] == new_board:
            found.append((r, c))
    if not found:
        return None
    return min(found, key=lambda p: abs(p[0] - target_r) + abs(p[1] - target_c))


# ---------------------------------------------------------------------------
# Ads
# ---------------------------------------------------------------------------
def find_ad_x(img):
    """White X in the top-right corner of an ad."""
    px = img.load()
    w, h = img.size
    if w < 200 or h < 120:
        return None
    white = [(x, y) for x in range(w - 90, w - 10) for y in range(40, 100)
             if min(px[x, y]) > 230]
    if 15 < len(white) < 400:
        return sum(p[0] for p in white) / len(white), sum(p[1] for p in white) / len(white)
    return None


def read_text(img):
    """Read the text on the screen with Windows OCR.
    Returns [(line text, centre x, centre y)] in window pixels."""
    if OcrEngine is None:
        return []
    scale = 2 if img.width < 900 else 1   # small text reads better bigger
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale))
    rgba = img.convert("RGBA")
    writer = DataWriter()
    writer.write_bytes(rgba.tobytes("raw", "BGRA"))
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                    rgba.width, rgba.height)

    async def run():
        return await OcrEngine.try_create_from_user_profile_languages().recognize_async(bitmap)
    try:
        result = asyncio.run(run())
    except Exception:
        return []
    lines = []
    for line in result.lines:
        boxes = [w.bounding_rect for w in line.words]
        x0 = min(b.x for b in boxes)
        x1 = max(b.x + b.width for b in boxes)
        y0 = min(b.y for b in boxes)
        y1 = max(b.y + b.height for b in boxes)
        text = " ".join(re.sub(r"[^a-z ]", " ", line.text.lower()).split())
        lines.append((text, (x0 + x1) / 2 / scale, (y0 + y1) / 2 / scale))
    return lines


NO_THANKS = ("no thanks", "no thank you", "nothanks", "no thx")
PLAY = ("play", "play again", "replay", "retry", "restart", "try again", "new game")


def find_play_triangle(img):
    """The white "play" triangle (pointing right) of the try-again button.
    Returns its centre (x, y) or None."""
    import cv2
    import numpy as np
    a = np.asarray(img)
    h, w = a.shape[:2]
    white = (a.min(axis=2) > 215).astype(np.uint8)
    white[: int(h * 0.1)] = 0                     # title bar + score
    count, labels, stats, _ = cv2.connectedComponentsWithStats(white, connectivity=8)
    best = None
    for i in range(1, count):
        x, y, bw, bh, area = stats[i]
        if not (w * 0.04 <= bh <= w * 0.35 and 0.9 <= bh / bw <= 1.5):
            continue
        if not 0.35 <= area / (bw * bh) <= 0.65:   # a triangle fills half its box
            continue
        mask = labels[y:y + bh, x:x + bw] == i
        cols = mask.sum(axis=0)                    # column heights: tall left, point right
        rows = mask.sum(axis=1)                    # row widths: widest in the middle
        third = max(1, bw // 3)
        if not (cols[:third].mean() > 0.6 * bh and cols[-third:].mean() < 0.4 * bh):
            continue
        edge = max(1, bh // 10)
        if not (rows[bh // 2 - edge:bh // 2 + edge + 1].mean() > 0.7 * bw
                and rows[:edge].mean() < 0.4 * bw and rows[-edge:].mean() < 0.4 * bw):
            continue
        if best is None or area > best[0]:
            best = (area, x + bw / 2, y + bh / 2)
    return None if best is None else best[1:]


def find_button(img, triangle=False):
    """The "No thanks" button of the revive question, else a "Play" button
    (by its label, or - with triangle=True - by its play-triangle icon).
    Returns (text, x, y) or None. Only short exact labels count, so ad text
    like "Play now" is never clicked."""
    lines = read_text(img)
    for text, x, y in lines:
        if text in NO_THANKS or text.startswith("no thanks"):
            return text, x, y
    for text, x, y in lines:
        if text in PLAY:
            return text, x, y
    if triangle:
        spot = find_play_triangle(img)
        if spot:
            return "play", spot[0], spot[1]
    return None


def click(rect, x, y):
    move_mouse(rect, x, y)
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def ad_coming(screen):
    """'Take a break!' / 'Ad will appear in Xs' notice under the board:
    the game ignores moves until the ad has played."""
    px = screen.img.load()
    s = screen.size
    x0, x1 = int(screen.x0 - 0.08 * s), int(screen.x0 + 0.30 * s)
    y0, y1 = int(screen.y1 + 0.06 * s), int(screen.y1 + 0.18 * s)
    w, h = screen.img.size
    count = 0
    for y in range(max(0, y0), min(h, y1)):
        for x in range(max(0, x0), min(w, x1)):
            r, g, b = px[x, y]
            if r > 150 and 80 < g < 170 and b < 60:   # orange notice text
                count += 1
    return count > 15


AD_WORDS = {"ad", "ads", "free", "video", "watch"}
_ad_cache = {}


def tray_key(item):
    """Names one tray slot: its shape + where it sits."""
    shape, gx, _ = item
    return (tuple(shape) if shape else None, round(gx / 25))


def looks_like_ad(screen, item):
    """An ad piece in the tray: something that is not a normal piece, or a
    piece with an "AD" / video label or a play-triangle icon on it."""
    shape, gx, gy = item
    if shape is None:
        return True
    key = tray_key(item)
    if key not in _ad_cache:
        if len(_ad_cache) > 200:
            _ad_cache.clear()
        s = screen.size
        box = (int(max(0, gx - 0.17 * s)), int(max(0, gy - 0.17 * s)),
               int(min(screen.img.width, gx + 0.17 * s)), int(min(screen.img.height, gy + 0.17 * s)))
        crop = screen.img.crop(box)
        words = {w for text, _, _ in read_text(crop) for w in text.split()}
        _ad_cache[key] = bool(words & AD_WORDS) or find_play_triangle(crop) is not None
    return _ad_cache[key]


def rotate_shape(shape):
    """Turn a piece 90 degrees clockwise."""
    h, w = len(shape), len(shape[0])
    return ["".join(shape[h - 1 - r][c] for r in range(h)) for c in range(w)]


def best_rotation(board, tray, lose=False):
    """When stuck: check every tray piece in all 3 other rotations.
    Returns (piece index, quarter turns) of the best (or worst in lose mode)
    rotation that fits somewhere, or None if nothing fits at all."""
    choice = None
    for i, (shape, _, _) in enumerate(tray):
        turned = shape
        for turns in (1, 2, 3):
            turned = rotate_shape(turned)
            for _, _, mask in bb.placements(turned):
                if board & mask:
                    continue
                new_board, lines = bb.place(board, mask)
                score = bb.line_points(lines) + bb.evaluate(new_board)
                if lose:
                    score = -score
                # prefer fewer turns when scores tie (each turn may use a rotate)
                key = (score, -turns)
                if choice is None or key > choice[0]:
                    choice = (key, i, turns)
    return None if choice is None else (choice[1], choice[2])


def worst_move(board, pieces):
    """Lose mode: the placement that leaves the worst board (no line clears)."""
    worst = None
    for idx, (_, shape) in enumerate(pieces):
        for r, c, mask in bb.placements(shape):
            if board & mask:
                continue
            new_board, lines = bb.place(board, mask)
            score = bb.evaluate(new_board) + bb.line_points(lines) * 10
            if worst is None or score < worst[0]:
                worst = (score, idx, r, c, lines)
    return worst


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def read_stable(rect, tries=20):
    """Read the screen until board + tray look the same twice in a row."""
    last = None
    screen = None
    same = 0
    for _ in range(tries):
        screen = Screen(screenshot(rect))
        if not screen.found:
            return screen, None, None
        try:
            state = (screen.board(), screen.pieces())
        except IndexError:   # odd screen (ad / menu) that looked a bit like a board
            screen.found = False
            return screen, None, None
        if state[1] and state == last:
            # an odd tray item must look the same for longer: pieces flying
            # in settle, a real ad piece stays
            same += 1
            if same >= (3 if any(shape is None for shape, _, _ in state[1]) else 1):
                return screen, state[0], state[1]
        else:
            same = 0
        last = state
        time.sleep(0.1)
    return screen, None, None


NUDGES = [(0, 0), (-0.4, 0), (0.4, 0), (0, -0.4), (0, 0.4)]   # retry offsets in cells


def any_fits(board, tray):
    return any(board & mask == 0 for shape, _, _ in tray if shape for _, _, mask in bb.placements(shape))


def play(max_moves=10 ** 12):
    threading.Thread(target=watch_keys, daemon=True).start()
    print("Playing forever. Ctrl+C = pause/resume, Shift+U = lose on purpose, E = close all.")
    if OcrEngine is None:
        print("  (winrt OCR not installed: can't press No thanks / Play, will reopen the game instead)")
    learn = load_learning()
    over_since = None   # when the last game ended (waiting for revive question / Play)
    moves_done = 0
    fails = 0
    hidden = 0
    no_fit = 0
    rotate_board, rotate_tries = None, 0
    ad_slots = set()      # tray slots found to be ad pieces (never dragged)
    slot_misses = {}      # tray slot -> drags that did nothing
    only_ads_since = None
    global careful
    stats = {"games": 0, "moves": 0}
    start = time.time()
    while moves_done < max_moves:
        if paused:
            time.sleep(0.1)
            continue
        fail_safe()
        rect = find_window()
        screen, board, tray = read_stable(rect)

        if over_since is not None or not screen.found:
            # the play triangle only counts after a game over (ads have them too)
            button = find_button(screen.img, triangle=over_since is not None)
            if button:
                text, bx, by = button
                if lose_mode:
                    close_game_and_exit(f"'{text}' screen showed up - GAME OVER.")
                if over_since is None:   # the game ended before the bot noticed
                    over_since = game_over("Game ended.", stats, learn)
                print(f"  pressing '{text}'")
                click(rect, bx, by)
                time.sleep(1.5)
                if text in PLAY:
                    over_since = None
                    no_fit, rotate_board, rotate_tries = 0, None, 0
                    print("New game started (keeps what it learned).")
                hidden = 0
                continue
        if over_since is not None:
            if screen.found and board is not None and any_fits(board, tray):
                over_since = None   # a fresh board is up: play on
                no_fit, rotate_board, rotate_tries = 0, None, 0
                print("New game started (keeps what it learned).")
            elif time.time() - over_since > 25:
                print("No revive question / Play button found - reopening the game instead.")
                close_game()
                time.sleep(1)
                over_since = None
                continue
            elif screen.found:
                time.sleep(0.5)
                continue

        if not screen.found:
            spot = find_ad_x(screen.img)
            if spot:
                print("  closing ad")
                click(rect, *spot)
                time.sleep(0.8)
                hidden = 0
                continue
            hidden += 1
            if hidden > 60 and over_since is None:   # about 30s with no board and no ad X
                over_since = game_over("Board gone for 30s with no ad to close - treating it as GAME OVER.",
                                       stats, learn)
                hidden = 0
                continue
            time.sleep(0.5)
            continue
        hidden = 0
        if board is None:
            continue

        # ad pieces: never drag them, plan with the other pieces only
        keys = [tray_key(item) for item in tray]
        ad_slots &= set(keys)          # forget slots whose piece is gone
        slot_misses = {k: v for k, v in slot_misses.items() if k in keys}
        for item, key in zip(tray, keys):
            if key not in ad_slots and looks_like_ad(screen, item):
                ad_slots.add(key)
                print(f"  ad piece in tray slot {keys.index(key) + 1} - ignoring it")
        real = [i for i, k in enumerate(keys) if k not in ad_slots and slot_misses.get(k, 0) < 3]
        if not real and any(slot_misses.get(k, 0) >= 3 for k in keys if k not in ad_slots):
            slot_misses = {}           # every piece "missed": it was the aim, not ads - try again
            real = [i for i, k in enumerate(keys) if k not in ad_slots]
        if not real:
            # only ad pieces left: the game normally deals new pieces soon
            only_ads_since = only_ads_since or time.time()
            if time.time() - only_ads_since < 5:
                time.sleep(0.3)
                continue
        else:
            only_ads_since = None
        tray = [tray[i] for i in real]

        pieces = [(f"P{i + 1}", shape) for i, (shape, _, _) in enumerate(tray)]
        if lose_mode:
            worst = worst_move(board, pieces)
            if worst is None:
                best = {"placed": 0}   # stuck: the rotate code below takes over
            else:
                best = {"placed": 1, "moves": [worst[1:]]}
        else:
            best = bb.solve(board, pieces)[0]
        if best["placed"] == 0:
            no_fit += 1
            if no_fit < 3:   # double-check it is really stuck
                time.sleep(0.4)
                continue
            # try every piece in every rotation (90, 180, 270 degrees)
            if find_button(screen.img):   # revive question already on top of the board
                over_since = game_over("Game ended.", stats, learn)
                continue
            plan = best_rotation(board, tray, lose_mode)
            if plan is None:
                print(bb.board_to_text(board))
                over_since = game_over("No piece fits in any rotation - GAME OVER.", stats, learn)
                no_fit, rotate_board, rotate_tries = 0, None, 0
                continue
            # a rotate that worked changes the board: start counting tries again
            if board != rotate_board:
                rotate_board, rotate_tries = board, 0
            if rotate_tries >= 8:
                over_since = game_over("Rotating does not work any more (no rotates left?) - GAME OVER.",
                                       stats, learn)
                no_fit, rotate_board, rotate_tries = 0, None, 0
                continue
            i, turns = plan
            rotate_tries += 1
            print(f"  stuck - rotating piece {i + 1} ({turns * 90} degrees fits)")
            # rotate power-up: press the button, then tap the piece = one turn.
            # The loop then reads the tray again and turns more if needed
            # (or the game drops the turned piece by itself).
            click(rect, screen.x0 + 0.054 * screen.size, screen.y1 + 0.32 * screen.size)
            time.sleep(0.6)
            _, gx, gy = tray[i]
            click(rect, gx, gy)
            time.sleep(1.2)
            no_fit = 2   # re-check right away next loop
            continue

        no_fit = 0
        idx, r, c, lines = best["moves"][0]
        shape, grab_x, grab_y = tray[idx]
        h, w = len(shape), len(shape[0])
        dr, dc = get_correction(learn, h, w)
        nr, nc = NUDGES[fails % len(NUDGES)]
        # where the middle of the piece must end up (+ learned correction + retry nudge)
        cx, cy = screen.cell_center(r + dr + nr, c + dc + nc)
        cx += (w - 1) / 2 * screen.cell
        cy += (h - 1) / 2 * screen.cell
        tx, ty = screen.cell_center(r, c)
        half = screen.cell / 2
        target = (tx - half, ty - half, tx - half + w * screen.cell, ty - half + h * screen.cell)
        if paused:
            continue   # Ctrl+C pressed while thinking: don't start a drag
        lift_key = f"_lift_{int(screen.size // 25) * 25}"   # one setting per window size
        lift = learn.get(lift_key, DRAG_LIFT)   # learned: how far above the mouse the piece is drawn
        wait_board_still(rect, screen)
        fx, fy, cancelled = drag(rect, grab_x, grab_y, cx, cy + lift * screen.size, screen, target)
        if cancelled:
            print("  piece did not follow the mouse - put it back, trying again (slower for a while)")
            careful = 10
            set_speed()
            time.sleep(0.3)
            continue

        # wait for the board to change (and any clear animation to finish)
        time.sleep(0.2)
        after, new_board, _ = read_stable(rect, tries=8)
        if not after.found:
            if find_ad_x(after.img):
                # dragging this piece opened an ad: likely an ad piece (2 of these = skipped)
                key = tray_key(tray[idx])
                slot_misses[key] = slot_misses.get(key, 0) + 2
            continue   # ad popped up - handled next loop
        if new_board is None:
            new_board = after.board()

        name = "/".join(shape)
        if new_board == board:
            if ad_coming(after):
                # "Take a break!" - the game is about to show an ad; that move
                # did not count, so just try again (the ad will appear)
                time.sleep(0.5)
                continue
            fails += 1
            careful = 10
            set_speed()
            key = tray_key(tray[idx])
            slot_misses[key] = slot_misses.get(key, 0) + 1
            if slot_misses[key] >= 3:
                print(f"  {name} will not go down after 3 tries - treating it as an ad piece, using the others")
            else:
                print(f"  miss ({name} -> R{r + 1}C{c + 1}), retrying with nudge (slower for a while)")
            if fails >= 6:
                time.sleep(2)   # game may be frozen before an ad
            continue

        fails = 0
        if careful:
            careful -= 1
            set_speed()
        moves_done += 1
        stats["moves"] += 1
        note = ""
        landed = where_did_it_land(board, new_board, shape, r, c)
        if landed:
            err_r, err_c = landed[0] - r, landed[1] - c
            # up/down aim is learned once for all pieces via _lift, left/right per piece size
            if abs(fx) < screen.cell * 0.5 and abs(fy) < screen.cell * 0.5:   # big fixes = glitches
                update_correction(learn, h, w, 0.0, fx / screen.cell, err_r, err_c)
            if (err_r, err_c) == (0, 0) and 2 < abs(fy) < screen.cell * 0.5:
                # the live check had to move the mouse a bit: learn the real lift
                # (big fixes are one-off pick-up glitches, not a wrong aim)
                n = learn[lift_key + "_n"] = learn.get(lift_key + "_n", 5) + 1   # old files: count as 5
                learn[lift_key] = round(lift + learn_rate(n) * fy / screen.size, 4)
            save_learning(learn)
            if (err_r, err_c) != (0, 0):
                note = f"  [landed off by {err_r},{err_c} - learning]"
        print(f"Move {moves_done}: {name} -> R{r + 1}C{c + 1}" + (f" clears {lines}" if lines else "") + f"  aim-fix {fx:+.0f},{fy:+.0f}px lift {learn.get(lift_key, DRAG_LIFT):.3f}" + note)
    rate = moves_done / max(1, time.time() - start)
    print(f"Done: {moves_done} pieces placed, {rate:.2f} moves/sec.")


if __name__ == "__main__":
    try:
        play(int(sys.argv[1]) if len(sys.argv) > 1 else 10 ** 12)
    except KeyboardInterrupt:   # Ctrl+C in the bot's own terminal window
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        print("Bot closed.")
