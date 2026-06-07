import sys
sys.path.insert(0, "src")
from lineai import pipeline
import lineai.customer as customer
import lineai.pricing as pricing
import lineai.ollama as ollama

# 重現真實情境：原價屋筆電品名「不含筆電二字」，只有規格斜線與「吋」
coolpc_data = [
    # 這是一台筆電（套餐），品名沒有「筆電」字眼
    {"category": "", "brand": "MSI",
     "item_name": "微星 Stealth Ultra 9-386H/RTX5060/16G/1T/16吋 黑",
     "price": 86900.0, "source": "coolpc"},
    # 這是單買顯卡
    {"category": "", "brand": "MSI",
     "item_name": "微星 RTX5060 GAMING 顯示卡 8G",
     "price": 11600.0, "source": "coolpc"},
    {"category": "", "brand": "技嘉",
     "item_name": "技嘉 RTX5060 EAGLE 顯示卡 8G",
     "price": 11900.0, "source": "coolpc"},
]


def fake_search_parts(settings, keyword, limit=10, category=""):
    return []  # 本店沒庫存


def fake_search(settings, keyword, limit=10, category=""):
    return list(coolpc_data)


customer.search_parts = fake_search_parts
pricing.search = fake_search
pricing.is_enabled = lambda s: True
ollama.run_model = lambda *a, **k: ""  # 模型回空，走後備，確保測程式邏輯

settings = {"RESTOCK_LEAD_TIME": "3-7"}

# ===== 核心情境：顧客只說「5060」、類別未定 =====
cheat = {"items": [{"keyword": "5060", "category": "", "brand": ""}]}
report = pipeline.layer2_inventory(settings, cheat)
line = report["lines"][0]
print("[L2] need_category:", line["need_category_clarification"],
      "best_price:", line["best_price"], "品名:", line["best_name"],
      "可選品牌:", line["available_brands"], "match:", line["match_count"])

# 1) 筆電應被剔除（不含筆電字，但有吋+斜線規格）
assert line["best_price"] != 86900.0, "筆電應被剔除，不該報 86900"
# 2) 必須要求釐清類別
assert line["need_category_clarification"] is True, "只給型號應要求釐清類別"

# L3 應強制反問類別
res = pipeline.layer3_sales(settings, cheat, report)
print("[L3]", res["action"], "-", res["message"])
assert res["action"] == "ask"
assert ("顯示卡" in res["message"] or "顯卡" in res["message"]) and "筆電" in res["message"]

# L4 應直接放行反問
final = pipeline.layer4_review(settings, res, report)
print("[L4]", final)
assert final == res["message"], "反問應原樣送出"
print("PASS 1：問 5060 會先反問『顯卡還是筆電』，且不報筆電價")

# ===== 接著顧客回「顯卡」=====
cheat2 = {"items": [{"keyword": "5060", "category": "顯示卡", "brand": ""}]}
report2 = pipeline.layer2_inventory(settings, cheat2)
line2 = report2["lines"][0]
print("[L2-2] need_category:", line2["need_category_clarification"],
      "可選品牌:", line2["available_brands"])
assert line2["need_category_clarification"] is False
res2 = pipeline.layer3_sales(settings, cheat2, report2)
print("[L3-2]", res2["action"], "-", res2["message"][:50])
assert res2["action"] == "ask" and "品牌" in res2["message"]
print("PASS 2：說顯卡後，因多品牌再反問品牌")

# ===== 顧客回「MSI」=====
cheat3 = {"items": [{"keyword": "5060", "category": "顯示卡", "brand": "MSI"}]}
report3 = pipeline.layer2_inventory(settings, cheat3)
line3 = report3["lines"][0]
print("[L2-3] best_price:", line3["best_price"], "來源:", line3["source"],
      "調貨:", line3["lead_time_days"])
# 缺貨走報價單，取最低（MSI 顯卡 11600，非筆電 86900）
assert line3["best_price"] == 11600.0, f"應取 MSI 顯卡最低 11600，實得 {line3['best_price']}"
res3 = pipeline.layer3_sales(settings, cheat3, report3)
print("[L3-3]", res3["action"], "-", res3["message"][:60])
assert res3["action"] == "quote" and "未稅" in res3["message"]
print("PASS 3：指定 MSI 後報未稅價，缺貨走報價單最低價，含調貨天數")

# ===== 價高優先規則：缺貨時 coolpc 取最低，不被 SQL 高定價override =====
# 模擬本店有此型號但缺貨且定價很高(已下架價)，報價單較低
def fake_sql_highprice(settings, keyword, limit=10, category=""):
    return [{"part_no": "X", "category": "顯示卡", "brand": "MSI",
             "part_name": "微星 RTX5060 顯示卡", "price": 99999.0,
             "stock_qty": 0, "in_stock": False, "source": "sql"}]
customer.search_parts = fake_sql_highprice
report4 = pipeline.layer2_inventory(settings, cheat3)
line4 = report4["lines"][0]
print("[L2-4] best_price:", line4["best_price"], "來源:", line4["source"],
      "sql:", line4["sql_price"], "coolpc:", line4["coolpc_price"])
assert line4["best_price"] == 11600.0, "缺貨時應走報價單最低 11600，不可用 SQL 的 99999"
print("PASS 4：缺貨時報價單取最低，不被本店高定價 override")

print("ALL_TESTS_PASS")
