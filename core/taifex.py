"""期交所(TAIFEX)頁面的共用工具:curl_cffi 抓取與表格解析。

期交所有 bot 防護,標準 requests 的 TLS 指紋會被擋,一律用 curl_cffi 模擬
Chrome。「大額交易人」與「三大法人」兩種表格的解析由 futures_traders 與
options_traders 共用;期交所改版時只需修這一份。
"""
import re


def get(url, data=None):
    """抓頁面回傳 html 文字(curl_cffi 模擬 Chrome);data 給定時改用 POST。"""
    from curl_cffi import requests as cr
    if data is None:
        return cr.get(url, impersonate="chrome", timeout=30).text
    return cr.post(url, data=data, impersonate="chrome", timeout=30).text


def get_bytes(url, data=None):
    """同 get 但回傳 bytes(CSV 下載等非 UTF-8 內容用)。"""
    from curl_cffi import requests as cr
    if data is None:
        return cr.get(url, impersonate="chrome", timeout=30).content
    return cr.post(url, data=data, impersonate="chrome", timeout=30).content


def to_int(s):
    s = re.sub(r"[^\d\-]", "", s or "")
    return int(s) if s not in ("", "-") else 0


def cell(td):
    """'74,926<br>(74,926)' → (全部交易人, 特定法人)。"""
    nums = re.findall(r"-?[\d,]+", re.sub(r"<[^>]+>", "\n", td))
    return (float(to_int(nums[0])) if nums else 0.0,
            float(to_int(nums[1])) if len(nums) > 1 else 0.0)


def page_date(html):
    """大額交易人頁的查詢日期(queryDate input)→ YYYY-MM-DD。"""
    m = re.search(r'name="queryDate"[^>]*value="(\d{4})/(\d{2})/(\d{2})"', html)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def inst_date(html):
    """三大法人頁的「日期 YYYY/MM/DD」→ YYYY-MM-DD。"""
    m = re.search(r"日期\s*(\d{4})/(\d{2})/(\d{2})", html)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def parse_inst(html, col=10):
    """解析三大法人 Excel 頁,回傳 {商品名: {'外資'|'投信'|'自營商': 淨口}}。

    表格用大寫 <TR>/<TD>;每商品三列(自營商/投信/外資),商品名稱用 rowspan。
    身份別後的數字欄:col=10 為「未平倉餘額-多空淨額-口數」(預設);col=4 為
    「交易口數-買賣淨額」(全日=日盤+夜盤的交易流量)。
    """
    def cells(tr):
        return [re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", c))
                for c in re.split(r"(?i)<t[dh][^>]*>", tr)[1:]]

    out, contract = {}, None
    for tr in re.split(r"(?i)<tr[^>]*>", html)[1:]:
        cs = [c for c in cells(tr) if c != ""]
        if len(cs) >= 15 and re.match(r"^\d+$", cs[0]) and cs[2] in (
                "自營商", "投信", "外資"):
            contract, who, nums = cs[1], cs[2], cs[3:]
        elif len(cs) >= 13 and cs[0] in ("自營商", "投信", "外資"):
            who, nums = cs[0], cs[1:]
        else:
            continue
        if contract:
            out.setdefault(contract, {})[who] = to_int(nums[col])
    return out


def parse_inst_flow(html):
    """解析三大法人「交易口數與契約金額」表(如夜盤頁,無未平倉欄),
    回傳 {商品名: {身份: 交易買賣淨額口數}}。

    列結構:序號,商品,身份,多方口數,多方金額,空方口數,空方金額,
    淨額口數,淨額金額;商品名用 rowspan 省略於延續列。
    """
    def cells(tr):
        return [re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", c))
                for c in re.split(r"(?i)<t[dh][^>]*>", tr)[1:]]

    out, contract = {}, None
    for tr in re.split(r"(?i)<tr[^>]*>", html)[1:]:
        cs = [c for c in cells(tr) if c != ""]
        if len(cs) >= 9 and re.match(r"^\d+$", cs[0]) and cs[2] in (
                "自營商", "投信", "外資"):
            contract, who, nums = cs[1], cs[2], cs[3:]
        elif len(cs) >= 7 and cs[0] in ("自營商", "投信", "外資"):
            who, nums = cs[0], cs[1:]
        else:
            continue
        if contract:
            out.setdefault(contract, {})[who] = to_int(nums[4])
    return out


def parse_inst_flow_cp(html):
    """解析買賣權分計的「交易口數與契約金額」表(如選擇權夜盤頁),
    回傳 {商品名: {權別: {身份: 交易買賣淨額口數}}}。結構同 parse_inst_flow
    多一層「買權/賣權」rowspan。"""
    def cells(tr):
        return [re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", c))
                for c in re.split(r"(?i)<t[dh][^>]*>", tr)[1:]]

    out, contract, cp = {}, None, None
    for tr in re.split(r"(?i)<tr[^>]*>", html)[1:]:
        cs = [c for c in cells(tr) if c != ""]
        if len(cs) >= 10 and re.match(r"^\d+$", cs[0]) and cs[3] in (
                "自營商", "投信", "外資"):
            contract, cp, who, nums = cs[1], cs[2], cs[3], cs[4:]
        elif len(cs) >= 8 and cs[0] in ("買權", "賣權") and cs[1] in (
                "自營商", "投信", "外資"):
            cp, who, nums = cs[0], cs[1], cs[2:]
        elif len(cs) >= 7 and cs[0] in ("自營商", "投信", "外資"):
            who, nums = cs[0], cs[1:]
        else:
            continue
        if contract and cp:
            out.setdefault(contract, {}).setdefault(cp, {})[who] = to_int(nums[4])
    return out


def parse_inst_cp(html):
    """解析三大法人「選擇權買賣權分計」頁,回傳 {商品: {權別: {身份: 淨口}}}。

    結構同 parse_inst,多一層「買權/賣權」欄(同樣用 rowspan 省略);
    取「未平倉餘額-買賣差額-口數」(身份別後第 11 個數字欄)。
    """
    def cells(tr):
        return [re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", c))
                for c in re.split(r"(?i)<t[dh][^>]*>", tr)[1:]]

    out, contract, cp = {}, None, None
    for tr in re.split(r"(?i)<tr[^>]*>", html)[1:]:
        cs = [c for c in cells(tr) if c != ""]
        if len(cs) >= 16 and re.match(r"^\d+$", cs[0]) and cs[3] in (
                "自營商", "投信", "外資"):
            contract, cp, who, nums = cs[1], cs[2], cs[3], cs[4:]
        elif len(cs) >= 14 and cs[0] in ("買權", "賣權") and cs[1] in (
                "自營商", "投信", "外資"):
            cp, who, nums = cs[0], cs[1], cs[2:]
        elif len(cs) >= 13 and cs[0] in ("自營商", "投信", "外資"):
            who, nums = cs[0], cs[1:]
        else:
            continue
        if contract and cp:
            out.setdefault(contract, {}).setdefault(cp, {})[who] = to_int(nums[10])
    return out


def parse_large_trader(html, name):
    """大額交易人頁中,契約 name 的「所有契約」列部位;找不到回 None。

    走訪 name_a 格子依純文字比對契約名,取該 rowspan 區塊中 expiry 含「所有」
    的列;每格 '全部<br>(特定法人)',特定法人與全部交易人的買賣部位都回傳。
    """
    for m in re.finditer(r'headers="name_a"', html):
        txt = re.sub(r"\s+", "",
                     re.sub(r"<[^>]+>", "", html[m.end():m.end() + 140]))
        if name not in txt:
            continue
        tstart = html.rfind("<tr", 0, m.start())
        rs = re.search(r'rowspan="(\d+)"', html[tstart:m.start() + 5])
        n = int(rs.group(1)) if rs else 1
        for tr in re.split(r"<tr[ >]", html[tstart:])[1:n + 1]:
            exp = re.search(r'headers="expiry_a"[^>]*>(.*?)</td>', tr, re.S)
            if not exp or "所有" not in re.sub(r"\s|<[^>]+>", "", exp.group(1)):
                continue

            def grab(h):
                mm = re.search(r'headers="[^"]*' + h + r'[^"]*"[^>]*>(.*?)</td>',
                               tr, re.S)
                return cell(mm.group(1)) if mm else (0.0, 0.0)

            b5a, b5 = grab("buyer_a_01_01")
            b10a, b10 = grab("buyer_a_02_01")
            s5a, s5 = grab("seller_a_01_01")
            s10a, s10 = grab("seller_a_02_01")
            oi_m = re.search(r'headers="position_a"[^>]*>(.*?)</td>', tr, re.S)
            oi = cell(oi_m.group(1))[0] if oi_m else 0.0
            return dict(buy5=b5, buy10=b10, sell5=s5, sell10=s10, buy5_all=b5a,
                        buy10_all=b10a, sell5_all=s5a, sell10_all=s10a, oi=oi)
    return None
