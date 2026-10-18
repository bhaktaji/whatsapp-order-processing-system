"""
Indian Hinglish, Devanagari & Kirana Shorthand Order Extraction Engine
Handles:
- Item-first ("Parle G 10 wala 5 peti") AND Qty-first ("5 peti Parle G 10 wala")
- Comma-less run-on WhatsApp messages ("bhaiya 5 peti parle g 2 bori chawal 3 tin tel bhej do")
- Hindi number words & Indian fractions (ek, do, teen, paanch, aadha=0.5, paav=0.25, dedh=1.5, dhai=2.5, sawa=1.25, paune=0.75, sadhe=+.5)
- Devanagari digits (०-९) and Devanagari Hindi words (पेटी, बोरी, कट्टा, किलो, डब्बा, लड़ी)
- MRP specifiers ("5 wala", "10 rs wala") & Pack weights ("25kg", "10kg", "15L")
- Dynamic custom item extraction for unlisted products
"""
import re
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional
from difflib import SequenceMatcher

def score_match(query: str, candidates: list[str]) -> float:
    q = query.lower().strip()
    if not q:
        return 0.0
    best = 0.0
    q_words = [w for w in q.split() if len(w) >= 2]
    for cand in candidates:
        c = cand.lower().strip()
        if not c:
            continue
        if q == c or c in q or q in c:
            return 98.0
        hits = sum(1 for w in q_words if w in c)
        if q_words and hits > 0:
            score = 68.0 + (hits / len(q_words)) * 28.0
        else:
            score = SequenceMatcher(None, q, c).ratio() * 100.0
        if score > best:
            best = score
    return best

DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

HINDI_SCRIPT_MAP = {
    "पारले जी": "parle g", "पारले-जी": "parle g", "पारले": "parle g", "बिस्कुट": "biscuit",
    "बासमती": "basmati", "चावल": "chawal", "आशीर्वाद": "aashirvaad", "आटा": "atta",
    "फॉर्च्यून": "fortune", "तेल": "tel", "अमूल": "amul", "दूध": "doodh", "ताज़ा": "taaza",
    "मैगी": "maggi", "मसाला": "masala", "टाटा": "tata", "चाय": "chai", "पत्ती": "patti",
    "सर्फ": "surf", "एक्सेल": "excel", "हल्दीराम": "haldiram", "भुजिया": "bhujia",
    "कुरकुरे": "kurkure", "चीनी": "chini", "शक्कर": "chini", "दाल": "dal", "जीरा": "jeera",
    "इलायची": "elaichi", "काजू": "kaju", "बादाम": "badam", "घी": "ghee", "पतंजलि": "patanjali",
    "पेटी": "peti", "कट्टा": "katta", "बोरी": "bori", "बोरा": "bori",
    "डब्बा": "dabba", "डिब्बा": "dabba", "टिन": "tin", "पीपा": "pipa",
    "लड़ी": "ladi", "पट्टी": "ladi", "गत्ता": "gatta", "बॉक्स": "box", "कार्टन": "carton",
    "किलो": "kg", "केजी": "kg", "ग्राम": "gm", "लीटर": "ltr",
    "पैकेट": "pkt", "पाउच": "pouch", "पीस": "pcs", "नग": "pcs", "दर्जन": "dozen",
    "और": " , ", "तथा": " , "
}

COMPOUND_NUM_PATTERNS = [
    (r"\b(?:sawa\s+do|सवा\s+दो)\b", "2.25"),
    (r"\b(?:sawa\s+teen|सवा\s+तीन)\b", "3.25"),
    (r"\b(?:sawa\s+cha?ar|सवा\s+चार)\b", "4.25"),
    (r"\b(?:paune\s+do|पौने\s+दो)\b", "1.75"),
    (r"\b(?:paune\s+teen|पौने\s+तीन)\b", "2.75"),
    (r"\b(?:sa+dhe\s+teen|साढ़े\s+तीन)\b", "3.5"),
    (r"\b(?:sa+dhe\s+cha?ar|साढ़े\s+चार)\b", "4.5"),
    (r"\b(?:sa+dhe\s+pa+nch|साढ़े\s+पांच|साढ़े\s+पाँच)\b", "5.5"),
    (r"\b(?:aadha|adha|आधा|half)\b", "0.5"),
    (r"(?<!\d)1/2(?!\d)", "0.5"),
    (r"\b(?:paav|pav|pava|पाव)\b", "0.25"),
    (r"(?<!\d)1/4(?!\d)", "0.25"),
    (r"\b(?:dedh|डेढ़)\b", "1.5"),
    (r"\b(?:dhai|dhaai|ढाई)\b", "2.5"),
    (r"\b(?:sawa|सवा)\b", "1.25"),
    (r"\b(?:paune|पौने)\b", "0.75"),
]

SINGLE_NUM_WORDS = {
    "ek": "1", "एक": "1", "one": "1",
    "do": "2", "दो": "2", "two": "2",
    "teen": "3", "तीन": "3", "three": "3",
    "char": "4", "chaar": "4", "चार": "4", "four": "4",
    "panch": "5", "paanch": "5", "पांच": "5", "पाँच": "5", "five": "5",
    "che": "6", "chhe": "6", "chhah": "6", "छह": "6", "छे": "6", "six": "6",
    "saat": "7", "sat": "7", "सात": "7", "seven": "7",
    "aath": "8", "ath": "8", "आठ": "8", "eight": "8",
    "nau": "9", "नौ": "9", "nine": "9",
    "das": "10", "dus": "10", "दस": "10", "ten": "10",
    "gyarah": "11", "ग्यारह": "11",
    "barah": "12", "बारह": "12",
    "pandrah": "15", "पंद्रह": "15",
    "bees": "20", "bis": "20", "बीस": "20",
    "pachees": "25", "pachis": "25", "पच्चीस": "25",
    "tees": "30", "तीस": "30",
    "pachaas": "50", "pachas": "50", "पचास": "50",
    "sau": "100", "सौ": "100",
}

CONTAINER_UNITS = "peti|petis|carton|cartons|ctn|case|katta|kattas|bori|boris|bora|bag|bags|sack|ladi|ladis|patti|strip|dabba|dabbas|tin|tins|pipa|can|jar|gatta|gattas|box|boxes|bx|crate|crates|tray|trays|darjan|dozen|dzn|bundle|bundles|gaddi|pkt|packet|packets|pouch|pcs|pc|piece|pieces|nag|block|blocks|set|sets|coil|coils"
WEIGHT_UNITS = "kg|kgs|kilo|gm|g|gram|grams|ltr|l|liter|litre|ml"
ALL_UNITS = f"{CONTAINER_UNITS}|{WEIGHT_UNITS}"

UNIT_CANONICAL = {
    "peti": "Peti", "petis": "Peti", "carton": "Carton", "cartons": "Carton", "ctn": "Carton", "case": "Peti",
    "katta": "Katta", "kattas": "Katta", "bori": "Bori", "boris": "Bori", "bora": "Bori", "bag": "Bori", "bags": "Bori", "sack": "Katta",
    "ladi": "Ladi", "ladis": "Ladi", "patti": "Ladi", "strip": "Ladi",
    "dabba": "Dabba", "dabbas": "Dabba", "tin": "Tin", "tins": "Tin", "pipa": "Tin", "can": "Tin", "jar": "Jar",
    "gatta": "Gatta", "gattas": "Gatta", "box": "Box", "boxes": "Box", "bx": "Box",
    "crate": "Crate", "crates": "Crate", "tray": "Tray", "trays": "Tray",
    "darjan": "Dozen", "dozen": "Dozen", "dzn": "Dozen",
    "bundle": "Bundle", "bundles": "Bundle", "gaddi": "Gaddi",
    "pkt": "Pkt", "packet": "Pkt", "packets": "Pkt", "pouch": "Pouch",
    "pcs": "Pcs", "pc": "Pcs", "piece": "Pcs", "pieces": "Pcs", "nag": "Pcs",
    "kg": "Kg", "kgs": "Kg", "kilo": "Kg",
    "gm": "Gm", "g": "Gm", "gram": "Gm", "grams": "Gm",
    "ltr": "Ltr", "l": "Ltr", "liter": "Ltr", "litre": "Ltr", "ml": "Ml",
    "block": "Block", "blocks": "Block", "set": "Set", "sets": "Set", "coil": "Coil", "coils": "Coil"
}

STOPWORDS = {
    "bhaiya", "bhai", "bhaiji", "sir", "ji", "seth", "sethji", "ram", "namaste",
    "please", "pls", "plz", "bhej", "bhejo", "bhejwa", "bhejna", "do", "dena",
    "de", "dijiye", "lagwa", "laga", "likh", "lo", "note", "karo", "kar",
    "kal", "aaj", "subah", "shaam", "dopahar", "urgent", "jaldi", "turant",
    "order", "maal", "saman", "chahiye", "need", "want", "send", "deliver",
    "delivery", "to", "for", "in", "ka", "ki", "ke", "ko", "mein", "me", "se",
    "baje", "tak", "payment", "khata", "udhar", "cash", "upi", "paid", "hey", "hi"
}


class OrderExtractionEngine:
    def __init__(self, tenant_config: Dict[str, Any], sku_catalog: List[Dict[str, Any]]):
        self.tenant = tenant_config
        self.skus = sku_catalog

    def parse_message(self, raw_text: str, price_tier: str = "WHOLESALE") -> Dict[str, Any]:
        s = raw_text.translate(DEVANAGARI_DIGITS)
        # Strip Hindi verb + do ("bhej do", "lagwa do", "kar do") before number conversion
        s = re.sub(r"\b(bhej|bhejo|bhijwa|bhejwa|laga|lagwa|kar|karo|de|likh|dal|nikal)\s+(do|dena|dijiye|de|lo)\b", " ", s, flags=re.IGNORECASE)
        for hi, ro in sorted(HINDI_SCRIPT_MAP.items(), key=lambda x: -len(x[0])):
            s = s.replace(hi, f" {ro} ")
        for pat, val in COMPOUND_NUM_PATTERNS:
            s = re.sub(pat, f" {val} ", s, flags=re.IGNORECASE)

        tokens = s.split()
        norm_tokens = []
        for tok in tokens:
            cl = tok.lower().strip(".,:-()")
            norm_tokens.append(SINGLE_NUM_WORDS.get(cl, tok))
        s = " ".join(norm_tokens)

        # Protect MRP tags ("10 wala", "5 rs wala")
        s = re.sub(r"\b(\d+)\s*(?:rs\.?|rupaye|rupee|₹)?\s*(?:wala|wali|wale|वाला|वाली|वाले)\b", r"[MRP_\1]", s, flags=re.IGNORECASE)
        # Protect pack weights before another qty+unit
        s = re.sub(rf"\b(\d+(?:\.\d+)?)\s*({WEIGHT_UNITS})\b(?=\s*[-:]?\s*\d+(?:\.\d+)?\s*(?:{ALL_UNITS})\b)", r"[PACK_\1\2]", s, flags=re.IGNORECASE)
        s = re.sub(rf"(\b\d+(?:\.\d+)?\s*(?:{CONTAINER_UNITS})\b[^,\n;+]*?)\b(\d+(?:\.\d+)?)\s*({WEIGHT_UNITS})\b", r"\1[PACK_\2\3]", s, flags=re.IGNORECASE)

        # Split clauses
        s = re.sub(r"\b(?:aur|and|plus|tatha)\b", ",", s, flags=re.IGNORECASE)
        s = re.sub(r"[\n;+&]+", ",", s)
        parts = [p.strip() for p in s.split(",") if p.strip()]

        clauses = []
        qu_re = re.compile(rf"\b(\d+(?:\.\d+)?)\s*({ALL_UNITS})\b", re.IGNORECASE)
        for part in parts:
            matches = list(qu_re.finditer(part))
            if len(matches) <= 1:
                clauses.append(part)
            else:
                before = part[:matches[0].start()].strip()
                before_words = [w for w in before.split() if w.lower().strip(".:-") not in STOPWORDS]
                if not before_words:
                    for i, m in enumerate(matches):
                        end = matches[i + 1].start() if i + 1 < len(matches) else len(part)
                        clauses.append(part[m.start():end].strip())
                else:
                    prev = 0
                    for m in matches:
                        clauses.append(part[prev:m.end()].strip())
                        prev = m.end()

        items = []
        for cl in clauses:
            m = qu_re.search(cl)
            if m:
                qty = float(m.group(1))
                raw_u = m.group(2).lower()
                unit = UNIT_CANONICAL.get(raw_u, "Pcs")
                item_str = (cl[:m.start()] + " " + cl[m.end():]).strip()
            else:
                num_m = re.search(r"\b(\d+(?:\.\d+)?)\b", cl)
                if not num_m:
                    continue
                qty = float(num_m.group(1))
                unit = ""
                item_str = (cl[:num_m.start()] + " " + cl[num_m.end():]).strip()

            mrp_m = re.search(r"\[MRP_(\d+)\]", item_str)
            mrp_tag = f"₹{mrp_m.group(1)} Wala" if mrp_m else ""
            pack_m = re.search(r"\[PACK_([^\]]+)\]", item_str)
            pack_tag = pack_m.group(1) if pack_m else ""

            item_str = re.sub(r"\[(?:MRP|PACK)_[^\]]+\]", " ", item_str)
            words = [w for w in re.split(r"[\s\-:]+", item_str) if w and w.lower() not in STOPWORDS]
            clean_item = " ".join(words).strip()
            if not clean_item:
                continue

            # Match against SKU catalog
            best_sku = None
            best_score = 0.0
            for sku in self.skus:
                candidates = [sku["name"], sku["code"]] + sku.get("aliases", [])
                sc = score_match(clean_item, candidates)
                if sc > best_score:
                    best_score = sc
                    best_sku = sku

            if best_sku and best_score >= 68:
                final_unit = unit or best_sku.get("baseUnit", "Pcs")
                rate = best_sku.get("wholesalePrice", best_sku["basePrice"])
                tax = float(best_sku.get("taxRate", 5))
                sku_name = best_sku["name"] + (f" ({mrp_tag})" if mrp_tag and mrp_tag.lower() not in best_sku["name"].lower() else "")
                sku_code = best_sku["code"]
                conf = min(0.99, round(best_score / 100.0, 2))
            else:
                # Dynamic Custom Item Extraction (never drop or mis-map unlisted Indian products)
                title_name = clean_item.title()
                extra = " ".join(x for x in [mrp_tag, pack_tag] if x)
                sku_name = f"{title_name} ({extra})" if extra else title_name
                sku_code = "CUSTOM-" + re.sub(r"[^A-Z0-9]", "", title_name.upper())[:5]
                final_unit = unit or "Pcs"
                rate = 450.0 if final_unit in ("Peti", "Bori", "Katta", "Tin", "Carton") else 120.0
                tax = 12.0
                conf = 0.90

            items.append({
                "raw_token": cl,
                "sku_code": sku_code,
                "sku_name": sku_name,
                "qty": qty,
                "unit": final_unit,
                "unit_price": rate,
                "tax_rate": tax,
                "line_total": round(qty * rate, 2),
                "confidence": conf,
            })

        subtotal = round(sum(i["line_total"] for i in items), 2)
        tax_amt = round(sum(i["line_total"] * (i["tax_rate"] / 100.0) for i in items), 2)
        return {
            "raw_message": raw_text,
            "items": items,
            "subtotal": subtotal,
            "tax_amount": tax_amt,
            "grand_total": round(subtotal + tax_amt, 2),
        }
