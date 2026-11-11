"""Explain Windows ICMP quantisation without changing stored measurements."""
import sys

WINDOWS_PRECISION=('Windows ICMP возвращает RTT с шагом 1 мс. Значение 0 означает менее 1 мс; '
                   'дробная средняя не повышает точность исходных проб. Нулевой jitter не исключает колебаний менее 1 мс.')

def precision_note():return WINDOWS_PRECISION if sys.platform=='win32' else ''

def rtt(value,digits=1):
    if value is None:return '—'
    if sys.platform=='win32' and value==0:return '<1'
    return f'{value:.{digits}f}'.replace('.',',')
