"""
兩層 AI agent 流水線（multi-agent）：

  第1層 L1（前線 AI 集合體）：銷售 / 客服 / 聊天 / 詢問。
      由意圖判斷分流：
        - 銷售意圖（要買、問價、問貨）：系統先用程式查好『貨況表』——有庫存就
          優先賣自家庫存（source=local），沒庫存才報原價屋調貨價（source=coolpc）。
          只有這條路徑會去『調用資料庫』，且僅用來確認有沒有貨、價格多少。
        - 其餘（客服 / 閒聊 / 一般詢問）：不碰資料庫，平時只調『長期記憶』，
          維持上下文順暢、避免無謂查庫拖慢與雜訊。
  第2層 L2（複查 / 檢核）：
      一律複查 L1 的輸出後才送出：
        - 銷售稿：核對價格未竄改、來源與話術一致（有貨才說現貨、調貨要說天數）、無臆測。
        - 一般稿：檢核語氣得體、未捏造商品/價格/庫存/承諾、立場一致。

設計原則：
- 事實（庫存有無、價格）一律由程式查資料庫/原價屋取得，模型只負責寫稿與校對。
- 只有銷售情境才調用資料庫；平時僅調用長期記憶（以客戶 ID 為代碼的專屬資料夾）。
- 庫存優先：本店有庫存就賣庫存價；缺貨才退而報原價屋調貨價。報價一律未稅。
- 任一層失敗都有後備，最終必定回傳一段可送出的文字。
- 每層可指定不同模型（PIPELINE_L1_MODEL／PIPELINE_L2_MODEL），留空回退主模型。
"""

import re

from . import ollama, customer, pricing, memory
from .logbuffer import log


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def is_enabled(settings: dict) -> bool:
    return str(settings.get("PIPELINE_ENABLED", "false")).strip().lower() in (
        "1", "true", "yes", "on"
    )


# 型號關鍵字樣式：含數字的英數型號（RTX5060、9800X3D、B850、DDR5、i7-14700）
_MODEL_TOKEN_RE = re.compile(r"[A-Za-z]+[A-Za-z0-9\-]*\d[A-Za-z0-9\-]*")

# 技術支援意圖：故障排除、操作問題、軟體安裝等（優先於銷售判斷）
# 即使句中含商品型號（如 RTX5060），只要在問故障/操作，就走客服而非報價。
_SUPPORT_HINT_RE = re.compile(
    r"故障|壞掉|壞了|當機|宕機|沒反應|沒畫面|黑畫面|黑屏|藍屏|藍底|花屏|"
    r"不開機|開不了機|無法開機|無法開啟|打不開|跑不動|跑很慢|卡頓|卡住|"
    r"閃退|當掉|過熱|太燙|異音|噪音|抓不到|偵測不到|沒訊號|無訊號|無聲|"
    r"連不上|連不到|連線|斷線|抓不到網路|沒網路|上不了網|wifi|wi-fi|藍芽|藍牙|"
    r"重灌|重新安裝|重裝|安裝|灌(?:不|系統|軟體|程式)|驅動|driver|更新|韌體|bios|"
    r"設定|怎麼用|怎麼裝|怎麼弄|怎麼設|如何|教學|操作|步驟|無法使用|不能用|不會用|"
    r"系統|windows|win\s?1[01]|office|word|excel|當機重開|修(?:理|復|不)|error|錯誤|"
    r"開機沒|沒聲音|風扇狂轉|一直重開|裝好.*沒|裝上.*沒",
    re.IGNORECASE,
)

# 銷售意圖提示詞：出現這些字眼多半是在問買賣、價格、貨況
_SALES_HINT_RE = re.compile(
    r"買|購|下單|訂購|訂一|報價|多少錢|幾元|幾塊|價格|價錢|售價|單價|成本|"
    r"庫存|有貨|沒貨|現貨|缺貨|調貨|到貨|出貨|便宜|優惠|折扣|划算|預算|"
    r"推薦|建議.*配|配一|組一|想換|想升級|想買|要買|入手|挑一"
)


def _extract_keyword(user_text: str) -> str:
    """從顧客整句訊息中抽出商品型號關鍵字（如 RTX5060、9800X3D、B850）。

    只認『含數字的英數型號』；抓不到就回空字串。
    """
    text = (user_text or "").strip()
    if not text:
        return ""
    tokens = _MODEL_TOKEN_RE.findall(text)
    if tokens:
        # 取最長的型號字串當主關鍵字（通常資訊量最大）
        return max(tokens, key=len)
    return ""


def classify_intent(user_text: str) -> str:
    """判斷 L1 該走哪條路：'sales'（要查資料庫報價）或 'chat'（只用長期記憶）。

    優先序（技術支援 > 銷售 > 一般）：
      1) 技術支援：故障排除、操作、軟體安裝等。即使句中含商品型號也走 chat，
         避免把「RTX5060 裝了沒畫面」誤判成報價而答非所問。
      2) 銷售：含銷售字眼（買、價格、推薦、升級…）→ sales。
      3) 純含型號但無支援、無明顯銷售字眼（如直接丟「RTX5060」）→ 仍視為想問貨況，sales。
      4) 其餘（打招呼、客服、閒聊）→ chat。
    chat 路徑一律不碰資料庫。
    """
    text = (user_text or "").strip()
    if not text:
        return "chat"
    # 1) 技術支援優先：故障/操作/安裝問題，不論有無型號都走客服
    if _SUPPORT_HINT_RE.search(text):
        return "chat"
    # 2) 明確銷售字眼
    if _SALES_HINT_RE.search(text):
        return "sales"
    # 3) 只丟了型號（想問這顆貨況）
    if _extract_keyword(text):
        return "sales"
    return "chat"


def _pick_representative(hits: list) -> dict | None:
    """從多筆查詢結果中挑出最能代表此關鍵字的單品（取有報價中價格最低者）。"""
    priced = [h for h in hits if h.get("price") is not None]
    if not priced:
        return None
    return min(priced, key=lambda x: x["price"])


def _hit_name(hit: dict) -> str:
    return (hit.get("item_name") or hit.get("part_name") or "").strip()


# ---------------------------------------------------------------------------
# 貨況查詢（庫存優先、缺貨才報原價屋）——只有銷售意圖才會呼叫
# ---------------------------------------------------------------------------
def query_stock(settings: dict, keyword: str) -> dict:
    """查詢單一關鍵字的貨況：本店有庫存就賣庫存，沒有才報原價屋調貨價。

    『調用資料庫』只做兩件事：確認有沒有貨、價格多少。

    回傳：
      {
        "keyword": 查詢關鍵字,
        "in_stock": 本店是否有現貨,
        "source": "local" | "coolpc" | "none",
        "name": 代表品名, "brand": 品牌, "price": 未稅價或 None,
        "lead_time_days": 調貨天數（缺貨報原價屋時才有）,
      }
    """
    sql_hits = customer.search_parts(settings, keyword, limit=10)
    in_stock = any(h.get("in_stock") for h in sql_hits)

    # 本店有現貨：優先賣庫存（取現貨中最低價代表）
    if in_stock:
        rep = _pick_representative([h for h in sql_hits if h.get("in_stock")])
        if rep:
            return {
                "keyword": keyword,
                "in_stock": True,
                "source": "local",
                "name": _hit_name(rep),
                "brand": (rep.get("brand") or "").strip(),
                "price": rep.get("price"),
                "lead_time_days": "",
            }

    # 本店沒現貨：報原價屋調貨價
    if pricing.is_enabled(settings):
        coolpc_hits = pricing.search(settings, keyword, limit=10)
        rep = _pick_representative(coolpc_hits)
        if rep:
            return {
                "keyword": keyword,
                "in_stock": False,
                "source": "coolpc",
                "name": _hit_name(rep),
                "brand": (rep.get("brand") or "").strip(),
                "price": rep.get("price"),
                "lead_time_days": settings.get("RESTOCK_LEAD_TIME", "3-7") or "3-7",
            }

    # 都查不到
    return {
        "keyword": keyword,
        "in_stock": False,
        "source": "none",
        "name": "",
        "brand": "",
        "price": None,
        "lead_time_days": "",
    }


def _format_stock_for_prompt(stock: dict) -> str:
    """把貨況整理成給模型看的精簡文字（含來源、品名、未稅價、調貨天數）。"""
    src = {"local": "本店現貨", "coolpc": "原價屋調貨", "none": "查無資料"}.get(
        stock.get("source"), stock.get("source"))
    price = stock.get("price")
    price_s = f"未稅 {int(price)} 元" if isinstance(price, (int, float)) else "無報價"
    parts = [f"查詢關鍵字：{stock.get('keyword')}",
             f"來源 source={stock.get('source')}（{src}）",
             f"報價：{price_s}"]
    if stock.get("brand"):
        parts.append(f"品牌：{stock.get('brand')}")
    if stock.get("name"):
        parts.append(f"品名：{stock.get('name')}")
    if stock.get("lead_time_days"):
        parts.append(f"調貨約 {stock.get('lead_time_days')} 天")
    return "；".join(parts)


# ---------------------------------------------------------------------------
# 第1層 L1：銷售路徑（依貨況表寫報價草稿）
# ---------------------------------------------------------------------------
def layer1_sales(settings: dict, user_text: str, stock: dict,
                 memory_summary: str = "") -> str:
    """銷售：依貨況表事實寫出親切、口語化的報價回覆草稿。"""
    prompt = settings.get("PIPELINE_L1_PROMPT", "")
    blocks = [f"【顧客訊息】\n{user_text}"]
    if memory_summary:
        blocks.append(f"【顧客過去重點（長期記憶）】\n{memory_summary}")
    blocks.append(f"【貨況表（價格皆為未稅價）】\n{_format_stock_for_prompt(stock)}")
    user_block = "\n\n".join(blocks)
    draft = ollama.run_model(settings, settings.get("PIPELINE_L1_MODEL", ""),
                             prompt, user_block)
    draft = (draft or "").strip()
    if not draft:
        draft = _fallback_quote(stock)
    log("[L1] 銷售草稿產生")
    return draft


def _fallback_quote(stock: dict) -> str:
    """銷售後備：依來源組一段基本報價（價格皆未稅）。"""
    name = stock.get("name") or stock.get("keyword")
    price = stock.get("price")
    source = stock.get("source")
    if source == "local" and stock.get("in_stock"):
        return (f"您好，{name} 店內有現貨，今天下單今天就能幫您出貨喔"
                + (f"，未稅單價 {int(price)} 元" if price else "")
                + "！需要幫您安排嗎？")
    if source == "coolpc" and price:
        lt = stock.get("lead_time_days") or "3-7"
        return (f"您好，{name} 本店目前沒有現貨，可以幫您向原價屋調貨"
                f"（約 {lt} 天），未稅參考價約 {int(price)} 元，需要幫您安排嗎？")
    return f"您好，{stock.get('keyword')} 目前查不到資料，方便再確認一下型號嗎？"


# ---------------------------------------------------------------------------
# 第1層 L1：一般路徑（客服 / 聊天 / 詢問，不碰資料庫，只用長期記憶）
# ---------------------------------------------------------------------------
def layer1_chat(settings: dict, user_text: str, memory_summary: str = "") -> str:
    """客服／聊天／一般詢問：只憑長期記憶與店家設定回覆，不查資料庫。"""
    prompt = (settings.get("PIPELINE_L1_CHAT_PROMPT", "")
              or settings.get("OLLAMA_SYSTEM_PROMPT", ""))
    blocks = [f"【顧客訊息】\n{user_text}"]
    if memory_summary:
        blocks.append(f"【顧客過去重點（長期記憶）】\n{memory_summary}")
    user_block = "\n\n".join(blocks)
    draft = ollama.run_model(settings, settings.get("PIPELINE_L1_MODEL", ""),
                             prompt, user_block)
    draft = (draft or "").strip()
    if not draft:
        # 後備：退回單層一般回覆（仍帶長期記憶）
        draft = ollama.ask(user_text, settings, memory_summary=memory_summary)
    log("[L1] 一般回覆草稿產生")
    return draft


# ---------------------------------------------------------------------------
# 第2層 L2：複查 / 檢核（一律複查 L1）
# ---------------------------------------------------------------------------
def layer2_review_sales(settings: dict, draft: str, stock: dict) -> str:
    """複查銷售稿：核對價格未竄改、來源與話術一致、無臆測。"""
    prompt = settings.get("PIPELINE_L2_PROMPT", "")
    user_block = (
        f"【貨況表（價格皆為未稅價，含來源 source 標籤）】\n"
        f"{_format_stock_for_prompt(stock)}\n\n"
        f"【L1 銷售草稿】\n{draft}\n\n"
        "請核對：(1) 價格是否與貨況表一致、皆為未稅、未被竄改；"
        "(2) 話術與來源是否一致（local 才可說現貨／可立即出貨，"
        "coolpc 必須說明需調貨與天數，none 不可硬報價）；"
        "(3) 有無臆測——不可猜測或捏造價格、貨況、規格。"
        "修正後只輸出要傳給顧客的最終訊息（保留口語化人味），不要附加說明。"
    )
    final = ollama.run_model(settings, settings.get("PIPELINE_L2_MODEL", ""),
                             prompt, user_block)
    final = (final or "").strip()
    if not final:
        return draft
    log("[L2] 銷售稿複查完成")
    return final


def layer2_review_chat(settings: dict, user_text: str, draft: str,
                       memory_summary: str = "") -> str:
    """複查一般稿：語氣得體、未捏造商品/價格/庫存/承諾、立場一致。"""
    prompt = settings.get("PIPELINE_L2_CHAT_PROMPT", "")
    if not prompt:
        # 沒設定一般稿複查 prompt 時，直接放行 L1（不阻斷對話）
        return draft
    blocks = [f"【顧客訊息】\n{user_text}"]
    if memory_summary:
        blocks.append(f"【顧客過去重點（長期記憶）】\n{memory_summary}")
    blocks.append(f"【L1 回覆草稿】\n{draft}")
    blocks.append(
        "請檢核這段一般回覆：(1) 語氣是否得體、符合店家立場；"
        "(2) 是否捏造了商品、價格、庫存或做出無法兌現的承諾（這類資訊需請顧客提供型號後另行查詢，"
        "不可在此臆測）；(3) 是否答非所問。"
        "修正後只輸出要傳給顧客的最終訊息（保留口語化人味），不要附加說明。"
    )
    user_block = "\n\n".join(blocks)
    final = ollama.run_model(settings, settings.get("PIPELINE_L2_MODEL", ""),
                             prompt, user_block)
    final = (final or "").strip()
    if not final:
        return draft
    log("[L2] 一般稿複查完成")
    return final


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(settings: dict, user_text: str, memory_summary: str = "",
        history: list | None = None, user_id: str = "") -> str:
    """執行兩層 agent 流水線，回傳最終要傳給顧客的訊息。

    流程：
    1) L1 意圖判斷：
       - chat（客服／聊天／一般詢問）：不碰資料庫，只用長期記憶寫稿 → L2 複查。
       - sales（要買／問價／問貨）：才『調用資料庫』查貨況（確認有沒有貨、價格多少）。
         查無資料（source=none）→ 禮貌請顧客確認型號，不臆測；
         查到貨 → L1 依事實寫報價草稿 → L2 複查後送出。
    2) L2 一律複查 L1 的輸出後才送出。

    memory_summary：呼叫端已載入的長期記憶；若未提供但有 user_id，會自行載入。
    所有報價皆為未稅價。history 參數保留以相容呼叫端。
    """
    try:
        # 平時只調長期記憶：呼叫端沒帶就用 user_id 自行載入專屬資料夾的精華
        if not memory_summary and user_id:
            memory_summary = memory.load_summary(settings, user_id)

        intent = classify_intent(user_text)

        # 一般路徑：客服 / 聊天 / 詢問 —— 不調用資料庫
        if intent == "chat":
            log("[流水線] 意圖=一般對話，只用長期記憶（不查資料庫）")
            draft = layer1_chat(settings, user_text, memory_summary)
            return layer2_review_chat(settings, user_text, draft, memory_summary)

        # 銷售路徑：才調用資料庫確認有沒有貨、價格多少
        keyword = _extract_keyword(user_text)
        if not keyword:
            # 有銷售字眼卻沒給型號（如「這個多少錢」）：請顧客補型號，不臆測
            log("[流水線] 意圖=銷售但未指定型號，請顧客確認")
            draft = layer1_chat(settings, user_text, memory_summary)
            return layer2_review_chat(settings, user_text, draft, memory_summary)

        log(f"[流水線] 意圖=銷售，調用資料庫查貨況：{keyword!r}")
        stock = query_stock(settings, keyword)
        if stock.get("source") == "none":
            log(f"[流水線] 關鍵字 {keyword!r} 查無資料，請顧客確認型號")
            return (f"您好～我這邊查了一下，目前沒找到「{keyword}」的資料耶，"
                    "方便再幫我確認一下完整型號嗎？這樣才能幫您查到正確的報價唷！")

        draft = layer1_sales(settings, user_text, stock, memory_summary)
        return layer2_review_sales(settings, draft, stock)
    except Exception as e:
        log(f"流水線執行失敗，回退單層回覆：{e}", "ERROR")
        return ollama.ask(user_text, settings, memory_summary=memory_summary)
