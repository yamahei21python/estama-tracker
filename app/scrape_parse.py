"""Pure parsing helpers for estama.jp schedules (no network, no DB).


Extracted so the scraper class, the store layer and the tests can
import this without the CLI noise of estama_scraper.py.
"""
import re

from common import parse_clock

def parse_shift_details(shift_str, slots=None):
    """Decompose shift string into start, end, work_minutes, work_hours.

    e.g. '12:00～20:00' -> ('12:00', '20:00', 480, 8.0)
    e.g. '18:00～27:30' -> ('18:00', '27:30', 570, 9.5)
    e.g. '19:00～翌5:00' -> ('19:00', '05:00', 600, 10.0)
    If shift text is missing/None but slots exist, estimate from active slot range.
    """
    if shift_str:
        s = shift_str.strip()
        m = re.match(r"^(\d{1,2}:\d{2})\s*[～~ー-]\s*(?:翌)?(\d{1,2}:\d{2})$", s)
        if m:
            start_s, end_s = m.group(1), m.group(2)
            sh, sm = map(int, start_s.split(":"))
            eh, em = map(int, end_s.split(":"))
            if "翌" in s or eh < sh:
                eh += 24
            start_mins = sh * 60 + sm
            end_mins = eh * 60 + em
            work_mins = end_mins - start_mins
            work_hours = round(work_mins / 60.0, 1)
            return start_s, end_s, work_mins, work_hours

    # Fallback to timetable slots if available
    if slots:
        active_times = [s[0] for s in slots
                        if normalize_slot_mark(s[1]) in ("○", "×")]
        if active_times:
            start_s = active_times[0]
            # Slot is 30 mins, so add 30 mins to last slot time
            last_t = active_times[-1]
            lh, lm = map(int, last_t.split(":"))
            end_mins = lh * 60 + lm + 30
            eh = end_mins // 60
            em = end_mins % 60
            end_s = f"{eh:02d}:{em:02d}"

            sh, sm = map(int, start_s.split(":"))
            start_mins = sh * 60 + sm
            work_mins = end_mins - start_mins
            work_hours = round(work_mins / 60.0, 1)
            return start_s, end_s, work_mins, work_hours

    return None, None, None, None


def _slot_pattern(circles, crosses, dashes, tels, days_offset):
    if circles > 0:
        slot_pattern = f"○{circles}コマ / ×{crosses}コマ (予約可)"
        if tels:
            slot_pattern += f" + TEL{tels}"
    elif crosses > 0:
        if days_offset == 0:
            slot_pattern = f"○0コマ / ×{crosses}コマ (本日完売)"
        else:
            slot_pattern = f"○0コマ / ×{crosses}コマ (全コマ×/予約不可)"
        if tels:
            slot_pattern += f" + TEL{tels}"
    elif tels > 0 and dashes == 0:
        slot_pattern = f"全{tels}コマ TEL (電話のみ)"
    elif tels > 0:
        slot_pattern = f"全{dashes}コマ ─ + TEL{tels} (電話のみ混在)"
    else:
        slot_pattern = f"全{dashes}コマ ─ (受付終了/待機)"
    return slot_pattern


def _column_status_text(raw, norm, circles, crosses, tels):
    if not raw:
        if norm == "sold_out":
            return f"本日完売(×{crosses})"
        if norm in ("available", "low"):
            return "予約受付中"
        if crosses > 0 and circles == 0:
            return "全コマ×(事前予約不可/おとり候補)"
        if norm == "dummy" or (tels > 0 and circles == 0 and crosses == 0):
            return "TELのみ"
        return "時間のみ"
    # Keep site text, but mark TEL-only in raw when norm is dummy
    # and the site text would otherwise look bookable.
    if norm == "dummy" and tels > 0 and circles == 0 and crosses == 0:
        return f"{raw} / TELのみ"
    return raw


def normalize_slot_mark(mark):
    """Canonical 30-min slot mark: ○ | × | ─ | TEL, otherwise None.

    The site mixes full/half-width variants; blank cells, headers and
    anything unrecognised are dropped so slot_events only holds real
    state.
    """
    if mark is None:
        return None
    m = str(mark).strip()
    if not m:
        return None
    if m in ("○", "◯", "〇"):
        return "○"
    if m in ("×", "✕", "✖", "✗", "x", "X"):
        return "×"
    if m in ("─", "－", "-", "ー", "—", "―", "‐"):
        return "─"
    if m.upper() == "TEL":
        return "TEL"
    return None


def _slot_within_shift(slot_minutes, shift_start, shift_end):
    """True/False when the shift bounds are known, None when they aren't.

    Late-night slots and shifts are compared on the same day offset
    (27:00 slot vs. 19:00～翌5:00 shift -> 1620 vs. 1140..1740).
    """
    start = parse_clock(shift_start)
    end = parse_clock(shift_end)
    if start is None or end is None:
        return None
    if end <= start:
        end += 1440
    m = slot_minutes
    if m < start:
        m += 1440
    return start <= m < end


