import io
import re
import time
import random
import hashlib
import textwrap
import colorsys
import asyncio
import threading
import tkinter as tk
from bisect import bisect_right

import requests
 
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
 
try:
    from winrt.windows.storage.streams import DataReader
    HAS_STREAMS = True
except Exception:
    HAS_STREAMS = False
    print("Peringatan: 'winrt-Windows.Storage.Streams' belum ter-install -- "
          "cover album tidak bisa diambil, pakai palet warna fallback saja.\n"
          "  Install dengan: pip install \"winrt-Windows.Storage.Streams\"")
 
try:
    from PIL import Image, ImageTk, ImageDraw, ImageFont
    HAS_PIL = True
except Exception:
    HAS_PIL = False
    print("FATAL: package 'pillow' belum ter-install. Wajib untuk versi ini.\n"
          "  Install dengan: pip install pillow")
    raise SystemExit(1)
 
WIDTH, HEIGHT = 1000, 600
TRANSPARENT_KEY = "#010102"
NUM_NOTES = 14
CARD_MAX_WIDTH = 460
SLIDE_DURATION = 0.9   
 
 
class NowPlaying:
    def __init__(self):
        self.track_id = None
        self.title = None
        self.artist = None
        self.duration_ms = 0
        self.progress_ms = 0
        self.is_playing = False
        self.thumbnail_bytes = None
        self._last_fetch = 0
        self._lock = threading.Lock()
 
    def poll(self):
        asyncio.run(self._poll_loop())
 
    async def _poll_loop(self):
        while True:
            try:
                await self._update_once()
            except Exception as e:
                print("Media session poll error:", e)
            await asyncio.sleep(0.05)
 
    async def _find_session(self):
        manager = await MediaManager.request_async()
        for s in manager.get_sessions():
            app_id = (s.source_app_user_model_id or "").lower()
            if "spotify" in app_id:
                return s
        return manager.get_current_session()
 
    async def _fetch_thumbnail(self, info):
        if not HAS_STREAMS:
            return None
        try:
            stream_ref = info.thumbnail
            if not stream_ref:
                return None
            stream = await stream_ref.open_read_async()
            size = int(stream.size)
            if size <= 0:
                return None
            reader = DataReader(stream)
            await reader.load_async(size)
            buf = bytearray(size)
            reader.read_bytes(buf)
            return bytes(buf)
        except Exception as e:
            print("Thumbnail fetch error:", e)
            return None
 
    async def _update_once(self):
        session = await self._find_session()
        if not session:
            with self._lock:
                self.track_id = None
            return
 
        info = await session.try_get_media_properties_async()
        timeline = session.get_timeline_properties()
        playback = session.get_playback_info()
 
        title = info.title or ""
        artist = info.artist or ""
        new_id = f"{artist}::{title}"
 
        duration_ms = (timeline.end_time - timeline.start_time).total_seconds() * 1000
        position_ms = timeline.position.total_seconds() * 1000
        is_playing = playback.playback_status == PlaybackStatus.PLAYING
 
        track_changed = False
        with self._lock:
            track_changed = new_id != self.track_id
            if track_changed:
                self.track_id = new_id
                self.title = title
                self.artist = artist
                self.thumbnail_bytes = None
            self.duration_ms = duration_ms
            self.progress_ms = position_ms
            self.is_playing = is_playing
            self._last_fetch = time.time()
 
        if track_changed:
            thumb = await self._fetch_thumbnail(info)
            with self._lock:
                if self.track_id == new_id:
                    self.thumbnail_bytes = thumb
 
    def get_snapshot(self):
        with self._lock:
            elapsed = (time.time() - self._last_fetch) * 1000 if self.is_playing else 0
            return {
                "track_id": self.track_id,
                "title": self.title,
                "artist": self.artist,
                "duration_ms": self.duration_ms,
                "progress_ms": self.progress_ms + elapsed,
                "is_playing": self.is_playing,
            }
 
    def get_thumbnail(self):
        with self._lock:
            return self.track_id, self.thumbnail_bytes
 
 
# parse lyrics from lrclib API
class LyricsStore:
    def __init__(self):
        self.track_id = None
        self.lines = []
        self._lock = threading.Lock()
 
    def ensure_loaded(self, snap):
        tid = snap["track_id"]
        with self._lock:
            if tid == self.track_id:
                return
        threading.Thread(target=self._fetch, args=(snap,), daemon=True).start()
 
    def _fetch(self, snap):
        lines = []
        try:
            resp = requests.get(
                "https://lrclib.net/api/get",
                params={
                    "track_name": snap["title"],
                    "artist_name": snap["artist"],
                    "duration": int(snap["duration_ms"] / 1000),
                },
                timeout=6,
            )
            if resp.ok:
                data = resp.json()
                synced = data.get("syncedLyrics")
                if synced:
                    lines = self._parse_lrc(synced)
                elif data.get("plainLyrics"):
                    plain = [t for t in data["plainLyrics"].splitlines() if t.strip()]
                    step = max(2000, snap["duration_ms"] // max(1, len(plain)))
                    lines = [(i * step, t) for i, t in enumerate(plain)]
        except Exception as e:
            print("Lyrics fetch error:", e)
 
        with self._lock:
            self.track_id = snap["track_id"]
            self.lines = lines or [(0, "(lirik tidak ditemukan)")]
 
    @staticmethod
    def _parse_lrc(text):
        out = []
        pattern = re.compile(r"\[(\d+):(\d+\.\d+)\](.*)")
        for line in text.splitlines():
            m = pattern.match(line)
            if m:
                mins, secs, txt = m.groups()
                ms = int((int(mins) * 60 + float(secs)) * 1000)
                if txt.strip():
                    out.append((ms, txt.strip()))
        return sorted(out)
 
    def current_index(self, progress_ms):
        with self._lock:
            lines = list(self.lines)

        if not lines:
            return -1, lines

        times = [ms for ms, _ in lines]
        idx = bisect_right(times, progress_ms) - 1
        return idx, lines
 
 
# color based on album
def _hsl_rgb(h, s, l):
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, l, s)
    return (int(r * 255), int(g * 255), int(b * 255))
 
 
def palette_from_hue(hue, sat=0.55):
    return {
        "bg": _hsl_rgb(hue, sat * 0.55, 0.16),
        "text": _hsl_rgb(hue, 0.05, 0.95),
        "accent": _hsl_rgb(hue, sat, 0.6),
        "glass_border": _hsl_rgb(hue, sat * 0.4, 0.34),
        "shadow_outer": (5, 4, 6),
        "edge_light": _hsl_rgb(hue, sat * 0.5, 0.55),
    }
 
 
def fallback_palette(track_id):
    seed = int(hashlib.md5((track_id or "default").encode()).hexdigest(), 16)
    return palette_from_hue(seed % 360)
 
 
def palette_from_cover(raw_bytes):
    try:
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        avg = img.resize((1, 1)).getpixel((0, 0))
        h, l, s = colorsys.rgb_to_hls(avg[0] / 255, avg[1] / 255, avg[2] / 255)
        return palette_from_hue(h * 360, max(0.4, s))
    except Exception as e:
        print("Gagal proses warna cover:", e)
        return None
 
 
TRANSPARENT_KEY_RGB = tuple(int(TRANSPARENT_KEY.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
 
 
# visual rendering
def _font(size):
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()
 
 
def render_card(text, palette, max_width=CARD_MAX_WIDTH):
    pad = 26
    font = _font(22)
    quote = f"\u201c{text}\u201d"
 
    tmp_img = Image.new("RGB", (10, 10))
    tmp_draw = ImageDraw.Draw(tmp_img)
    avg_char_w = max(6, tmp_draw.textlength("x", font=font))
    max_chars = max(10, int((max_width - 2 * pad) / avg_char_w))
    wrapped = textwrap.wrap(quote, width=max_chars) or [quote]
 
    line_h = font.size + 10
    card_w = max_width
    card_h = pad * 2 + line_h * len(wrapped)
    margin = 22  # ruang ekstra utk shadow offset
 
    img = Image.new("RGB", (card_w + margin * 2, card_h + margin * 2), TRANSPARENT_KEY_RGB)
    draw = ImageDraw.Draw(img)
 
    shadow_box = [margin + 6, margin + 10, margin + 6 + card_w, margin + 10 + card_h]
    draw.rounded_rectangle(shadow_box, radius=20, fill=palette["shadow_outer"])
 
    card_box = [margin, margin, margin + card_w, margin + card_h]
    draw.rounded_rectangle(card_box, radius=20, fill=palette["bg"],
                            outline=palette["glass_border"], width=2)
    draw.line([(margin + 22, margin + 2), (margin + card_w - 22, margin + 2)],
              fill=palette["edge_light"], width=2)
 
    ty = margin + pad
    for line in wrapped:
        tw = draw.textlength(line, font=font)
        tx = margin + (card_w - tw) / 2
        draw.text((tx, ty), line, font=font, fill=palette["text"])
        ty += line_h
 
    return img
 
 
# flying music note
class Note:
    SYMBOLS = ["\u266a", "\u266b", "\u266c"]
 
    def __init__(self, canvas, accent_ref):
        self.canvas = canvas
        self.accent_ref = accent_ref
        self.item = canvas.create_text(0, 0, text="")
        self.reset(random.uniform(0, HEIGHT))
 
    def reset(self, y=None):
        self.x = random.uniform(0, WIDTH)
        self.y = y if y is not None else HEIGHT + 20
        self.speed = random.uniform(20, 55)
        self.drift = random.uniform(-12, 12)
        size = random.randint(14, 26)
        symbol = random.choice(self.SYMBOLS)
        r, g, b = self.accent_ref[0]
        shade = random.uniform(0.4, 0.85)
        color = "#{:02x}{:02x}{:02x}".format(int(r * shade), int(g * shade), int(b * shade))
        self.canvas.itemconfig(self.item, text=symbol, fill=color, font=("Segoe UI Emoji", size))
        self.canvas.tag_lower(self.item)
 
    def update(self, dt):
        self.y -= self.speed * dt
        self.x += self.drift * dt
        if self.y < -20:
            self.reset()
        self.canvas.coords(self.item, self.x, self.y)
 
 
def _ease_out(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3
 
 
# Tkinter overlay
class LyricsOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.geometry(f"{WIDTH}x{HEIGHT}+80+80")
        self.root.config(bg=TRANSPARENT_KEY)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            pass
 
        self.canvas = tk.Canvas(self.root, width=WIDTH, height=HEIGHT,
                                 bg=TRANSPARENT_KEY, highlightthickness=0, bd=0)
        self.canvas.pack()
 
        self.canvas.bind("<ButtonPress-1>", self._start_move)
        self.canvas.bind("<B1-Motion>", self._do_move)
        self.root.bind("<Escape>", lambda e: self.root.destroy())
 
        self.accent_ref = [(200, 70, 90)]
        self.notes = [Note(self.canvas, self.accent_ref) for _ in range(NUM_NOTES)]
 
        self.card_item = self.canvas.create_image(WIDTH / 2, HEIGHT / 2, anchor="center")
        self._card_photo = None
 
        self.txt_meta = self.canvas.create_text(16, HEIGHT - 18, text="", fill="#d8d8e0",
                                                  font=("Segoe UI", 11), anchor="w")
        self.txt_empty = self.canvas.create_text(WIDTH / 2, HEIGHT / 2, text="", fill="#c9c9d4",
                                                   font=("Segoe UI", 22, "bold"))
 
        self.now_playing = NowPlaying()
        threading.Thread(target=self.now_playing.poll, daemon=True).start()
        self.lyrics = LyricsStore()
 
        self._current_palette = fallback_palette(None)
        self._theme_track_id = object()
        self._last_line_idx = None
        self._side_left = True
        self._slide_start_y = HEIGHT + 120
        self._slide_end_y = HEIGHT / 2
        self._anim_start = time.time()
        self._pos = [WIDTH / 2, HEIGHT / 2]
        self._target = [WIDTH / 2, HEIGHT / 2]
        self._raw_card_img = None
 
        self._last_tick = time.time()
        self.root.after(16, self.tick)
 
    def _start_move(self, event):
        self._drag_x, self._drag_y = event.x, event.y
 
    def _do_move(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")
 
    def _pick_new_target(self, card_w, card_h):
     margin = 50

    # randomize pos
     x = random.randint(
        int(card_w / 2 + margin),
        int(WIDTH - card_w / 2 - margin)
    )

     y = random.randint(
        int(HEIGHT * 0.18),
        int(HEIGHT * 0.78)
    )

    # animations
     self._pos = [x, HEIGHT + card_h + 60]
     self._target = [x, y]
 
    def _update_theme(self):
        track_id, thumb_bytes = self.now_playing.get_thumbnail()
        if track_id == self._theme_track_id:
            return self._current_palette
        self._theme_track_id = track_id
        palette = None
        if HAS_PIL and thumb_bytes:
            palette = palette_from_cover(thumb_bytes)
        if palette is None:
            palette = fallback_palette(track_id)
        self.accent_ref[0] = palette["accent"]
        self._current_palette = palette
        return palette
 
    def tick(self):
      now = time.time()
      dt = now - self._last_tick
      self._last_tick = now

      snap = self.now_playing.get_snapshot()
      palette = self._update_theme()

      for n in self.notes:
        n.update(dt)

      if snap["track_id"]:
        self.canvas.itemconfig(self.txt_empty, text="")
        self.lyrics.ensure_loaded(snap)
        idx, lines = self.lyrics.current_index(snap["progress_ms"])

        if idx >= 0 and lines:

            # change lyrics line
            if idx != self._last_line_idx:
                self._last_line_idx = idx

                self._raw_card_img = render_card(lines[idx][1], palette)
                self._anim_start = now

                margin = 40
                card_w = self._raw_card_img.width
                card_h = self._raw_card_img.height

                # left and right
                if self._side_left:
                    x = margin + card_w / 2
                else:
                    x = WIDTH - margin - card_w / 2

                self._side_left = not self._side_left

                self._target = [x, HEIGHT / 2]
                self._pos = [x, HEIGHT + card_h]

            if self._raw_card_img is not None:
                t = min(1.0, (now - self._anim_start) / SLIDE_DURATION)
                ease = _ease_out(t)

                x = self._target[0]
                start_y = HEIGHT + self._raw_card_img.height + 60
                end_y = self._target[1]

                y = start_y + (end_y - start_y) * ease

                self._card_photo = ImageTk.PhotoImage(self._raw_card_img)
                self.canvas.itemconfig(
                self.card_item,
                image=self._card_photo,
                state="normal"
                              )
            self.canvas.coords(self.card_item, x, y)

        else:
            self.canvas.itemconfig(self.card_item, state="hidden")

        self.canvas.itemconfig(
            self.txt_meta,
            text=f"{snap['title']} — {snap['artist']}"
        )

      else:
        self._last_line_idx = None
        self.canvas.itemconfig(self.card_item, state="hidden")
        self.canvas.itemconfig(self.txt_meta, text="")
        self.canvas.itemconfig(
            self.txt_empty,
            text="Tidak ada lagu yang sedang diputar..."
        )

      self.root.after(16, self.tick)
 
    def run(self):
        self.root.mainloop()
 
 
if __name__ == "__main__":
    LyricsOverlay().run()