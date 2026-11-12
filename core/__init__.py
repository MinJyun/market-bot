"""共用套件。匯入時套用一個進程級的網路修正(見 _prefer_ipv4)。

放在這裡而不是各入口,是因為每個資料源都會 `from core import store`,
載入 core 必然發生,不必記得在新入口補上。
"""
import socket

_getaddrinfo = socket.getaddrinfo


def _prefer_ipv4(*args, **kwargs):
    """把 getaddrinfo 的 IPv6 結果排到 IPv4 之後。

    2026-11-12:HiNet 配的 IPv6 到 Yahoo 的 SYN 完全沒有回應,而 requests/
    urllib3 沒有 Happy Eyeballs(curl 有,所以手測一律正常、不易察覺),
    會照 getaddrinfo 的順序一個個等滿 timeout。Yahoo 回兩個 IPv6 位址
    × timeout 20s = 每個序列慢 40 秒,12 個序列就把整輪 20 分鐘的上限吃光,
    下午 15:30/16:15/18:00 三個時段全被 watchdog 砍掉、一則都沒推。

    用排序而不是濾掉 IPv6,是為了哪天只有 IPv6 可用時仍連得上
    (sorted 穩定,IPv4 之間與 IPv6 之間的原順序不變)。
    """
    res = _getaddrinfo(*args, **kwargs)
    return sorted(res, key=lambda r: r[0] == socket.AF_INET6)


socket.getaddrinfo = _prefer_ipv4
