"""
四層 AI 流水線（multi-agent）：

  第1層 語言理解大師：把顧客訊息解析成結構化「小抄」(JSON)。
  第2層 最強庫管    ：用小抄關鍵字查 SQL 庫存；缺貨再查原價屋報價快取，
                      彙整成「貨況表」(以程式整理事實，再交模型潤飾)。
  第3層 金牌銷售    ：用小抄 + 貨況表，自行判斷「資訊是否足夠」：
                      不足就生成『反問』向顧客逐步釐清（先問類別，再問品牌…），
                      足夠才生成『報價回覆草稿』。輸出含 action 的 JSON。
  第4層 最嚴苛店長  ：檢核第3層的草稿（反問或報價）是否正確、有無亂猜，
                      價格須為未稅價、同品項多價時「價高優先」，
                      確認無誤後才輸出要傳給顧客的最終訊息。

設計原則：
- 每層可指定不同模型（PIPELINE_L{n}_MODEL），留空回退主模型。
- 事實（庫存有無、價格數字）以程式查資料庫/快取取得，不交給模型臆測；
  模型只負責理解、彙整文字與話術，降低幻覺風險。
- 報價一律為「未稅價」。
- 任一層失敗都有後備，最終必定回傳一段可送出的文字。
"""

import json
import re

from . import ollama, customer, pricing
from .logbuffer import log


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def is_enabled(settings: dict) -> bool:
    return str(settings.get("PIPELINE_ENABLED", "false")).strip().lower() in (
        "1", "true", "yes", "on"
    )


def _parse_json(text: str) -> dict:
    """盡力把模型輸出解析成 dict；失敗回空 dict。"""
    if not text:
        return {}
    text = text.strip()
    # 去除 ```json ... ``` 圍欄
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except Exception:
        # 嘗試擷取第一個 {...} 區塊
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                return {}
    return {}


# ---------------------------------------------------------------------------
# 第1層 語言理解大師
# ---------------------------------------------------------------------------
def layer1_understand(settings: dict, user_text: str, memory_summary: str = "") -> dict:
    prompt = settings.get("PIPELINE_L1_PROMPT", "")
    user_block = user_text
    if memory_summary:
        user_block = f"【顧客過去重點】\n{memory_summary}\n\n【本次訊息】\n{user_text}"
    raw = ollama.run_model(settings, settings.get("PIPELINE_L1_MODEL", ""),
                           prompt, user_block, as_json=True)
    cheat = _parse_json(raw)
    if not cheat:
        # 後備：當成一般詢價，關鍵字用整句
        cheat = {"intent": "其他", "items": [{"keyword": user_text, "qty": None}],
                 "budget": None, "notes": ""}
    # 正規化 items（保留每項的「類別」與「品牌」，供第二層過濾與第三層逐步釐清）
    items = cheat.get("items") or []
    norm = []
    for it in items:
        if isinstance(it, dict) and it.get("keyword"):
            norm.append({"keyword": str(it["keyword"]).strip(),
                         "qty": it.get("qty"),
                         "category": str(it.get("category") or "").strip(),
                         "brand": str(it.get("brand") or "").strip()})
        elif isinstance(it, str) and it.strip():
            norm.append({"keyword": it.strip(), "qty": None,
                         "category": "", "brand": ""})
    cheat["items"] = norm
    log(f"[L1] 小抄：intent={cheat.get('intent')} "
        f"items={[(i['keyword'], i.get('category'), i.get('brand')) for i in norm]}")
    return cheat


# ---------------------------------------------------------------------------
# 第2層 最強庫管
# ---------------------------------------------------------------------------
# 整台電腦（非單一零件）的字眼：用於把「問零件卻撈到整機」的結果剔除
_WHOLE_MACHINE_WORDS = ("筆電", "筆記型", "桌機", "主機", "整機", "套裝",
                        "電競機", "準系統", "laptop", "notebook", "desktop",
                        "all-in-one", "aio")
# 整機類別
_WHOLE_MACHINE_CATS = ("筆電", "筆記型", "桌機", "主機", "整機", "準系統", "電腦")
# 看起來像「零件型號」的關鍵字（顧客多半是要單買零件，不是整台電腦）
_COMPONENT_MODEL_RE = re.compile(
    r"(RTX|GTX|RX|ARC|RYZEN|RADEON|GEFORCE|CORE\s*I[3579]|I[3579]-|"
    r"DDR[45]|NVME|SSD|HDD)", re.IGNORECASE)
# 螢幕尺寸（如 14吋 / 16.1吋 / 15inch）— 出現多半代表是整台筆電/AIO
_SCREEN_SIZE_RE = re.compile(r"\d{2}(?:\.\d)?\s*(?:吋|inch)", re.IGNORECASE)
# 顧客口語的型號（含裸數字，如「5060」「4070」），用於判斷是否需先釐清類別
_BARE_MODEL_RE = re.compile(
    r"(RTX|GTX|RX|ARC|RYZEN|RADEON|GEFORCE|CORE\s*I[3579]|I[3579])?\s*\d{3,4}\b",
    re.IGNORECASE)
# 已能確定是「單一零件」的類別（顧客講清楚了，不需再反問類別）
_COMPONENT_CATS = ("顯示卡", "顯卡", "處理器", "cpu", "主機板", "記憶體", "ram",
                   "硬碟", "ssd", "固態", "電源", "機殼", "螢幕", "顯示器",
                   "散熱", "風扇", "鍵盤", "滑鼠")


def _looks_like_whole_machine(hit: dict) -> bool:
    """判斷一筆結果是否其實是『整台電腦』而非單一零件。

    多重特徵判斷（原價屋筆電品名常不含「筆電」二字，需靠特徵）：
    - 類別屬整機類；
    - 品名含整機字眼（筆電/桌機…）；
    - 品名含螢幕尺寸「吋」（筆電/AIO 才會標尺寸）；
    - 品名是多段斜線規格組合（如 CPU/GPU/RAM/SSD/螢幕，>=3 段含數字），多為整機套餐。
    """
    cat = (hit.get("category") or "")
    if any(w in cat for w in _WHOLE_MACHINE_CATS):
        return True
    name = (hit.get("item_name") or hit.get("part_name") or "")
    low = name.lower()
    if any((w in low) if w.isascii() else (w in name) for w in _WHOLE_MACHINE_WORDS):
        return True
    if _SCREEN_SIZE_RE.search(name):
        return True
    slash_segs = [s for s in name.split("/") if any(c.isdigit() for c in s)]
    if len(slash_segs) >= 3:
        return True
    return False


def _wants_whole_machine(category: str) -> bool:
    """顧客是否明確要整台電腦。"""
    return any(w in (category or "") for w in _WHOLE_MACHINE_CATS)


def _filter_whole_machines(hits: list, keyword: str, category: str) -> list:
    """關鍵字像零件型號、且顧客沒明確要整機時，剔除整台電腦的命中。

    這是類別過濾之外的第二道防護：即使第1層沒填類別、或資料沒分類，
    也能避免「問 RTX5060 顯示卡卻回一台 95999 筆電」的窘況。
    若過濾後全空（代表本來就只有整機），則保留原結果不誤刪。
    """
    if _wants_whole_machine(category) or not _BARE_MODEL_RE.search(keyword or ""):
        return hits
    filtered = [h for h in hits if not _looks_like_whole_machine(h)]
    return filtered if filtered else hits


def _needs_category_clarification(keyword: str, category: str) -> bool:
    """是否需要先反問「要單買零件還是整台電腦」。

    條件：顧客只丟了型號（像 5060/RTX5060），且沒有明確指出類別
    （既不是某個單一零件類別，也不是整機類別）→ 需先釐清，避免亂猜。
    """
    cat = (category or "").strip().lower()
    if not _BARE_MODEL_RE.search(keyword or ""):
        return False
    if _wants_whole_machine(category):
        return False
    if any(c in cat for c in _COMPONENT_CATS):
        return False
    return True


def _brand_match(hit: dict, brand: str) -> bool:
    """判斷一筆結果是否屬於指定品牌（比對 brand 欄位與品名）。"""
    b = (brand or "").strip().lower()
    if not b:
        return True
    hb = (hit.get("brand") or "").strip().lower()
    name = (hit.get("item_name") or hit.get("part_name") or "").lower()
    return b in hb or b in name


def _pick_representative(hits: list) -> dict | None:
    """從多筆查詢結果中挑出「最能代表此關鍵字的單一品項」。

    取有報價中『價格最低』者作代表，理由：關鍵字模糊比對（LIKE）常會
    連帶命中「含相同型號的整台筆電/桌機/套裝」，這些售價遠高於單一零件；
    若用最高價當代表，就會發生「問顯卡卻報出一台筆電價格」的錯誤。
    取最低價可避免被高價的整機/套餐綁架，貼近顧客實際詢問的單品。
    """
    priced = [h for h in hits if h.get("price") is not None]
    if not priced:
        return None
    return min(priced, key=lambda x: x["price"])


def layer2_inventory(settings: dict, cheat: dict) -> dict:
    """以程式查 SQL 庫存與原價屋快取，整理出事實型「貨況表」。

    重要邏輯（依使用者澄清修正）：
    - 「價高優先」指的是『同一品項：現有庫存價 vs 報價單價，兩者取較高者』，
      不是「在一堆模糊命中的不同產品中挑最貴的那台」。
    - 用第1層判斷的『類別』收斂查詢，避免「RTX5060」這種型號關鍵字
      把含相同型號的整台筆電一起撈進來。
    """
    lines = []
    for it in cheat.get("items", []):
        keyword = it.get("keyword", "")
        if not keyword:
            continue
        category = it.get("category", "")
        brand = it.get("brand", "")

        # 先用類別過濾查詢；若類別過濾後查無，再放寬為不限類別
        sql_hits = customer.search_parts(settings, keyword, limit=10, category=category)
        if not sql_hits and category:
            sql_hits = customer.search_parts(settings, keyword, limit=10)
        in_stock = any(h.get("in_stock") for h in sql_hits)

        # 同時查原價屋報價單（作為比價依據），同樣優先用類別收斂
        coolpc_hits = []
        if pricing.is_enabled(settings):
            coolpc_hits = pricing.search(settings, keyword, limit=10, category=category)
            if not coolpc_hits and category:
                coolpc_hits = pricing.search(settings, keyword, limit=10)

        # 第二道防護：關鍵字像零件型號、顧客又沒要整機時，剔除整台電腦的命中，
        # 避免「問 RTX5060 顯示卡卻回一台筆電」（即使資料沒分類也擋得住）。
        sql_hits = _filter_whole_machines(sql_hits, keyword, category)
        coolpc_hits = _filter_whole_machines(coolpc_hits, keyword, category)

        # 收集可選品牌清單（供第三層在顧客未指定品牌時逐步反問）
        brands = sorted({(h.get("brand") or "").strip()
                         for h in (sql_hits + coolpc_hits)
                         if (h.get("brand") or "").strip()})

        # 顧客已指定品牌時，進一步收斂到該品牌
        if brand:
            sql_b = [h for h in sql_hits if _brand_match(h, brand)]
            coolpc_b = [h for h in coolpc_hits if _brand_match(h, brand)]
            if sql_b or coolpc_b:
                sql_hits, coolpc_hits = sql_b, coolpc_b
        in_stock = any(h.get("in_stock") for h in sql_hits)

        # 各來源各挑一個「代表單品」：
        # - 報價單(coolpc)：一律取『最低價』（已排除 NULL），避免被高價整機/套餐綁架。
        # - 本店庫存(sql)：優先取現貨中的代表（也取最低，代表同型號入門款）。
        sql_stock_rep = _pick_representative(
            [h for h in sql_hits if h.get("in_stock")])
        sql_rep = sql_stock_rep or _pick_representative(sql_hits)
        coolpc_rep = _pick_representative(coolpc_hits)

        sql_price = sql_rep["price"] if sql_rep else None
        coolpc_price = coolpc_rep["price"] if coolpc_rep else None

        # 決定報價與來源（依使用者最終澄清的規則）：
        # 1) 本店有現貨：以本店庫存價為主。「價高優先」只在『與庫存相比』時成立——
        #    若報價單同品項價格比庫存高，採較高者（讓現貨報價不低於市場行情）。
        # 2) 本店缺貨：完全改走報價單，採『最低』報價（已是 coolpc_rep），
        #    不可再去比 SQL 的高定價（那是先前的錯誤邏輯，已移除）。
        if in_stock and sql_stock_rep is not None:
            best = sql_stock_rep
            source = "sql"
            if coolpc_price is not None and sql_price is not None and coolpc_price > sql_price:
                best = coolpc_rep
                source = "coolpc"
        elif coolpc_rep is not None:
            # 本店缺貨：用報價單最低參考價（可調貨），不做價高 override
            best = coolpc_rep
            source = "coolpc"
        elif sql_rep is not None:
            best = sql_rep
            source = "sql"
        else:
            best = None
            source = "none"

        # 是否需要先反問「要單買零件還是整台電腦」（程式判斷，不依賴模型）
        need_category = _needs_category_clarification(keyword, category)

        # 調貨天數估計：本店缺貨、改走報價單調貨時提供（預設 3-7 天，可由設定調整）
        lead_time = ""
        if not in_stock and source == "coolpc":
            lead_time = settings.get("RESTOCK_LEAD_TIME", "3-7") or "3-7"

        lines.append({
            "keyword": keyword,
            "category": category,
            "asked_brand": brand,
            "qty": it.get("qty"),
            "in_stock": in_stock,
            "best_price": best["price"] if best else None,
            "best_name": (best.get("item_name") if best and "item_name" in best
                          else (best.get("part_name") if best else None)),
            "best_brand": best.get("brand") if best else None,
            "best_category": best.get("category") if best else None,
            "sql_price": sql_price,
            "coolpc_price": coolpc_price,
            "source": source,
            "lead_time_days": lead_time,
            # 供第三層逐步反問用：是否需先釐清類別、可選品牌清單、命中筆數
            "need_category_clarification": need_category,
            "available_brands": brands,
            "match_count": len(sql_hits) + len(coolpc_hits),
            "sql_options": sql_hits,
            "coolpc_options": coolpc_hits,
        })

    stock_report = {"lines": lines}
    log(f"[L2] 貨況：{[(l['keyword'], l['in_stock'], l['best_price'], l['source'], l['need_category_clarification'], l['available_brands']) for l in lines]}")
    return stock_report


def _format_stock_for_prompt(stock_report: dict) -> str:
    """把貨況表轉成給模型看的精簡文字（含類別、品牌、品名、未稅價、可選品牌、調貨天數）。"""
    rows = []
    for l in stock_report.get("lines", []):
        price = l.get("best_price")
        price_s = f"未稅 {int(price)}" if isinstance(price, (int, float)) else "無報價"
        status = "有貨" if l.get("in_stock") else "缺貨"
        src = {"sql": "本店庫存", "coolpc": "原價屋參考", "none": "查無資料"}.get(
            l.get("source"), l.get("source"))
        parts = [f"- 查詢「{l.get('keyword')}」（{status}）：報價 {price_s}，來源 {src}"]
        if l.get("asked_brand"):
            parts.append(f"顧客指定品牌 {l.get('asked_brand')}")
        if l.get("best_category"):
            parts.append(f"類別 {l.get('best_category')}")
        if l.get("best_brand"):
            parts.append(f"品牌 {l.get('best_brand')}")
        if l.get("best_name"):
            parts.append(f"品名 {l.get('best_name')}")
        # 揭露兩個來源的價格（未稅），方便店長核對「同品項取較高者」
        sp, cp = l.get("sql_price"), l.get("coolpc_price")
        cmp_bits = []
        if isinstance(sp, (int, float)):
            cmp_bits.append(f"本店未稅 {int(sp)}")
        if isinstance(cp, (int, float)):
            cmp_bits.append(f"報價單未稅 {int(cp)}")
        if cmp_bits:
            parts.append("（" + "、".join(cmp_bits) + "）")
        # 調貨天數
        if l.get("lead_time_days"):
            parts.append(f"調貨約 {l.get('lead_time_days')} 天")
        # 可選品牌（供逐步反問）
        brands = l.get("available_brands") or []
        if brands:
            parts.append(f"可選品牌：{'、'.join(brands)}")
        parts.append(f"命中筆數 {l.get('match_count', 0)}")
        rows.append("，".join(parts))
    return "\n".join(rows) if rows else "（無查詢結果）"


# ---------------------------------------------------------------------------
# 第3層 金牌銷售（自行判斷：資訊足夠才報價，不足則生成反問）
# ---------------------------------------------------------------------------
def _forced_clarification(stock_report: dict) -> tuple[str, str] | None:
    """以程式『強制』判斷是否必須先反問（不依賴模型，確保一定會問）。

    依使用者要求的逐步釐清順序：
    1) 類別未明（只丟型號，分不清要單買零件或整台電腦）→ 先反問類別。
    2) 類別已明、同型號多品牌、顧客未指定品牌 → 再反問品牌。
    回傳 (action="ask", message)；都不需要時回 None。
    """
    lines = stock_report.get("lines", [])
    # 1) 類別釐清優先
    for l in lines:
        if l.get("need_category_clarification"):
            kw = l.get("keyword")
            return ("ask",
                    f"請問您說的「{kw}」是想找單買的顯示卡（零件）嗎？"
                    f"還是要搭載 {kw} 的整台筆電/桌機呢？")
    # 2) 品牌釐清
    for l in lines:
        brands = l.get("available_brands") or []
        if not l.get("asked_brand") and len(brands) > 1:
            return ("ask",
                    f"請問您對「{l.get('keyword')}」有指定品牌嗎？"
                    f"目前有：{'、'.join(brands)}，方便告訴我偏好哪一個嗎？")
    return None


def layer3_sales(settings: dict, cheat: dict, stock_report: dict) -> dict:
    """金牌銷售。輸出 {"action":"ask|quote","message":"要回顧客的話"}。

    - action=ask  ：資訊不足（分不清要顯卡或筆電、或同型號多品牌需先選），
                    message 為要反問顧客的問題（一次只問一件事，逐步釐清）。
    - action=quote：資訊足夠，message 為報價/貨況回覆（價格皆為未稅價）。

    重要：是否需要『反問』由程式強制判斷（_forced_clarification），不交給模型，
    避免模型直接亂報（例如問 5060 卻回一台筆電）。只有在不需反問時，
    才讓模型生成報價話術；模型失效則用後備報價。
    """
    # 先做強制反問檢查：需要就直接回反問，不進模型
    forced = _forced_clarification(stock_report)
    if forced is not None:
        log("[L3] action=ask（程式強制反問）")
        return {"action": "ask", "message": forced[1]}

    # 不需反問 → 請模型生成報價話術（仍要求 JSON，便於與 ask 一致）
    prompt = settings.get("PIPELINE_L3_PROMPT", "")
    user_block = (
        f"【顧客需求小抄】\n{json.dumps(cheat, ensure_ascii=False)}\n\n"
        f"【貨況表（價格皆為未稅價）】\n{_format_stock_for_prompt(stock_report)}\n\n"
        "資訊已足夠報價，請輸出 JSON：{\"action\":\"quote\",\"message\":\"...\"}"
    )
    raw = ollama.run_model(settings, settings.get("PIPELINE_L3_MODEL", ""),
                           prompt, user_block, as_json=True)
    data = _parse_json(raw)
    message = str(data.get("message") or "").strip()
    if not message:
        # 後備：用程式組報價
        message = _fallback_quote(stock_report)
    log("[L3] action=quote")
    return {"action": "quote", "message": message}


def _fallback_quote(stock_report: dict) -> str:
    """第3層後備：組一段基本報價回覆（價格皆未稅）。"""
    parts = []
    for l in stock_report.get("lines", []):
        price = l.get("best_price")
        kw = l.get("keyword")
        name = l.get("best_name") or kw
        if l.get("in_stock"):
            parts.append(f"{name} 現貨供應"
                         + (f"，未稅單價 {int(price)} 元" if price else ""))
        elif price:
            lt = l.get("lead_time_days") or "3-7"
            parts.append(f"{name} 目前缺貨，可調貨（約 {lt} 天），"
                         f"未稅參考價約 {int(price)} 元")
        else:
            parts.append(f"{kw} 查無資料，方便提供更明確的型號嗎？")
    return ("您好，" + "；".join(parts) + "。需要我為您安排嗎？") if parts else \
        "您好，請問需要查詢哪項商品呢？"


# ---------------------------------------------------------------------------
# 第4層 最嚴苛的店長（檢核第3層草稿：反問或報價，確認無誤才送出）
# ---------------------------------------------------------------------------
def layer4_review(settings: dict, l3_result: dict, stock_report: dict) -> str:
    prompt = settings.get("PIPELINE_L4_PROMPT", "")
    action = l3_result.get("action", "quote")
    draft = l3_result.get("message", "")
    action_desc = ("反問（資訊不足，向顧客釐清）" if action == "ask"
                   else "報價（資訊足夠，提供貨況與價格）")
    user_block = (
        f"【貨況表（價格皆為未稅價；同一品項的本店價與報價單價並列時取較高者，"
        f"不同產品請勿混為一談）】\n"
        f"{_format_stock_for_prompt(stock_report)}\n\n"
        f"【第3層判斷的動作】{action_desc}\n"
        f"【第3層草稿】\n{draft}\n\n"
        "請檢核：若動作為『反問』，確認問題合理且必要（資訊真的不足才問），"
        "問句清楚、一次只問一件事；若動作為『報價』，確認價格與貨況和貨況表一致、"
        "皆為未稅價、商品類別與顧客詢問相符、沒有亂猜或捏造。"
        "修正後只輸出要傳給顧客的最終訊息，不要附加說明。"
    )
    # 反問是由程式『強制』判定的必要釐清（資訊真的不足），直接送出，
    # 不讓模型有機會把它改寫成報價而又亂猜。
    if action == "ask":
        log("[L4] 反問直接放行（程式強制釐清）")
        return draft

    final = ollama.run_model(settings, settings.get("PIPELINE_L4_MODEL", ""),
                             prompt, user_block)
    final = (final or "").strip()
    if not final:
        # 審查失敗就退回草稿，確保一定有回覆
        return draft
    log("[L4] 店長審查完成")
    return final


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(settings: dict, user_text: str, memory_summary: str = "") -> str:
    """執行四層流水線，回傳最終要傳給顧客的訊息。

    反問機制：由第3層（金牌銷售）判斷資訊是否足夠——不足就生成反問逐步釐清
    （先確認類別如顯卡/筆電，再確認品牌…），足夠才報價；第4層（店長）負責
    檢核第3層的反問或報價是否正確、有無亂猜，確認無誤後才回覆顧客。
    所有報價皆為未稅價。
    """
    try:
        cheat = layer1_understand(settings, user_text, memory_summary)
        stock_report = layer2_inventory(settings, cheat)
        l3_result = layer3_sales(settings, cheat, stock_report)
        final = layer4_review(settings, l3_result, stock_report)
        return final or l3_result.get("message", "")
    except Exception as e:
        log(f"流水線執行失敗，回退單層回覆：{e}", "ERROR")
        return ollama.ask(user_text, settings, memory_summary=memory_summary)
