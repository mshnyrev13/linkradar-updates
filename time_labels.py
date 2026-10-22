"""Portable timestamp labels. An unsupported date is unknown, never a GUI error."""
import math
import time


def format_timestamp(timestamp,pattern='%H:%M:%S',*,utc=False,milliseconds=False):
    try:
        if isinstance(timestamp,bool):return '—'
        value=float(timestamp)
        if not math.isfinite(value):return '—'
        calendar=time.gmtime(value) if utc else time.localtime(value)
        # These numeric labels dominate chart/log/report rendering. libc
        # strftime is comparatively expensive on macOS. Keep custom patterns
        # and old-year padding semantics on the system implementation.
        clock=f'{calendar.tm_hour:02d}:{calendar.tm_min:02d}'
        if pattern=='%H:%M':label=clock
        elif pattern=='%H:%M:%S':label=clock+f':{calendar.tm_sec:02d}'
        elif pattern=='%Y-%m-%d %H:%M:%S' and calendar.tm_year>=1000:
            label=f'{calendar.tm_year:04d}-{calendar.tm_mon:02d}-{calendar.tm_mday:02d} '+clock+f':{calendar.tm_sec:02d}'
        else:label=time.strftime(pattern,calendar)
        return label+(f'.{int(value%1*1000):03d}' if milliseconds else '')
    except (OSError,OverflowError,ValueError,TypeError):
        # Windows accepts a smaller time range than macOS. Do not invent a
        # local time or let an old/corrupt timestamp abort Qt's paint callback.
        return '—'
